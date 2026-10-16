"""
Text-to-speech, async-by-design so speaking never blocks the rest of the assistant —
this is the one thing Prodigy-AI's `speaker.py` gets clearly right and is worth keeping
as a hard requirement here. Backends:
  - "edge_tts": free, natural-sounding, needs internet, no API key (Microsoft Edge's
    read-aloud voices). Default.
  - "piper": fully offline neural TTS, natural-sounding, runs locally via the Piper
    CLI or the piper-tts Python package. Good quality with no API key or internet.
    This mirrors JARVIS OS's local TTS capability scaled to a single Windows desktop.
  - "fish_audio": Fish Audio's cloud TTS with a configurable reference voice (FRIDAY
    preset) — mp3 output cached and played via native Windows players.
  - "pyttsx3": fully offline, lower quality, zero dependencies beyond the OS's own
    speech engine — the true zero-network, zero-key fallback.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import threading
from pathlib import Path

from veronica.config import TTSConfig
from veronica.logging_setup import get_logger

logger = get_logger("speech.tts")

# ---------------------------------------------------------------------------
# Audio playback, with zero mandatory dependencies.
#
# Playing the synthesized file (mp3 for edge-tts / fish_audio, wav for piper) is the
# one thing that used to force pygame onto every install. pygame has no prebuilt wheel
# for some Python versions, so `pip install pygame` could fall back to compiling from
# source on Windows — a hard failure for a mere "make a sound" feature.
#
# The ladder below makes playback native-first on the target platform (Windows,
# where VERONICA lives) and keeps pygame only as an *optional* cross-platform
# fallback:
#   1. winsound   — stdlib, Windows-only, plays *.wav synchronously (piper).
#   2. winmm MCI  — stdlib `ctypes` against winmm.dll, plays mp3/wav on Windows
#                   (edge-tts / fish_audio). Synchronous like the old pygame busy-loop.
#   3. pygame     — optional; used only when neither native player is available.
#
# If no player is available at all, the file is still synthesized and cached, so
# every TTS backend keeps working — only the speaker output is skipped (with one
# per-process warning). `pip install -r requirements.txt` therefore never compiles
# pygame, and the Stitch GUI / bridge never touch it.
# ---------------------------------------------------------------------------


def _play_wav_winsound(path: Path) -> bool:
    """Native Windows WAV playback via the stdlib `winsound`. Synchronous."""
    if sys.platform != "win32" or Path(path).suffix.lower() != ".wav":
        return False
    try:
        import winsound
    except ImportError:
        return False
    try:
        winsound.PlaySound(str(path), winsound.SND_FILENAME)
        return True
    except Exception:
        logger.debug("winsound playback failed for %s", path)
        return False


def _play_winmm_mci(path: Path) -> bool:
    """Native Windows media playback (mp3 and wav) through the winmm MCI driver,
    called via the stdlib `ctypes` module — no third-party package, nothing to
    compile. `play <alias> wait` blocks until the track finishes, matching the
    old pygame busy-loop's synchronous semantics."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        winmm = ctypes.windll.winmm
    except (AttributeError, OSError):
        return False
    alias = "__veronica_tts__"
    try:
        winmm.mciSendStringW(f"close {alias}", None, 0, None)  # clear a stale track
        if winmm.mciSendStringW(f'open "{path}" alias {alias}', None, 0, None) != 0:
            return False
        try:
            return winmm.mciSendStringW(f"play {alias} wait", None, 0, None) == 0
        finally:
            winmm.mciSendStringW(f"close {alias}", None, 0, None)
    except Exception:
        logger.debug("winmm MCI playback failed for %s", path)
        return False


def _play_pygame(path: Path) -> bool:
    """Cross-platform fallback player, used only when the native Windows players
    are unavailable. pygame stays strictly optional — never a hard requirement."""
    try:
        import pygame
    except ImportError:
        return False
    try:
        pygame.mixer.init()
        pygame.mixer.music.load(str(path))
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            pygame.time.wait(100)
        return True
    except Exception:
        logger.debug("pygame playback failed for %s", path)
        return False


_warned_no_player = False


class SynchronousSpeaker:
    """Wrapper for TTS backends that blocks until playback completes."""
    def __init__(self, tts_instance):
        self.tts = tts_instance

    def speak(self, text: str) -> None:
        """Speak text and block until audio playback finishes."""
        self.tts.speak(text, block=True)


class TextToSpeech:
    def __init__(self, config: TTSConfig, cache_dir: Path) -> None:
        self.config = config
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def speak(self, text: str, block: bool = False) -> None:
        """Speak `text`. Runs on a background thread by default so callers (the CLI
        loop, the orchestrator) never stall waiting on audio playback."""
        if not text or self.config.backend == "none":
            return

        if self.config.backend == "edge_tts":
            target = self._speak_edge_tts
        elif self.config.backend == "piper":
            target = self._speak_piper
        elif self.config.backend == "fish_audio":
            target = self._speak_fish_audio
        elif self.config.backend == "elevenlabs":
            target = self._speak_elevenlabs
        else:
            target = self._speak_pyttsx3
        if block:
            target(text)
        else:
            threading.Thread(target=target, args=(text,), daemon=True).start()

    # -- backends -----------------------------------------------------------------
    def _cache_path(self, text: str) -> Path:
        # Key on whichever setting picks the voice, so switching the Fish Audio reference
        # voice never replays clips cached from the previous voice.
        if self.config.backend == "fish_audio":
            voice = f"fish:{self.config.fish_audio_reference_id}:{self.config.fish_audio_model}"
        elif self.config.backend == "elevenlabs":
            voice = f"eleven:{self.config.elevenlabs_voice_id}:{self.config.elevenlabs_model}"
        else:
            voice = self.config.voice
        digest = hashlib.sha256(f"{voice}:{text}".encode("utf-8")).hexdigest()[:24]
        return self.cache_dir / f"{digest}.mp3"

    def _speak_edge_tts(self, text: str) -> None:
        try:
            import edge_tts
        except ImportError:
            logger.warning("edge-tts not installed; falling back to pyttsx3. Run `pip install edge-tts`.")
            return self._speak_pyttsx3(text)

        out_path = self._cache_path(text)
        try:
            if not out_path.exists():
                async def _gen():
                    communicate = edge_tts.Communicate(text, self.config.voice, rate=self.config.rate)
                    await communicate.save(str(out_path))
                asyncio.run(_gen())
            self._play_audio(out_path)
        except Exception as e:
            logger.warning("edge-tts failed (%s); falling back to pyttsx3.", e)
            self._speak_pyttsx3(text)

    def _speak_piper(self, text: str) -> None:
        """Offline neural TTS via Piper. First tries the piper CLI (most common
        install path), then falls back to the piper-tts Python package. Audio is
        cached identically to edge-tts."""
        import shutil
        import subprocess

        out_path = self._cache_path(text).with_suffix(".wav")

        # Fast path: piper CLI on PATH (e.g. installed via pip or a direct download).
        piper_bin = shutil.which("piper")
        if piper_bin:
            try:
                if not out_path.exists():
                    result = subprocess.run(
                        [piper_bin, "--model", self.config.piper_model,
                         "--output_file", str(out_path)],
                        input=text.encode("utf-8"),
                        capture_output=True,
                        timeout=30,
                    )
                    if result.returncode != 0:
                        raise RuntimeError(result.stderr.decode(errors="replace") or "piper CLI failed")
                self._play_audio(out_path)
                return
            except Exception as e:
                logger.warning("piper CLI failed (%s); trying Python fallback.", e)

        # Fallback: piper-tts Python package (same API, ships its own binaries).
        try:
            from piper import PiperVoice
        except ImportError:
            logger.error(
                "piper not available; run `pip install piper-tts` or place the piper CLI on PATH. "
                "Falling back to pyttsx3."
            )
            return self._speak_pyttsx3(text)

        try:
            if not out_path.exists():
                voice = PiperVoice.load(self.config.piper_model)
                with open(out_path, "wb") as f:
                    voice.synthesize(text, f)
            self._play_audio(out_path)
        except Exception as e:
            logger.warning("piper-tts Python backend failed (%s); falling back to pyttsx3.", e)
            self._speak_pyttsx3(text)

    def _speak_fish_audio(self, text: str) -> None:
        """Fish Audio TTS via HTTPS API (configurable reference voice FRIDAY).
        Audio is cached as mp3 in the same cache directory."""
        try:
            import requests
        except ImportError:
            logger.error("Fish Audio backend requires `pip install requests`.")
            return self._speak_pyttsx3(text)

        reference_id = self.config.fish_audio_reference_id
        # Read API key from environment — TTSConfig has no .secret() method
        api_key = os.environ.get("FISH_AUDIO_API_KEY")
        if not reference_id:
            logger.error("Fish Audio reference_id not set in config.tts.fish_audio_reference_id")
            return self._speak_pyttsx3(text)
        if not api_key or api_key.startswith("<"):
            logger.error("Fish Audio API key not configured (set FISH_AUDIO_API_KEY in .env)")
            return self._speak_pyttsx3(text)

        out_path = self._cache_path(text)
        try:
            if not out_path.exists():
                url = "https://api.fish.audio/v1/tts"
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "reference_id": reference_id,
                    "text": text,
                    "format": self.config.fish_audio_format,
                    "model": self.config.fish_audio_model,
                }
                resp = requests.post(url, json=payload, headers=headers, timeout=30)
                # Capture server message for 402 / any non-2xx WITHOUT exposing api_key
                if resp.status_code == 402:
                    body = resp.text[:500] if resp.text else "(no body)"
                    logger.error(
                        "Fish Audio TTS 402 Payment Required (ref=%s model=%s). "
                        "Server response (key not exposed): %s",
                        reference_id, self.config.fish_audio_model, body,
                    )
                    self._notice_once(
                        "[voice] Fish Audio has no API credit, so replies are text-only. "
                        "API credit is separate from your fish.audio plan credit - top it up at "
                        "https://fish.audio/app/developers")
                    return  # FAIL CLEANLY — do NOT invoke pyttsx3
                resp.raise_for_status()
                out_path.write_bytes(resp.content)
            self._play_audio(out_path)
        except Exception as e:
            # Fail cleanly for any Fish Audio failure — never fall back to pyttsx3 hang
            logger.error("Fish Audio TTS failed cleanly (%s). FRIDAY voice unavailable; returning to loop.", e)
            self._notice_once(f"[voice] Fish Audio voice failed ({type(e).__name__}); replies are text-only. "
                              "Details in data/veronica.log")
            # Intentionally do NOT call self._speak_pyttsx3(text)

    def _speak_elevenlabs(self, text: str) -> None:
        """ElevenLabs TTS via HTTPS API; mp3 cached like the other cloud backends.
        Like Fish Audio, failures are reported and the turn continues text-only
        (no pyttsx3 fallback)."""
        try:
            import requests
        except ImportError:
            logger.error("ElevenLabs backend requires `pip install requests`.")
            return

        voice_id = self.config.elevenlabs_voice_id
        api_key = os.environ.get("ELEVENLABS_API_KEY")
        if not voice_id:
            self._notice_once("[voice] tts.elevenlabs_voice_id is not set in config/config.yaml; replies are text-only.")
            return
        if not api_key or api_key.startswith("<"):
            self._notice_once("[voice] ELEVENLABS_API_KEY is not set in config/.env; replies are text-only.")
            return

        out_path = self._cache_path(text)
        try:
            if not out_path.exists():
                resp = requests.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                    params={"output_format": self.config.elevenlabs_output_format},
                    headers={"xi-api-key": api_key, "Content-Type": "application/json"},
                    json={"text": text, "model_id": self.config.elevenlabs_model},
                    timeout=30,
                )
                if not resp.ok:
                    # ElevenLabs explains the problem in detail.message (e.g. a library voice
                    # on the free plan, or quota exhausted). Never log the key.
                    try:
                        detail = resp.json().get("detail", {})
                        reason = detail.get("message") if isinstance(detail, dict) else str(detail)
                    except ValueError:
                        reason = resp.text[:200]
                    logger.error("ElevenLabs TTS %s (voice=%s model=%s): %s",
                                 resp.status_code, voice_id, self.config.elevenlabs_model, reason)
                    self._notice_once(f"[voice] ElevenLabs refused the request ({resp.status_code}): {reason} "
                                      "Replies are text-only.")
                    return
                out_path.write_bytes(resp.content)
            self._play_audio(out_path)
        except Exception as e:
            logger.error("ElevenLabs TTS failed cleanly (%s); returning to loop.", e)
            self._notice_once(f"[voice] ElevenLabs voice failed ({type(e).__name__}); replies are text-only. "
                              "Details in data/veronica.log")

    _noticed: set = set()

    @classmethod
    def _notice_once(cls, message: str) -> None:
        """Tell the person at the terminal once per run; the log alone is easy to miss."""
        if message not in cls._noticed:
            cls._noticed.add(message)
            print(message)

    def _speak_pyttsx3(self, text: str) -> None:
        try:
            import pyttsx3
        except ImportError:
            logger.error("Neither edge-tts nor pyttsx3 is available; run `pip install pyttsx3`.")
            return
        try:
            engine = pyttsx3.init()
            engine.say(text)
            engine.runAndWait()
        except Exception:
            logger.exception("pyttsx3 failed to speak.")

    @staticmethod
    def _play_audio(path: Path) -> None:
        """Play a cached audio file with zero mandatory dependencies.

        Ladder, native-first: winsound (WAV, Windows) → winmm MCI (mp3/wav, Windows,
        via ctypes) → pygame (optional cross-platform). When none is available the
        file is still generated and cached, so every backend continues to work —
        only the speaker output is skipped, with one warning per process."""
        global _warned_no_player
        path = Path(path)
        for player in (_play_wav_winsound, _play_winmm_mci, _play_pygame):
            if player(path):
                return
        if not _warned_no_player:
            _warned_no_player = True
            logger.warning(
                "No audio player available for %s (file cached at %s). On Windows "
                "winsound/winmm should handle playback; install `pygame` only if you "
                "need playback on a non-Windows platform.",
                path.suffix, path,
            )
