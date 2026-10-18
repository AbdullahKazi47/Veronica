"""
One always-open microphone stream shared by everything that listens.

Before this, every listen opened the device, threw away ~0.5 s of warm-up silence and
closed it again: a fixed delay on every turn, and words spoken right after the beep (or
right after VERONICA finished talking) were clipped — a big source of mishearing. Now one
stream runs for the whole session and each consumer (wake word, speech recognition, the
"talk over her" listener) takes a lightweight *tap* with the same open/read/close shape as
MicrophoneInput. Taps start with a short pre-roll from a ring buffer, so the first
syllable is never lost.

The hub also keeps a running estimate of the room's noise level, so a TV switched on
mid-conversation raises the bar for what counts as the user speaking. Frames captured
while VERONICA herself is talking are left out of that estimate.
"""
from __future__ import annotations

import queue
import threading
import time
from collections import deque
from typing import Optional

from veronica.logging_setup import get_logger

logger = get_logger("voice.mic_hub")

RATE = 16000
BLOCK = 320                  # 20 ms per callback block
_RING_S = 1.5                # pre-roll / recent-audio buffer
_AMBIENT_WINDOW_S = 4.0      # room-level estimate looks at the last few seconds
_AMBIENT_PERCENTILE = 0.25   # low percentile: speech bursts don't inflate the estimate


def rms(buf: bytes) -> float:
    import numpy as np
    a = np.frombuffer(buf, dtype=np.int16).astype(np.float64)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


class MicTap:
    """A consumer's view of the hub: open() / read() -> chunk_size samples / close()."""

    needs_warmup = False          # the stream is already running: no warm-up silence
    _backend = "hub"

    def __init__(self, hub: "MicHub", chunk_size: int, preroll_s: float = 0.0) -> None:
        self.hub = hub
        self.chunk_size = chunk_size
        self.rate = RATE
        self.device_index = hub.device_index
        self._preroll_s = preroll_s
        self._q: "queue.Queue[bytes]" = queue.Queue(maxsize=int(10 * RATE / BLOCK))  # ~10 s
        self._buf = b""
        self._open = False

    def open(self) -> bool:
        if not self.hub.ensure_running():
            return False
        self._buf = self.hub.recent(self._preroll_s) if self._preroll_s else b""
        self.hub._add(self)
        self._open = True
        return True

    def _push(self, data: bytes) -> None:
        try:
            self._q.put_nowait(data)
        except queue.Full:            # a stalled consumer must never block the stream
            try:
                self._q.get_nowait()
                self._q.put_nowait(data)
            except queue.Empty:
                pass

    def read(self) -> bytes:
        need = self.chunk_size * 2
        deadline = time.monotonic() + 1.0
        while len(self._buf) < need:
            if not self._open:
                return b""
            try:
                self._buf += self._q.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                return b""            # the stream stopped delivering
        out, self._buf = self._buf[:need], self._buf[need:]
        return out

    def close(self) -> None:
        self.hub._remove(self)
        self._open = False
        self._buf = b""


class MicHub:
    def __init__(self, device_index: Optional[int] = None, stream_factory=None) -> None:
        self.device_index = device_index
        self._stream_factory = stream_factory
        self._stream = None
        self._taps: list[MicTap] = []
        self._lock = threading.Lock()
        self._ring: deque[bytes] = deque(maxlen=int(_RING_S * RATE / BLOCK))
        self._levels: deque[float] = deque(maxlen=int(_AMBIENT_WINDOW_S * RATE / BLOCK))
        self.self_speaking = False    # set while VERONICA talks: keep her voice out of the room level
        self.user_speaking = False    # set while an utterance is being recorded: same for the user
        self.failed = ""

    # -- stream ---------------------------------------------------------------------------
    def ensure_running(self) -> bool:
        with self._lock:
            if self._stream is not None:
                return True
            try:
                if self._stream_factory is not None:
                    self._stream = self._stream_factory(self._on_audio)
                else:
                    import sounddevice as sd
                    kw = {"samplerate": RATE, "channels": 1, "dtype": "int16", "blocksize": BLOCK,
                          "callback": lambda indata, frames, t, status: self._on_audio(indata.tobytes())}
                    if self.device_index is not None:
                        kw["device"] = self.device_index
                    self._stream = sd.InputStream(**kw)
                    self._stream.start()
                self.failed = ""
                logger.info("Microphone hub running (device=%s).", self.device_index)
                return True
            except Exception as e:  # noqa: BLE001
                self._stream, self.failed = None, str(e)
                logger.warning("Microphone hub couldn't open the device: %s", e)
                return False

    def stop(self) -> None:
        with self._lock:
            s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:  # noqa: BLE001
                pass

    def _on_audio(self, data: bytes) -> None:
        from veronica.voice.microphone import boost
        data = boost(data)                   # earphones mode: quiet speech made louder
        self._ring.append(data)
        if not (self.self_speaking or self.user_speaking):
            self._levels.append(rms(data))
        for tap in list(self._taps):
            tap._push(data)

    # -- consumers --------------------------------------------------------------------------
    def tap(self, chunk_size: int, preroll_s: float = 0.0) -> MicTap:
        return MicTap(self, chunk_size, preroll_s)

    def _add(self, tap: MicTap) -> None:
        with self._lock:
            if tap not in self._taps:
                self._taps.append(tap)

    def _remove(self, tap: MicTap) -> None:
        with self._lock:
            if tap in self._taps:
                self._taps.remove(tap)

    def recent(self, seconds: float) -> bytes:
        n = max(0, int(seconds * RATE / BLOCK))
        return b"".join(list(self._ring)[-n:]) if n else b""

    def ambient_rms(self) -> Optional[float]:
        """The room's background level right now (None until ~0.5 s has been heard)."""
        levels = list(self._levels)
        if len(levels) < 25:
            return None
        levels.sort()
        return levels[int(len(levels) * _AMBIENT_PERCENTILE)]
