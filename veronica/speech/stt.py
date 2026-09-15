"""
Speech-to-text, behind one interface with three backends:
  - "google_free": speech_recognition's free Google Web Speech endpoint (no API key,
    needs internet). This is what Prodigy-AI/J.A.R.V.I.S use, generalized behind a
    real interface instead of being called ad-hoc from a GUI callback.
  - "vosk": fully offline, no internet and no key at all, for privacy/offline use —
    none of the six studied repos offer this; added because "no paid API keys" is a
    stated requirement and offline STT is the natural fully-free endpoint of that.
  - "faster_whisper": fully offline Whisper via the faster-whisper package (uses
    CTranslate2). Runs on CPU or CUDA on Windows; higher quality than Vosk for a
    modest install cost. This mirrors the Whisper-STT pipeline JARVIS OS documents,
    scaled down to a single desktop machine and gated behind its own optional
    dependency — VERONICA keeps working if it isn't installed.

All three raise a clear, typed result rather than throwing, so the orchestrator can
react to "didn't understand" vs. "no microphone available" differently.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from veronica.config import STTConfig
from veronica.logging_setup import get_logger

logger = get_logger("speech.stt")


@dataclass
class STTResult:
    success: bool
    text: str = ""
    error: str = ""


class SpeechToText:
    def __init__(self, config: STTConfig) -> None:
        self.config = config

    def listen_once(self, timeout_s: int = 6) -> STTResult:
        if self.config.backend == "none":
            return STTResult(success=False, error="STT is disabled in config.")
        if self.config.backend == "vosk":
            return self._listen_vosk(timeout_s)
        if self.config.backend == "faster_whisper":
            return self._listen_faster_whisper(timeout_s)
        return self._listen_google_free(timeout_s)

    # -- backends -----------------------------------------------------------------
    def _listen_google_free(self, timeout_s: int) -> STTResult:
        try:
            import speech_recognition as sr
        except ImportError:
            return STTResult(success=False, error="`SpeechRecognition` not installed; run `pip install SpeechRecognition PyAudio`.")

        recognizer = sr.Recognizer()
        recognizer.energy_threshold = self.config.energy_threshold
        recognizer.pause_threshold = self.config.pause_threshold
        try:
            with sr.Microphone() as source:
                recognizer.adjust_for_ambient_noise(source, duration=0.4)
                audio = recognizer.listen(source, timeout=timeout_s)
        except ImportError as e:
            # `sr.Microphone()` raises ImportError("Please install PyAudio") when the
            # portaudio binding is missing. PyAudio is optional — VERONICA does not
            # install it by default (no prebuilt wheel for every Python version) — so
            # surface it as the actionable choice it is, not as a listening failure.
            return STTResult(
                success=False,
                error=f"Microphone needs `PyAudio`, which is optional: {e} "
                "VERONICA only requires it for this google_free backend. Install a "
                "PyAudio wheel for your Python (or switch stt.backend to "
                "`faster_whisper`/`vosk`, which capture audio via `sounddevice`).",
            )
        except OSError as e:
            return STTResult(success=False, error=f"No microphone available: {e}")
        except Exception as e:
            return STTResult(success=False, error=f"Listening timed out or failed: {e}")

        try:
            text = recognizer.recognize_google(audio, language=self.config.language)
            return STTResult(success=True, text=text)
        except sr.UnknownValueError:
            return STTResult(success=False, error="Couldn't understand the audio.")
        except sr.RequestError as e:
            return STTResult(success=False, error=f"Speech service unreachable: {e}")

    def _listen_faster_whisper(self, timeout_s: int) -> STTResult:
        """Offline Whisper via faster-whisper (CTranslate2). Quality is high even
        for the 'base' model, and it works on CPU (float32/int8) or CUDA (float16).
        Audio is captured from the mic in a short fixed window — same shape as the
        vosk backend, so the interface is identical to the caller."""
        try:
            import numpy as np
            import sounddevice as sd
            from faster_whisper import WhisperModel
        except ImportError:
            return STTResult(
                success=False,
                error="Offline Whisper needs `pip install faster-whisper sounddevice numpy`. "
                "On first use it downloads a small model from Hugging Face.",
            )

        try:
            model = WhisperModel(
                self.config.faster_whisper_model,
                device="auto",  # picks CUDA when available, else CPU
                compute_type=self.config.faster_whisper_compute_type,
            )
        except Exception as e:
            return STTResult(
                success=False,
                error=f"Couldn't load faster-whisper model '{self.config.faster_whisper_model}': {e}",
            )

        sample_rate = 16000
        blocks = int(timeout_s * sample_rate)  # one capture window, then transcribe
        try:
            audio = sd.rec(blocks, samplerate=sample_rate, channels=1, dtype="float32")
            sd.wait()
            samples = np.asarray(audio).flatten()
        except Exception as e:
            return STTResult(success=False, error=f"No microphone available: {e}")

        try:
            segments, _info = model.transcribe(samples, language="en", beam_size=5)
            text = " ".join(seg.text.strip() for seg in segments).strip()
        except Exception as e:
            return STTResult(success=False, error=f"Whisper transcription failed: {e}")

        if not text:
            return STTResult(success=False, error="No speech detected in time.")
        return STTResult(success=True, text=text)

    def _listen_vosk(self, timeout_s: int) -> STTResult:
        try:
            import json as _json
            import queue

            import sounddevice as sd
            import vosk
        except ImportError:
            return STTResult(
                success=False,
                error="Offline STT needs `pip install vosk sounddevice` plus a downloaded Vosk model.",
            )

        model_path = self.config.vosk_model_path
        try:
            model = vosk.Model(model_path)
        except Exception:
            return STTResult(success=False, error=f"Vosk model not found at '{model_path}'. Download one from alphacephei.com/vosk/models.")

        q: "queue.Queue[bytes]" = queue.Queue()

        def callback(indata, frames, time_info, status):
            q.put(bytes(indata))

        rec = vosk.KaldiRecognizer(model, 16000)
        try:
            with sd.RawInputStream(samplerate=16000, blocksize=8000, dtype="int16", channels=1, callback=callback):
                import time
                start = time.time()
                while time.time() - start < timeout_s:
                    data = q.get()
                    if rec.AcceptWaveform(data):
                        result = _json.loads(rec.Result())
                        text = result.get("text", "")
                        if text:
                            return STTResult(success=True, text=text)
                return STTResult(success=False, error="No speech detected in time.")
        except Exception as e:
            return STTResult(success=False, error=f"Offline STT failed: {e}")
