"""Multi-Model Routing V1 tests (Phase 17).

Covers the full routing bundle: ModelProfile metadata, deterministic TaskProfile
classification, ModelRegistry ownership, ModelRouter selection priorities (explicit
>>> capability >> task affinity >> context >> availability >> priority >> default),
capability-preserving fallback execution, provider construction reuse, thread-memory
isolation, safety (routing grants NO permission), failure isolation, config coercion,
and regression (routing disabled = byte-identical single-model behaviour).

Every model below is a local FakeProvider / EchoProvider — no network, no external
process, deterministic, consistent with the rest of the suite.
"""
from __future__ import annotations

import json

import pytest
import yaml

from veronica.config import Config, ModelEntryConfig, load_config
from veronica.core.orchestrator import Orchestrator
from veronica.llm.base import ChatMessage, LLMProvider, LLMResponse, ToolCall
from veronica.llm.router import EchoProvider, build_provider, build_provider_for_model
from veronica.llm.routing.builder import build_routing
from veronica.llm.routing.health import HealthState, ModelHealth
from veronica.llm.routing.profiles import (
    CAP_TOOLS, CAP_VISION,
    ModelProfile, ROLE_CODING, ROLE_FAST, ROLE_GENERAL, ROLE_REASONING, ROLE_VISION,
    TASK_ANALYSIS, TASK_CHAT, TASK_CODING, TASK_GENERAL, TASK_PLANNING,
    TASK_REASONING, TASK_SUMMARIZATION, TASK_TOOL_USE, TASK_VISION,
)
from veronica.llm.routing.registry import ModelRegistry
from veronica.llm.routing.router import (
    ModelRouter, ModelSelection, RoutedResponse, RoutingConfig,
)
from veronica.llm.routing.task import TaskAnalyzer, TaskProfile
from veronica.memory.long_term import LongTermMemory
from veronica.safety.permissions import PermissionGate, PermissionLevel
from veronica.tools.base import Tool, ToolResult
from veronica.tools.registry import ToolRegistry

# --------------------------------------------------------------------------- #
# Fakes and builders
# --------------------------------------------------------------------------- #


class FakeProvider(LLMProvider):
    """Deterministic per-instance provider for routing/fallback tests."""

    name = "fake"

    def __init__(
        self,
        model: str = "fake",
        *,
        fail: str | None = None,          # None | "raise" | "ok_false" | "empty"
        content: str | None = None,
        tool_calls: list[ToolCall] | None = None,
        available: bool = True,
    ) -> None:
        self.model = model
        self.fail = fail
        self.fixed_content = content
        self.fixed_tool_calls = list(tool_calls or [])
        self._available = available
        self.calls = 0
        self.probes = 0

    def chat(self, messages, tools=None, temperature=0.4) -> LLMResponse:
        self.calls += 1
        if self.fail == "raise":
            raise RuntimeError("deliberate provider failure")
        if self.fail == "ok_false":
            return LLMResponse(content=f"({self.model}) error", ok=False)
        if self.fail == "empty":
            return LLMResponse(content="", ok=True)
        tc = [ToolCall(id=t.id, name=t.name, arguments=dict(t.arguments)) for t in self.fixed_tool_calls]
        return LLMResponse(content=self.fixed_content or f"({self.model}) reply", tool_calls=tc)

    def is_available(self) -> bool:
        self.probes += 1
        return self._available


class HopProvider(LLMProvider):
    """Returns a canned sequence of responses, repeating the last (for loop tests)."""

    name = "hop"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.index = 0

    def chat(self, messages, tools=None, temperature=0.4) -> LLMResponse:
        r = self.responses[min(self.index, len(self.responses) - 1)]
        self.index += 1
        return r


def mk(
    model_id: str,
    role: str = ROLE_GENERAL,
    *,
    tools: bool = False,
    vision: bool = False,
    streaming: bool = False,
    task_types: tuple[str, ...] = (),
    speed: str = "fast",
    priority: int = 100,
    default: bool = False,
    context_window: int = 8192,
    enabled: bool = True,
) -> ModelProfile:
    return ModelProfile(
        model_id=model_id,
        provider="fake",
        role=role,
        supports_tools=tools,
        supports_vision=vision,
        supports_streaming=streaming,
        task_types=frozenset(task_types),
        speed_class=speed,
        priority=priority,
        default=default,
        enabled=enabled,
        context_window=context_window,
    )


STANDARD = [
    mk("fast-echo", ROLE_FAST, priority=10),
    mk("reason-echo", ROLE_REASONING, task_types=("reasoning", "analysis", "planning"), priority=50),
    mk("code-echo", ROLE_CODING, tools=True, task_types=("coding",), priority=50),
    mk("gen-echo", ROLE_GENERAL, tools=True, priority=100, default=True),
    mk("vision-echo", ROLE_VISION, vision=True, priority=40),
]


def register_standard(reg: ModelRegistry, *, overrides: dict | None = None) -> ModelRegistry:
    overrides = overrides or {}
    for prof in STANDARD:
        factory = overrides.get(prof.model_id)
        if factory is None:
            factory = (lambda m=prof.model_id: FakeProvider(model=m))  # type: ignore[assignment]
        reg.register(prof, provider_factory=factory)
    return reg


def make_router(reg: ModelRegistry, **kw) -> ModelRouter:
    cfg = RoutingConfig(enabled=True, health_ttl_s=30.0, max_fallback_attempts=2, **kw)
    return ModelRouter(reg, config=cfg)


def make_analyzer(reg: ModelRegistry) -> TaskAnalyzer:
    return TaskAnalyzer(known_model_ids=set(reg.model_ids()))


def general_task(**kw) -> TaskProfile:
    base = {"task_type": TASK_GENERAL, "complexity": "medium", "latency_sensitive": False}
    base.update(kw)
    return TaskProfile(**base)


# --------------------------------------------------------------------------- #
# ModelProfile: metadata, capability sync, handles_task, no secrets
# --------------------------------------------------------------------------- #


class TestModelProfile:
    def test_fields_populated(self):
        p = mk("m", ROLE_CODING, tools=True, task_types=("coding",), priority=7, context_window=4096)
        assert p.model_id == "m"
        assert p.provider == "fake"
        assert p.role == ROLE_CODING
        assert p.priority == 7
        assert p.context_window == 4096

    def test_capability_booleans_sync_into_set(self):
        p = ModelProfile(model_id="m", provider="fake", supports_tools=True, supports_vision=True)
        assert CAP_TOOLS in p.capabilities
        assert CAP_VISION in p.capabilities
        assert p.has_capability(CAP_TOOLS)
        assert p.has_capability(CAP_VISION)

    def test_handles_task_empty_means_general_purpose(self):
        p = mk("m")
        for t in (TASK_GENERAL, TASK_CODING, TASK_VISION, TASK_CHAT):
            assert p.handles_task(t)

    def test_handles_task_declared_only(self):
        p = mk("m", task_types=(TASK_CODING,))
        assert p.handles_task(TASK_CODING)
        assert not p.handles_task(TASK_CHAT)

    def test_summary_excludes_secrets(self):
        p = ModelProfile(
            model_id="m", provider="openai_compatible",
            base_url="http://internal:8080/v1", api_key_env="VERONICA_LLM_API_KEY",
        )
        s = p.summary()
        assert "base_url" not in s
        assert "api_key_env" not in s
        assert "api_key" not in s
        assert "VERONICA_LLM_API_KEY" not in json.dumps(s)
        assert "secret" not in json.dumps(s).lower()


# --------------------------------------------------------------------------- #
# TaskProfile / TaskAnalyzer: deterministic classification
# --------------------------------------------------------------------------- #


class TestTaskAnalyzer:
    @pytest.fixture()
    def analyzer(self) -> TaskAnalyzer:
        return TaskAnalyzer(known_model_ids={"fast-echo", "code-echo", "reason-echo"})

    def test_classify_all_types(self, analyzer):
        cases = {
            TASK_CHAT: "hello there how are you today",
            TASK_CODING: "write a script to parse json",
            TASK_REASONING: "explain why this happens and what the implications might be",
            TASK_ANALYSIS: "compare and contrast the trade-offs between these two approaches",
            TASK_PLANNING: "make a plan with a roadmap and milestones for delivery",
            TASK_SUMMARIZATION: "summarize the key points of the meeting",
            TASK_TOOL_USE: "open the browser and search the web for weather",
            TASK_VISION: "look at this screenshot and describe what is on my screen",
            TASK_GENERAL: "tell me about your favorite color",
        }
        for expected, text in cases.items():
            tp = analyzer.analyze(text)
            assert tp.task_type == expected, f"{text!r} -> {tp.task_type}, expected {expected}"

    def test_deterministic_same_input(self, analyzer):
        text = "write a python script to parse a json file"
        a = analyzer.analyze(text).to_dict()
        b = analyzer.analyze(text).to_dict()
        assert a == b

    def test_explicit_model_detected(self, analyzer):
        tp = analyzer.analyze("use code-echo to fix my script please")
        assert tp.explicit_model_request == "code-echo"
        assert tp.priority == 90

    def test_explicit_unknown_model_not_honoured(self, analyzer):
        tp = analyzer.analyze("use nonexistent-model to do this")
        assert tp.explicit_model_request is None

    def test_latency_sensitive_short_chat(self, analyzer):
        assert analyzer.analyze("hello there").latency_sensitive is True

    def test_not_latency_sensitive_for_coding(self, analyzer):
        assert analyzer.analyze("write a python script to parse json").latency_sensitive is False

    def test_empty_text_is_safe(self, analyzer):
        tp = analyzer.analyze("   ")
        assert tp.task_type == TASK_GENERAL
        assert tp.complexity == "low"


# --------------------------------------------------------------------------- #
# ModelRegistry: registration, laziness, queries, health caching
# --------------------------------------------------------------------------- #


class TestModelRegistry:
    def test_register_get_len(self):
        reg = ModelRegistry()
        reg.register(mk("a"), (lambda: FakeProvider(model="a")))
        reg.register(mk("b"), (lambda: FakeProvider(model="b")))
        assert len(reg) == 2
        assert reg.get("a").model_id == "a"
        assert reg.profile_of("b").model_id == "b"
        assert reg.model_ids() == ["a", "b"]

    def test_duplicate_raises(self):
        reg = ModelRegistry()
        reg.register(mk("a"), (lambda: FakeProvider(model="a")))
        with pytest.raises(ValueError):
            reg.register(mk("a"), (lambda: FakeProvider(model="a")))

    def test_unregister(self):
        reg = ModelRegistry()
        reg.register(mk("a"), (lambda: FakeProvider(model="a")))
        reg.unregister("a")
        assert reg.get("a") is None
        assert len(reg) == 0

    def test_provider_factory_is_lazy_and_singleton(self):
        reg = ModelRegistry()
        made = []

        def factory():
            made.append(1)
            return FakeProvider(model="lazy")

        reg.register(mk("lazy"), factory)
        assert made == []                     # registering builds nothing
        first = reg.get("lazy").provider()
        assert made == [1]
        assert reg.get("lazy").provider() is first
        assert made == [1]                    # constructed once per model

    def test_find_capable_filters(self):
        reg = register_standard(ModelRegistry())
        tools_capable = {e.model_id for e in reg.find_capable(CAP_TOOLS)}
        assert tools_capable == {"code-echo", "gen-echo"}
        vision_only = reg.find_capable(CAP_VISION)
        assert [e.model_id for e in vision_only] == ["vision-echo"]
        assert reg.find_capable(CAP_TOOLS, provider="fake") == reg.find_capable(CAP_TOOLS)
        assert reg.find_capable(CAP_TOOLS, provider="bogus") == []

    def test_find_by_role_and_handles_task(self):
        reg = register_standard(ModelRegistry())
        assert {e.model_id for e in reg.find_by_role(ROLE_REASONING)} == {"reason-echo"}
        assert {e.model_id for e in reg.find_handles_task(TASK_CODING)} == {"code-echo", "gen-echo", "fast-echo", "vision-echo"}

    def test_default_entry_explicit(self):
        reg = ModelRegistry()
        reg.register(mk("low", priority=5, default=True), (lambda: FakeProvider(model="low")))
        reg.register(mk("high", priority=1, default=True), (lambda: FakeProvider(model="high")))
        assert reg.default_entry().model_id == "high"   # lowest priority default wins

    def test_default_entry_falls_back_to_general(self):
        reg = ModelRegistry()
        reg.register(mk("g1", role=ROLE_GENERAL, priority=10), (lambda: FakeProvider(model="g1")))
        reg.register(mk("f", role=ROLE_FAST, priority=1), (lambda: FakeProvider(model="f")))
        assert reg.default_entry().model_id == "g1"     # no default flag -> general, not fastest

    def test_check_health_is_ttl_cached(self):
        provider: list = []

        def factory():
            p = FakeProvider(model="ok", available=True)
            provider.append(p)
            return p

        reg = ModelRegistry(health_ttl_s=100.0)
        reg.register(mk("ok"), factory)
        h = reg.check_health("ok")
        assert h.state is HealthState.AVAILABLE
        assert h.method == "probe"
        assert provider[0].probes == 1

        h2 = reg.check_health("ok")                     # within TTL -> cached, no re-probe
        assert h2 is h
        assert provider[0].probes == 1                   # is_available NOT called again

        reg.check_health("ok", force=True)              # force re-probes
        assert provider[0].probes == 2                   # same provider, fresh probe

    def test_check_health_unavailable(self):
        reg = ModelRegistry(health_ttl_s=100.0)
        reg.register(mk("down"), (lambda: FakeProvider(model="down", available=False)))
        h = reg.check_health("down")
        assert h.state is HealthState.UNAVAILABLE


# --------------------------------------------------------------------------- #
# ModelHealth: state transitions
# --------------------------------------------------------------------------- #


class TestModelHealth:
    def test_transitions_to_unavailable(self):
        h = ModelHealth("m")
        assert h.state is HealthState.UNKNOWN
        assert h.usable()
        h.record_failure("first")
        assert h.state is HealthState.DEGRADED
        assert h.method == "observed"
        assert h.usable()                      # DEGRADED still routable (penalized)
        h.record_failure("second")
        assert h.state is HealthState.UNAVAILABLE
        assert not h.usable()
        assert h.last_error is not None

    def test_record_success_resets(self):
        h = ModelHealth("m")
        h.record_failure("boom")
        h.record_failure("boom")
        assert h.state is HealthState.UNAVAILABLE
        h.record_success(latency_s=0.2)
        assert h.state is HealthState.AVAILABLE
        assert h.consecutive_failures == 0
        assert h.usable()

    def test_mark_available_unavailable(self):
        h = ModelHealth("m")
        h.mark_unavailable("probe down")
        assert h.state is HealthState.UNAVAILABLE
        assert h.method == "probe"
        h.mark_available()
        assert h.state is HealthState.AVAILABLE

    def test_summary(self):
        h = ModelHealth("m")
        h.record_success(latency_s=0.1)
        h.record_failure("err")
        s = h.summary()
        assert s["model_id"] == "m"
        assert s["state"] == "DEGRADED"
        assert s["last_error"] == "err"


# --------------------------------------------------------------------------- #
# ModelRouter: selection priorities and hard capabilities
# --------------------------------------------------------------------------- #


class TestRouting:
    @pytest.fixture()
    def reg(self) -> ModelRegistry:
        return register_standard(ModelRegistry())

    @pytest.fixture()
    def router(self, reg) -> ModelRouter:
        return make_router(reg)

    def test_coding_task_selects_coding_model(self, reg, router):
        sel = router.route(make_analyzer(reg).analyze("write a python script to parse json"))
        assert sel.model_id == "code-echo"
        assert sel.decision == "routed"
        assert sel.satisfied is True

    def test_reasoning_task_selects_reasoning_model(self, reg, router):
        sel = router.route(make_analyzer(reg).analyze("explain why this happens and what the implications might be"))
        assert sel.model_id == "reason-echo"

    def test_task_affinity_dominates_priority(self, reg, router):
        # fast-echo has priority 10 (better than code-echo's 50) but the CODING task
        # type must beat configured priority per the mandated priority order.
        sel = router.route(make_analyzer(reg).analyze("write a script to parse json"))
        assert sel.model_id == "code-echo"
        assert sel.priority == 50
        assert any("task_type=" in r for r in sel.reason)

    def test_explicit_request_honoured_first(self, reg, router):
        sel = router.route(general_task(explicit_model_request="code-echo"))
        assert sel.model_id == "code-echo"
        assert sel.decision == "explicit"
        assert sel.reason[0] == "explicit_user_instruction"

    def test_explicit_unknown_records_and_routes_normally(self, reg, router):
        sel = router.route(general_task(explicit_model_request="phantom-model"))
        assert sel.decision == "routed_with_unknown_explicit"
        assert sel.satisfied is True
        assert sel.model_id == "gen-echo"                # still routed to a real model
        assert "explicit_user_instruction_unavailable" in sel.reason
        assert "model=phantom-model" in sel.reason

    def test_explicit_disabled_treated_as_unknown(self):
        reg = ModelRegistry()
        reg.register(mk("off-echo", enabled=False), (lambda: FakeProvider(model="off-echo")))
        reg.register(mk("gen-echo", ROLE_GENERAL, default=True), (lambda: FakeProvider(model="gen-echo")))
        router = make_router(reg)
        sel = router.route(general_task(explicit_model_request="off-echo"))
        assert sel.model_id == "gen-echo"
        assert sel.decision == "routed_with_unknown_explicit"

    def test_vision_hard_cap_selects_vision_model(self, reg, router):
        sel = router.route(general_task(task_type=TASK_VISION, requires_vision=True))
        assert sel.model_id == "vision-echo"
        assert sel.satisfied is True

    def test_vision_unsupported_is_unsatisfied(self):
        reg = register_standard(ModelRegistry())
        reg.unregister("vision-echo")
        router = make_router(reg)
        sel = router.route(general_task(task_type=TASK_VISION, requires_vision=True))
        assert sel.satisfied is False
        assert sel.decision == "unsupported_capability:vision"
        assert sel.capability_gaps == ["vision"]
        # Calling chat() on an unsatisfied selection must NOT fake success.
        resp = sel.chat([ChatMessage(role="user", content="look")])
        assert resp.succeeded is False
        assert resp.ok is False

    def test_tools_hard_cap_applies_when_tools_available(self, reg, router):
        ctx = {"tools_available": True}
        sel = router.route(make_analyzer(reg).analyze("open the browser and search the web for weather"), context=ctx)
        assert sel.model_id == "gen-echo"                # general (preferred for tool_use) among tools-capable
        assert sel.satisfied is True

    def test_no_tools_cap_when_tools_unavailable(self, reg, router):
        ctx = {"tools_available": False}
        sel = router.route(make_analyzer(reg).analyze("open the browser and search the web for weather"), context=ctx)
        assert sel.satisfied is True
        assert sel.model_id  # still picks a real model, no unsatisfied gap

    def test_context_window_fit(self):
        reg = ModelRegistry()
        reg.register(mk("small", context_window=4096), (lambda: FakeProvider(model="small")))
        reg.register(mk("big", context_window=32768, priority=5, default=True),
                     (lambda: FakeProvider(model="big")))
        router = make_router(reg)
        sel = router.route(general_task(estimated_context_size=20000))
        assert sel.model_id == "big"
        assert "context_fit" in sel.reason

    def test_no_eligible_model_when_all_disabled(self):
        reg = ModelRegistry()
        reg.register(mk("only", enabled=False), (lambda: FakeProvider(model="only")))
        router = make_router(reg)
        sel = router.route(general_task())
        assert sel.satisfied is False
        assert sel.decision == "no_eligible_model"


# --------------------------------------------------------------------------- #
# Fallback execution through ModelSelection.chat
# --------------------------------------------------------------------------- #


class TestFallbackExecution:
    @pytest.fixture()
    def pair(self):
        """A registry with a preferred general model plus a backup."""
        reg = ModelRegistry()
        reg.register(mk("primary", ROLE_GENERAL, priority=10, default=True),
                     (lambda: FakeProvider(model="primary")))
        reg.register(mk("backup", ROLE_GENERAL, priority=20),
                     (lambda: FakeProvider(model="backup")))
        return reg

    def test_primary_ok_no_fallback(self, pair):
        router = make_router(pair)
        sel = router.route(general_task())
        assert sel.model_id == "primary"
        resp = sel.chat([ChatMessage(role="user", content="hi")])
        assert resp.succeeded is True and resp.ok is True
        assert "(primary)" in resp.content
        assert resp.routing["fallback_used"] is False
        assert resp.routing["model_id"] == "primary"
        # the successful call flipped health to AVAILABLE
        assert pair.health_of("primary").state is HealthState.AVAILABLE

    def test_ok_false_falls_back_successfully(self):
        reg = ModelRegistry()
        reg.register(mk("primary", ROLE_GENERAL, priority=10, default=True),
                     (lambda: FakeProvider(model="primary", fail="ok_false")))
        reg.register(mk("backup", ROLE_GENERAL, priority=20),
                     (lambda: FakeProvider(model="backup")))
        router = make_router(reg)
        resp = router.route(general_task()).chat([ChatMessage(role="user", content="hi")])
        assert resp.succeeded is True
        assert "(backup)" in resp.content
        assert resp.routing["fallback_used"] is True
        # trace lists both attempts
        assert [a["model_id"] for a in resp.routing["attempts"]] == ["primary", "backup"]
        assert resp.routing["attempts"][0]["failed"] == "ok_false"

    def test_raise_falls_back(self):
        reg = ModelRegistry()
        reg.register(mk("primary", ROLE_GENERAL, priority=10, default=True),
                     (lambda: FakeProvider(model="primary", fail="raise")))
        reg.register(mk("backup", ROLE_GENERAL, priority=20),
                     (lambda: FakeProvider(model="backup")))
        router = make_router(reg)
        resp = router.route(general_task()).chat([ChatMessage(role="user", content="hi")])
        assert resp.succeeded is True
        assert "(backup)" in resp.content

    def test_all_fail_no_fabricated_success(self):
        reg = ModelRegistry()
        reg.register(mk("primary", ROLE_GENERAL, priority=10, default=True),
                     (lambda: FakeProvider(model="primary", fail="ok_false")))
        reg.register(mk("backup", ROLE_GENERAL, priority=20),
                     (lambda: FakeProvider(model="backup", fail="raise")))
        router = make_router(reg)
        resp = router.route(general_task()).chat([ChatMessage(role="user", content="hi")])
        assert resp.succeeded is False
        assert resp.ok is False
        assert resp.routing["succeeded"] is False
        # content must surface the honest last failure — never a fake answer
        assert resp.content == "deliberate provider failure"

    def test_empty_response_triggers_fallback(self):
        reg = ModelRegistry()
        reg.register(mk("primary", ROLE_GENERAL, priority=10, default=True),
                     (lambda: FakeProvider(model="primary", fail="empty")))
        reg.register(mk("backup", ROLE_GENERAL, priority=20),
                     (lambda: FakeProvider(model="backup")))
        router = make_router(reg)
        resp = router.route(general_task()).chat([ChatMessage(role="user", content="hi")])
        assert resp.succeeded is True
        assert "(backup)" in resp.content
        assert resp.routing["fallback_used"] is True

    def test_tool_calls_forwarded_without_fallback(self):
        reg = ModelRegistry()
        tc = ToolCall(id="t1", name="some_tool", arguments={"a": 1})
        reg.register(mk("primary", ROLE_GENERAL, priority=10, default=True),
                     (lambda: FakeProvider(model="primary", tool_calls=[tc])))
        reg.register(mk("backup", ROLE_GENERAL, priority=20),
                     (lambda: FakeProvider(model="backup")))
        router = make_router(reg)
        resp = router.route(general_task()).chat([ChatMessage(role="user", content="hi")])
        assert resp.succeeded is True and resp.ok is True
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "some_tool"
        assert resp.tool_calls[0].arguments == {"a": 1}
        assert resp.routing["fallback_used"] is False

    def test_routed_response_is_an_llm_response(self, pair):
        router = make_router(pair)
        resp = router.route(general_task()).chat([ChatMessage(role="user", content="hi")])
        assert isinstance(resp, LLMResponse)

    def test_unsatisfied_response_is_controlled(self):
        reg = ModelRegistry()
        reg.register(mk("visionless", ROLE_GENERAL, default=True), (lambda: FakeProvider(model="visionless")))
        router = make_router(reg)
        sel = router.route(general_task(task_type=TASK_VISION, requires_vision=True))
        assert sel.satisfied is False
        resp = sel.chat([ChatMessage(role="user", content="describe the image")])
        assert resp.succeeded is False
        assert resp.ok is False
        assert "vision" in resp.content


# --------------------------------------------------------------------------- #
# Provider construction (reuse of llm.router)
# --------------------------------------------------------------------------- #


class TestProviderConstruction:
    def test_build_provider_for_model_knows_family(self):
        assert build_provider_for_model("echo", model="m").name == "echo"
        assert build_provider_for_model("ollama", model="m", base_url="http://localhost:9").name == "ollama"
        o = build_provider_for_model("openai_compatible", model="m", api_key=None)
        assert o.name == "openai_compatible"
        with pytest.raises(ValueError):
            build_provider_for_model("bogus", model="m")

    def test_openai_compatible_missing_key_ok_false(self):
        p = build_provider_for_model("openai_compatible", model="m", api_key=None)
        assert p.is_available() is False
        resp = p.chat([ChatMessage(role="user", content="hi")])
        assert resp.ok is False
        assert "API key" in resp.content

    def test_build_provider_backward_compat(self):
        config = Config()
        config.llm.provider = "echo"
        config.llm.model = "m"
        assert build_provider(config).name == "echo"


# --------------------------------------------------------------------------- #
# Thread Memory isolation & explicit request precedence
# --------------------------------------------------------------------------- #


class TestThreadMemoryIsolation:
    @pytest.fixture()
    def reg(self) -> ModelRegistry:
        return register_standard(ModelRegistry())

    @pytest.fixture()
    def router(self, reg) -> ModelRouter:
        return make_router(reg)

    def test_memory_text_never_sets_explicit_model(self):
        analyzer = TaskAnalyzer(known_model_ids={"code-echo", "reason-echo"})
        # Thread context is NOT an input to analysis; even if the text happened to
        # mention a model id, only the CURRENT user text may set the explicit request.
        tp = analyzer.analyze("hello", context={"memory_context": "previously preferred reason-echo"})
        assert tp.explicit_model_request is None

    def test_explicit_current_wins_over_any_preference(self, reg):
        router = make_router(reg)
        ctx = {"memory_preference": "reason-echo"}      # hypothetical memory signal
        sel = router.route(general_task(explicit_model_request="fast-echo"), context=ctx)
        assert sel.model_id == "fast-echo"
        assert sel.decision == "explicit"

    def test_routing_metadata_has_no_permission_fields(self, reg, router):
        sel = router.route(make_analyzer(reg).analyze("hello there"))
        meta = sel.to_metadata()
        flat = json.dumps(meta)
        assert "permission" not in flat.lower()
        assert "authorize" not in flat.lower()
        assert "approve" not in flat.lower()
        resp = sel.chat([ChatMessage(role="user", content="hi")])
        assert "permission" not in json.dumps(resp.routing).lower()

    def test_selection_cannot_dispatch_tools(self, reg, router):
        sel = router.route(make_analyzer(reg).analyze("hello there"))
        # Routing owns chat(); tool EXECUTION lives in the orchestrator's ToolRegistry
        # (through PermissionGate). A ModelSelection must not expose dispatch.
        assert not hasattr(sel, "dispatch")
        assert not hasattr(sel, "run_tool")
        assert not hasattr(sel.chat, "tool_access")


# --------------------------------------------------------------------------- #
# Orchestrator integration: failure isolation & permission authority
# --------------------------------------------------------------------------- #


class _Memory:
    def __init__(self, memory_type, content, importance=0.7):
        self.memory_type = memory_type
        self.content = content
        self.importance = importance


class StubThreadMemory:
    def __init__(self, memories):
        self._memories = memories

    def retrieve(self, query):
        class R:
            pass
        r = R()
        r.memories = self._memories
        r.scores = [0.9] * len(self._memories)
        return r

    def semantic_search(self, text, top_k):
        return self._memories[:top_k]


class RaisingAnalyzer:
    def analyze(self, text, context=None):
        raise RuntimeError("analyzer down")


class RaisingRouter:
    def route(self, task_profile, context=None):
        raise RuntimeError("router down")


class SensitiveTool(Tool):
    name = "sensitive_thing"
    description = "A SENSITIVE action that must never run without permission."
    permission = PermissionLevel.SENSITIVE
    parameters = {"type": "object", "properties": {}, "required": []}

    def matches(self, text: str) -> bool:
        return False                          # never fast-path

    def extract_arguments(self, text: str) -> dict:
        return {}

    def run(self, **kwargs) -> ToolResult:
        return ToolResult.ok("SENSITIVE_EXECUTED")


class EchoLLM(LLMProvider):
    name = "noop"

    def chat(self, messages, tools=None, temperature=0.4) -> LLMResponse:
        last = next((m.content for m in reversed(messages) if m.role == "user"), "")
        return LLMResponse(content=f"(noop) you said: {last}")


def build_orchestrator(
    tmp_path,
    *,
    llm: LLMProvider,
    router=None,
    analyzer=None,
    thread_memory=None,
    threshold: str = "CRITICAL",
) -> Orchestrator:
    config = Config()
    config.safety.require_confirmation_above = threshold
    gate = PermissionGate(threshold=PermissionLevel[threshold], confirm_callback=None)
    tools = ToolRegistry(gate)
    memory = LongTermMemory(tmp_path / "mem.json")
    return Orchestrator(
        config=config, llm=llm, tools=tools, long_term_memory=memory,
        router=router, analyzer=analyzer, thread_memory=thread_memory,
    )


class TestOrchestratorIntegration:
    def test_routed_handler_selects_model_and_returns_metadata(self, tmp_path):
        reg = register_standard(ModelRegistry())
        orch = build_orchestrator(
            tmp_path, llm=EchoLLM(), router=make_router(reg), analyzer=make_analyzer(reg),
        )
        resp = orch.handle("hello there", orch.new_session())
        assert resp.routing is not None
        assert resp.routing["model_id"] == "fast-echo"   # CHAT -> fast role
        assert resp.routing["succeeded"] is True
        assert "(fast-echo)" in resp.text

    def test_memory_does_not_authorize_model_selection(self, tmp_path):
        # Thread memory recollects a model preference. It must influence NOTHING —
        # MEMORY RETRIEVAL != MODEL SELECTION AUTHORIZATION.
        stub = StubThreadMemory([_Memory("semantic", "The user prefers reason-echo for everything.")])
        reg = register_standard(ModelRegistry())
        orch = build_orchestrator(
            tmp_path, llm=EchoLLM(), router=make_router(reg), analyzer=make_analyzer(reg),
            thread_memory=stub,
        )
        resp = orch.handle("hello there", orch.new_session())
        assert resp.routing["model_id"] == "fast-echo"   # NOT reason-echo from memory
        assert resp.routing["decision"] == "routed"

    def test_routing_never_bypasses_permission_gate(self, tmp_path):
        # The router selects a model; the model asks for a SENSITIVE tool; the gate
        # (SAFE threshold, no interactive confirm) must deny — routing granted nothing.
        reg = ModelRegistry()
        reg.register(
            mk("gen-echo", ROLE_GENERAL, tools=True, priority=10, default=True),
            (lambda: HopProvider([
                LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="sensitive_thing", arguments={})]),
                LLMResponse(content="(gen) done"),
            ])),
        )
        tools_registry = ToolRegistry(
            PermissionGate(threshold=PermissionLevel.SAFE, confirm_callback=None)
        )
        tools_registry.register(SensitiveTool())
        config = Config()
        config.safety.require_confirmation_above = "SAFE"
        memory = LongTermMemory(tmp_path / "mem.json")
        orch = Orchestrator(
            config=config, llm=EchoLLM(), tools=tools_registry, long_term_memory=memory,
            router=make_router(reg), analyzer=make_analyzer(reg),
        )
        resp = orch.handle("do the sensitive thing now", orch.new_session())
        assert "(gen)" in resp.text
        assert "SENSITIVE_EXECUTED" not in resp.text
        assert all("SENSITIVE_EXECUTED" not in r.get("message", "") for r in resp.tool_results)
        # The routed model requested the tool, but the PERMISSION GATE refused it.
        assert any(r.get("success") is False for r in resp.tool_results)
        assert any("SENSITIVE" in r.get("message", "") for r in resp.tool_results)

    def test_analyzer_failure_falls_back_to_primary_llm(self, tmp_path):
        reg = register_standard(ModelRegistry())
        orch = build_orchestrator(
            tmp_path, llm=EchoLLM(), router=make_router(reg), analyzer=RaisingAnalyzer(),
        )
        resp = orch.handle("hello there", orch.new_session())
        assert resp.routing is None
        assert "(noop)" in resp.text            # degraded cleanly to self.llm

    def test_router_failure_falls_back_to_primary_llm(self, tmp_path):
        reg = register_standard(ModelRegistry())
        orch = build_orchestrator(
            tmp_path, llm=EchoLLM(), router=RaisingRouter(), analyzer=make_analyzer(reg),
        )
        resp = orch.handle("hello there", orch.new_session())
        assert resp.routing is None
        assert "(noop)" in resp.text

    def test_explicit_current_instruction_beats_memory(self, tmp_path):
        stub = StubThreadMemory([_Memory("semantic", "Always use gen-echo.")])
        reg = register_standard(ModelRegistry())
        orch = build_orchestrator(
            tmp_path, llm=EchoLLM(), router=make_router(reg), analyzer=make_analyzer(reg),
            thread_memory=stub,
        )
        resp = orch.handle("use code-echo to fix this script", orch.new_session())
        assert resp.routing["model_id"] == "code-echo"
        assert resp.routing["decision"] == "explicit"


# --------------------------------------------------------------------------- #
# Regression: routing disabled == byte-identical single-model behaviour
# --------------------------------------------------------------------------- #


class TestRegression:
    def test_routing_config_disabled_by_default(self):
        assert Config().routing.enabled is False

    def test_build_routing_disabled_returns_none(self):
        assert build_routing(Config()) is None

    def test_build_routing_enabled_echo(self):
        config = Config()
        config.routing.enabled = True
        config.routing.models = [
            ModelEntryConfig(model_id="gen-echo", provider="echo", role="general", default=True),
        ]
        asm = build_routing(config)
        assert asm is not None
        assert len(asm.registry) == 1
        assert asm.router is not None
        assert asm.analyzer is not None

    def test_synthesized_default_declares_tools_for_11_behaviour(self):
        # No explicit routing.models -> the global llm.* model becomes the enabled
        # routing default, and because the single-model path always handed tools to
        # the model, the synthesized default must declare tool support.
        config = Config()
        config.routing.enabled = True
        config.llm.provider = "echo"
        config.llm.model = "single-default"
        asm = build_routing(config)
        p = asm.registry.profile_of("single-default")
        assert p is not None
        assert p.default is True
        assert p.has_capability(CAP_TOOLS)

    def test_duplicate_models_in_config_rejected(self):
        config = Config()
        config.routing.enabled = True
        config.routing.models = [
            ModelEntryConfig(model_id="a", provider="echo", default=True),
            ModelEntryConfig(model_id="a", provider="echo"),
        ]
        with pytest.raises(ValueError):
            build_routing(config)


def _app_config(tmp_path, *, routing_enabled: bool) -> Config:
    config = Config()
    config.llm.provider = "echo"
    config.llm.model = "test-echo"
    config.memory.long_term_path = str(tmp_path / "mem.json")
    config.learning.experience_path = str(tmp_path / "exp.jsonl")
    config.learning.confidence_path = str(tmp_path / "conf.json")
    config.log_file = str(tmp_path / "veronica.log")
    config.routing.enabled = routing_enabled
    if routing_enabled:
        config.routing.models = [ModelEntryConfig(model_id="test-echo", provider="echo", role="general", default=True)]
    return config


class TestBuildAppRegression:
    def test_build_app_routing_disabled(self, tmp_path):
        from veronica.main import build_app
        app = build_app(_app_config(tmp_path, routing_enabled=False), interactive=False)
        assert app.orchestrator.router is None
        assert app.orchestrator.analyzer is None
        resp = app.orchestrator.handle("tell me a story about the ocean", app.orchestrator.new_session())
        assert resp.routing is None                     # pre-routing callers unchanged
        assert resp.text

    def test_build_app_routing_enabled_end_to_end(self, tmp_path):
        from veronica.main import build_app
        app = build_app(_app_config(tmp_path, routing_enabled=True), interactive=False)
        assert app.orchestrator.router is not None
        resp = app.orchestrator.handle("tell me a story about the ocean", app.orchestrator.new_session())
        assert resp.routing is not None
        assert resp.routing["model_id"] == "test-echo"
        assert resp.routing["succeeded"] is True


# --------------------------------------------------------------------------- #
# Config: YAML routing.models coercion
# --------------------------------------------------------------------------- #


class TestConfigCoercion:
    def test_yaml_routing_models_are_typed(self, tmp_path):
        yaml_cfg = tmp_path / "cfg.yaml"
        yaml_cfg.write_text(
            "routing:\n"
            "  enabled: true\n"
            "  max_fallback_attempts: 3\n"
            "  models:\n"
            "    - model_id: fast\n"
            "      provider: echo\n"
            "      role: fast\n"
            "      supports_tools: true\n"
            "      default: true\n",
            encoding="utf-8",
        )
        cfg = load_config(config_path=yaml_cfg, env_path=tmp_path / ".env")
        assert cfg.routing.enabled is True
        assert cfg.routing.max_fallback_attempts == 3
        assert len(cfg.routing.models) == 1
        entry = cfg.routing.models[0]
        assert isinstance(entry, ModelEntryConfig)
        assert entry.model_id == "fast"
        assert entry.provider == "echo"
        assert entry.role == "fast"
        assert entry.supports_tools is True
        assert entry.default is True

    def test_routing_disabled_by_default_config(self):
        assert Config().routing.models == []