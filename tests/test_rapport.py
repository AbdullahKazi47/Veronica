"""She gets to know the user slowly, and her manner follows (memory/rapport.py)."""
import json

from veronica.memory.rapport import Rapport, _parse
from veronica.tools.memory_tools import AboutMeTool, ResetPersonalityTool, _ABOUT_ME, _RESET_PERSONALITY


def _model(about, style, seen):
    def ask(system, user):
        seen.append(user)
        return "Sure:\n" + json.dumps({"about": about, "style": style})
    return ask


def test_profile_updates_only_after_enough_exchanges(tmp_path):
    seen = []
    r = Rapport(tmp_path / "r.json", ask=_model(["Studies networking"], ["Keep answers short."], seen), every_turns=3)
    assert r.observe("what is subnetting exactly", "It's splitting a network.") is None
    assert r.observe("thanks", "Anytime.") is None                       # too short to say anything
    assert r.observe("explain a VLAN in one line", "A virtual LAN.") is None
    t = r.observe("too long, just give me the gist", "Fair, short then.")
    t.join()
    assert len(seen) == 1 and "subnetting" in seen[0]
    assert r.data["style"] == ["Keep answers short."] and r.data["pending"] == []
    again = Rapport(tmp_path / "r.json")                                   # kept across restarts
    assert again.data["about"] == ["Studies networking"]
    block = again.prompt_block()
    assert "Keep answers short." in block and "never overrides" in block


def test_secrets_are_never_collected(tmp_path):
    r = Rapport(tmp_path / "r.json", is_secret=lambda t: "password" in t)
    r.observe("my password is sunflower okay", "I won't keep that.")
    assert r.data["pending"] == []


def test_a_bad_model_answer_changes_nothing(tmp_path):
    r = Rapport(tmp_path / "r.json", ask=lambda s, u: "I can't do that.", every_turns=1)
    r.data["style"] = ["Be warm."]
    r.observe("tell me something nice today", "You're doing well.").join()
    assert r.data["style"] == ["Be warm."] and len(r.data["pending"]) == 1   # kept for the next try


def test_parse_caps_and_cleans():
    about, style = _parse(json.dumps({"about": ["a"] * 3 + [f"n{i}" for i in range(20)], "style": ["  x  y "]}))
    assert about[0] == "a" and about.count("a") == 1 and len(about) == 12
    assert style == ["x y"]
    assert _parse("no json here") == (None, None)


def test_voice_commands(tmp_path):
    r = Rapport(tmp_path / "r.json")
    assert "still getting to know you" in AboutMeTool(rapport=r).run().message
    r.data.update(about=["Loves football"], style=["Use light humour."])
    said = AboutMeTool(rapport=r).run().message
    assert "Loves football" in said and "Use light humour" in said
    assert ResetPersonalityTool(rapport=r).run().success and r.data["about"] == [] and r.prompt_block() == ""
    for s in ("what have you learned about me", "how well do you know me?", "describe me"):
        assert _ABOUT_ME.search(s), s
    for s in ("reset your personality", "forget what you've learned about me"):
        assert _RESET_PERSONALITY.search(s), s
    assert not _ABOUT_ME.search("what have you learned about Python")
