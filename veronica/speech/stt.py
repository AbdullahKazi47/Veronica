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

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from veronica.config import STTConfig
from veronica.logging_setup import get_logger

logger = get_logger("speech.stt")

WARMUP_S = 0.5             # mic output is near-silent right after opening; ignore it
CALIBRATE_S = 0.4          # then measure the room for this long
SPEECH_OVER_AMBIENT = 2.5  # speech must be this many times louder than the room


@dataclass
class STTResult:
    success: bool
    text: str = ""
    error: str = ""
    confidence: Optional[float] = None          # recognizer's confidence in `text`, when it gives one
    alternatives: list[str] = field(default_factory=list)   # other readings, best first (text excluded)
    stats: dict = field(default_factory=dict)   # capture levels: ambient, speech, voiced seconds


def whisper_junk(text: str, hint: str, logprob: Optional[float], min_logprob: float = -0.78) -> str:
    """Why a Whisper transcript looks invented ('' when it looks like real speech)."""
    t = (text or "").strip()
    if logprob is not None and logprob < min_logprob:
        return "low confidence"
    letters = re.sub(r"[^A-Za-zÀ-￿]", "", t)
    if letters and sum(1 for c in letters if ord(c) > 127) / len(letters) > 0.3:
        return "not English"
    if re.fullmatch(r"(?:[A-Za-z]\.\s*){1,8}", t):
        return "dotted letters"
    words = re.findall(r"[a-z']+", t.lower())
    hint_words = set(re.findall(r"[a-z']+", (hint or "").lower()))
    if words and hint_words:
        # The hint echoed back. Seen live: noise transcribed as "SILENCIA, Jev, Bengaluru" and
        # "Bengaluru" from the hint "VERONICA, Jev, Bengaluru". Two hint terms side by side in
        # the hint's own order, or a short line that is (almost) only hint words, is an echo;
        # "Jev, why?" or "weather in Bengaluru" are real requests.
        terms = [" ".join(re.findall(r"[a-z']+", x.lower())) for x in (hint or "").split(",")]
        terms = [x for x in terms if x]
        joined = " " + " ".join(words) + " "
        if any(f" {a} {b} " in joined for a, b in zip(terms, terms[1:])):
            return "echoed the hint"
        in_hint = sum(w in hint_words for w in words)
        if len(words) <= 6 and in_hint >= 2 and len(words) - in_hint <= 1:
            return "echoed the hint"
        if len(words) == 1 and words[0] in hint_words and (logprob is None or logprob < -0.3):
            return "only a hint word"
        if (len(words) <= 5 and all(w in hint_words for w in words)
                and logprob is not None and logprob < -0.55):
            return "only the hint words"
    return ""


def highpass(pcm: bytes, cutoff_hz: float, rate: int = 16000) -> bytes:
    """Remove low-frequency hum (fans, AC, desk thumps) below `cutoff_hz`. Speech lives
    well above 100 Hz, so this only strips noise the recognizer would otherwise chew on."""
    if not pcm or cutoff_hz <= 0:
        return pcm
    import numpy as np
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    if a.size < 64:
        return pcm
    spec = np.fft.rfft(a)
    freqs = np.fft.rfftfreq(a.size, d=1.0 / rate)
    spec[freqs < cutoff_hz] = 0
    out = np.fft.irfft(spec, n=a.size)
    return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


class SpeechToText:
    def __init__(self, config: STTConfig, corrections=None) -> None:
        self.config = config
        self._whisper_model = None  # cached: loading Whisper per utterance is slow
        self.last_capture_stats: dict = {}  # levels from the last capture, for diagnostics
        # Shared always-open mic (voice/mic_hub.py): mic_factory(chunk) -> tap, hub for the
        # live room level. Both None = open the device per listen, as before.
        self.mic_factory: Optional[Callable[[int], object]] = None
        self.hub = None
        self.corrections = corrections            # speech/corrections.py, applied to every transcript
        # Words to expect (her name, your area, routine names...): a hint for Whisper, set by the CLI.
        self.vocabulary: Optional[Callable[[], str]] = None

    # -- shared microphone capture (Phase 11F) ---------------------------------------
    def _capture_utterance(self, timeout_s: float) -> tuple[Optional[bytes], str]:
        """Record one utterance via VERONICA's own MicrophoneInput (sounddevice-first),
        so no backend needs PyAudio. Simple energy endpointing: calibrate on ambient
        noise, wait up to `timeout_s` for speech to start, then stop after
        `pause_threshold` seconds of silence or `max_utterance_s`.

        Returns (int16 mono 16 kHz PCM bytes, "") or (None, error message)."""
        try:
            import numpy as np
            from veronica.voice.microphone import MicrophoneInput
        except ImportError as e:
            return None, f"Microphone capture needs `pip install sounddevice numpy`: {e}"

        rate = 16000
        chunk = 1024
        chunk_s = chunk / rate
        if self.mic_factory is not None:
            mic = self.mic_factory(chunk)           # a tap on the always-open stream
        else:
            from veronica.voice.microphone import resolve_input_device
            device = resolve_input_device(getattr(self.config, "mic_device_name", None), self.config.mic_device_index)
            # None = the system default input (never MicrophoneInput's hard-coded fallback index).
            mic = MicrophoneInput(device_index=device, chunk_size=chunk, rate=rate)
        if not mic.open():
            return None, "No microphone available (sounddevice could not open the input device)."

        def rms(buf: bytes) -> float:
            a = np.frombuffer(buf, dtype=np.int16).astype(np.float64)
            return float(np.sqrt(np.mean(a * a))) if a.size else 0.0

        frames: list[bytes] = []
        try:
            # 1) ambient calibration. The device delivers near-silence for its first
            # ~0.5 s after opening; calibrating on that reads a quiet room in any room,
            # so the threshold fell to the fixed floor (too high for normal speech in a
            # quiet room, too low in a noisy one). Skip the warm-up, then take the
            # median of ~0.4 s so one click/cough doesn't inflate it.
            if getattr(mic, "needs_warmup", True):
                for _ in range(max(1, int(WARMUP_S / chunk_s))):
                    mic.read()
            live = self.hub.ambient_rms() if self.hub is not None else None
            known = live if live is not None else getattr(self, "known_ambient_rms", None)
            if known is not None:
                # Room level measured by the wake-word listener BEFORE the user spoke.
                # Calibrating here instead would measure the user, who often starts
                # talking right after the beep, and demand they out-shout themselves.
                ambient = float(known)
            else:
                calib = [mic.read() for _ in range(max(1, int(CALIBRATE_S / chunk_s)))]
                levels = sorted(rms(c) for c in calib)
                ambient = levels[len(levels) // 2] if levels else 0.0
            threshold = max(ambient * SPEECH_OVER_AMBIENT, float(self.config.min_speech_rms))
            stats = {"ambient_rms": round(ambient, 1), "threshold": round(threshold, 1),
                     "device": mic.device_index, "backend": mic._backend, "peak_rms": 0.0}
            self.last_capture_stats = stats

            # 2) wait for speech to start; keep a short pre-roll so the first syllable isn't clipped
            preroll: list[bytes] = []
            started = False
            waited = 0.0
            ticks = 0
            while waited < timeout_s:
                buf = mic.read()
                if not buf:
                    return None, "Microphone returned no audio."
                ticks += 1
                if self.hub is not None and ticks % 8 == 0:
                    # The room can change while we wait (a TV switched on): follow it.
                    live = self.hub.ambient_rms()
                    if live is not None:
                        ambient = live
                        threshold = max(ambient * SPEECH_OVER_AMBIENT, float(self.config.min_speech_rms))
                        stats["ambient_rms"], stats["threshold"] = round(ambient, 1), round(threshold, 1)
                level = rms(buf)
                stats["peak_rms"] = max(stats["peak_rms"], round(level, 1))
                waited += chunk_s
                preroll.append(buf)
                preroll = preroll[-5:]
                if level >= threshold:
                    started = True
                    frames.extend(preroll)
                    if self.hub is not None:
                        self.hub.user_speaking = True   # the user's voice is not room noise
                    break
            if not started:
                return None, (f"No speech detected in {timeout_s:.0f}s "
                              f"(peak level {stats['peak_rms']}, needed {stats['threshold']}).")

            # 3) record until a pause or the max length
            silent_s = 0.0
            spoken_s = 0.0
            voiced: list[float] = [level]
            while spoken_s < self.config.max_utterance_s:
                buf = mic.read()
                if not buf:
                    break
                frames.append(buf)
                spoken_s += chunk_s
                level = rms(buf)
                stats["peak_rms"] = max(stats["peak_rms"], round(level, 1))
                if level >= threshold * 0.6:
                    silent_s = 0.0
                    voiced.append(level)
                else:
                    silent_s += chunk_s
                if silent_s >= self.config.pause_threshold:
                    break
            stats["duration_s"] = round(len(frames) * chunk_s, 2)
            stats["voiced_s"] = round(len(voiced) * chunk_s, 2)
            ordered = sorted(voiced)
            stats["speech_rms"] = round(ordered[int(len(ordered) * 0.8)] if ordered else 0.0, 1)
            min_voiced = float(getattr(self.config, "min_voiced_s", 0) or 0)
            if min_voiced and stats["voiced_s"] < min_voiced:
                return None, "Too short to be speech (a click or a cough)."
        finally:
            mic.close()
            if self.hub is not None:
                self.hub.user_speaking = False
        return b"".join(frames), ""

    def listen_once(self, timeout_s: int = 6) -> STTResult:
        if self.config.backend == "none":
            return STTResult(success=False, error="STT is disabled in config.")
        if self.config.backend in ("auto", "groq_whisper"):
            return self._listen_chain(timeout_s)
        if self.config.backend == "vosk":
            return self._listen_vosk(timeout_s)
        if self.config.backend == "faster_whisper":
            return self._listen_faster_whisper(timeout_s)
        return self._listen_google_free(timeout_s)

    # -- "auto": capture once, then the most accurate engine that answers ---------
    def _listen_chain(self, timeout_s: int) -> STTResult:
        """Groq Whisper (with a vocabulary hint) -> Google -> offline faster-whisper.
        Measured on this PC: 8% / 24% / 21% word errors, all about a second."""
        pcm, err = self._capture_utterance(timeout_s)
        stats = dict(self.last_capture_stats)
        if pcm is None:
            return STTResult(success=False, error=err, stats=stats)
        pcm = highpass(pcm, float(getattr(self.config, "highpass_hz", 0) or 0))
        engines = [self._recognize_groq_whisper] if self._groq_key() else []
        if self.config.backend == "auto":
            engines += [self._recognize_google_pcm, self._recognize_local_whisper]
        last = STTResult(success=False, error="No speech recognizer is available.")
        for engine in engines:
            try:
                result = engine(pcm)
            except Exception as e:  # noqa: BLE001 - try the next engine
                logger.info("Speech engine %s failed: %s", engine.__name__, e)
                continue
            result.stats = {**stats, **(result.stats or {})}
            if result.success or result.error.startswith("Not speech"):
                return result              # a clear "not speech" is an answer, not an outage
            last = result
        return last

    def _groq_key(self) -> str:
        import os
        name = getattr(self.config, "groq_api_key_env", "GROQ_API_KEY")
        key = os.environ.get(name, "")
        if not key:
            try:
                from veronica.config import load_config
                key = load_config().secret(name) or ""
            except Exception:  # noqa: BLE001
                key = ""
        return key

    def _recognize_groq_whisper(self, pcm: bytes) -> STTResult:
        import io
        import math
        import wave
        import requests
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm)
        data = {"model": getattr(self.config, "groq_whisper_model", "whisper-large-v3-turbo"),
                "language": (self.config.language or "en").split("-")[0], "temperature": "0",
                "response_format": "verbose_json"}
        hint = self.vocabulary() if callable(getattr(self, "vocabulary", None)) else ""
        if hint:
            data["prompt"] = hint[:600]
        r = requests.post("https://api.groq.com/openai/v1/audio/transcriptions", timeout=15,
                          headers={"Authorization": f"Bearer {self._groq_key()}"},
                          files={"file": ("speech.wav", buf.getvalue(), "audio/wav")}, data=data)
        if r.status_code >= 400:
            return STTResult(success=False, error=f"Groq Whisper error {r.status_code}")
        return self._from_whisper(r.json(), hint)

    def _from_whisper(self, payload: dict, hint: str = "") -> STTResult:
        """Whisper invents words for silence and noise, so suspicious output is rejected
        rather than acted on. Measured on this PC (Groq whisper-large-v3-turbo): real speech,
        even a short "Yes" or "Thanks", averages a log-probability above -0.65; silence, hum,
        fan noise and clicks came out at -0.81 or lower, as dotted letters ("S.T.A.R.S.") or
        as the hint words repeated back ("Rek, Bengaluru"). Groq leaves no_speech_prob at 0,
        so it isn't relied on."""
        raw = (payload.get("text") or "").strip()
        text = self.correct(raw)
        if not re.sub(r"[\W_]+", "", text):
            return STTResult(success=False, error="Couldn't understand the audio.")
        segments = payload.get("segments") or []
        logprob = None
        if segments:
            no_speech = max(float(seg.get("no_speech_prob", 0) or 0) for seg in segments)
            if no_speech >= float(getattr(self.config, "no_speech_reject", 0.6) or 0.6):
                return STTResult(success=False, error=f"Not speech (no-speech score; heard {text!r}).")
            logprob = sum(float(seg.get("avg_logprob", 0) or 0) for seg in segments) / len(segments)
        why = whisper_junk(raw, hint, logprob, float(getattr(self.config, "whisper_min_logprob", -0.78)))
        if why:
            return STTResult(success=False, error=f"Not speech ({why}; heard {text!r}).")
        # Whisper's score isn't on Google's confidence scale, so the "did you say...?" and
        # "ignore unclear speech" bars (tuned for Google) aren't fed from it.
        return STTResult(success=True, text=text, confidence=None,
                         stats={"whisper_logprob": round(logprob, 3) if logprob is not None else None})

    def _recognize_google_pcm(self, pcm: bytes) -> STTResult:
        import speech_recognition as sr
        recognizer = sr.Recognizer()
        return self._recognize_google(sr, recognizer, sr.AudioData(pcm, 16000, 2))

    def _recognize_local_whisper(self, pcm: bytes) -> STTResult:
        """Offline last resort: faster-whisper on this PC (no internet needed)."""
        import numpy as np
        from faster_whisper import WhisperModel
        if self._whisper_model is None:
            self._whisper_model = WhisperModel(getattr(self.config, "offline_whisper_model", "base"),
                                               device="cpu", compute_type="int8")
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        hint = self.vocabulary() if callable(getattr(self, "vocabulary", None)) else None
        segments, _ = self._whisper_model.transcribe(samples, language="en", beam_size=5,
                                                     initial_prompt=hint or None)
        segs = list(segments)
        return self._from_whisper({"text": " ".join(seg.text.strip() for seg in segs),
                                   "segments": [{"no_speech_prob": seg.no_speech_prob, "avg_logprob": seg.avg_logprob}
                                                for seg in segs]}, hint or "")

    # -- backends -----------------------------------------------------------------
    def _listen_google_free(self, timeout_s: int) -> STTResult:
        try:
            import speech_recognition as sr
        except ImportError:
            return STTResult(success=False, error="`SpeechRecognition` not installed; run `pip install SpeechRecognition PyAudio`.")

        recognizer = sr.Recognizer()
        recognizer.energy_threshold = self.config.energy_threshold
        recognizer.pause_threshold = self.config.pause_threshold

        # Phase 11F: PyAudio is optional and has no wheel for Python 3.14, so capture
        # through VERONICA's sounddevice-backed MicrophoneInput unless PyAudio is
        # actually present (or mic_backend is explicitly "pyaudio").
        use_pyaudio = False
        if self.config.mic_backend == "pyaudio":
            try:
                import pyaudio  # noqa: F401
                use_pyaudio = True
            except ImportError:
                use_pyaudio = False
        if not use_pyaudio:
            pcm, err = self._capture_utterance(timeout_s)
            if pcm is None:
                return STTResult(success=False, error=err, stats=dict(self.last_capture_stats))
            pcm = highpass(pcm, float(getattr(self.config, "highpass_hz", 0) or 0))
            audio = sr.AudioData(pcm, 16000, 2)
            result = self._recognize_google(sr, recognizer, audio)
            result.stats = dict(self.last_capture_stats)
            return result

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
        return self._recognize_google(sr, recognizer, audio)

    def _recognize_google(self, sr, recognizer, audio) -> STTResult:
        try:
            raw = recognizer.recognize_google(audio, language=self.config.language, show_all=True)
        except sr.UnknownValueError:
            return STTResult(success=False, error="Couldn't understand the audio.")
        except sr.RequestError as e:
            return STTResult(success=False, error=f"Speech service unreachable: {e}")
        return self._from_google(raw)

    def _from_google(self, raw) -> STTResult:
        """Google's full answer -> best transcript, its confidence, and the alternatives
        (all with personal corrections applied)."""
        alts = (raw or {}).get("alternative") if isinstance(raw, dict) else None
        if not alts:
            return STTResult(success=False, error="Couldn't understand the audio.")
        texts, seen = [], set()
        for a in alts:
            t = self.correct((a.get("transcript") or "").strip())
            if t and t.lower() not in seen:
                seen.add(t.lower())
                texts.append(t)
        if not texts:
            return STTResult(success=False, error="Couldn't understand the audio.")
        conf = alts[0].get("confidence")
        return STTResult(success=True, text=texts[0], confidence=float(conf) if conf is not None else None,
                         alternatives=texts[1:5])

    def correct(self, text: str) -> str:
        if self.corrections is None or not text:
            return text
        try:
            return self.corrections.apply(text)
        except Exception:  # noqa: BLE001 - a bad correction file must never break listening
            return text

    def _listen_faster_whisper(self, timeout_s: int) -> STTResult:
        """Offline Whisper via faster-whisper (CTranslate2). Quality is high even
        for the 'base' model, and it works on CPU (float32/int8) or CUDA (float16).
        Audio is captured from the mic in a short fixed window — same shape as the
        vosk backend, so the interface is identical to the caller."""
        try:
            import numpy as np
            from faster_whisper import WhisperModel
        except ImportError:
            return STTResult(
                success=False,
                error="Offline Whisper needs `pip install faster-whisper sounddevice numpy`. "
                "On first use it downloads a small model from Hugging Face.",
            )

        if self._whisper_model is None:
            try:
                self._whisper_model = WhisperModel(
                    self.config.faster_whisper_model,
                    device="auto",  # picks CUDA when available, else CPU
                    compute_type=self.config.faster_whisper_compute_type,
                )
            except Exception as e:
                return STTResult(
                    success=False,
                    error=f"Couldn't load faster-whisper model '{self.config.faster_whisper_model}': {e}",
                )
        model = self._whisper_model

        # Phase 11F: same endpointed capture as google_free, on the configured device.
        pcm, err = self._capture_utterance(timeout_s)
        if pcm is None:
            return STTResult(success=False, error=err)
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

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
