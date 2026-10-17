"""Abilities added by voice: the safety check, the restricted web access, testing in a
separate process, approval, tamper detection and removal."""
from __future__ import annotations

import socket

import pytest

from veronica import plugin_api
from veronica.core.abilities import AbilityManager, check_code, run_examples, sha256
from veronica.plugin_api import AbilityError
from veronica.safety.permissions import PermissionGate, PermissionLevel

GOOD = '''
from veronica.plugin_api import Ability

class DiceRoller(Ability):
    name = "dice_roller"
    description = "Adds up dice: count and sides, e.g. 2 dice with 6 sides."
    parameters = {"type": "object", "properties": {"count": {"type": "integer"}, "sides": {"type": "integer"}}}
    hosts = []

    def run(self, count=2, sides=6):
        total = count * (sides + 1) / 2
        return f"On average {count} dice with {sides} sides add up to {total:g}."

EXAMPLES = [{"args": {"count": 2, "sides": 6}, "expect": "add up to 7"}]
'''


def test_good_ability_passes():
    problems, facts = check_code(GOOD)
    assert problems == [] and facts["name"] == "dice_roller" and facts["hosts"] == []


@pytest.mark.parametrize("evil", [
    "import os",
    "import subprocess",
    "from pathlib import Path",
    "import requests",
    "import socket",
    "import string",                                     # string.Formatter escape
    "from veronica.plugin_api import _run_with_hosts",   # host allow-list bypass
    "from veronica.plugin_api import _ctx",
    "from veronica import config",
    "x = open('C:/secret.txt').read()",
    "x = eval('1+1')",
    "x = getattr(object, 'mro')",
    "x = (1).__class__",
    "x = ().__class__.__bases__[0].__subclasses__()",
    "x = __builtins__",
    "x = '{0.__class__}'.format(1)",
    "import typing\nx = typing.get_type_hints(int)",
    "def _helper():\n    return 1",
])
def test_escape_attempts_are_rejected(evil):
    problems, _ = check_code(GOOD + "\n" + evil + "\n")
    assert problems, evil


def test_structure_rules():
    assert any("exactly one class" in p for p in check_code("x = 1")[0])
    no_examples = GOOD.split("EXAMPLES")[0]
    assert any("EXAMPLES" in p for p in check_code(no_examples)[0])
    bad_host = GOOD.replace('hosts = []', 'hosts = ["not a host"]')
    assert any("hosts" in p for p in check_code(bad_host)[0])


# ---------------------------------------------------------------- restricted web

def test_only_declared_hosts(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, p: [(None, None, None, None, ("93.184.216.34", p))])
    def call(url):
        return plugin_api._run_with_hosts(["api.example.com"], plugin_api._allowed, url)
    assert call("https://api.example.com/x")
    assert call("https://v2.api.example.com/x")                  # subdomain of a declared host
    with pytest.raises(AbilityError):
        call("https://evil.com/steal")
    with pytest.raises(AbilityError):
        call("file:///C:/secret.txt")


def test_private_and_local_addresses_blocked(monkeypatch):
    for ip in ("127.0.0.1", "192.168.1.5", "10.0.0.2", "169.254.1.1"):
        monkeypatch.setattr(socket, "getaddrinfo", lambda h, p, ip=ip: [(None, None, None, None, (ip, p))])
        with pytest.raises(AbilityError):
            plugin_api._run_with_hosts(["sneaky.example.com"], plugin_api._allowed, "https://sneaky.example.com/")


def test_veronicas_own_keys_are_never_handed_out():
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY", "CALENDAR_ICS_URLS", "PATH"):
        with pytest.raises(AbilityError):
            plugin_api.api_key(name)


# ---------------------------------------------------------------- separate-process tests

def test_examples_run_in_a_separate_process(tmp_path):
    path = tmp_path / "dice_roller.py"
    path.write_text(GOOD, encoding="utf-8")
    passed, results, err = run_examples(path)
    assert passed and results[0]["ok"] and "add up to 7" in results[0]["output"], err


def test_failing_example_is_reported(tmp_path):
    path = tmp_path / "dice_roller.py"
    path.write_text(GOOD.replace("add up to 7", "add up to 99"), encoding="utf-8")
    passed, results, _ = run_examples(path)
    assert not passed and "99" in results[0]["error"]


# ---------------------------------------------------------------- build -> approve -> load

def _manager(tmp_path, replies, tester=None):
    from veronica.tools.registry import ToolRegistry
    reg = ToolRegistry(PermissionGate())
    replies = list(replies)
    return AbilityManager(registry=reg, root=tmp_path / "abilities", ask=lambda s, u: replies.pop(0),
                          tester=tester or run_examples), reg


def _reply(code):
    return f"```python\n{code}\n```\nSUMMARY: It averages dice rolls."


def test_build_fixes_a_rejected_draft_then_activates(tmp_path):
    bad = GOOD.replace("from veronica.plugin_api import Ability", "import os\nfrom veronica.plugin_api import Ability")
    mgr, reg = _manager(tmp_path, [_reply(bad), _reply(GOOD)])
    res = mgr.build("average dice rolls")
    assert res["ok"] and res["name"] == "dice_roller" and mgr.pending == "dice_roller"
    assert reg.get("dice_roller") is None                         # not live until approved
    ok, msg = mgr.activate("dice_roller")
    assert ok and reg.get("dice_roller") is not None
    out = reg.dispatch("dice_roller", count=3, sides=6)
    assert out.success and "add up to 10.5" in out.message


def test_tampered_ability_is_refused_on_load(tmp_path):
    mgr, reg = _manager(tmp_path, [_reply(GOOD)])
    mgr.build("dice")
    mgr.activate("dice_roller")
    (mgr.approved / "dice_roller.py").write_text(GOOD.replace("average", "AVERAGE"), encoding="utf-8")
    mgr2, reg2 = _manager(tmp_path, [])
    assert mgr2.load_approved() == [] and reg2.get("dice_roller") is None


def test_remove_keeps_a_copy(tmp_path):
    mgr, reg = _manager(tmp_path, [_reply(GOOD)])
    mgr.build("dice")
    mgr.activate("dice_roller")
    assert mgr.remove("dice_roller") and reg.get("dice_roller") is None
    assert list((mgr.root / "removed").glob("dice_roller.*.py"))


def test_cannot_shadow_a_builtin_tool(tmp_path):
    from veronica.tools.base import Tool, ToolResult
    clash = GOOD.replace('name = "dice_roller"', 'name = "get_news"')
    mgr, reg = _manager(tmp_path, [_reply(clash)] * 3)

    class News(Tool):
        name = "get_news"; description = "news"; parameters = {}
        def run(self, **k): return ToolResult.ok("news")
    reg.register(News())
    assert not mgr.build("news")["ok"]


def test_gives_up_after_the_allowed_fixes(tmp_path):
    bad = GOOD.replace("from veronica.plugin_api import Ability", "import os\nfrom veronica.plugin_api import Ability")
    mgr, _ = _manager(tmp_path, [_reply(bad)] * 3)
    res = mgr.build("dice", max_fixes=2)
    assert not res["ok"] and "3 tries" in res["message"]


# ---------------------------------------------------------------- voice commands & permissions

def test_tools_routing_and_permissions(tmp_path):
    from veronica.tools.ability_tools import (
        ActivateAbilityTool, BuildAbilityTool, ListAbilitiesTool, RemoveAbilityTool,
    )
    mgr, _ = _manager(tmp_path, [_reply(GOOD)])
    build, act, lst, rm = BuildAbilityTool(mgr), ActivateAbilityTool(mgr), ListAbilitiesTool(mgr), RemoveAbilityTool(mgr)
    assert build.matches("add an ability for flight tracking")
    assert build.extract_arguments("add an ability for flight tracking") == {"request": "flight tracking"}
    assert build.matches("build a skill that tells me cricket scores")
    assert ActivateAbilityTool.permission >= PermissionLevel.SENSITIVE
    assert RemoveAbilityTool.permission >= PermissionLevel.SENSITIVE
    assert not act.matches("activate it")                       # nothing pending yet
    assert build.run(request="dice").success
    assert act.matches("activate it") and act.extract_arguments("activate it") == {"name": "dice_roller"}
    assert act.run(name="dice_roller").success
    assert "dice roller" in lst.run().message
    assert rm.matches("remove the dice roller ability")


def test_activation_asks_through_the_gate(tmp_path):
    from veronica.tools.ability_tools import ActivateAbilityTool
    from veronica.tools.registry import ToolRegistry
    mgr, reg = _manager(tmp_path, [_reply(GOOD)])
    mgr.build("dice")
    asked = []
    gated = ToolRegistry(PermissionGate(threshold=PermissionLevel.SENSITIVE,
                                        confirm_callback=lambda d, l: asked.append(d) or False))
    gated.register(ActivateAbilityTool(mgr))
    assert not gated.dispatch("activate_ability", name="dice_roller").success
    assert asked and reg.get("dice_roller") is None             # "no" means it stays off
