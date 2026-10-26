"""Understanding what the user MEANS: paraphrases reach the same action — no model, no network."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from tests.paraphrase_set import AMBIGUOUS, CHAT, INTENTS
from veronica.core.intent_catalogue import CATALOGUE, build_intents
from veronica.core.intents import IntentRouter, normalise, slot_app, slot_name, vague_verb
from veronica.safety.permissions import PermissionLevel

# Tool shapes the router needs (name, permission, required args) — the real ones' values.
_TOOLS = {
    "get_news": ("SAFE", []), "calendar": ("SAFE", []), "close_window": ("SENSITIVE", ["app"]),
    "set_volume": ("MODERATE", []), "open_path": ("MODERATE", []), "open_application": ("MODERATE", ["app_name"]),
    "get_weather": ("SAFE", []), "get_current_time": ("SAFE", []), "run_diagnostics": ("SAFE", []),
    "system_status": ("SAFE", []), "look_at_screen": ("MODERATE", []), "fix_problem": ("MODERATE", []),
    "apply_fix": ("MODERATE", []), "fix_history": ("SAFE", []), "window_control": ("MODERATE", ["action"]),
    "lock_pc": ("MODERATE", []), "list_tabs": ("SAFE", []), "close_tab": ("SENSITIVE", ["scope"]),
    "list_reminders": ("SAFE", []), "daily_briefing": ("SAFE", []),
}


class FakeTool:
    def __init__(self, name, level, required, regex=None):
        self.name = name
        self.permission = getattr(PermissionLevel, level)
        self.parameters = {"type": "object", "properties": {}, "required": required}
        self._regex = regex

    def matches(self, text):
        return bool(self._regex and self._regex(text))

    def extract_arguments(self, text):
        return {}


class FakeRegistry:
    """No exact rules unless a test gives one: this measures the meaning layer by itself."""

    def __init__(self, regex: dict | None = None):
        self.tools = {n: FakeTool(n, lvl, req, (regex or {}).get(n)) for n, (lvl, req) in _TOOLS.items()}

    def fast_match(self, text):
        for t in self.tools.values():
            if t.matches(text):
                return t, text
        return None, text

    def get(self, name):
        return self.tools.get(name)

    def all(self):
        return list(self.tools.values())


@pytest.fixture(scope="module")
def router():
    return IntentRouter(build_intents(FakeRegistry().all()), find_window=lambda app: (1, app))


def _right(decision, tool):
    if decision.kind == "tool":
        return decision.tool.name == tool
    return decision.kind == "model" and decision.hint.split("Most likely: ")[-1].startswith(tool)


# ---------------------------------------------------------------- the paraphrase suite

def test_paraphrases_reach_the_right_action_at_least_90_percent(router):
    reg = FakeRegistry()
    results = {name: [_right(router.decide(p, reg), tool) for p in phrases] for name, (tool, phrases) in INTENTS.items()}
    total = sum(len(v) for v in results.values())
    right = sum(sum(v) for v in results.values())
    misses = {n: [INTENTS[n][1][i] for i, ok in enumerate(v) if not ok] for n, v in results.items() if not all(v)}
    assert len(INTENTS) >= 15 and all(len(v[1]) >= 10 for v in INTENTS.values())
    assert right / total >= 0.90, (right, total, misses)


def test_small_talk_never_triggers_a_tool(router):
    reg = FakeRegistry()
    acted = [(p, router.decide(p, reg).tool.name) for p in CHAT if router.decide(p, reg).kind == "tool"]
    assert len(CHAT) >= 20 and acted == []


def test_ambiguous_lines_get_a_question_and_never_a_risky_action(router):
    reg = FakeRegistry()
    decisions = [(p, router.decide(p, reg)) for p in AMBIGUOUS]
    risky = [(p, d.tool.name) for p, d in decisions if d.kind == "tool" and d.tool.permission >= PermissionLevel.SENSITIVE]
    asked = [p for p, d in decisions if d.kind == "ask"]
    assert len(AMBIGUOUS) >= 10 and risky == []
    assert len(asked) >= 9, [p for p, d in decisions if d.kind != "ask"]
    assert all(d.reply.endswith("?") for _p, d in decisions if d.kind == "ask")


@pytest.mark.parametrize("group", [
    ["what's the news today", "what's happening around the world", "catch me up", "duniya mein kya chal raha hai"],
    ["shut the browser", "get rid of that chrome window", "chrome band karo", "close chrome"],
    ["it's too loud", "turn it down", "lower the volume", "volume kam karo"],
    ["what's my day like", "anything on my calendar?", "am I free today"],
    ["open placement", "show me my placement folder", "take me to placement"],
])
def test_the_users_own_examples_behave_identically(router, group):
    reg = FakeRegistry()
    tools = {router.decide(p, reg).tool.name if router.decide(p, reg).kind == "tool" else "?" for p in group}
    assert len(tools) == 1 and "?" not in tools, {p: router.decide(p, reg).kind for p in group}


def test_folder_paraphrases_carry_the_same_folder_name(router):
    reg = FakeRegistry()
    names = {router.decide(p, reg).args.get("name") for p in ("open placement", "show me my placement folder", "take me to placement")}
    assert names == {"placement"}


def test_volume_paraphrases_carry_the_direction(router):
    reg = FakeRegistry()
    assert {router.decide(p, reg).args.get("action") for p in ("it's too loud", "volume kam karo", "turn it down")} == {"down"}
    assert router.decide("awaaz badhao", reg).args.get("action") == "up"


# ---------------------------------------------------------------- exact rules stay first

def test_a_regex_match_is_kept_when_meaning_agrees_or_is_unsure(router):
    reg = FakeRegistry(regex={"set_volume": lambda t: "volume" in t})
    d = router.decide("set the volume to 30", reg)
    assert d.kind == "tool" and d.tool.name == "set_volume" and d.why == "regex"


def test_a_greedy_regex_is_overruled_when_the_meaning_clearly_differs(router):
    """The real bug: 'what's happening around the world today' ran the calendar (because of 'today')."""
    reg = FakeRegistry(regex={"calendar": lambda t: "today" in t})
    d = router.decide("okay tell me what's happening around the world today", reg)
    assert d.kind == "tool" and d.tool.name == "get_news" and "overrules" in d.why


def test_sensitive_actions_are_not_taken_on_a_medium_guess(router):
    reg = FakeRegistry()
    d = router.decide("get rid of all of it", reg)
    assert not (d.kind == "tool" and d.tool.permission >= PermissionLevel.SENSITIVE)


# ---------------------------------------------------------------- pieces

def test_hinglish_and_synonyms_are_normalised():
    assert "close" in normalise("Chrome band karo")
    assert "lower" in normalise("volume kam karo")
    assert "world" in normalise("duniya mein kya chal raha hai") and "happening" in normalise("kya chal raha hai")
    assert "lower volume" in normalise("it's too loud")


def test_slots():
    assert slot_name("take me to placement") == "placement"
    assert slot_name("show me my placement folder") == "placement"
    assert slot_app("get rid of that Chrome window", find_window=lambda a: (1, a)) == "Chrome"
    assert slot_app("chrome band karo", find_window=lambda a: (1, a)) == "chrome"
    assert slot_app("shut the browser", browser_names=lambda: ["Edge"]) == "Edge"
    assert slot_app("shut the browser", browser_names=lambda: ["Edge", "Chrome"]) == ""     # which one? the model asks
    assert slot_app("close zork", find_window=lambda a: None) == ""                       # no such window open


def test_vague_requests_name_the_missing_object():
    assert vague_verb("close everything") == "close"
    assert vague_verb("turn it off") == "turn off"
    assert vague_verb("close chrome") == ""


def test_the_catalogue_is_rich_enough():
    for entry in CATALOGUE:
        assert len(entry["examples"]) >= (6 if entry["tool"] else 20), entry["name"]
    assert len([e for e in CATALOGUE if e["tool"]]) >= 15


def test_tools_can_add_their_own_intents():
    tool = SimpleNamespace(name="feed_cat", intents=[{"name": "feed", "examples": ["feed the cat", "the cat is hungry"]}])
    intents = build_intents([tool])
    assert any(i.name == "feed" and i.tool == "feed_cat" for i in intents)
    assert not any(i.tool == "get_news" for i in intents)          # tools that don't exist are left out


def test_matching_is_fast(router):
    m = router.matcher
    t0 = time.perf_counter()
    for p in [p for _t, ps in INTENTS.values() for p in ps]:
        m.rank(p)
    per = (time.perf_counter() - t0) * 1000 / sum(len(ps) for _t, ps in INTENTS.values())
    assert per < 25, f"{per:.1f} ms per request"


# ---------------------------------------------------------------- learning from corrections

def test_a_correction_teaches_the_phrasing_and_it_is_kept(tmp_path):
    path = tmp_path / "intent_examples.json"
    r = IntentRouter(build_intents(FakeRegistry().all()), examples_path=path)
    reg = FakeRegistry()
    before = r.decide("give me the lowdown", reg)
    assert not (before.kind == "tool" and before.tool.name == "get_news")
    assert r.learn("give me the lowdown", "world_news")
    assert r.decide("give me the lowdown", reg).tool.name == "get_news"
    again = IntentRouter(build_intents(FakeRegistry().all()), examples_path=path)       # after a restart
    assert again.decide("give me the lowdown", reg).tool.name == "get_news"
    assert r.intent_for_tool("set_volume", {"action": "down"}) == "volume_down"


# ---------------------------------------------------------------- the orchestrator uses it

def test_the_orchestrator_routes_a_paraphrase_without_the_model(tmp_path):
    from veronica.core.orchestrator import Orchestrator  # noqa: F401 - the real wiring is exercised below
    from veronica.core.intents import Decision

    class Router:
        def decide(self, text, reg):
            return Decision("ask", reply="Do you mean the news, or what's on your calendar?")

    from veronica.core import orchestrator as orch_mod
    o = orch_mod.Orchestrator.__new__(orch_mod.Orchestrator)
    o._intents = Router()
    o.config = SimpleNamespace(understanding=SimpleNamespace(enabled=True, intents=True))
    o.tools = FakeRegistry()
    o.tool_event_callback = None
    import threading
    o._turn = threading.local()
    session = SimpleNamespace(short_term=SimpleNamespace(add=lambda *a: None), last_tool_used=None)
    res = o._quick_route("anything new today", session)
    assert res.text.startswith("Do you mean") and res.tool_used is None
