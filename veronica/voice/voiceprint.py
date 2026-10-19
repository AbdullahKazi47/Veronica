"""
"Only listen to me": her owner's voiceprint.

Say "Veronica, learn my voice": she asks for a few sentences, turns each into a speaker
embedding (sherpa-onnx, the WeSpeaker CAM++ model, on this PC) and keeps their average.
From then on every captured clip is compared with it before transcription; a clip that
clearly isn't the owner (a TV, other people in the room, a video) is dropped there.

The comparison bar is calibrated from the owner's own sentences: how alike they are to
each other, minus a margin, so a quiet or tired day still passes. Very short clips are
not judged (too little voice to tell), and "stop checking my voice" turns it off.

Privacy: the voiceprint is a list of numbers describing a voice, kept only in
data/voiceprint.json on this PC. It is never sent anywhere and is never used to
identify anyone else; other voices are only "not the owner". "Forget my voice" deletes it.
Model: data/models/sherpa/wespeaker_en_voxceleb_CAM++.onnx (~29 MB, Apache-2.0).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Callable, Optional

from veronica.logging_setup import get_logger

logger = get_logger("voice.voiceprint")

MODEL_FILE = "wespeaker_en_voxceleb_CAM++.onnx"
MIN_JUDGE_S = 0.8            # shorter than this: too little voice to judge, let it through
FLOOR, CEILING = 0.30, 0.65  # the calibrated bar always stays between these
MARGIN = 0.12


def _cos(a, b) -> float:
    import numpy as np
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return float(a @ b / (na * nb)) if na and nb else 0.0


class Voiceprint:
    def __init__(self, path: Path, model_path: Path, embedder: Optional[Callable[[bytes], list]] = None) -> None:
        self.path = Path(path)
        self.model_path = Path(model_path)
        self._embedder = embedder
        self._extractor = None
        self._lock = threading.Lock()
        self.data = self._load()

    # -- store --------------------------------------------------------------------------
    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("embedding"), list):
                return d
        except (OSError, ValueError):
            pass
        return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)

    @property
    def enrolled(self) -> bool:
        return bool(self.data.get("embedding"))

    @property
    def active(self) -> bool:
        return self.enrolled and self.data.get("enabled", True)

    def set_enabled(self, on: bool) -> None:
        if self.enrolled:
            self.data["enabled"] = bool(on)
            self._save()

    def forget(self) -> bool:
        had = self.enrolled
        self.data = {}
        try:
            self.path.unlink()
        except OSError:
            pass
        return had

    # -- embeddings -----------------------------------------------------------------------
    def available(self) -> bool:
        if self._embedder is not None:
            return True
        try:
            import sherpa_onnx  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return self.model_path.exists()

    def warm(self) -> None:
        threading.Thread(target=lambda: self.available() and self._get_extractor(), name="voiceprint-warm",
                         daemon=True).start()

    def _get_extractor(self):
        with self._lock:
            if self._extractor is None:
                import sherpa_onnx as so
                self._extractor = so.SpeakerEmbeddingExtractor(so.SpeakerEmbeddingExtractorConfig(
                    model=str(self.model_path), num_threads=2))
            return self._extractor

    def embed(self, pcm: bytes) -> list:
        if self._embedder is not None:
            return list(self._embedder(pcm))
        import numpy as np
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        ext = self._get_extractor()
        s = ext.create_stream()
        s.accept_waveform(16000, x)
        s.input_finished()
        return list(ext.compute(s))

    # -- enrol / check ------------------------------------------------------------------------
    def enroll(self, clips: list[bytes]) -> float:
        """Learn the owner's voice from a few clips; returns the calibrated bar."""
        embs = [self.embed(c) for c in clips if len(c) >= int(MIN_JUDGE_S * 16000 * 2)]
        if len(embs) < 3:
            raise ValueError("I need at least three clear sentences.")
        import numpy as np
        mean = np.mean(np.asarray(embs, dtype=float), axis=0)
        # How alike is each sentence to the others? The bar sits a margin below the worst.
        loo = []
        for i, e in enumerate(embs):
            rest = np.mean(np.asarray([x for j, x in enumerate(embs) if j != i], dtype=float), axis=0)
            loo.append(_cos(e, rest))
        bar = max(FLOOR, min(CEILING, min(loo) - MARGIN))
        self.data = {"embedding": [float(v) for v in mean], "threshold": round(bar, 3),
                     "self_similarity": [round(v, 3) for v in loo], "samples": len(embs), "enabled": True}
        self._save()
        logger.info("Voiceprint learned from %d sentences (self-similarity %s, bar %.2f).",
                    len(embs), self.data["self_similarity"], bar)
        return bar

    def check(self, pcm: bytes) -> tuple[bool, Optional[float]]:
        """(is it the owner?, score). Inactive, too short, or a failed embedding -> (True, None)."""
        if not self.active or len(pcm) < int(MIN_JUDGE_S * 16000 * 2):
            return True, None
        try:
            score = _cos(self.embed(pcm), self.data["embedding"])
        except Exception as e:  # noqa: BLE001 - a broken check must never deafen her
            logger.warning("Voice check failed (%s); letting the clip through.", e)
            return True, None
        return score >= float(self.data.get("threshold", FLOOR)), round(score, 3)


_SHARED: Optional[Voiceprint] = None


def get_voiceprint(config) -> Voiceprint:
    global _SHARED
    if _SHARED is None:
        _SHARED = Voiceprint(config.resolve_path("data/voiceprint.json"),
                             config.resolve_path(f"data/models/sherpa/{MODEL_FILE}"))
    return _SHARED
