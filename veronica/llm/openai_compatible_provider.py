"""
Optional cloud provider for anyone who *wants* to point VERONICA at a paid or free-tier
hosted model (OpenRouter, OpenAI, any OpenAI-compatible endpoint). Never used unless the
user explicitly sets `llm.provider: openai_compatible` in config.yaml AND supplies an API
key — VERONICA never assumes or requires this.
"""
from __future__ import annotations

import json
from typing import Optional

from veronica.llm.base import ChatMessage, LLMProvider, LLMResponse, StreamChunk, ToolCall
from veronica.logging_setup import get_logger

logger = get_logger("llm.openai_compatible")

_MAX_REMEMBERED_TURNS = 32

# Google answers 503 ("overloaded") and 429 ("too many requests") in short bursts; a quick
# retry usually lands. Two retries, ~0.5 s then ~1.2 s; after that the backup model (Groq) answers.
_RETRY_STATUS = {429, 500, 502, 503, 504}
_RETRY_DELAYS_S = (0.5, 1.2)
BUSY_MESSAGE = ("Google's AI service is overloaded right now, so I couldn't think that one through. "
                "Quick commands like opening apps or folders still work. Try again in a moment.")
OFFLINE_MESSAGE = ("I can't reach Google's AI service right now. Check the internet connection; "
                   "quick commands like opening apps still work.")


def _is_quota(body: str) -> bool:
    """A 429 that means "this model's free allowance is used up", not "slow down a second"."""
    b = (body or "").lower()
    return "quota" in b or "resource_exhausted" in b or "perday" in b or "per day" in b


def _post_with_retry(requests, url, *, sleep=None, **kw):
    """POST, retrying the brief overload errors. Returns the final response (raises on
    connection failure after the retries)."""
    import time as _time
    sleep = sleep or _time.sleep
    resp = None
    for attempt, delay in enumerate((0.0,) + _RETRY_DELAYS_S):
        if delay:
            sleep(delay)
        try:
            resp = requests.post(url, **kw)
        except requests.ConnectionError:
            if attempt == len(_RETRY_DELAYS_S):
                raise
            continue
        if resp.status_code not in _RETRY_STATUS:
            return resp
        if resp.status_code == 429 and _is_quota(getattr(resp, "text", "")):
            return resp          # the model's daily allowance is spent: waiting can't bring it back
        logger.info("Cloud model busy (%s); retrying.", resp.status_code)
    return resp


class OpenAICompatibleProvider(LLMProvider):
    name = "openai_compatible"
    supports_streaming = True

    def __init__(self, base_url: str, api_key: Optional[str], model: str, timeout_s: int = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        # tool_call id -> the model's exact assistant message that requested it.
        self._tool_turns: dict[str, dict] = {}

    def is_available(self) -> bool:
        return bool(self.api_key)

    def _remember(self, message: dict) -> None:
        for tc in message.get("tool_calls") or []:
            self._tool_turns[tc["id"]] = message
        while len(self._tool_turns) > _MAX_REMEMBERED_TURNS:
            self._tool_turns.pop(next(iter(self._tool_turns)))

    def _wire_messages(self, messages: list[ChatMessage]) -> list[dict]:
        """VERONICA messages -> OpenAI chat messages.

        The orchestrator records a tool-calling turn as plain assistant text plus
        role="tool" results, but the API needs the assistant turn's `tool_calls` and
        each result's `tool_call_id` (Gemini 3.x also needs the call's thought
        signature, carried in the replayed message). Without them every request after
        a tool call is rejected with 400 INVALID_ARGUMENT. Replay the remembered turn
        exactly; if it is unknown (e.g. after a restart), send the results as text."""
        out: list[dict] = []
        i = 0
        while i < len(messages):
            m = messages[i]
            if m.role != "assistant":
                if m.role == "tool":  # orphan result with no remembered call
                    out.append({"role": "user", "content": f"[result of tool {m.name or 'unknown'}]\n{m.content}"})
                else:
                    out.append({"role": m.role, "content": m.content})
                i += 1
                continue
            j, results = i + 1, []
            while j < len(messages) and messages[j].role == "tool":
                results.append(messages[j])
                j += 1
            replay = next((self._tool_turns[r.tool_call_id] for r in results
                           if r.tool_call_id in self._tool_turns), None)
            if replay is not None:
                known = [tc["id"] for tc in replay["tool_calls"]]
                out.append(replay)
                by_id = {r.tool_call_id: r for r in results}
                for call_id in known:  # every requested call gets exactly one result
                    r = by_id.get(call_id)
                    out.append({"role": "tool", "tool_call_id": call_id,
                                "content": r.content if r else "(not executed)"})
                leftovers = [r for r in results if r.tool_call_id not in known]
            else:
                if m.content:
                    out.append({"role": "assistant", "content": m.content})
                leftovers = results
            for r in leftovers:
                out.append({"role": "user", "content": f"[result of tool {r.name or 'unknown'}]\n{r.content}"})
            i = j
        # Last step before anything leaves the machine: strip secrets / sensitive IDs.
        # This covers replayed tool turns and tool results too, not just user text.
        from veronica.safety import privacy
        return privacy.get_guard().scrub_wire(out)

    def chat(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.4,
        voice_mode: bool = False,
    ) -> LLMResponse:
        if not self.api_key:
            return LLMResponse(
                content=(
                    "No API key is configured for the cloud provider. Set VERONICA_LLM_API_KEY "
                    "in config/.env, or switch llm.provider back to 'ollama' in config.yaml."
                ),
                ok=False,
            )
        try:
            import requests
        except ImportError:
            return LLMResponse(content="[VERONICA] `requests` isn't installed; run `pip install requests`.", ok=False)

        payload = {
            "model": self.model,
            "messages": self._wire_messages(messages),
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        try:
            resp = _post_with_retry(
                requests, f"{self.base_url}/chat/completions", json=payload, headers=headers,
                timeout=self.timeout_s,
            )
            if resp.status_code >= 400:  # keep the provider's own explanation in the log
                logger.warning("Cloud LLM %s error body: %s", resp.status_code, resp.text[:300])
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            # Technical detail goes to the log; the person hears a plain sentence. The raw
            # error (status + body) goes to routing, which sets a spent model aside.
            logger.warning("Cloud LLM request failed: %s", e)
            offline = isinstance(e, requests.ConnectionError) and not isinstance(e, requests.Timeout)
            body = getattr(getattr(e, "response", None), "text", "") or ""
            return LLMResponse(content=OFFLINE_MESSAGE if offline else BUSY_MESSAGE, ok=False,
                               raw={"error": f"{e} {body[:400]}"})

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message", {})
        content = message.get("content", "") or ""
        tool_calls = []
        for i, tc in enumerate(message.get("tool_calls", []) or []):
            if not tc.get("id"):
                tc["id"] = f"call_{i}"  # the replayed turn and the result must share an id
            fn = tc.get("function", {})
            args = fn.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            tool_calls.append(ToolCall(id=str(tc["id"]), name=fn.get("name", ""), arguments=args))
        if tool_calls:
            self._remember(dict(message, role="assistant"))

        return LLMResponse(content=content, tool_calls=tool_calls, raw=data)

    def chat_stream(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.4,
        voice_mode: bool = False,
    ):
        """Stream via the OpenAI-compatible SSE endpoint (stream: True). Same
        construction as ``chat`` — no separate SDK, no second client."""
        if not self.api_key:
            yield StreamChunk(
                text=("No API key is configured for the cloud provider. Set "
                      "VERONICA_LLM_API_KEY in config/.env, or switch llm.provider "
                      "back to 'ollama' in config.yaml."),
                finish_reason="error", done=True,
            )
            return
        try:
            import requests
        except ImportError:
            yield StreamChunk(
                text="[VERONICA] `requests` isn't installed; run `pip install requests`.",
                finish_reason="error", done=True,
            )
            return

        payload = {
            "model": self.model,
            "messages": self._wire_messages(messages),
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        seq = 0
        # Minimal tool-call delta assembler (V1: capture tool calls on the terminal
        # chunk so the orchestrator's existing tool loop stays authoritative — the
        # full delta protocol is explicitly out of scope).
        tool_assembler: dict[int, dict] = {}
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions", json=payload, headers=headers,
                timeout=self.timeout_s, stream=True,
            )
            resp.raise_for_status()
            # Server-sent events carry no charset, so requests guesses ISO-8859-1 and every
            # dash or accent came out garbled ("Yes \xe2\x80\x94" for "Yes —"), which also
            # crashed the Supertonic voice. The stream is UTF-8.
            resp.encoding = "utf-8"
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                line = raw.strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    yield StreamChunk(text="", sequence=seq, finish_reason="stop", done=True)
                    return
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choice = (data.get("choices") or [{}])[0]
                delta = choice.get("delta", {}) or {}
                text = delta.get("content", "") if isinstance(delta, dict) else ""
                finish = choice.get("finish_reason")
                done = finish is not None or bool(data.get("done", False))
                metadata: dict = {}
                if finish:
                    metadata["finish_reason"] = finish
                if isinstance(delta, dict) and delta.get("tool_calls"):
                    for frag in delta["tool_calls"]:
                        if not isinstance(frag, dict):
                            continue
                        idx = int(frag.get("index", 0))
                        slot = tool_assembler.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if frag.get("id"):
                            slot["id"] = frag["id"]
                        if frag.get("extra_content"):       # Gemini's thought signature, replayed with the call
                            slot["extra_content"] = frag["extra_content"]
                        fn = frag.get("function", {}) or {}
                        if isinstance(fn, dict):
                            if fn.get("name"):
                                slot["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
                # Gemini ends a streamed tool call with finish_reason "stop", not "tool_calls" (OpenAI
                # does the latter). Gating on "tool_calls" dropped every Gemini tool call: the stream
                # "completed empty" and she asked Google again, so each tool request took twice as long
                # and spent twice the quota. Whatever the finish reason, collected calls are the answer.
                if finish and tool_assembler:
                    assembled = []
                    for idx in sorted(tool_assembler):
                        slot = tool_assembler[idx]
                        try:
                            args = json.loads(slot["arguments"]) if slot["arguments"] else {}
                        except json.JSONDecodeError:
                            args = {}
                        assembled.append({
                            "id": slot["id"] or f"call_{idx}",
                            "name": slot["name"],
                            "arguments": args if isinstance(args, dict) else {},
                        })
                    metadata["tool_calls"] = assembled
                    # Best effort: streamed deltas carry no thought signature to replay.
                    self._remember({"role": "assistant", "content": None, "tool_calls": [
                        {"id": c["id"], "type": "function", "function": {
                            "name": c["name"], "arguments": json.dumps(c["arguments"])},
                         **({"extra_content": tool_assembler[i]["extra_content"]}
                            if tool_assembler[i].get("extra_content") else {})}
                        for i, c in zip(sorted(tool_assembler), assembled)]})
                    if finish != "tool_calls":
                        yield StreamChunk(text=text or "", sequence=seq, done=False, metadata={})
                        seq += 1
                        yield StreamChunk(text="", sequence=seq, done=True, finish_reason="tool_calls",
                                          metadata=metadata)
                        return
                yield StreamChunk(text=text or "", sequence=seq, done=done,
                                  finish_reason=finish, metadata=metadata)
                seq += 1
                if done:
                    return
        except Exception as e:  # noqa: BLE001 — connection failure is a controlled event
            logger.warning("Cloud LLM stream failed: %s", e)
            yield StreamChunk(
                text=f"The cloud model stream failed: {e}",
                finish_reason="error", done=True,
            )
