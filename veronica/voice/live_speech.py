"""
Speak a reply while it is still being written.

The model's reply streams in as small pieces of text (Streaming LLM V1). LiveSpeech
collects them, cuts off each finished sentence and hands it to the voice straight away,
so she starts talking after the first sentence instead of after the whole answer. The
voice makes sentence N+1 while sentence N plays (speech/tts.py speak_stream).

Only the user-visible reply text is spoken; markdown symbols are dropped. If what she
ends up saying doesn't cover the final reply (a failure message, a note about tools that
ran), the caller speaks that reply normally afterwards.
"""
from __future__ import annotations

import queue
import re
import threading
from typing import Iterator, Optional

_END = re.compile(r"(?<=[.!?])[\"')\]]*\s+|\n{1,}")
_MARKDOWN = re.compile(r"[*_`#>]+|^\s*[-•]\s+", re.M)
_FIRST_MAX = 70          # a long first sentence is cut at a comma so she starts sooner


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _MARKDOWN.sub("", text)).strip()


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


class LiveSpeech:
    def __init__(self, min_chars: int = 12) -> None:
        self._buf = ""
        self._q: "queue.Queue[Optional[str]]" = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._first = True
        self._pending = ""
        self.sent: list[str] = []
        self.min_chars = min_chars
        self.on_first_piece = None          # callback: start the speaker thread

    # -- feeding -------------------------------------------------------------------
    def on_event(self, name: str, payload: dict) -> None:
        """StreamController event sink."""
        if name == "response_delta":
            self.feed(payload.get("delta") or "")

    def feed(self, delta: str) -> None:
        with self._lock:
            if self._closed:
                return
            self._buf += delta
            while True:
                m = _END.search(self._buf)
                if not m:
                    break
                sentence, self._buf = self._buf[:m.start()].strip(), self._buf[m.end():]
                # Tiny sentences ("Sure.") ride along with the next one, except the very
                # first: that one should be heard as early as possible.
                self._pending = f"{self._pending} {sentence}".strip()
                if self._first or len(_clean(self._pending)) >= self.min_chars:
                    self._push(self._pending)
                    self._pending = ""
            if self._first and not self._pending and len(self._buf) > _FIRST_MAX:
                cut = self._buf.find(", ", 15)
                if 0 < cut < _FIRST_MAX:
                    self._push(self._buf[:cut + 1])
                    self._buf = self._buf[cut + 2:]

    def _push(self, text: str) -> None:
        piece = _clean(text)
        if not piece:
            return
        parts = [piece]
        if self._first and len(piece) > _FIRST_MAX:   # a long first sentence: say its first clause now
            cut = piece.find(", ", 15)
            if 0 < cut < len(piece) - 15:
                parts = [piece[:cut + 1], piece[cut + 2:]]
        for part in parts:
            self.sent.append(part)
            self._q.put(part)
        if self._first:
            self._first = False
            cb = self.on_first_piece
            if cb is not None:
                cb()

    def close(self) -> None:
        """The reply is complete: speak whatever is left and end the stream of pieces."""
        with self._lock:
            if self._closed:
                return
            rest, self._buf = f"{self._pending} {self._buf}".strip(), ""
            self._pending = ""
            if rest:
                self._push(rest)
            self._closed = True
            self._q.put(None)

    # -- consuming -----------------------------------------------------------------
    def pieces(self) -> Iterator[str]:
        while True:
            piece = self._q.get()
            if piece is None:
                return
            yield piece

    @property
    def started(self) -> bool:
        return bool(self.sent)

    def covers(self, reply: str) -> bool:
        """Did the streamed speech already say this reply?"""
        said, want = _norm(" ".join(self.sent)), _norm(_clean(reply or ""))
        return bool(want) and want in said
