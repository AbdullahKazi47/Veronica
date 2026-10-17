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
import itertools
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

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
_alias_ids = itertools.count(1)


def play_interruptible(path: Path, stop: threading.Event) -> Optional[bool]:
    """Play an mp3/wav through winmm MCI WITHOUT blocking in the driver, so `stop` can
    cut it off mid-word. True = finished, False = interrupted, None = MCI unavailable
    (the caller falls back to the ordinary blocking player)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        winmm = ctypes.windll.winmm
    except (AttributeError, OSError):
        return None
    alias = f"veronica_{next(_alias_ids)}"   # unique: two threads never share a device
    send = winmm.mciSendStringW
    if send(f'open "{path}" alias {alias}', None, 0, None) != 0:
        return None
    try:
        buf = ctypes.create_unicode_buffer(64)
        send(f"status {alias} length", buf, 64, None)
        try:
            length_s = int(buf.value) / 1000.0
        except ValueError:
            length_s = 60.0
        if send(f"play {alias}", None, 0, None) != 0:
            return None
        started = time.monotonic()
        while True:
            if stop.is_set():
                send(f"stop {alias}", None, 0, None)
                return False
            send(f"status {alias} mode", buf, 64, None)
            elapsed = time.monotonic() - started
            if (buf.value != "playing" and elapsed > 0.15) or elapsed > length_s + 2.0:
                return True
            time.sleep(0.03)
    finally:
        send(f"close {alias}", None, 0, None)


def split_for_speech(text: str, min_chars: int = 40, max_chars: int = 240) -> list[str]:
    """Sentence-sized pieces for pipelined speech: the first is short so audio starts
    fast; tiny sentences are merged so we don't make an API call per 'Okay.'"""
    parts = [p.strip() for p in re.split(r"(?<=[.!?;:])\s+", (text or "").strip()) if p.strip()]
    pieces: list[str] = []
    for part in parts:
        while len(part) > max_chars:                      # a very long sentence: split at a comma
            cut = part.rfind(", ", 0, max_chars)
            cut = cut + 1 if cut > min_chars else max_chars
            pieces.append(part[:cut].strip())
            part = part[cut:].strip()
        if part:
            pieces.append(part)
    chunks: list[str] = []
    for part in pieces:
        # The first chunk stays alone so the voice starts quickly; later short bits are
        # merged into the previous chunk while it stays under max_chars.
        mergeable = len(chunks) > 1 and len(chunks[-1]) < min_chars or (chunks and len(part) < min_chars // 2)
        if mergeable and len(chunks[-1]) + 1 + len(part) <= max_chars:
            chunks[-1] = f"{chunks[-1]} {part}"
        else:
            chunks.append(part)
    return chunks or ([text.strip()] if text and text.strip() else [])


def _short_first(pieces: list[str], longest: int = 70) -> list[str]:
    """For a voice made on this PC, where time to the first sound grows with its length:
    cut a long first sentence at its first comma so she starts talking sooner."""
    if not pieces or len(pieces[0]) <= longest:
        return pieces
    first = pieces[0]
    cut = first.find(", ", 15)
    if cut < 0 or cut > len(first) - 15:
        return pieces
    return [first[:cut + 1], first[cut + 2:]] + pieces[1:]


class SynchronousSpeaker:
    """Wrapper for TTS backends that blocks until playback completes."""
    def __init__(self, tts_instance):
        self.tts = tts_instance

    def speak(self, text: str) -> None:
        """Speak text and block until audio playback finishes."""
        self.tts.speak(text, block=True)

    def speak_interruptible(self, text: str, stop: threading.Event,
                            on_piece: Optional[Callable[[str], None]] = None) -> bool:
        """Speak, returning False if `stop` cut it short (the user talked over her)."""
        return self.tts.speak_interruptible(text, stop, on_piece=on_piece)

    def speak_stream(self, pieces, stop: threading.Event,
                     on_piece: Optional[Callable[[str], None]] = None) -> bool:
        """Speak sentences as they arrive (a reply still being written)."""
        return self.tts.speak_stream(pieces, stop, on_piece=on_piece)


class TextToSpeech:
    def __init__(self, config: TTSConfig, cache_dir: Path) -> None:
        self.config = config
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._primary_out_until = 0.0     # main voice refused (no credits): use the backup until then

    def warm(self) -> None:
        """Get the voice ready in the background at start-up. The first request to
        Microsoft's voice service in a run takes 3-7 s (connection set-up) and later ones
        under 1 s; Kokoro needs ~2 s to load. Doing that now keeps it off the greeting."""
        def go():
            try:
                if self.config.backend == "kokoro":
                    from veronica.speech import kokoro_voice
                    if kokoro_voice.installed(self.kokoro_folder()):
                        kokoro_voice._engine(self.kokoro_folder())   # load the model now
                    return
                import edge_tts

                async def ping():          # not cached: the point is the connection
                    async for _chunk in edge_tts.Communicate("Ready.", self.config.voice).stream():
                        pass
                asyncio.run(ping())
            except Exception:  # noqa: BLE001
                pass
        if self.config.backend in ("edge_tts", "kokoro"):
            threading.Thread(target=go, name="tts-warm", daemon=True).start()

    # -- backup voice ------------------------------------------------------------------
    def _primary_out(self) -> bool:
        return time.time() < self._primary_out_until

    def _mark_primary_out(self, reason: str) -> None:
        minutes = float(getattr(self.config, "backup_minutes", 180) or 180)
        self._primary_out_until = time.time() + minutes * 60
        logger.warning("Main voice unavailable (%s); using the backup voice for %.0f min.", reason, minutes)
        self._notice_once("[voice] My ElevenLabs voice credits have run out, so I'm using my backup voice "
                          "until they're topped up or reset.")

    def _backup_file(self, text: str) -> Optional[Path]:
        """Synthesize with the free backup voice (edge-tts). None if that fails too."""
        return self._edge_file(text, getattr(self.config, "backup_voice", "") or "en-GB-SoniaNeural")

    def _edge_file(self, text: str, voice: str) -> Optional[Path]:
        """One clip from a free Microsoft neural voice (edge-tts), cached. None on failure."""
        digest = hashlib.sha256(f"edge:{voice}:{text}".encode("utf-8")).hexdigest()[:24]
        out = self.cache_dir / f"{digest}.mp3"
        if out.exists():
            return out
        try:
            import edge_tts

            async def _gen():
                await edge_tts.Communicate(text, voice, rate=getattr(self.config, "rate", "+0%") or "+0%").save(str(out))
            asyncio.run(_gen())
            return out if out.exists() and out.stat().st_size > 0 else None
        except Exception as e:  # noqa: BLE001
            logger.warning("Edge voice %s failed (%s).", voice, e)
            return None

    def _voice_file(self, text: str, previous: str = "", following: str = "") -> Optional[Path]:
        """The main voice if it's available, else the backup."""
        if self.config.backend == "elevenlabs" and not self._primary_out():
            path = self._elevenlabs_file(text, previous=previous, following=following)
            if path is not None:
                return path
        if self.config.backend == "kokoro" and self.config.voice:
            path = self._kokoro_file(text, self.config.voice)
            if path is not None:
                return path
        if self.config.backend == "edge_tts" and self.config.voice:
            path = self._edge_file(text, self.config.voice)
            if path is not None:
                return path
        return self._backup_file(text)

    def kokoro_folder(self) -> Path:
        return self.cache_dir.parent / "models" / "kokoro"

    def _kokoro_file(self, text: str, voice: str) -> Optional[Path]:
        """One clip from Kokoro, the neural voice on this PC (speech/kokoro_voice.py)."""
        from veronica.speech import kokoro_voice
        folder = self.kokoro_folder()
        if not kokoro_voice.installed(folder):
            return None
        speed = float(getattr(self.config, "kokoro_speed", 1.0) or 1.0)
        digest = hashlib.sha256(f"kokoro:{voice}:{speed}:{text}".encode("utf-8")).hexdigest()[:24]
        out = self.cache_dir / f"{digest}.wav"
        if out.exists():
            return out
        return kokoro_voice.synthesize(folder, text, voice, out, speed=speed)

    def speak(self, text: str, block: bool = False) -> None:
        """Speak `text`. Runs on a background thread by default so callers (the CLI
        loop, the orchestrator) never stall waiting on audio playback."""
        if not text or self.config.backend == "none":
            return

        if self.config.backend == "kokoro":
            target = self._speak_pipelined
        elif self.config.backend == "edge_tts":
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

    def speak_interruptible(self, text: str, stop: threading.Event,
                            on_piece: Optional[Callable[[str], None]] = None) -> bool:
        """Pipelined, interruptible speech. The reply is cut into sentences; sentence N+1
        is synthesized while sentence N plays, so the voice starts after ONE sentence's
        synthesis instead of the whole reply's. `stop` ends playback mid-word.
        Returns True when everything was spoken, False when interrupted.
        Backends without a file-per-clip path speak whole (not interruptible)."""
        if not text or self.config.backend == "none":
            return True
        if self.config.backend not in ("elevenlabs", "edge_tts", "kokoro"):
            self.speak(text, block=True)
            return True
        pieces = split_for_speech(text)
        if self.config.backend == "kokoro":
            pieces = _short_first(pieces)
        return self.speak_stream(pieces, stop, on_piece=on_piece)

    def speak_stream(self, pieces, stop: threading.Event,
                     on_piece: Optional[Callable[[str], None]] = None) -> bool:
        """Speak `pieces` (any iterable of sentences; it may still be filling up while the
        reply is being written). Each one is synthesized while the previous one plays.
        Returns True when all were spoken, False when `stop` cut it short."""
        if self.config.backend not in ("elevenlabs", "edge_tts", "kokoro"):
            for piece in pieces:
                if stop.is_set():
                    return False
                if on_piece is not None:
                    on_piece(piece)
                self.speak(piece, block=True)
            return True
        clips: "queue.Queue[Optional[tuple[str, Optional[Path]]]]" = queue.Queue()

        known = pieces if isinstance(pieces, list) else None    # whole reply known: give the next sentence too

        def produce() -> None:
            previous = ""
            try:
                for i, piece in enumerate(pieces):
                    if stop.is_set():
                        break
                    following = known[i + 1] if known is not None and i + 1 < len(known) else ""
                    clips.put((piece, self._voice_file(piece, previous=previous, following=following)))
                    previous = piece
            finally:
                clips.put(None)
        threading.Thread(target=produce, name="tts-synth", daemon=True).start()

        while True:
            item = clips.get()
            if item is None:
                return not stop.is_set()
            piece, path = item
            if on_piece is not None:
                on_piece(piece)
            if path is None:            # main and backup voices both failed: Windows' own voice
                self._speak_pyttsx3(piece)
                continue
            done = play_interruptible(path, stop)
            if done is None:
                self._play_audio(path)  # no MCI: ordinary blocking playback
            elif done is False:
                return False

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

    def _speak_pipelined(self, text: str) -> None:
        self.speak_interruptible(text, threading.Event())

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
        path = self._voice_file(text)
        if path is not None:
            self._play_audio(path)
        else:
            self._speak_pyttsx3(text)

    def _elevenlabs_file(self, text: str, previous: str = "", following: str = "") -> Optional[Path]:
        """Synthesize `text` (cached) and return the mp3 path, or None on failure.
        `previous`/`following` are the neighbouring sentences: ElevenLabs uses them so
        sentence-by-sentence speech keeps one natural intonation across the reply."""
        try:
            import requests
        except ImportError:
            logger.error("ElevenLabs backend requires `pip install requests`.")
            return None

        voice_id = self.config.elevenlabs_voice_id
        api_key = os.environ.get("ELEVENLABS_API_KEY")
        if not voice_id:
            self._notice_once("[voice] tts.elevenlabs_voice_id is not set in config/config.yaml; replies are text-only.")
            return None
        if not api_key or api_key.startswith("<"):
            self._notice_once("[voice] ELEVENLABS_API_KEY is not set in config/.env; replies are text-only.")
            return None

        out_path = self._cache_path(text)
        try:
            if not out_path.exists():
                body = {"text": text, "model_id": self.config.elevenlabs_model}
                if previous:
                    body["previous_text"] = previous
                if following:
                    body["next_text"] = following
                resp = requests.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                    params={"output_format": self.config.elevenlabs_output_format},
                    headers={"xi-api-key": api_key, "Content-Type": "application/json"},
                    json=body,
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
                    if resp.status_code in (401, 402, 429) or "quota" in str(reason).lower() \
                            or "credit" in str(reason).lower():
                        self._mark_primary_out(f"{resp.status_code}: {reason}")
                    else:
                        self._notice_once(f"[voice] ElevenLabs refused the request ({resp.status_code}): {reason} "
                                          "Using my backup voice.")
                    return None
                out_path.write_bytes(resp.content)
            return out_path
        except Exception as e:
            logger.error("ElevenLabs TTS failed cleanly (%s); returning to loop.", e)
            self._notice_once(f"[voice] ElevenLabs voice failed ({type(e).__name__}); replies are text-only. "
                              "Details in data/veronica.log")
            return None

    _noticed: set = set()

    @classmethod
    def _notice_once(cls, message: str) -> None:
        """Tell the person at the terminal once per run; the log alone is easy to miss."""
        if message not in cls._noticed:
            cls._noticed.add(message)
            # Shown like the rest of the conversation: as VERONICA saying it.
            print("VERONICA: " + message.replace("[voice] ", "", 1))

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
