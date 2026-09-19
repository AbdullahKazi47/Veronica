"""
StreamController: the ONE coordinator for LLM streaming. It owns how chunks are
consumed from a provider, sequenced, accumulated, and surfaced as events — and
nothing else.

Hard rules (Streaming LLM V1 spec):
  - It does NOT plan tasks, select tools, grant permissions, write memory, or modify
    the GUI. It turns provider chunks into ordered incremental events + one final,
    authoritative accumulated text.
  - Lifecycle: STREAM_STARTED -> STREAMING -> STREAM_COMPLETED | STREAM_CANCELLED |
    STREAM_FAILED. Exactly ONE terminal event per stream — never completed after a
    failure, never cancelled-then-completed.
  - Per-stream state only (stream_id-keyed), fully thread-safe, no global mutable
    stream state, no cross-request mixing.
  - Resource safety: the accumulator is capped (max_response_chars). Hitting the cap
    is an explicit terminal status (completed with truncated=True) — never silent
    truncation, never unbounded memory.
  - Timeouts: first-token / idle / overall. A timeout that fires before any output is
    a FAILED stream; the provider (not a stale reader) is the only one who can deliver
    chunks after that point.
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from veronica.llm.base import ChatMessage, LLMProvider, StreamChunk
from veronica.logging_setup import get_logger

logger = get_logger("llm.streaming")

# -- lifecycle vocabulary (controlled strings; the single source of truth) --------
STREAM_STARTED = "started"                    # stream opened, waiting for first token
STREAMING = "streaming"                       # at least one delta delivered
STREAM_COMPLETED = "completed"                # terminal: clean finish
STREAM_CANCELLED = "cancelled"                # terminal: explicitly cancelled
STREAM_FAILED = "failed"                      # terminal: provider/timeout/resource failure

_TERMINAL = frozenset({STREAM_COMPLETED, STREAM_CANCELLED, STREAM_FAILED})

# -- SSE event vocabulary (the exact signal names the bridge/GUI expect) ---------
EVT_STREAM_STARTED = "stream_started"
EVT_RESPONSE_DELTA = "response_delta"
EVT_STREAM_STATUS = "stream_status"
EVT_STREAM_COMPLETED = "stream_completed"
EVT_STREAM_CANCELLED = "stream_cancelled"
EVT_STREAM_ERROR = "stream_error"


@dataclass
class StreamingTimeouts:
    """Timeout policy for one stream. Safe, generous defaults; all disabled at 0."""
    first_token_s: float = 30.0      # nothing received within this -> FAILED timeout
    idle_s: float = 60.0             # nothing received between deltas -> FAILED timeout
    overall_s: float = 300.0         # hard ceiling on total stream duration


def _default_timeouts() -> StreamingTimeouts:
    return StreamingTimeouts()


@dataclass
class StreamResult:
    """The final, authoritative outcome of one stream. Returned by StreamController.run.

    ``final_text`` is the concatenation of every received delta, in receive order —
    this is what conversation memory, episodic memory, and the final reply use (Phase 7,
    19, 20). Partial output on a FAILED/CANCELLED stream is retained for diagnostics
    only; callers must never present it as a complete answer.
    """
    stream_id: str = ""
    request_id: str = ""
    status: str = STREAM_FAILED
    final_text: str = ""
    error: Optional[str] = None
    cancelled: bool = False
    truncated: bool = False
    finish_reason: Optional[str] = None          # "stop" | "length" | "max_length" | "error" | None
    char_count: int = 0
    chunks_received: int = 0
    tool_calls: list[dict] = field(default_factory=list)   # assembled tool calls (may be empty)
    first_chunk_at: Optional[float] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    routing: Optional[dict] = field(default_factory=dict, repr=False)

    def duration_s(self) -> float:
        if self.started_at is None or self.completed_at is None:
            return 0.0
        return round(max(0.0, self.completed_at - self.started_at), 3)

    def to_dict(self) -> dict:
        return {
            "stream_id": self.stream_id,
            "request_id": self.request_id,
            "status": self.status,
            "cancelled": self.cancelled,
            "truncated": self.truncated,
            "finish_reason": self.finish_reason,
            "char_count": self.char_count,
            "chunks_received": self.chunks_received,
            "duration_s": self.duration_s(),
            "model": self.model,
            "provider": self.provider,
        }


@dataclass
class _StreamState:
    """All mutable per-stream state. Guarded by ``lock``; nothing here is module-global."""
    stream_id: str
    request_id: str
    lock: threading.Lock
    status: str = STREAM_STARTED
    terminal: bool = False                    # exactly one terminal transition ever
    cancel_requested: bool = False
    accumulator: str = ""
    seq: int = 0
    chunks_received: int = 0
    truncated: bool = False
    finish_reason: Optional[str] = None
    error: Optional[str] = None
    first_chunk_at: Optional[float] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    timed_out: bool = False
    timed_out_reason: str = ""
    failed_requested: bool = False             # provider reported an error chunk → FAILED terminal
    cap: Optional[int] = None                  # max_response_chars for this run
    on_event: Optional[Callable[[str, dict], None]] = None
    pending_delta: str = ""                    # coalescing buffer (event-only, not final text)
    pending_seq: int = 0
    last_emit_at: float = 0.0


class StreamController:
    """Coordinate a streaming LLM turn: consume -> normalize -> sequence -> accumulate
    -> emit -> finalize. Instantiate at app-build time; drive one stream per request."""

    def __init__(self, timeouts: Optional[StreamingTimeouts] = None,
                 coalesce_ms: float = 60.0, max_buffer_chars: int = 1024) -> None:
        self._timeouts = timeouts if timeouts is not None else _default_timeouts()
        # Coalescing: buffer deltas and emit them as one event every coalesce_ms
        # (or sooner if max_buffer_chars is reached). NEVER changes the accumulated
        # final text — the accumulator concatenates every chunk exactly.
        self._coalesce_s = max(0.0, coalesce_ms) / 1000.0
        self._max_buffer_chars = max(1, max_buffer_chars)
        # stream_id -> _StreamState. Guarded so two requests can never share a stream.
        self._streams: dict[str, _StreamState] = {}
        self._registry_lock = threading.Lock()

    # -- lifecycle helpers (thread-safe) ---------------------------------------
    def _get(self, stream_id: str) -> Optional[_StreamState]:
        with self._registry_lock:
            return self._streams.get(stream_id)

    def _reset(self, state: _StreamState) -> None:
        """Re-arm a stream state for a fresh run (same id, new turn)."""
        with state.lock:
            state.status = STREAM_STARTED
            state.terminal = False
            state.cancel_requested = False
            state.accumulator = ""
            state.seq = 0
            state.chunks_received = 0
            state.truncated = False
            state.finish_reason = None
            state.error = None
            state.first_chunk_at = None
            state.started_at = None
            state.completed_at = None
            state.timed_out = False
            state.timed_out_reason = ""
            state.failed_requested = False
            state.cap = None
            state.on_event = None
            state.pending_delta = ""
            state.pending_seq = 0
            state.last_emit_at = 0.0

    def new_stream(self, request_id: str = "") -> str:
        """Create a fresh stream id. Auto-generates a request_id for correlation when
        the caller has none (e.g. a headless/CLI stream)."""
        stream_id = uuid.uuid4().hex[:12]
        request_id = request_id or uuid.uuid4().hex[:12]
        with self._registry_lock:
            self._streams[stream_id] = _StreamState(
                stream_id=stream_id, request_id=request_id,
                lock=threading.Lock(),
            )
        return stream_id

    def cancel(self, stream_id: str) -> bool:
        """Request cancellation of a running stream. Returns False when there is no
        such stream (or it already terminated). The worker notices the flag and
        stops forwarding chunks; the terminal CANCELLED event is emitted by the
        worker, not here."""
        state = self._get(stream_id)
        if state is None:
            return False
        with state.lock:
            if state.terminal:
                return False
            state.cancel_requested = True
            return True

    # -- main entry --------------------------------------------------------------
    def run(
        self,
        provider: LLMProvider,
        messages: list[ChatMessage],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.4,
        *,
        stream_id: Optional[str] = None,
        request_id: str = "",
        on_event: Optional[Callable[[str, dict], None]] = None,
        routing: Optional[dict] = None,
        max_response_chars: Optional[int] = None,
    ) -> StreamResult:
        """Consume one provider stream to exactly one terminal state and return the
        authoritative StreamResult. Synchronous: returns when the stream finalizes."""
        sid = stream_id or self.new_stream(request_id=request_id)
        request_id = request_id or sid
        if stream_id is None:
            # new_stream chose a different id above; keep the auto request_id aligned
            request_id = request_id or sid
        state = self._get(sid)
        if state is None:
            state = _StreamState(stream_id=sid, request_id=request_id, lock=threading.Lock())
            with self._registry_lock:
                self._streams[sid] = state
        self._reset(state)

        model = getattr(provider, "model", None)
        provider_name = getattr(provider, "name", "unknown")
        started = time.monotonic()
        with state.lock:
            state.started_at = started
            state.status = STREAM_STARTED
            state.cap = max_response_chars
            if state.cap is not None and state.cap <= 0:
                state.cap = None   # 0 / negative = uncapped (matches timeout "0=disabled" convention)
            state.on_event = on_event

        self._emit(on_event, EVT_STREAM_STARTED, {
            "request_id": state.request_id, "stream_id": sid,
            "model": model, "provider": provider_name,
            "routing": routing or {},
        })
        self._emit(on_event, EVT_STREAM_STATUS, {
            "request_id": state.request_id, "stream_id": sid,
            "status": STREAM_STARTED, "char_count": 0,
        })

        # -- reader thread: pull chunks from the provider into a bounded queue -----
        # so cancellation/idle timeouts work even when the provider blocks on I/O.
        chunk_q: "queue.Queue" = queue.Queue(maxsize=64)
        _SENTINEL = object()

        def _reader() -> None:
            try:
                gen = provider.chat_stream(messages, tools=tools, temperature=temperature)
                for chunk in gen:
                    with state.lock:
                        if state.cancel_requested or state.terminal:
                            return           # stop forwarding after cancel/finalize
                    _queue_put_blocking(chunk_q, state, chunk if isinstance(chunk, StreamChunk)
                                        else _normalize(chunk))
            except Exception as e:  # noqa: BLE001 — any provider bug is a controlled failure
                logger.exception("Provider stream raised (stream_id=%s)", sid)
                try:
                    chunk_q.put_nowait(StreamChunk(
                        text="", finish_reason="error", done=True,
                        metadata={"error": str(e)},
                    ))
                except queue.Full:
                    pass
            finally:
                try:
                    chunk_q.put_nowait(_SENTINEL)
                except queue.Full:
                    pass

        reader = threading.Thread(target=_reader, name=f"stream-{sid}", daemon=True)
        reader.start()
        timeout_cfg = self._timeouts

        last_activity = started
        result = StreamResult(
            stream_id=sid, request_id=state.request_id,
            model=model, provider=provider_name,
            started_at=started, routing=routing,
        )

        try:
            while True:
                # Cancellation / timeout checks run between queue reads so a stuck
                # provider cannot wedge the stream forever.
                with state.lock:
                    cancelled = state.cancel_requested
                    timed_out = state.timed_out

                if cancelled:
                    with state.lock:
                        state.status = STREAM_CANCELLED
                        state.cancelled = True
                    result.cancelled = True
                    self._finalize(state, result, on_event, EVT_STREAM_CANCELLED, {
                        "request_id": state.request_id, "stream_id": sid,
                        "char_count": result.char_count,
                    })
                    return result

                if timed_out:
                    msg = state.timed_out_reason or "stream timed out"
                    self._fail(state, result, on_event, msg)
                    return result

                now = time.monotonic()
                if timeout_cfg.overall_s > 0 and (now - started) > timeout_cfg.overall_s:
                    with state.lock:
                        state.timed_out = True
                        state.timed_out_reason = "overall stream timeout"
                    self._fail(state, result, on_event, "overall stream timeout exceeded")
                    return result

                if state.first_chunk_at is None:
                    if timeout_cfg.first_token_s > 0 and (now - started) > timeout_cfg.first_token_s:
                        with state.lock:
                            state.timed_out = True
                            state.timed_out_reason = "first-token timeout"
                        self._fail(state, result, on_event, "no output received before the first-token timeout")
                        return result
                elif timeout_cfg.idle_s > 0 and (now - last_activity) > timeout_cfg.idle_s:
                    with state.lock:
                        state.timed_out = True
                        state.timed_out_reason = "idle timeout"
                    self._fail(state, result, on_event, "stream went idle; no deltas received in time")
                    return result

                try:
                    item = chunk_q.get(timeout=0.2)
                except queue.Empty:
                    continue

                if item is _SENTINEL:
                    # Reader is done. It may have stopped forwarding because a cancel
                    # (or timeout) fired while the provider was mid-IO — re-check the
                    # flags BEFORE declaring completion, or a cancelled stream would
                    # silently finish as COMPLETED (spec: only the provider may end a
                    # stream, but the consumer MUST surface the requested terminal).
                    with state.lock:
                        cancelled_after = state.cancel_requested
                        timed_out_after = state.timed_out
                        timeout_reason_after = state.timed_out_reason
                    if cancelled_after:
                        with state.lock:
                            state.status = STREAM_CANCELLED
                            state.cancelled = True
                        result.cancelled = True
                        self._finalize(state, result, on_event, EVT_STREAM_CANCELLED, {
                            "request_id": state.request_id, "stream_id": sid,
                            "char_count": result.char_count,
                        })
                        return result
                    if timed_out_after:
                        self._fail(state, result, on_event, timeout_reason_after or "stream timed out")
                        return result
                    break
                chunk: StreamChunk = item

                # Monitor the queue drain end from the state itself: if a first chunk
                # never arrived before the generator finished, we still complete with
                # whatever text the provider produced (e.g. an error chunk).
                with state.lock:
                    if state.terminal:
                        break

                self._consume_chunk(state, result, chunk)
                if state.first_chunk_at is not None:
                    last_activity = time.monotonic()

                # Provider-reported error (raised exception or finish_reason="error"):
                # FAILED is this stream's terminal, never a natural completion.
                # Checked immediately so the SENTINEL / done-chunk can't mask it.
                with state.lock:
                    fail_requested = state.failed_requested
                if fail_requested:
                    self._fail(state, result, on_event, state.error or "stream failed")
                    return result

                # Explicit terminal from the provider (finish_reason or done=True),
                # or the resource cap firing (never silent truncation).
                if chunk.done or chunk.finish_reason or state.truncated:
                    break

            # -- flag re-check before the natural-completion block ------------------
            # A cancel (or timeout) that landed while the FINAL chunk was being consumed
            # must still win: the user asked to stop, so this must not silently finish
            # as COMPLETED (spec: exactly one terminal; CANCELLED after a cancel).
            with state.lock:
                cancelled_at_end = state.cancel_requested
                timed_out_at_end = state.timed_out
                timeout_reason_at_end = state.timed_out_reason
            if cancelled_at_end:
                with state.lock:
                    state.status = STREAM_CANCELLED
                    state.cancelled = True
                result.cancelled = True
                self._finalize(state, result, on_event, EVT_STREAM_CANCELLED, {
                    "request_id": state.request_id, "stream_id": sid,
                    "char_count": result.char_count,
                })
                return result
            if timed_out_at_end:
                self._fail(state, result, on_event, timeout_reason_at_end or "stream timed out")
                return result

            # -- natural completion -------------------------------------------------
            with state.lock:
                state.status = STREAM_COMPLETED
                state.finish_reason = state.finish_reason or "stop"
                state.completed_at = time.monotonic()
            result.final_text = self._snapshot_text(state)
            result.status = STREAM_COMPLETED
            result.finish_reason = state.finish_reason
            result.completed_at = state.completed_at
            result.char_count = result.final_text and len(result.final_text) or 0
            result.chunks_received = state.chunks_received
            self._finalize(state, result, on_event, EVT_STREAM_COMPLETED, {
                "request_id": state.request_id, "stream_id": sid,
                "final_text": result.final_text,
                "char_count": result.char_count,
                "finish_reason": result.finish_reason,
                "truncated": result.truncated,
                "duration_s": result.duration_s(),
                "model": model, "provider": provider_name,
                "routing": routing or {},
            })
            return result

        except Exception as e:  # noqa: BLE001 — controller must never raise outward
            logger.exception("Stream controller failed (stream_id=%s)", sid)
            self._fail(state, result, on_event, f"stream controller error: {e}")
            return result

    # -- chunk pipeline -----------------------------------------------------------
    def _consume_chunk(self, state: _StreamState, result: StreamResult, chunk: StreamChunk) -> None:
        """Normalize, sequence, accumulate (exact text), and coalesce deltas into
        RESPONSE_DELTA events. The accumulator concatenates EVERY chunk exactly; the
        events only batch the display cadence."""
        text = chunk.text if chunk and isinstance(chunk.text, str) else ""
        seq = state.seq
        state.seq += 1
        state.chunks_received += 1
        result.chunks_received = state.chunks_received      # keep the authoritative count in sync
        now = time.monotonic()
        if state.first_chunk_at is None:
            state.first_chunk_at = now
            result.first_chunk_at = now
            with state.lock:
                state.status = STREAMING

        # -- resource cap: explicit terminal, never silent truncation ---------------
        cap = state.cap
        if cap is not None and len(state.accumulator) + len(text) > cap:
            room = cap - len(state.accumulator)
            if room > 0:
                text = text[:room]
                state.accumulator += text
                state.pending_delta += text
            state.truncated = True
            result.truncated = True
            state.finish_reason = "max_length"
            self._flush_delta(state, force=True)
            return
        state.accumulator += text
        result.char_count = len(state.accumulator)
        tcs = (chunk.metadata or {}).get("tool_calls")
        if isinstance(tcs, list) and tcs:
            result.tool_calls = [dict(tc) for tc in tcs if isinstance(tc, dict)]
        if chunk.finish_reason:
            state.finish_reason = chunk.finish_reason
        if chunk.done and not state.finish_reason:
            state.finish_reason = "stop"
        # A provider-reported error (raised exceptions surface as finish_reason="error"
        # error-chunks from the reader; providers may also yield them directly) is a
        # FAILED stream — never a natural completion. The partial text stays diagnostics-only.
        if chunk.finish_reason == "error":
            state.error = ((chunk.metadata or {}).get("error")
                           if isinstance(chunk.metadata, dict) else None) \
                or "stream failed (provider error)"
            state.failed_requested = True

        # -- coalescing: emit one event per window (final text unaffected) ---------
        state.pending_delta += text
        state.pending_seq = seq
        window_elapsed = self._coalesce_s > 0 and (now - state.last_emit_at) >= self._coalesce_s
        if window_elapsed or len(state.pending_delta) >= self._max_buffer_chars:
            self._flush_delta(state, force=False)

    def _flush_delta(self, state: _StreamState, force: bool) -> None:
        if not state.pending_delta:
            return
        delta = state.pending_delta
        seq = state.pending_seq
        state.pending_delta = ""
        state.last_emit_at = time.monotonic()
        self._emit(state.on_event, EVT_RESPONSE_DELTA,
                   {"request_id": state.request_id, "stream_id": state.stream_id,
                    "delta": delta, "sequence": seq, "chunk_index": state.chunks_received - 1})

    def _snapshot_text(self, state: _StreamState) -> str:
        with state.lock:
            return state.accumulator

    # -- terminal handling ----------------------------------------------------------
    def _finalize(self, state: _StreamState, result: StreamResult, on_event, event_name: str, payload: dict) -> None:
        self._flush_delta(state, force=True)   # never strand the tail behind the terminal event
        with state.lock:
            if state.terminal:
                return                     # exactly one terminal event ever
            state.terminal = True
            state.completed_at = time.monotonic()
            result.completed_at = state.completed_at
            result.status = state.status
            result.final_text = state.accumulator
            result.char_count = len(state.accumulator)
            result.truncated = state.truncated or result.truncated
            result.finish_reason = state.finish_reason or result.finish_reason
            result.error = state.error
            result.chunks_received = state.chunks_received
        self._emit(on_event, event_name, payload)
        self._emit(on_event, EVT_STREAM_STATUS, {
            "request_id": state.request_id, "stream_id": state.stream_id,
            "status": state.status, "char_count": result.char_count,
        })
        logger.info("Stream %s ended as %s (chars=%d, chunks=%d, duration=%.2fs)",
                    state.stream_id, state.status, result.char_count,
                    result.chunks_received, result.duration_s())

    def _fail(self, state: _StreamState, result: StreamResult, on_event, error: str) -> None:
        self._flush_delta(state, force=True)   # keep partial output visible as it happened
        with state.lock:
            state.status = STREAM_FAILED
            state.error = error
            state.terminal = True
            state.completed_at = time.monotonic()
            result.completed_at = state.completed_at
            result.status = STREAM_FAILED
            result.error = error
            result.final_text = state.accumulator          # partial kept for diagnostics only
            result.char_count = len(state.accumulator)
            result.finish_reason = "error"
            result.chunks_received = state.chunks_received
        self._emit(on_event, EVT_STREAM_ERROR, {
            "request_id": state.request_id, "stream_id": state.stream_id,
            "error": error, "char_count": result.char_count,
        })
        self._emit(on_event, EVT_STREAM_STATUS, {
            "request_id": state.request_id, "stream_id": state.stream_id,
            "status": STREAM_FAILED, "char_count": result.char_count,
        })
        logger.warning("Stream %s failed: %s", state.stream_id, error)
        # Never emit stream_completed after a failure — terminal is FAILED exactly once.

    @staticmethod
    def _emit(on_event: Optional[Callable[[str, dict], None]], name: str, payload: dict) -> None:
        if on_event is None:
            return
        try:
            on_event(name, payload)
        except Exception:  # noqa: BLE001 — an event sink must never break the stream
            logger.exception("Stream event sink raised on %s", name)


def _queue_put_blocking(chunk_q, state: "_StreamState", chunk: StreamChunk) -> None:
    """Enqueue one chunk with TRUE backpressure: never drop a chunk (the final-text
    invariant depends on every chunk reaching the accumulator), never block forever.

    ``put_nowait`` on a full bounded queue would either raise (and we'd have to drop,
    corrupting final_text) or, with a blocking ``put``, risk a deadlock — the main loop
    cannot see the terminal done-chunk behind a full queue. So retry with a micro-sleep
    loop instead: the main loop drains promptly (its only terminal comes from reading a
    done-chunk or the SENTINEL, which the reader pushes only after every chunk), so the
    queue always makes room; the reader just slows to consumer speed. Re-checking the
    cancel flag each attempt lets a cancelled stream stop forwarding immediately."""
    while True:
        try:
            chunk_q.put_nowait(chunk)
            return
        except queue.Full:
            with state.lock:
                if state.cancel_requested or state.terminal:
                    return
            time.sleep(0.001)


def _normalize(obj: Any) -> StreamChunk:
    """Defensive: providers may return raw strings; normalize to StreamChunk."""
    if isinstance(obj, StreamChunk):
        return obj
    if isinstance(obj, str):
        return StreamChunk(text=obj)
    return StreamChunk(text=str(obj) if obj is not None else "")


def provider_supports_streaming(provider: LLMProvider) -> bool:
    """True when the provider can deliver progressive chunks (not just the single
    base-class fallback chunk). Mirrors CAP_STREAMING on the routing profile."""
    return bool(getattr(provider, "supports_streaming", False))


__all__ = [
    "StreamController", "StreamResult", "StreamingTimeouts", "StreamChunk",
    "STREAM_STARTED", "STREAMING", "STREAM_COMPLETED", "STREAM_CANCELLED", "STREAM_FAILED",
    "EVT_STREAM_STARTED", "EVT_RESPONSE_DELTA", "EVT_STREAM_STATUS",
    "EVT_STREAM_COMPLETED", "EVT_STREAM_CANCELLED", "EVT_STREAM_ERROR",
    "provider_supports_streaming",
]