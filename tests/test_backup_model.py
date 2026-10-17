"""Groq (or any other service) as a backup brain when Gemini fails."""
from __future__ import annotations

import time

import pytest

from veronica.config import Config, ModelEntryConfig
from veronica.llm.base import ChatMessage, LLMProvider, LLMResponse
from veronica.llm.router import FallbackProvider
from veronica.llm.routing.health import HealthState, ModelHealth


class Scripted(LLMProvider):
    def __init__(self, name, replies):
        self.model, self.replies, self.calls = name, list(replies), 0
    def is_available(self): return True
    def chat(self, messages, tools=None, temperature=0.4, voice_mode=False):
        self.calls += 1
        r = self.replies.pop(0) if self.replies else self.replies_default
        return r
    replies_default = LLMResponse(content="fine", ok=True)


BUSY = LLMResponse(content="Google's AI service is overloaded", ok=False)
MSG = [ChatMessage(role="user", content="hi")]


# ---------------------------------------------------------------- direct calls

def test_backup_answers_when_main_fails():
    main, groq = Scripted("gemini", [BUSY]), Scripted("groq", [LLMResponse(content="from groq")])
    fp = FallbackProvider(main, [groq])
    assert fp.chat(MSG).content == "from groq"


def test_after_a_failure_the_backup_goes_first_then_main_recovers():
    now = [1000.0]
    main = Scripted("gemini", [BUSY, LLMResponse(content="gemini back")])
    groq = Scripted("groq", [LLMResponse(content="g1"), LLMResponse(content="g2")])
    fp = FallbackProvider(main, [groq], cooldown_s=45, clock=lambda: now[0])
    fp.chat(MSG)                                  # main fails -> groq
    assert fp.chat(MSG).content == "g2" and main.calls == 1   # within cooldown: main not even tried
    now[0] += 60
    assert fp.chat(MSG).content == "gemini back"  # cooldown over: main again


def test_everything_down_returns_the_failure():
    fp = FallbackProvider(Scripted("a", [BUSY]), [Scripted("b", [BUSY])])
    res = fp.chat(MSG)
    assert not res.ok


def test_build_provider_adds_backup_only_with_a_key(monkeypatch):
    from veronica.llm import router
    cfg = Config()
    cfg.llm.provider, cfg.llm.model = "echo", "echo"
    cfg.llm.backup = {"provider": "echo", "model": "b", "base_url": "", "api_key_env": "NO_SUCH_KEY_X"}
    monkeypatch.setattr(cfg, "secret", lambda name: None)
    assert not isinstance(router.build_provider(cfg), FallbackProvider)     # no key -> no backup
    cfg.llm.backup = {"provider": "echo", "model": "b"}
    assert isinstance(router.build_provider(cfg), FallbackProvider)


# ---------------------------------------------------------------- health cooldown

def test_outage_expires_after_cooldown():
    h = ModelHealth(model_id="m")
    h.record_failure("503"); h.record_failure("503")
    assert h.state is HealthState.UNAVAILABLE and h.recently_failed() and not h.retry_due()
    h.checked_at = time.time() - 60
    assert h.retry_due() and not h.recently_failed()


# ---------------------------------------------------------------- routing chain

def _routing(models):
    from veronica.llm.routing.builder import build_routing
    cfg = Config()
    cfg.routing.enabled = True
    cfg.routing.max_fallback_attempts = 3
    cfg.routing.models = models
    return build_routing(cfg)


def _entry(mid, role, **kw):
    return ModelEntryConfig(model_id=mid, provider="echo", role=role, supports_tools=True, **kw)


@pytest.fixture
def assembly():
    return _routing([
        _entry("flash-lite", "fast", priority=10, default=True),
        _entry("flash", "reasoning", priority=20, task_types=("reasoning",)),
        _entry("groq-20b", "backup", priority=50, reasoning_class="light"),
        _entry("groq-120b", "backup", priority=60, reasoning_class="heavy"),
    ])


def _task(kind="chat"):
    from types import SimpleNamespace
    return SimpleNamespace(task_type=kind, complexity="low", estimated_context_size=0,
                           requires_tools=False, requires_vision=False, requires_streaming=False,
                           explicit_model_request=None, latency_sensitive=False)


def test_backups_come_first_among_fallbacks(assembly):
    sel = assembly.router.route(_task("chat"))
    assert sel.model_id == "flash-lite" and sel.fallback_ids[:2] == ["groq-20b", "groq-120b"]
    assert "flash" in sel.fallback_ids


def test_reasoning_prefers_the_strong_backup(assembly):
    sel = assembly.router.route(_task("reasoning"))
    assert sel.model_id == "flash" and sel.fallback_ids[0] == "groq-120b"


def test_backup_leads_while_main_is_failing(assembly):
    h = assembly.registry.get("flash-lite").health
    h.record_failure("503")
    sel = assembly.router.route(_task("chat"))
    assert sel.model_id == "groq-20b" and "flash-lite" in sel.fallback_ids
    h.checked_at = time.time() - 60          # outage over
    assert assembly.router.route(_task("chat")).model_id == "flash-lite"


def test_backup_never_leads_while_main_is_fine(assembly):
    for kind in ("chat", "general", "tool_use", "reasoning"):
        assert assembly.registry.get(assembly.router.route(_task(kind)).model_id).profile.role != "backup"
