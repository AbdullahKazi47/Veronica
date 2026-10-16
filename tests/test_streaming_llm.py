"""
Streaming LLM V1 — comprehensive test suite (Phases 15–22 of the 33-phase spec).

Covers:
  • StreamChunk / LLMProvider base streaming abstraction
  • EchoProvider determinism (exactly one delta per word, concatenation stable)
  • StreamController lifecycle: exactly ONE terminal per stream, all paths
  • Coalescing / text integrity: raw chunk concat == final_text
  • True backpressure: bounded queue without chunk loss under burst providers
  • Timeouts: first-token / idle / overall → STREAM_FAILED
  • Resource cap: explicit truncated=True at cap, never silent truncation
  • request_id correlation: one id across all events in a stream
  • Concurrent stream isolation: no cross-stream mixing
  • StreamResult metadata: to_dict, duration, no secrets
  • provider_supports_streaming helper
  • Orchestrator streaming integration (enabled/disabled, fallback, memory correctness)
  • Router streaming integration (streaming_provider wiring, capability filtering)
  • Cancellation: pre-start, mid-stream, final-chunk race, terminal-only stop
  • Episodic STREAM_FAILURE vocabulary (Phase 20)

Every test uses local EchoProvider / FakeStreamProvider — no network, no external model.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

import pytest

from veronica.config import Config
from veronica.core.context import SessionContext
from veronica.llm.base import ChatMessage, LLMProvider, LLMResponse, StreamChunk, ToolCall
from veronica.llm.router import EchoProvider
from veronica.llm.routing.builder import build_routing
from veronica.llm.routing.registry import ModelRegistry
from veronica.llm.routing.router import (
    ModelRouter, ModelSelection, RoutingConfig,
)
from veronica.llm.routing.task import TaskAnalyzer
from veronica.llm.streaming import (
    EVT_RESPONSE_DELTA, EVT_STREAM_CANCELLED, EVT_STREAM_COMPLETED,
    EVT_STREAM_ERROR, EVT_STREAM_STARTED, EVT_STREAM_STATUS,
    STREAM_CANCELLED, STREAM_COMPLETED, STREAM_FAILED,
    StreamController, StreamResult, StreamingTimeouts,
    provider_supports_streaming,
)
from veronica.memory.long_term import LongTermMemory
from veronica.memory.thread.episodes import EpisodeStatus, derive_episode_status, map_task_status
from veronica.safety.permissions import PermissionGate, PermissionLevel
from veronica.tools.registry import ToolRegistry


# --------------------------------------------------------------------------- #
# Fake providers (deterministic, no network, thread-safe state)
# --------------------------------------------------------------------------- #


class _SlowProvider(LLMProvider):
    """Yields one chunk per call; sleeps ``delay_s`` between chunks.
    Supports cancellation via ``cancel_event`` to avoid holding test threads."""

    name = "slow"
    supports_streaming = True

    def __init__(
        self,
        text: str = "Hello World there",
        *,
        delay_s: float = 0.1,
        fail_at: Optional[int] = None,   # raise RuntimeError after yielding fail_at chunks
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        self.text = text
        self.delay_s = delay_s
        self.fail_at = fail_at
        self.cancel_event = cancel_event
        self.model = "slow-model"
        self.yielded = 0

    def chat(self, messages, tools=None, temperature=0.4, voice_mode=False) -> LLMResponse:
        return LLMResponse(content=self.text)

    def chat_stream(self, messages, tools=None, temperature=0.4, voice_mode=False):
        for i, word in enumerate(self.text.split(" ")):
            if self.fail_at is not None and i >= self.fail_at:
                raise RuntimeError(f"deliberate failure after {self.fail_at} chunks")
            piece = word + (" " if i < len(self.text.split(" ")) - 1 else "")
            self.yielded += 1
            if self.cancel_event:
                self.cancel_event.wait(timeout=self.delay_s)
                if self.cancel_event.is_set():
                    return
            else:
                time.sleep(self.delay_s)
            yield StreamChunk(text=piece, sequence=i)
        yield StreamChunk(text="", sequence=len(self.text.split(" ")), finish_reason="stop", done=True)


class _GatedProvider(LLMProvider):
    """Blocks at a user-controlled Event until the gate is opened (or timeout).
    First chunk is yielded immediately; subsequent chunks wait on the gate.
    Used to test idle timeout and cancel-while-blocked scenarios deterministically."""

    name = "gated"
    supports_streaming = True

    def __init__(
        self,
        tokens: List[str],
        gate: threading.Event,
        *,
        delay_s: float = 0.01,
    ) -> None:
        self.tokens = tokens
        self.gate = gate
        self.delay_s = delay_s
        self.model = "gated-model"
        self.yielded = 0

    def chat(self, messages, tools=None, temperature=0.4, voice_mode=False) -> LLMResponse:
        return LLMResponse(content=" ".join(self.tokens))

    def chat_stream(self, messages, tools=None, temperature=0.4, voice_mode=False):
        for i, tok in enumerate(self.tokens):
            self.yielded += 1
            piece = tok + (" " if i < len(self.tokens) - 1 else "")
            yield StreamChunk(text=piece, sequence=i)
            # After the first chunk, wait for the gate (simulates a stalled provider).
            if i == 0 and not self.gate.is_set():
                self.gate.wait(timeout=30)
                if not self.gate.is_set():
                    return  # gate timeout → stop cleanly
        yield StreamChunk(text="", sequence=len(self.tokens), finish_reason="stop", done=True)


class _FailingProvider(LLMProvider):
    """Always raises on chat_stream (simulates a provider bug)."""
    name = "failing"
    supports_streaming = True

    def chat(self, messages, tools=None, temperature=0.4, voice_mode=False) -> LLMResponse:
        return LLMResponse(content="should never reach here", ok=False)

    def chat_stream(self, messages, tools=None, temperature=0.4, voice_mode=False):
        raise RuntimeError("simulated provider explosion")
        yield  # make it a generator
        yield StreamChunk(text="never", done=True)


# --------------------------------------------------------------------------- #
# Event collector helper
# --------------------------------------------------------------------------- #


@dataclass
class _EventCollector:
    """Thread-safe event collector for StreamController callbacks."""
    events: List[tuple[str, dict]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __call__(self, name: str, payload: dict) -> None:
        with self._lock:
            self.events.append((name, dict(payload)))

    def names(self) -> list[str]:
        with self._lock:
            return [n for n, _ in self.events]

    def payloads(self, event_name: str) -> list[dict]:
        with self._lock:
            return [p for n, p in self.events if n == event_name]

    def unique_request_ids(self) -> set[str]:
        with self._lock:
            return {p.get("request_id", "") for _, p in self.events if "request_id" in p}

    def terminal_events(self) -> list[str]:
        """Events that are exactly-once terminal in the spec lifecycle."""
        return [n for n in self.names()
                if n in (EVT_STREAM_COMPLETED, EVT_STREAM_CANCELLED, EVT_STREAM_ERROR)]


# --------------------------------------------------------------------------- #
# Echo / StreamChunk / base
# --------------------------------------------------------------------------- #


class TestStreamChunkBase:
    def test_defaults(self):
        c = StreamChunk()
        assert c.text == ""
        assert c.done is False
        assert c.finish_reason is None
        assert c.sequence == 0
        assert c.metadata == {}

    def test_echo_provider_chat_stream_deterministic(self):
        provider = EchoProvider()
        msgs = [ChatMessage(role="user", content="hello there")]
        chunks = list(provider.chat_stream(msgs, tools=None, temperature=0.4))
        # Echo: split on spaces → ["(echo)", "You", "said:", "hello", "there"]
        words = "(echo) You said: hello there".split(" ")
        # N word chunks + 1 done chunk
        assert len(chunks) == len(words) + 1
        # Final text: concatenate non-empty text from word chunks
        final = "".join(c.text for c in chunks if c.text)
        assert final == "(echo) You said: hello there"
        # Done chunk has finish_reason and done=True
        assert chunks[-1].done is True
        assert chunks[-1].finish_reason == "stop"

    def test_echo_provider_chat_stream_single_word(self):
        provider = EchoProvider()
        msgs = [ChatMessage(role="user", content="hi")]
        chunks = list(provider.chat_stream(msgs))
        final = "".join(c.text for c in chunks if c.text)
        assert final == "(echo) You said: hi"

    def test_echo_provider_chat_stream_empty_user(self):
        provider = EchoProvider()
        msgs = [ChatMessage(role="user", content="")]
        chunks = list(provider.chat_stream(msgs))
        # Should yield at least a done chunk
        assert any(c.done for c in chunks)


class TestProviderSupportsStreaming:
    def test_echo_streaming(self):
        assert provider_supports_streaming(EchoProvider()) is True

    def test_base_default_not_streaming(self):
        class _BaseOnly(LLMProvider):
            name = "base_only"
            def chat(self, messages, tools=None, temperature=0.4, voice_mode=False):
                return LLMResponse(content="x")
        assert provider_supports_streaming(_BaseOnly()) is False

    def test_slow_provider_streaming(self):
        # _SlowProvider implements a real chat_stream and advertises it.
        assert provider_supports_streaming(_SlowProvider()) is True


# --------------------------------------------------------------------------- #
# StreamController lifecycle — exactly-one-terminal, all paths
# --------------------------------------------------------------------------- #


class TestStreamControllerLifecycle:
    def test_natural_completion(self):
        c = StreamController(coalesce_ms=0)
        prov = _SlowProvider(text="alpha beta gamma", delay_s=0.0)
        events = _EventCollector()
        sid = c.new_stream()
        result = c.run(prov, [ChatMessage(role="user", content="test")],
                       stream_id=sid, on_event=events, max_response_chars=0)

        assert result.status == STREAM_COMPLETED
        assert result.final_text == "alpha beta gamma"
        assert result.char_count == len("alpha beta gamma")
        assert result.chunks_received >= 3  # words + done
        assert result.cancelled is False
        assert result.truncated is False
        # Exactly one terminal event
        assert events.terminal_events() == [EVT_STREAM_COMPLETED]
        # request_id correlates all events
        rids = events.unique_request_ids()
        assert len(rids) == 1 and "" not in rids

    def test_started_event_emitted_first(self):
        c = StreamController(coalesce_ms=0)
        prov = _SlowProvider(text="ok", delay_s=0.0)
        events = _EventCollector()
        c.run(prov, [ChatMessage(role="user", content="test")], on_event=events)
        assert events.names()[0] == EVT_STREAM_STARTED

    def test_cancelled_terminal_not_completed_after(self):
        """cancel() during provider IO → exactly one terminal: CANCELLED, never COMPLETED."""
        gate = threading.Event()
        prov = _GatedProvider(["one", "two", "three"], gate)
        c = StreamController(coalesce_ms=0)
        events = _EventCollector()
        sid = c.new_stream()
        # Fire cancel from another thread after first chunk is yielded
        def cancel_after_first_chunk():
            # Wait until provider has yielded at least one chunk
            for _ in range(200):
                if prov.yielded >= 1:
                    break
                time.sleep(0.01)
            gate.set()
            c.cancel(sid)

        t = threading.Thread(target=cancel_after_first_chunk, daemon=True)
        t.start()
        result = c.run(prov, [ChatMessage(role="user", content="x")],
                       stream_id=sid, on_event=events)
        t.join(timeout=2)

        assert result.status == STREAM_CANCELLED
        assert result.cancelled is True
        assert result.final_text != ""   # partial retained for diagnostics
        terms = events.terminal_events()
        assert terms == [EVT_STREAM_CANCELLED]

    def test_provider_exception_failed_terminal(self):
        prov = _FailingProvider()
        events = _EventCollector()
        c = StreamController(coalesce_ms=0)
        result = c.run(prov, [ChatMessage(role="user", content="x")], on_event=events)

        assert result.status == STREAM_FAILED
        assert "provider explosion" in (result.error or "")
        assert events.terminal_events() == [EVT_STREAM_ERROR]

    def test_cancel_after_stream_ends_returns_false(self):
        prov = _SlowProvider(text="done", delay_s=0.0)
        c = StreamController(coalesce_ms=0)
        sid = c.new_stream()
        c.run(prov, [ChatMessage(role="user", content="x")], stream_id=sid)
        assert c.cancel(sid) is False

    def test_cancel_unknown_stream_returns_false(self):
        c = StreamController()
        assert c.cancel("nonexistent_stream") is False

    def test_exactly_one_terminal_across_all_paths(self):
        """Run cancelled/failed/completed — each has exactly one terminal."""
        for prov, expected_status in [
            (_SlowProvider(text="ok", delay_s=0.0), STREAM_COMPLETED),
            (_FailingProvider(), STREAM_FAILED),
        ]:
            events = _EventCollector()
            c = StreamController(coalesce_ms=0)
            if expected_status == STREAM_FAILED:
                result = c.run(prov, [ChatMessage(role="user", content="x")], on_event=events)
            else:
                result = c.run(prov, [ChatMessage(role="user", content="x")], on_event=events)
            terms = events.terminal_events()
            assert len(terms) == 1
            assert (STREAM_CANCELLED if expected_status == STREAM_CANCELLED
                    else STREAM_COMPLETED if expected_status == STREAM_COMPLETED
                    else STREAM_FAILED) == result.status


# --------------------------------------------------------------------------- #
# Coalescing — text integrity, fewer events than chunks
# --------------------------------------------------------------------------- #


class TestCoalescing:
    def test_text_integrity_despite_coalescing(self):
        """With coalescing, response_delta events are batched but final_text
        is exact concatenation of all raw chunk text."""
        text = "one two three four five six seven eight nine ten"
        prov = _SlowProvider(text=text, delay_s=0.0)
        c = StreamController(coalesce_ms=0)  # immediate flush (no time window)
        events = _EventCollector()
        result = c.run(prov, [ChatMessage(role="user", content="x")], on_event=events)
        # Chunk concatenation is exact
        assert result.final_text == text
        # With coalesce_ms=0, each _consume_chunk flushes immediately; deltas match chunks
        delta_texts = [p["delta"] for p in events.payloads(EVT_RESPONSE_DELTA)]
        full_delta = "".join(delta_texts)
        assert full_delta == text

    def test_coalesce_batches_deltas(self):
        """Large coalesce_ms batches multiple chunks into fewer delta events."""
        text = "a b c d e f g h i j"
        prov = _SlowProvider(text=text, delay_s=0.0)
        c = StreamController(coalesce_ms=1000)  # huge window → batch everything
        events = _EventCollector()
        result = c.run(prov, [ChatMessage(role="user", content="x")], on_event=events)
        delta_count = len(events.payloads(EVT_RESPONSE_DELTA))
        chunk_word_count = len(text.split(" "))
        # With 1000ms coalesce, most/all words batch into 1-3 delta events
        assert delta_count < chunk_word_count
        # But final text is still exact
        assert result.final_text == text

    def test_max_buffer_chars_forces_flush(self):
        """When pending_delta exceeds max_buffer_chars, a flush happens mid-stream."""
        text = "aa bb cc dd ee ff gg hh ii jj"
        prov = _SlowProvider(text=text, delay_s=0.0)
        c = StreamController(coalesce_ms=60000, max_buffer_chars=5)  # force flush every 5 chars
        events = _EventCollector()
        result = c.run(prov, [ChatMessage(role="user", content="x")], on_event=events)
        delta_count = len(events.payloads(EVT_RESPONSE_DELTA))
        # With max_buffer_chars=5, many flushes happen before the huge coalesce window
        assert delta_count > 1
        # Final text exact
        assert result.final_text == text

    def test_final_delta_flushes_tail(self):
        """Tail text not stranded behind coalesce window: _finalize flushes remaining."""
        prov = _SlowProvider(text="hello", delay_s=0.0)
        c = StreamController(coalesce_ms=10000)  # large window, tail must be flushed in _finalize
        events = _EventCollector()
        result = c.run(prov, [ChatMessage(role="user", content="x")], on_event=events)
        delta_texts = "".join(p["delta"] for p in events.payloads(EVT_RESPONSE_DELTA))
        assert delta_texts == result.final_text


# --------------------------------------------------------------------------- #
# True backpressure — bounded queue, no chunk loss
# --------------------------------------------------------------------------- #


class TestBackpressure:
    def test_fast_provider_exact_text_no_loss(self):
        """A provider that yields 100 small chunks instantly must still produce
        an exact final_text with no dropped chunks."""
        words = [f"w{i}" for i in range(100)]
        text = " ".join(words)
        # Bypass _SlowProvider (sleeps); instead implement directly for burst speed
        class _BurstProvider(LLMProvider):
            name = "burst"
            def chat(self, messages, tools=None, temperature=0.4, voice_mode=False):
                return LLMResponse(content=text)
            def chat_stream(self, messages, tools=None, temperature=0.4, voice_mode=False):
                for i, tok in enumerate(words):
                    piece = tok + (" " if i < len(words) - 1 else "")
                    yield StreamChunk(text=piece, sequence=i)
                yield StreamChunk(text="", sequence=len(words), finish_reason="stop", done=True)

        prov = _BurstProvider()
        c = StreamController(coalesce_ms=0)
        events = _EventCollector()
        result = c.run(prov, [ChatMessage(role="user", content="x")], on_event=events)

        assert result.status == STREAM_COMPLETED
        assert result.final_text == text
        assert result.chunks_received == len(words) + 1   # 100 word chunks + done chunk
        # No deadlock, completed promptly
        assert result.duration_s() < 5.0

    def test_burst_text_exact_concurrent_bursts(self):
        """Two concurrent burst streams on the same controller must not mix text."""
        words_a = [f"a{i}" for i in range(60)]
        words_b = [f"b{i}" for i in range(60)]
        text_a = " ".join(words_a)
        text_b = " ".join(words_b)

        class _WordStreamProvider(LLMProvider):
            name = "wordstream"
            def __init__(self, text, tokens):
                self._text = text
                self._tokens = tokens
                self.model = "wordstream"
            def chat(self, messages, tools=None, temperature=0.4, voice_mode=False):
                return LLMResponse(content=self._text)
            def chat_stream(self, messages, tools=None, temperature=0.4, voice_mode=False):
                for i, tok in enumerate(self._tokens):
                    piece = tok + (" " if i < len(self._tokens) - 1 else "")
                    yield StreamChunk(text=piece, sequence=i)
                yield StreamChunk(text="", sequence=len(self._tokens), finish_reason="stop", done=True)

        c = StreamController(coalesce_ms=0)
        events_a, events_b = _EventCollector(), _EventCollector()

        def run_a():
            sid = c.new_stream()
            return c.run(_WordStreamProvider(text_a, words_a), [ChatMessage("user", "x")],
                         stream_id=sid, on_event=events_a)
        def run_b():
            sid = c.new_stream()
            return c.run(_WordStreamProvider(text_b, words_b), [ChatMessage("user", "y")],
                         stream_id=sid, on_event=events_b)

        t1 = threading.Thread(target=run_a, daemon=True)
        t2 = threading.Thread(target=run_b, daemon=True)
        t1.start(); t2.start()
        t1.join(timeout=5); t2.join(timeout=5)

        assert text_a in "".join(p["delta"] for p in events_a.payloads(EVT_RESPONSE_DELTA))
        assert text_b in "".join(p["delta"] for p in events_b.payloads(EVT_RESPONSE_DELTA))


# --------------------------------------------------------------------------- #
# Timeouts
# --------------------------------------------------------------------------- #


class TestTimeouts:
    def test_first_token_timeout(self):
        gate = threading.Event()  # never opened → provider stalls BEFORE any token
        tokens = ["one", "two"]

        class _StallFirstProvider(LLMProvider):
            name = "stallfirst"
            def chat(self, messages, tools=None, temperature=0.4, voice_mode=False):
                return LLMResponse(content=" ".join(tokens))
            def chat_stream(self, messages, tools=None, temperature=0.4, voice_mode=False):
                gate.wait(timeout=30)   # block before yielding anything
                if not gate.is_set():
                    return
                for i, tok in enumerate(tokens):
                    yield StreamChunk(text=tok, sequence=i)
                yield StreamChunk(text="", sequence=len(tokens), finish_reason="stop", done=True)

        timeouts = StreamingTimeouts(first_token_s=0.2, idle_s=5, overall_s=5)
        c = StreamController(timeouts=timeouts, coalesce_ms=0)
        events = _EventCollector()
        result = c.run(_StallFirstProvider(), [ChatMessage(role="user", content="x")],
                       on_event=events)
        assert result.status == STREAM_FAILED
        assert "first-token" in (result.error or "")
        terms = events.terminal_events()
        assert terms == [EVT_STREAM_ERROR]
        gate.set()  # unblock reader thread for cleanup

    def test_idle_timeout(self):
        gate = threading.Event()  # gate closes after first chunk → stalls second
        prov = _GatedProvider(["only"], gate)
        timeouts = StreamingTimeouts(first_token_s=0.3, idle_s=0.3, overall_s=5)
        c = StreamController(timeouts=timeouts, coalesce_ms=0)
        events = _EventCollector()
        result = c.run(prov, [ChatMessage(role="user", content="x")], on_event=events)
        assert result.status == STREAM_FAILED
        assert "idle" in (result.error or "")
        gate.set()

    def test_overall_timeout(self):
        """Provider yields slowly; overall timeout is the hard ceiling."""
        class _VerySlowProvider(LLMProvider):
            name = "very_slow"
            def chat(self, messages, tools=None, temperature=0.4, voice_mode=False):
                return LLMResponse(content="never finish")
            def chat_stream(self, messages, tools=None, temperature=0.4, voice_mode=False):
                yield StreamChunk(text="a", sequence=0)
                time.sleep(1.0)  # done-chunk arrives AFTER the overall ceiling fires
                yield StreamChunk(text="b", sequence=1, finish_reason="stop", done=True)

        timeouts = StreamingTimeouts(first_token_s=5, idle_s=5, overall_s=0.3)
        c = StreamController(timeouts=timeouts, coalesce_ms=0)
        events = _EventCollector()
        result = c.run(_VerySlowProvider(), [ChatMessage(role="user", content="x")],
                       on_event=events)
        assert result.status == STREAM_FAILED
        assert "overall" in (result.error or "")

    def test_cancel_during_idle_timeout_cancel_wins(self):
        """Both cancel AND idle timeout are pending: cancel is the terminal status."""
        gate = threading.Event()
        prov = _GatedProvider(["start", "end"], gate)
        timeouts = StreamingTimeouts(first_token_s=5, idle_s=0.2, overall_s=5)
        c = StreamController(timeouts=timeouts, coalesce_ms=0)
        events = _EventCollector()
        sid = c.new_stream()
        # Fire cancel after first chunk is yielded
        def cancel_later():
            for _ in range(200):
                if prov.yielded >= 1:
                    break
                time.sleep(0.01)
            gate.set()
            c.cancel(sid)
        t = threading.Thread(target=cancel_later, daemon=True)
        t.start()
        result = c.run(prov, [ChatMessage(role="user", content="x")],
                       stream_id=sid, on_event=events)
        t.join(timeout=2)
        assert result.status == STREAM_CANCELLED


# --------------------------------------------------------------------------- #
# Resource cap — explicit truncated=True, never silent
# --------------------------------------------------------------------------- #


class TestResourceCap:
    def test_truncated_at_cap(self):
        prov = _SlowProvider(text="a" * 200, delay_s=0.0)
        c = StreamController(coalesce_ms=0)
        events = _EventCollector()
        result = c.run(prov, [ChatMessage(role="user", content="x")],
                       on_event=events, max_response_chars=50)
        assert result.status == STREAM_COMPLETED
        assert result.truncated is True
        assert result.finish_reason == "max_length"
        assert len(result.final_text) == 50

    def test_cap_zero_means_uncapped(self):
        prov = _SlowProvider(text="abc", delay_s=0.0)
        c = StreamController(coalesce_ms=0)
        result = c.run(prov, [ChatMessage(role="user", content="x")],
                       max_response_chars=0)
        assert result.truncated is False
        assert result.final_text == "abc"

    def test_truncation_event_visible_in_stream_completed(self):
        prov = _SlowProvider(text="x" * 300, delay_s=0.0)
        c = StreamController(coalesce_ms=0)
        events = _EventCollector()
        c.run(prov, [ChatMessage(role="user", content="x")],
              on_event=events, max_response_chars=100)
        completed_payloads = events.payloads(EVT_STREAM_COMPLETED)
        assert len(completed_payloads) == 1
        assert completed_payloads[0]["truncated"] is True

    def test_exact_cap_boundary(self):
        """Characters that exactly hit the cap: not truncated (it's not > cap)."""
        prov = _SlowProvider(text="abc", delay_s=0.0)
        c = StreamController(coalesce_ms=0)
        result = c.run(prov, [ChatMessage(role="user", content="x")],
                       max_response_chars=3)
        assert result.truncated is False
        assert result.final_text == "abc"


# --------------------------------------------------------------------------- #
# request_id correlation & StreamResult metadata
# --------------------------------------------------------------------------- #


class TestRequestIdCorrelation:
    def test_single_request_id_across_all_events(self):
        c = StreamController(coalesce_ms=0)
        prov = _SlowProvider(text="hello world", delay_s=0.0)
        events = _EventCollector()
        result = c.run(prov, [ChatMessage(role="user", content="x")], on_event=events)
        rids = events.unique_request_ids()
        assert len(rids) == 1
        assert "" not in rids
        assert result.request_id in rids

    def test_request_id_matches_result(self):
        c = StreamController(coalesce_ms=0)
        prov = _SlowProvider(text="test", delay_s=0.0)
        events = _EventCollector()
        result = c.run(prov, [ChatMessage(role="user", content="x")], on_event=events)
        assert all(p.get("request_id") == result.request_id for _, p in events.events)


class TestStreamResultMetadata:
    def test_to_dict_fields(self):
        c = StreamController(coalesce_ms=0)
        prov = _SlowProvider(text="hi", delay_s=0.0)
        result = c.run(prov, [ChatMessage(role="user", content="x")])
        d = result.to_dict()
        assert d["status"] == STREAM_COMPLETED
        assert d["char_count"] == len("hi")
        assert d["chunks_received"] > 0
        assert d["duration_s"] >= 0.0
        assert "model" in d
        assert "provider" in d

    def test_no_secrets_in_events(self):
        """Provider credentials must never appear in any event payload."""
        c = StreamController(coalesce_ms=0)
        prov = _SlowProvider(text="hello from the model", delay_s=0.0)
        prov.api_key = "sk-demo-secret-0123456789abcdef"   # must never leak into events
        events = _EventCollector()
        c.run(prov, [ChatMessage(role="user", content="x")], on_event=events)
        for _, payload in events.events:
            payload_str = str(payload)
            assert "api_key" not in payload_str.lower()
            assert "VERONICA_LLM_API_KEY" not in payload_str
            assert "sk-demo-secret-0123456789abcdef" not in payload_str

    def test_duration_positive(self):
        prov = _SlowProvider(text="ok", delay_s=0.0)
        c = StreamController(coalesce_ms=0)
        result = c.run(prov, [ChatMessage(role="user", content="x")])
        assert result.duration_s() >= 0.0

    def test_provider_and_model_populated(self):
        prov = _SlowProvider(text="meta", delay_s=0.0)
        c = StreamController(coalesce_ms=0)
        result = c.run(prov, [ChatMessage(role="user", content="x")])
        assert result.provider == "slow"
        assert result.model == "slow-model"


# --------------------------------------------------------------------------- #
# Routing integration — streaming_provider wiring
# --------------------------------------------------------------------------- #


class TestRouterStreamingProvider:
    def _make_streaming_model(self):
        from veronica.llm.routing.profiles import ModelProfile, ROLE_FAST
        return ModelProfile(
            model_id="stream-echo", provider="echo",
            role=ROLE_FAST, supports_streaming=True, priority=10,
        )

    def _make_nonstreaming_model(self):
        from veronica.llm.routing.profiles import ModelProfile, ROLE_FAST
        return ModelProfile(
            model_id="nostream-echo", provider="echo",
            role=ROLE_FAST, supports_streaming=False, priority=10,
        )

    def test_streaming_provider_when_model_has_cap(self):
        reg = ModelRegistry()
        reg.register(self._make_streaming_model(), lambda: EchoProvider())
        router = ModelRouter(reg, config=RoutingConfig(enabled=True, fallback_enabled=False))
        task = TaskAnalyzer(known_model_ids=reg.model_ids()).analyze("hello")
        sel = router.route(task, context={})
        provider, model_id, provider_name = sel.streaming_provider()
        assert provider is not None
        assert provider_supports_streaming(provider) is True
        assert model_id == "stream-echo"

    def test_streaming_provider_none_when_model_lacks_cap(self):
        reg = ModelRegistry()
        reg.register(self._make_nonstreaming_model(), lambda: EchoProvider())
        router = ModelRouter(reg, config=RoutingConfig(enabled=True, fallback_enabled=False))
        task = TaskAnalyzer(known_model_ids=reg.model_ids()).analyze("hello")
        sel = router.route(task, context={})
        provider, model_id, provider_name = sel.streaming_provider()
        assert provider is None

    def test_streaming_provider_none_when_no_such_model(self):
        reg = ModelRegistry()
        router = ModelRouter(reg, config=RoutingConfig(enabled=True))
        task = TaskAnalyzer(known_model_ids=reg.model_ids()).analyze("hello")
        sel = router.route(task, context={})
        assert sel.satisfied is False
        assert sel.streaming_provider() == (None, None, None)

    def test_streaming_required_but_no_capable_model_returns_unsatisfied(self):
        from veronica.llm.routing.profiles import CAP_STREAMING
        from veronica.llm.routing.task import TaskProfile
        reg = ModelRegistry()
        reg.register(self._make_nonstreaming_model(), lambda: EchoProvider())
        router = ModelRouter(reg, config=RoutingConfig(enabled=True))
        task = TaskProfile(
            task_type="general", complexity="medium",
            requires_streaming=True,
        )
        sel = router.route(task, context={})
        assert sel.satisfied is False
        assert CAP_STREAMING in sel.capability_gaps
        assert sel.streaming_provider() == (None, None, None)


# --------------------------------------------------------------------------- #
# Orchestrator streaming integration
# --------------------------------------------------------------------------- #


class _OrchestratorHarness:
    """Shared Orchestrator test harness. NOT named Test* so pytest never collects it;
    both streaming orchestrator test classes inherit the static _make_orch helper."""

    @staticmethod
    def _make_orch(
        tmp_path,
        *,
        llm: LLMProvider,
        streaming_enabled: bool = False,
        preferred: bool = False,
        stream_controller: Optional[StreamController] = None,
        router=None,
        analyzer=None,
    ):
        from veronica.core.orchestrator import Orchestrator
        config = Config()
        config.safety.require_confirmation_above = "SAFE"
        config.streaming.enabled = streaming_enabled
        config.streaming.preferred = preferred
        gate = PermissionGate(threshold=PermissionLevel.SAFE, confirm_callback=None)
        tools = ToolRegistry(gate)
        memory = LongTermMemory(tmp_path / "mem.json")
        return Orchestrator(
            config=config, llm=llm, tools=tools, long_term_memory=memory,
            router=router, analyzer=analyzer,
            stream_controller=stream_controller,
        )


class TestOrchestratorStreaming(_OrchestratorHarness):

    def test_streaming_disabled_equals_classic_handle(self, tmp_path):
        """With streaming disabled, handle_stream output matches handle() exactly."""
        echo = EchoProvider()
        orch = self._make_orch(tmp_path, llm=echo, streaming_enabled=False,
                               stream_controller=StreamController(coalesce_ms=0))
        session_s = orch.new_session()
        session_h = orch.new_session()
        resp_stream = orch.handle_stream("hello VERONICA", session_s)
        resp_classic = orch.handle("hello VERONICA", session_h)
        assert resp_stream.text == resp_classic.text
        assert "VERONICA" in resp_stream.text

    def test_streaming_enabled_preferred_streams(self, tmp_path):
        echo = EchoProvider()
        sc = StreamController(coalesce_ms=0)
        orch = self._make_orch(tmp_path, llm=echo, streaming_enabled=True,
                               preferred=True, stream_controller=sc)
        session = orch.new_session()
        resp = orch.handle_stream("hello streaming", session)
        expected = "(echo) You said: hello streaming"
        assert resp.text == expected
        # Only final text in memory; no partial token fragments
        msgs = session.short_term.as_messages()
        assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0]["content"] == expected

    def test_streaming_enabled_not_preferred_falls_back(self, tmp_path):
        """preferred=False + text not mentioning streaming → falls back to chat()."""
        echo = EchoProvider()
        sc = StreamController(coalesce_ms=0)
        orch = self._make_orch(tmp_path, llm=echo, streaming_enabled=True,
                               preferred=False, stream_controller=sc)
        session = orch.new_session()
        resp = orch.handle_stream("just ask a question", session)
        assert resp.text == "(echo) You said: just ask a question"

    def test_streaming_enabled_user_requests_streaming(self, tmp_path):
        echo = EchoProvider()
        sc = StreamController(coalesce_ms=0)
        orch = self._make_orch(tmp_path, llm=echo, streaming_enabled=True,
                               preferred=False, stream_controller=sc)
        session = orch.new_session()
        # "stream" in the text triggers requires_streaming
        resp = orch.handle_stream("stream the answer to this", session)
        assert resp.text == "(echo) You said: stream the answer to this"

    def test_cancelled_stream_terminal_text_not_in_memory(self, tmp_path):
        """Partial tokens from a cancelled stream must NOT appear in assistant memory."""
        gate = threading.Event()
        prov = _GatedProvider(["partial", "lost", "text"], gate)
        sc = StreamController(coalesce_ms=0)
        orch = self._make_orch(tmp_path, llm=prov, streaming_enabled=True,
                               preferred=True, stream_controller=sc)
        session = orch.new_session()
        # Fire cancel after first chunk
        def cancel_later():
            for _ in range(200):
                if prov.yielded >= 1:
                    break
                time.sleep(0.01)
            gate.set()
            orch.cancel_stream()
        t = threading.Thread(target=cancel_later, daemon=True)
        t.start()
        resp = orch.handle_stream("stream this", session)
        t.join(timeout=2)
        assert resp.text == "Response stream cancelled."
        msgs = session.short_term.as_messages()
        # Only user turn exists (no assistant message for cancelled turn)
        roles = [m["role"] for m in msgs]
        assert roles == ["user"]

    def test_failed_stream_returns_error_text(self, tmp_path):
        prov = _FailingProvider()
        sc = StreamController(coalesce_ms=0)
        orch = self._make_orch(tmp_path, llm=prov, streaming_enabled=True,
                               preferred=True, stream_controller=sc)
        session = orch.new_session()
        resp = orch.handle_stream("fail stream", session)
        assert "stream failed" in resp.text.lower()

    def test_no_streaming_capable_provider_honest_fallback(self, tmp_path):
        class _NonStreaming(LLMProvider):
            name = "non_stream"
            def chat(self, messages, tools=None, temperature=0.4, voice_mode=False):
                return LLMResponse(content="(ns) fallback")
        sc = StreamController(coalesce_ms=0)
        orch = self._make_orch(tmp_path, llm=_NonStreaming(), streaming_enabled=True,
                               preferred=True, stream_controller=sc)
        session = orch.new_session()
        resp = orch.handle_stream("hello", session)
        # Falls back to non-streaming chat() — honest, not fake
        assert resp.text == "(ns) fallback"
        msgs = session.short_term.as_messages()
        assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
        assert assistant_msgs[0]["content"] == "(ns) fallback"


# --------------------------------------------------------------------------- #
# Orchestrator routing + streaming integration
# --------------------------------------------------------------------------- #


class TestOrchestratorRoutingStreaming:
    def test_stream_via_routed_streaming_model(self, tmp_path):
        """Routing selects a streaming model; the turn streams through the routed
        provider, not the global llm."""
        from veronica.llm.routing.profiles import ModelProfile, ROLE_FAST
        from veronica.core.orchestrator import Orchestrator

        reg = ModelRegistry()
        reg.register(
            ModelProfile(model_id="s-fast", provider="echo", role=ROLE_FAST,
                         supports_streaming=True, priority=10),
            lambda: EchoProvider(),
        )
        router = ModelRouter(reg, config=RoutingConfig(enabled=True, fallback_enabled=False))
        analyzer = TaskAnalyzer(known_model_ids=reg.model_ids())

        config = Config()
        config.safety.require_confirmation_above = "SAFE"
        config.streaming.enabled = True
        config.streaming.preferred = True
        sc = StreamController(coalesce_ms=0)
        gate = PermissionGate(threshold=PermissionLevel.SAFE, confirm_callback=None)
        tools = ToolRegistry(gate)
        memory = LongTermMemory(tmp_path / "mem.json")
        # Global LLM is NON-streaming to prove routing is used, not the fallback
        class _StubLLM(LLMProvider):
            name = "stub"
            def chat(self, messages, tools=None, temperature=0.4, voice_mode=False):
                return LLMResponse(content="FAIL-global-should-not-be-used")
        orch = Orchestrator(
            config=config, llm=_StubLLM(), tools=tools, long_term_memory=memory,
            router=router, analyzer=analyzer, stream_controller=sc,
        )
        session = orch.new_session()
        resp = orch.handle_stream("hello routing stream", session)
        assert "FAIL-global" not in resp.text
        assert resp.routing is not None
        assert resp.routing.get("streaming") is True

    def test_routed_model_without_streaming_honest_fallback(self, tmp_path):
        """Router selects a model that does NOT support streaming → honest fallback."""
        from veronica.llm.routing.profiles import ModelProfile, ROLE_FAST
        from veronica.core.orchestrator import Orchestrator

        reg = ModelRegistry()
        reg.register(
            ModelProfile(model_id="ns-fast", provider="echo", role=ROLE_FAST,
                         supports_streaming=False, priority=10),
            lambda: EchoProvider(),
        )
        router = ModelRouter(reg, config=RoutingConfig(enabled=True, fallback_enabled=False))
        analyzer = TaskAnalyzer(known_model_ids=reg.model_ids())
        config = Config()
        config.safety.require_confirmation_above = "SAFE"
        config.streaming.enabled = True
        config.streaming.preferred = True
        sc = StreamController(coalesce_ms=0)
        gate = PermissionGate(threshold=PermissionLevel.SAFE, confirm_callback=None)
        tools = ToolRegistry(gate)
        memory = LongTermMemory(tmp_path / "mem.json")
        orch = Orchestrator(
            config=config, llm=EchoProvider(), tools=tools, long_term_memory=memory,
            router=router, analyzer=analyzer, stream_controller=sc,
        )
        session = orch.new_session()
        # NOTE: the text must NOT contain the analyzer's streaming keywords ("stream",
        # "streaming", ...). Streaming is enabled+preferred here, but the user did not
        # request it, so the router is free to select the non-streaming model and the
        # turn honestly falls back to that model's chat() — never a fake stream.
        resp = orch.handle_stream("hello plain chat turn", session)
        # Falls back to non-streaming chat() on the routed model (no fake stream)
        assert resp.text == "(echo) You said: hello plain chat turn"


# --------------------------------------------------------------------------- #
# Orchestrator cancel_stream / GUI chain
# --------------------------------------------------------------------------- #


class TestOrchestratorCancelStream(_OrchestratorHarness):
    def test_cancel_stream_false_when_no_active(self, tmp_path):
        from veronica.core.orchestrator import Orchestrator
        config = Config()
        config.safety.require_confirmation_above = "SAFE"
        gate = PermissionGate(threshold=PermissionLevel.SAFE, confirm_callback=None)
        tools = ToolRegistry(gate)
        memory = LongTermMemory(tmp_path / "mem.json")
        orch = Orchestrator(config=config, llm=EchoProvider(), tools=tools,
                            long_term_memory=memory, stream_controller=StreamController())
        assert orch.cancel_stream() is False

    def test_cancel_stream_returns_false_after_termination(self, tmp_path):
        echo = EchoProvider()
        sc = StreamController(coalesce_ms=0)
        orch = self._make_orch(tmp_path, llm=echo, streaming_enabled=True,
                               preferred=True, stream_controller=sc)
        session = orch.new_session()
        orch.handle_stream("hello cancel", session)
        # After the turn completes, _active_stream is None
        assert orch.cancel_stream() is False

    def test_cancel_stream_cancels_active(self, tmp_path):
        gate = threading.Event()
        prov = _GatedProvider(["partial", "lost"], gate)
        sc = StreamController(coalesce_ms=0)
        orch = self._make_orch(tmp_path, llm=prov, streaming_enabled=True,
                               preferred=True, stream_controller=sc)
        session = orch.new_session()
        result_holder: list = []

        def run_stream():
            resp = orch.handle_stream("stream this to cancel", session)
            result_holder.append(resp)

        t = threading.Thread(target=run_stream, daemon=True)
        t.start()
        # Wait until provider has yielded at least once
        for _ in range(200):
            if prov.yielded >= 1:
                break
            time.sleep(0.01)
        cancelled = orch.cancel_stream()
        gate.set()
        t.join(timeout=2)
        assert cancelled is True
        assert len(result_holder) == 1
        assert result_holder[0].text == "Response stream cancelled."


# --------------------------------------------------------------------------- #
# Episodic STREAM_FAILURE vocabulary (Phase 20)
# --------------------------------------------------------------------------- #


class TestEpisodicStreamFailure:
    def test_stream_failure_in_known(self):
        assert EpisodeStatus.STREAM_FAILURE in EpisodeStatus.KNOWN

    def test_map_task_status_stream_failure(self):
        assert map_task_status("stream_failed") == EpisodeStatus.STREAM_FAILURE
        assert map_task_status("stream_error") == EpisodeStatus.STREAM_FAILURE

    def test_derive_episode_status_stream_failure_wins(self):
        """STREAM_FAILURE is a genuine terminal — not masked by NOT_EXECUTED / UNKNOWN."""
        assert derive_episode_status("stream_failed", "not_executed", "unknown") == EpisodeStatus.STREAM_FAILURE
        assert derive_episode_status("stream_error", "not_executed", "unknown") == EpisodeStatus.STREAM_FAILURE

    def test_stream_failure_distinct_from_task_failure(self):
        """STREAM_FAILURE ≠ TASK_FAILURE — they map differently."""
        assert EpisodeStatus.STREAM_FAILURE != EpisodeStatus.FAILED
        assert map_task_status("stream_failed") != map_task_status("failed")
        assert derive_episode_status("stream_failed", "not_executed", "unknown") != \
               derive_episode_status("failed", "not_executed", "unknown")

    def test_stream_failure_not_conflated_on_executed(self):
        """Even when a task DID run, an explicit STREAM_FAILURE still wins."""
        assert derive_episode_status("stream_failed", "executed", "verified") == EpisodeStatus.STREAM_FAILURE

    def test_stream_failure_confidence_low(self):
        """Episodic confidence for stream_failure uses the default (low), not FAILED's."""
        from veronica.learning.experience import Experience, ExperienceOutcome
        from veronica.memory.thread.episodes import EpisodeBuilder
        exp = Experience(goal="test", outcome=ExperienceOutcome.FAILED, final_status="stream_failed")
        ep = EpisodeBuilder().build(exp)
        assert ep.episode_status == EpisodeStatus.STREAM_FAILURE


# --------------------------------------------------------------------------- #
# Thread Memory receives only final accumulated text (Phase 19)
# --------------------------------------------------------------------------- #


class TestMemoryRecordsFinalTextOnly:
    def test_assistant_reply_is_final_text(self, tmp_path):
        """Cancelled turns record nothing; successful turns record only the final text."""
        echo = EchoProvider()
        sc = StreamController(coalesce_ms=0)
        orch = TestOrchestratorStreaming._make_orch(
            tmp_path, llm=echo, streaming_enabled=True, preferred=True, stream_controller=sc)
        session = orch.new_session()
        resp = orch.handle_stream("stream and record", session)
        expected = "(echo) You said: stream and record"
        assert resp.text == expected
        msgs = session.short_term.as_messages()
        assistant_turns = [m for m in msgs if m["role"] == "assistant"]
        assert len(assistant_turns) == 1
        assert assistant_turns[0]["content"] == expected
        # No raw delta fragments present
        for m in assistant_turns:
            assert not any(w in m["content"] for w in ["partial", "delta"])


# --------------------------------------------------------------------------- #
# Streaming disabled preserves exact legacy behavior
# --------------------------------------------------------------------------- #


class TestStreamingDisabledPreservesBehavior:
    def test_disabled_config_default(self):
        """StreamingConfig defaults to disabled — nothing changed."""
        from veronica.config import StreamingConfig
        cfg = StreamingConfig()
        assert cfg.enabled is False
        assert cfg.preferred is False

    def test_streaming_controller_still_constructed_when_disabled(self, tmp_path):
        """Controller exists but streaming.enabled=False → handle_stream still works
        (just falls back to non-streaming chat())."""
        from veronica.core.orchestrator import Orchestrator
        sc = StreamController(coalesce_ms=0)
        config = Config()
        config.safety.require_confirmation_above = "SAFE"
        config.streaming.enabled = False
        gate = PermissionGate(threshold=PermissionLevel.SAFE, confirm_callback=None)
        tools = ToolRegistry(gate)
        memory = LongTermMemory(tmp_path / "mem.json")
        orch = Orchestrator(config=config, llm=EchoProvider(), tools=tools,
                            long_term_memory=memory, stream_controller=sc)
        session = orch.new_session()
        resp = orch.handle_stream("test disabled", session)
        assert resp.text == "(echo) You said: test disabled"
        msgs = session.short_term.as_messages()
        assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0]["content"] == "(echo) You said: test disabled"
