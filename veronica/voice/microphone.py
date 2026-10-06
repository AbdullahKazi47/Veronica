"""Phase 11E.1 — Microphone with sounddevice backend + pyaudio fallback."""
from __future__ import annotations

class MicrophoneInput:
    """Sounddevice-first (Python 3.14-supported); pyaudio optional fallback.
    Never fakes audio capture. BLOCKED reported honestly when no backend works."""
    def __init__(self, device_index: int | None = 2, chunk_size: int = 1024, rate: int = 16000):
        self.device_index = device_index
        self.chunk_size = chunk_size
        self.rate = rate
        self._stream = None
        self._backend = None  # "sounddevice" | "pyaudio" | None
        self._open = False

    def list_devices(self) -> list[dict]:
        try:
            import sounddevice as sd
            devices = []
            for d in sd.query_devices():
                if isinstance(d, dict) and d.get("max_input_channels", 0) > 0:
                    devices.append({"index": d.get("index"), "name": d.get("name"), "channels": d.get("max_input_channels")})
            return devices
        except Exception:
            # Fallback to pyaudio
            try:
                import pyaudio
                p = pyaudio.PyAudio()
                devices = []
                for i in range(p.get_device_count()):
                    info = p.get_device_info_by_index(i)
                    if info.get("maxInputChannels", 0) > 0:
                        devices.append({"index": i, "name": info.get("name"), "channels": info.get("maxInputChannels")})
                p.terminate()
                return devices
            except ImportError:
                return []  # BLOCKED — honest, not faked

    def open(self) -> bool:
        # Prefer sounddevice (Python 3.14 supported)
        try:
            import sounddevice as sd
            import numpy as np
            kw = {"samplerate": self.rate, "channels": 1, "dtype": "int16", "blocksize": self.chunk_size}
            if self.device_index is not None:
                kw["device"] = self.device_index
            self._stream = sd.InputStream(**kw)
            self._stream.start()
            self._backend = "sounddevice"
            self._open = True
            return True
        except Exception:
            # Fallback: pyaudio optional
            try:
                import pyaudio
                self._stream = pyaudio.PyAudio().open(format=8 if self.chunk_size <= 1024 else pyaudio.paInt16,
                                                      channels=1, rate=self.rate, input=True,
                                                      frames_per_buffer=self.chunk_size,
                                                      input_device_index=self.device_index)
                self._stream.start_stream()
                self._backend = "pyaudio"
                self._open = True
                return True
            except ImportError:
                self._backend = None
                return False  # BLOCKED — honest

    def read(self) -> bytes:
        if not self._open or self._stream is None:
            return b""
        try:
            if self._backend == "sounddevice":
                import numpy as np, sounddevice as sd
                # Read float32/int16 frames; return raw bytes
                frame, overflow = self._stream.read(self.chunk_size)
                if isinstance(frame, np.ndarray):
                    return frame.tobytes()
                return b""
            else:
                return self._stream.read(self.chunk_size, exception_on_overflow=False)
        except Exception:
            return b""

    def close(self):
        if self._stream is not None:
            try:
                if self._backend == "sounddevice":
                    self._stream.stop()
                    self._stream.close()
                else:
                    self._stream.stop_stream()
                    self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._open = False
        self._backend = None
