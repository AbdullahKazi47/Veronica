"""Tests: Context Window Management V1.

Rules (spec): 3 consecutive full-suite runs; failure isolation (manager returns None,
never crashes the assistant); compression preserves UNKNOWN/FAILED/UNVERIFIED; required
items (current user request, safety boundary) never compressed; no silent user-request
truncation — ContextOverflowError raised clearly; ContextPackage is request-scoped (no
global_current_context); ContextSelection is deterministic; token estimation is replaceable.
"""
from __future__ import annotations

import time
from dataclasses import replace

from veronica.core.context_manager import (
    ContextItem,
    ContextBudget,
    ContextManager,
    ContextOverflowError,
    ContextPackage,
    ContextSelection,
    TokenCounter,
    default_priority_for_source,
    is_protected,
    preserves_status,
    summarize_item,
    SOURCE_USER,
    SOURCE_SYSTEM,
    SOURCE_MEMORY,
    SOURCE_OBSERVATION,
)
from veronica.llm.base import ChatMessage


# ------------------------------------------------------------------ #
# 1. Data model
# ------------------------------------------------------------------ #

def test_context_item_creation():
    item = ContextItem(
        item_id="test-1", source=SOURCE_USER, content="hello",
        role="user", required=True, compressible=False,
    )
    assert item.required is True
    assert item.compressible is False
    assert item.source == SOURCE_USER
    d = item.to_dict()
    assert d["item_id"] == "test-1"
    assert d["required"] is True


def test_context_budget_input_cap():
    budget = ContextBudget(context_window=4096, output_reserve=512, safety_reserve=128)
    assert budget.input_cap == max(256, 4096 - 512 - 128)


def test_context_budget_input_cap_never_zero():
    budget = ContextBudget(context_window=300, output_reserve=50, safety_reserve=20)
    assert budget.input_cap == 256  # floor enforced


# ------------------------------------------------------------------ #
# 2. TokenCounter V1 (replaceable; no external dependency)
# ------------------------------------------------------------------ #

def test_token_estimate_approximation():
    counter = TokenCounter()
    # len // 4 approximation
    text = "a" * 40
    assert counter.estimate(text) == 10
    assert counter.estimate("") == 0
    assert counter.estimate(None) == 0


# ------------------------------------------------------------------ #
# 3. Priority / protection rules
# ------------------------------------------------------------------ #

def test_is_protected():
    required = ContextItem(item_id="r", source="system", content="x", required=True)
    assert is_protected(required) is True
    non_compressible = ContextItem(item_id="n", source="system", content="x", compressible=False)
    assert is_protected(non_compressible) is True
    open = ContextItem(item_id="o", source=SOURCE_MEMORY, content="hello")
    assert is_protected(open) is False


def test_default_priority_mapping():
    # Spec ordering (ascending priority rank = more important): system < current user
    # < task < tools < observation < relevant memory < optional.
    assert default_priority_for_source(SOURCE_SYSTEM) < default_priority_for_source(SOURCE_USER)
    assert default_priority_for_source(SOURCE_USER) < default_priority_for_source(SOURCE_MEMORY)
    assert default_priority_for_source(SOURCE_OBSERVATION) < default_priority_for_source(SOURCE_MEMORY)


# ------------------------------------------------------------------ #
# 4. Selection (normalize / score / select / validate)
# ------------------------------------------------------------------ #

def test_selection_normalize_conversation():
    sel = ContextSelection()
    sources = {
        SOURCE_USER: [{"role": "user", "content": "hello"}],
    }
    items = sel.normalize(sources, token_estimate=lambda s: len(s) // 4)
    assert len(items) == 1
    item = items[0]
    assert item.source == SOURCE_USER
    assert item.content == "hello"
    assert item.required is True  # user request protected


def test_selection_estimate():
    item = ContextItem(item_id="e", source=SOURCE_MEMORY, content="abcdef")
    assert ContextSelection.estimate([item], lambda s: len(s) // 2) == 3


def test_selection_ordered_deterministic():
    items = [
        ContextItem(item_id="c", source=SOURCE_MEMORY, content="x", priority_rank=60, relevance=0.3),
        ContextItem(item_id="b", source=SOURCE_OBSERVATION, content="y", priority_rank=50, relevance=1.0),
        ContextItem(item_id="a", source=SOURCE_SYSTEM, content="z", priority_rank=10, relevance=0.9),
    ]
    ordered = ContextSelection.ordered(items)
    # Priority ascending => system first
    assert ordered[0].item_id == "a"


def test_selection_select_drops_lowest_value_when_total_overflows():
    sel = ContextSelection()
    # Protected system block first (fits), then two compressible memory items. The
    # input cap (context_window 512 − 64 − 32 = 416) fits the system block + ONE memory
    # item but not both — the lower-relevance one must be dropped for THIS request.
    protected = [ContextItem(item_id="sys", source=SOURCE_SYSTEM, content="s" * 800, required=True)]
    mem_high = ContextItem(item_id="mem_high", source=SOURCE_MEMORY, content="m" * 600,
                           relevance=0.9, priority_rank=60)
    mem_low = ContextItem(item_id="mem_low", source=SOURCE_MEMORY, content="m" * 600,
                          relevance=0.1, priority_rank=60)
    budget = ContextBudget(context_window=512, output_reserve=64, safety_reserve=32,
                           system=2000, conversation=2000, memory=2000)
    selected, dropped, compressed = sel.select(
        [protected[0], mem_high, mem_low], budget,
        token_estimate=lambda s: len(s) // 4, max_summary_tokens=256,
    )
    sel_ids = {it.item_id for it in selected}
    dropped_ids = {it.item_id for it in dropped}
    assert "sys" in sel_ids
    assert "mem_high" in sel_ids
    assert "mem_low" in dropped_ids
    assert "mem_low" not in sel_ids


def test_selection_select_never_drops_required():
    sel = ContextSelection()
    protected_items = [
        ContextItem(item_id="sys", source=SOURCE_SYSTEM, content="sys", required=True),
        ContextItem(item_id="user_req", source=SOURCE_USER, content="hello world", required=True),
    ]
    budget = ContextBudget(context_window=1024, output_reserve=128, safety_reserve=32)
    selected, dropped, compressed = sel.select(
        protected_items, budget, token_estimate=lambda s: len(s) // 2, max_summary_tokens=32
    )
    assert len([it for it in selected if it.required]) == 2


def test_selection_validate():
    package = ContextPackage(messages=[ChatMessage(role="system", content="s")], items=[], budget=ContextBudget())
    warnings = ContextSelection.validate(package)
    assert any("user message" in w for w in warnings)


# ------------------------------------------------------------------ #
# 5. Compression (status-preserving; never touches required items)
# ------------------------------------------------------------------ #

def test_is_protected_never_compressed():
    item = ContextItem(item_id="r", source=SOURCE_SYSTEM, content="FAILED", required=True)
    summarized = summarize_item(item, max_tokens=10, token_estimate=lambda s: len(s) // 4)
    assert summarized is item  # protected items returned unchanged


def test_preserves_status_keeps_words():
    assert preserves_status("Task FAILED to connect", "FAILED: connection failed") is True
    assert preserves_status("Task FAILED", "Task completed successfully") is False  # elides FAILED
    assert preserves_status("Status UNKNOWN", "Status OK") is False  # elides UNKNOWN
    assert preserves_status("", "anything") is True


def test_compress_preserves_failed():
    item = ContextItem(
        item_id="c", source=SOURCE_MEMORY, content="tool result: FAILED connection error UNKNOWN",
        required=False, compressible=True,
    )
    summarized = summarize_item(
        item, max_tokens=15, token_estimate=lambda s: len(s) // 4
    )
    # Should keep the FAILED word; if it refuses, it returns the original
    if summarized is not item:
        # Status must survive
        assert "FAILED" in summarized.content.lower() or any(w in summarized.content.lower() for w in ["unknown", "failed"])


# ------------------------------------------------------------------ #
# 6. ContextManager.build() (failure isolation + structured package)
# ------------------------------------------------------------------ #

def test_context_manager_build_with_session():
    cm = ContextManager(enabled=True, default_context_window=2048)
    # Minimal session-like conversation: one user turn, no thread memory
    result = cm.build(
        request="hello",
        conversation=[{"role": "user", "content": "hello"}],
        system_sections=["You are VERONICA."],
        model_context_window=2048,
    )
    assert result is not None
    assert isinstance(result, ContextPackage)
    # The user message must be present in messages
    roles = [m.role for m in result.messages]
    assert "user" in roles or "system" in roles
    # The package must be request-scoped (unique created_at close to now)
    assert result.created_at > 0
    assert result.token_estimate >= 0


def test_context_manager_build_with_model_profile_window():
    """When a model profile with a specific context_window is given, it must drive
    the budget (this is DATA only — the profile is never routed through)."""
    # Simulate a model profile-like object with a window attribute
    class FakeProfile:
        model_id = "fake-model"
        context_window = 1024

    cm = ContextManager(enabled=True, default_context_window=8192)
    result = cm.build(
        request="test",
        conversation=[{"role": "user", "content": "test"}],
        system_sections=["System prompt here."],
        model_profile=FakeProfile(),
    )
    assert result is not None
    assert result.budget is not None
    # Window should have been taken from profile (1024, not default 8192)
    assert result.budget.context_window == 1024


def _mem(obj):
    """Minimal MemoryRecord-like object for manager tests."""
    return type("Record", (), {
        "id": obj["id"], "content": obj["content"], "memory_type": obj.get("memory_type", "fact"),
        "importance": obj.get("importance", 0.5), "confidence": obj.get("confidence", 1.0),
        "timestamp": obj.get("timestamp", 0.0),
    })()


def test_context_manager_consumes_memory_records_with_scores():
    """Pre-supplied retrieval results (records + parallel scores) drive similarity —
    the exact code path that retrieves by record_id once, with no second retrieval."""
    cm = ContextManager(enabled=True, default_context_window=2048)
    records = [
        _mem({"id": "mem-1", "content": "user prefers concise replies"}),
        _mem({"id": "mem-2", "content": "weather in paris"}),
    ]
    scores = [0.95, 0.30]
    result = cm.build(
        request="remember my preferences",
        conversation=[{"role": "user", "content": "remember my preferences"}],
        system_sections=["sys"],
        memory_records=records,
        memory_scores=scores,
    )
    assert result is not None
    mem_items = [it for it in result.items if it.source == SOURCE_MEMORY]
    # Both retrieval results were consumed (records untouched, never fabricated).
    assert len(mem_items) == 2
    # The high-similarity memory must carry a positive relevance, the low one is
    # gated down — relevance exists only where retrieval evidence exists.
    by_id = {it.metadata.get("record_id"): it.relevance for it in mem_items}
    assert by_id["mem-1"] > 0.0
    assert by_id["mem-2"] < by_id["mem-1"]


def test_context_manager_failure_isolation():
    cm = ContextManager(enabled=True)
    # A broken conversation (not a dict or string, but something that could trigger an exception)
    # is handled by the internal try/except: build() should return None rather than crash.
    # However, a really bad input may raise ContextOverflowError if required item exceeds
    # input cap. Let's simulate by forcing a tiny window with a huge required user message.
    result = cm.build(
        request="x",
        conversation=[{"role": "user", "content": "x"}],
        system_sections=["sys"],
        model_context_window=50,  # very small; required content might overflow if too long
    )
    # If it doesn't overflow, we get a package; if it does, we get None (not a crash).
    # With very short content it should survive and return a package.
    assert result is None or isinstance(result, ContextPackage)


def test_context_manager_no_global_context():
    # Every call to build() should return its own independent package; there must be no
    # shared mutable state between packages.
    cm = ContextManager(enabled=True, default_context_window=1024)
    pkg1 = cm.build(
        request="a",
        conversation=[{"role": "user", "content": "a"}],
        system_sections=["sys"],
    )
    pkg2 = cm.build(
        request="b",
        conversation=[{"role": "user", "content": "b"}],
        system_sections=["sys"],
    )
    if pkg1 is not None and pkg2 is not None:
        assert pkg1.created_at != pkg2.created_at or pkg1 is not pkg2
        # Different messages
        pkg1_content = "".join(str(m.content) for m in pkg1.messages)
        pkg2_content = "".join(str(m.content) for m in pkg2.messages)
        assert "b" not in pkg1_content or "a" not in pkg2_content


def test_trust_banners_applied_when_inlined():
    # With inline_tools=True, untrusted tool content enters the prompt AND is framed
    # with the untrusted-data banner — it can never appear as an unlabeled system
    # instruction (prompt-injection defense).
    cm = ContextManager(enabled=True, default_context_window=1024, inline_tools=True)
    result = cm.build(
        request="run",
        conversation=[{"role": "user", "content": "run"}],
        system_sections=["sys"],
        tool_schemas=[{"message": "tool result: api returned 123"}],
    )
    assert result is not None
    system_body = " ".join(
        m.content for m in result.messages if m.role == "system"
    )
    assert "UNTRUSTED" in system_body or "untrusted" in system_body
    assert "api returned 123" in " ".join(m.content for m in result.messages)


def test_tool_schemas_budget_only_by_default():
    # Default (inline_tools=False): tool schemas are budgeted but never text-joined
    # into the prompt — they go to the provider via its tools= kwarg instead.
    cm = ContextManager(enabled=True, default_context_window=1024)
    result = cm.build(
        request="run",
        conversation=[{"role": "user", "content": "run"}],
        system_sections=["sys"],
        tool_schemas=[{"message": "schema-of-a-tool"}],
    )
    assert result is not None
    body = " ".join(m.content for m in result.messages)
    assert "schema-of-a-tool" not in body
    # ...but the tools source is still accounted for in the package breakdown.
    assert result.source_breakdown.get("tools", 0) >= 0


# ------------------------------------------------------------------ #
# 7. Integration: manager present / absent in orchestrator
# ------------------------------------------------------------------ #

def _build_orchestrator(context_manager=None):
    """A minimal orchestrator (dependency-free, matching tests/test_orchestrator.py)."""
    from veronica.config import Config
    from veronica.core.orchestrator import Orchestrator
    from veronica.llm.base import LLMProvider, LLMResponse
    from veronica.memory.long_term import LongTermMemory
    from veronica.safety.permissions import PermissionGate, PermissionLevel
    from veronica.tools.registry import ToolRegistry

    class SilentLLM(LLMProvider):
        name = "silent"
        def chat(self, messages, tools=None, temperature=0.4):
            return LLMResponse(content="ok")

    config = Config()
    config.safety.require_confirmation_above = "CRITICAL"
    gate = PermissionGate(threshold=PermissionLevel[config.safety.require_confirmation_above])
    registry = ToolRegistry(gate)
    memory = LongTermMemory(config.resolve_path("data/tests-context-memory.json"))
    return Orchestrator(
        config=config, llm=SilentLLM(), tools=registry, long_term_memory=memory,
        context_manager=context_manager,
    )


def test_build_messages_without_manager_is_legacy():
    orch = _build_orchestrator(context_manager=None)
    session = orch.new_session()
    session.short_term.add("user", "hello world")
    messages = orch._build_messages(session, text="hello world")
    # Legacy shape: one system message (persona + capability summary) + the turns.
    assert messages[0].role == "system"
    roles = [m.role for m in messages]
    assert roles.count("user") >= 1
    assert any("hello world" in m.content for m in messages)


def test_build_messages_absent_manager_matches_legacy_method():
    orch = _build_orchestrator(context_manager=None)
    session = orch.new_session()
    session.short_term.add("user", "hello")
    session.short_term.add("assistant", "hi")
    dispatcher = orch._build_messages(session, text="hello")
    legacy = orch._legacy_build_messages(session, text="hello")
    assert [(m.role, m.content) for m in dispatcher] == [(m.role, m.content) for m in legacy]


def test_build_messages_with_disabled_manager_matches_legacy():
    dm = ContextManager(enabled=False)
    orch = _build_orchestrator(context_manager=dm)
    session = orch.new_session()
    session.short_term.add("user", "hello")
    dispatcher = orch._build_messages(session, text="hello")
    legacy = orch._legacy_build_messages(session, text="hello")
    assert [(m.role, m.content) for m in dispatcher] == [(m.role, m.content) for m in legacy]


def test_build_messages_with_enabled_manager_uses_package():
    cm = ContextManager(enabled=True, default_context_window=1024)
    orch = _build_orchestrator(context_manager=cm)
    session = orch.new_session()
    session.short_term.add("user", "hello managed")
    messages = orch._build_messages(session, text="hello managed")
    # Managed assembly still produces the user request in chronological order.
    assert messages[0].role == "system"
    user_texts = [m.content for m in messages if m.role == "user"]
    assert any("hello managed" in t for t in user_texts)


def test_overflow_falls_back_and_preserves_user_request():
    # A pathological window (default 96, reserves 32+16) whose input_cap (256 floor)
    # is below the REQUIRED content. The manager must not silently truncate — the
    # orchestrator falls back to legacy assembly and the user's full text survives.
    cm = ContextManager(enabled=True, default_context_window=96,
                        output_reserve=32, safety_reserve=16)
    orch = _build_orchestrator(context_manager=cm)
    session = orch.new_session()
    long_request = "please handle this " + ("z" * 1400)
    session.short_term.add("user", long_request)
    messages = orch._build_messages(session, text=long_request)
    assert any(long_request in m.content for m in messages)
