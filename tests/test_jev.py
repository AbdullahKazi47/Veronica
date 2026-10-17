"""Jev, the decision layer: the model scores, the code ranks."""
from __future__ import annotations

import json

import pytest

from veronica.core.jev import Jev, explain_decision, explain_route, say_decision, score
from veronica.tools.jev_tools import JevDecideTool, JevRouteTool, JevWhyTool

ANALYSIS = {
    "options": ["Metro", "Auto"],
    "criteria": [{"name": "cost", "weight": 3}, {"name": "time", "weight": 5}, {"name": "comfort", "weight": 1}],
    "scores": {"Metro": {"cost": 9, "time": 7, "comfort": 6}, "Auto": {"cost": 4, "time": 8, "comfort": 7}},
    "pros": {"Metro": ["Cheap and predictable in traffic"]}, "cons": {"Metro": ["Crowded at 9 am"]},
    "risk": "a breakdown on your line", "would_change": "You were carrying heavy luggage",
}


def test_ranking_is_computed_not_asserted():
    totals, ranking = score(ANALYSIS)
    # Metro: (9*3 + 7*5 + 6*1) / 9 = 7.6 ; Auto: (4*3 + 8*5 + 7*1) / 9 = 6.6
    assert totals == {"Metro": 7.6, "Auto": 6.6} and ranking == ["Metro", "Auto"]


def test_scores_are_clamped_and_missing_ones_ignored():
    data = {"options": ["A", "B"], "criteria": [{"name": "x", "weight": 99}, {"name": "y", "weight": 1}],
            "scores": {"A": {"x": 50, "y": "n/a"}, "B": {"x": 2}}}
    totals, ranking = score(data)
    assert totals == {"A": 10.0, "B": 2.0} and ranking[0] == "A"


def _jev(tmp_path, replies):
    replies = list(replies)
    return Jev(ask=lambda s, u: replies.pop(0), log_path=tmp_path / "decisions.json")


def test_decide_and_speak(tmp_path):
    jev = _jev(tmp_path, [json.dumps(ANALYSIS)])
    d = jev.decide("metro or auto to work?")
    said = say_decision(d)
    assert said.startswith("Jev here. I'd go with Metro: 7.6 out of 10 against Auto's 6.6 — cheap and predictable")
    assert "The catch: a breakdown on your line." in said
    assert "I'd change my mind if you were carrying heavy luggage." in said
    assert not say_decision(d, as_jev=False).startswith("Jev here")


def test_close_call_is_reported_as_such(tmp_path):
    close = dict(ANALYSIS, scores={"Metro": {"cost": 7, "time": 7, "comfort": 7},
                                   "Auto": {"cost": 7, "time": 7, "comfort": 6}})
    d = _jev(tmp_path, [json.dumps(close)]).decide("x")
    assert d.close_call and "close call" in say_decision(d)


def test_bad_json_is_retried_then_given_up(tmp_path):
    good = _jev(tmp_path, ["no json here", "// comment\n" + json.dumps(ANALYSIS)])
    assert good.decide("q").ranking == ["Metro", "Auto"]
    bad = _jev(tmp_path, ["nope", '{"options": ["only one"], "criteria": []}'])
    with pytest.raises(RuntimeError):
        bad.decide("q")


def test_why_explains_the_numbers(tmp_path):
    jev = _jev(tmp_path, [json.dumps(ANALYSIS)])
    jev.decide("metro or auto?")
    why = JevWhyTool(jev)
    assert why.matches("Jev, why?") and why.matches("why did you pick that")
    text = why.run().message
    assert "cost (weight 3)" in text and "Metro: 7.6 overall" in text and "weighted average I calculate" in text
    assert "Crowded at 9 am" in explain_decision(jev.last())


def test_why_needs_a_recent_decision(tmp_path):
    assert not JevWhyTool(_jev(tmp_path, [])).matches("why?")


@pytest.mark.parametrize("text,expect", [
    ("Jev, should I take the metro or an auto?", "should I take the metro or an auto?"),
    ("jeff which laptop should I buy", "which laptop should I buy"),
    ("help me choose between a Pixel and an iPhone", "help me choose between a Pixel and an iPhone"),
    ("compare Python and JavaScript for a beginner", "compare Python and JavaScript for a beginner"),
    ("should I study tonight or tomorrow morning", "should I study tonight or tomorrow morning"),
])
def test_decide_phrases(tmp_path, text, expect):
    tool = JevDecideTool(_jev(tmp_path, []))
    assert tool.matches(text) and tool.extract_arguments(text)["question"] == expect


@pytest.mark.parametrize("text", ["open chrome", "what's the weather", "should I", "minimise all"])
def test_not_decisions(tmp_path, text):
    assert not JevDecideTool(_jev(tmp_path, [])).matches(text)


def test_route_explanation_for_quick_command_and_model():
    from veronica.safety.permissions import PermissionGate
    from veronica.tools.registry import build_default_registry
    reg = build_default_registry(PermissionGate())
    tool = JevRouteTool(reg)
    assert tool.matches("Jev, how would you handle 'minimise all'?")
    msg = tool.run(**tool.extract_arguments("Jev, how would you handle 'minimise all'?")).message
    assert "window control" in msg and "no cloud model needed" in msg
    assert "ask you first" in explain_route("shut down my pc", reg)
    assert "main language model" in explain_route("write me a poem about rain", reg)


def test_routing_registry_order():
    from veronica.safety.permissions import PermissionGate
    from veronica.tools.registry import build_default_registry
    reg = build_default_registry(PermissionGate())
    assert reg.find_fast_match("Jev, should I buy a Pixel or an iPhone?").name == "jev_decide"
    assert reg.find_fast_match("jev how would you handle open notepad").name == "jev_route"


def test_no_doubled_if(tmp_path):
    d = _jev(tmp_path, [json.dumps(dict(ANALYSIS, would_change="If your office moved to Whitefield."))]).decide("q")
    s = say_decision(d)
    assert "if if" not in s.lower() and "I'd change my mind if your office moved to Whitefield." in s
