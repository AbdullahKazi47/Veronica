# Streaming LLM V1

Streaming turns provider chunks into ordered incremental events plus one final,
authoritative accumulated text. One coordinator owns the whole pipeline; nothing
else re-plans, re-selects, or re-derives stream state.

Scope: the pure-LLM turn. Fast-path tools, compound commands, and agentic tasks
reply through their existing non-streaming paths — only the LLM text turn streams.

---

## 1. Lifecycle (exactly one terminal)

```
STREAM_STARTED -> STREAMING -> STREAM_COMPLETED | STREAM_CANCELLED | STREAM_FAILED
                         \----> (a timeout/error/cancel may fire before STREAMING)
```

- **STREAM_COMPLETED** — the provider's final chunk (or the resource cap) ended the
  stream cleanly. `truncated=True` at the cap is an explicit, visible terminal, never
  silent truncation.
- **STREAM_CANCELLED** — the user asked to stop. `cancel()` is a request; the worker
  notices the flag and emits the terminal. Partial text is retained for diagnostics
  only — callers must never present it as an answer.
- **STREAM_FAILED** — a provider exception, a provider-reported error chunk
  (`finish_reason="error"`), or a first-token / idle / overall timeout.

Guard rails: exactly one terminal event per stream; a cancel that lands while the
final chunk is being consumed still wins over natural completion; a timeout that
fires mid-IO still wins over the stale reader.

## 2. Hard invariants

1. **Exact-text**: `final_text` is the concatenation of every received delta, in
   receive order. Coalescing batches *events* only — it never changes the accumulated
   text. Queue-full backpressure is a bounded retry loop, never a drop.
2. **No mid-stream model switch**: the provider to stream is resolved once, before any
   text flows (`ModelSelection.streaming_provider()`). If nothing can stream, the turn
   falls back to the classic `chat()` path — never a fake stream.
3. **Honest fallback**: when the user asked to stream but no configured model advertises
   streaming, the router returns an unsatisfied selection and the orchestrator declines
   truthfully. Routing never downgrades an explicit user request silently.
4. **One request_id per stream**: every event and the final `StreamResult` share it.
5. **Per-stream state only**: `StreamController` keeps state in a `stream_id`-keyed dict,
   fully thread-safe; concurrent streams never mix text.
6. **Discovery only via truth**: `provider_supports_streaming()` reads the provider's
   declared capability — a provider that merely implements `chat_stream` but does not
   advertise `supports_streaming` is still treated as non-streaming.

## 3. Worker architecture

- A daemon **reader thread** pulls chunks from the provider into a bounded queue
  (size 64), so cancellation and idle timeouts work even when the provider blocks on I/O.
- A **terminal sentinel** is pushed after the generator ends; the consumer may only
  declare completion after seeing the done-chunk **or** the sentinel — never a
  "just wait for the queue to empty" guess.
- Fixed flags are re-checked after every `break` before declaring completion
  (cancel → CANCELLED, timeout → FAILED), so a cancel/timeout racing the final chunk
  can never be masked as a natural completion.
- A provider that raises mid-stream produces an error chunk
  (`finish_reason="error"`, `metadata["error"]=<msg>`); the consumer converts that
  into STREAM_FAILED, emitting `stream_error` exactly once.

## 4. Backpressure (why no chunk is ever dropped)

The queue is bounded to keep memory flat for fast providers. Options would be:

- `put_nowait` on a full queue → raises or drops → **corrupts `final_text`**. Rejected.
- Blocking `put` → the main loop cannot see the terminal done-chunk behind a full queue
  → potential deadlock. Rejected.
- **Bounded retry** (`_queue_put_blocking`): loop, put, on `queue.Full` re-check the
  cancel/terminal flag then `sleep(0.001)`. The main loop drains promptly because it
  only terminates after a done-chunk or the sentinel (pushed *after* all chunks), so
  room is always made; a cancelled stream stops forwarding immediately.

## 5. Timeouts (each disabled at 0)

| Timeout | Fires when | Terminal |
| --- | --- | --- |
| `first_token_s` | nothing received since start | FAILED |
| `idle_s` | nothing received since the last delta | FAILED |
| `overall_s` | hard ceiling on total duration | FAILED |

Cancel and timeout can land together; **cancel wins** (it's a user instruction).

## 6. Resource cap

`max_response_chars` caps the accumulator. Hitting it is an explicit,
`truncated=True`, `finish_reason="max_length"` **STREAM_COMPLETED** terminal — the
event payload reports `truncated: True`. `0` (or negative) means uncapped, matching
the timeout "0 = disabled" convention.

## 7. SSE events

| Event | Payload highlights |
| --- | --- |
| `stream_started` | request_id, stream_id, model, provider, routing |
| `stream_status` | status transitions (started / streaming / terminal) |
| `response_delta` | delta text, sequence, chunk_index (coalesced cadence) |
| `stream_completed` | final_text, char_count, finish_reason, truncated, duration_s |
| `stream_cancelled` | char_count (partial retained for diagnostics only) |
| `stream_error` | error, char_count |

Exactly one of `stream_completed` / `stream_cancelled` / `stream_error` terminates.

## 8. Orchestrator integration

- `handle_stream()` is the single entry point; `handle()` behaviour is unchanged when
  streaming is disabled.
- Streaming applies only when `streaming.enabled` AND a controller is attached AND
  (`preferred` or the user explicitly asked — `TaskAnalyzer.requires_streaming`).
- With routing: `ModelSelection.streaming_provider()` returns the single provider that
  will stream. No streaming-capable model → honest fallback through that selection's
  `chat()`.
- A cancelled or failed stream ends the turn with an explicit terminal response
  ("Response stream cancelled." / "The response stream failed: …"), **never** records
  the partial output as an assistant reply, and keeps partial text diagnostics-only.
- Conversation memory and episodic memory receive only the final accumulated text of a
  completed stream.

## 9. Episodic memory (Phase 20)

`EpisodeStatus.STREAM_FAILURE` (`"stream_failure"`) is a distinct terminal status for
streaming transport failures — never collapsed into task `FAILED`. `map_task_status`
maps `stream_failed` / `stream_error` to it, and `derive_episode_status` gives it the
same unconditional-wins treatment as `CANCELLED`/`BLOCKED`:

```python
derive_episode_status("stream_failed", "executed", "verified") == STREAM_FAILURE
```

## 10. Verification

`tests/test_streaming_llm.py` — 59 tests covering the chunk base, provider capability
discovery, lifecycle (exactly-one-terminal across all paths), coalescing/text integrity,
true backpressure under 100-chunk bursts and concurrent streams, timeouts, the resource
cap, request_id correlation, metadata/secrets, routing + orchestrator integration,
cancellation (pre-start, mid-stream, post-termination), and the episodic STREAM_FAILURE
vocabulary. `StreamingDisabledPreservesBehavior` proves an untouched config is
behaviour-identical to the classic path.