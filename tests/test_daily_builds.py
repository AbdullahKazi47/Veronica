"""Daily builds: pick -> build -> check -> commit -> push, resumed after interruptions.
Real git (a local bare repo stands in for GitHub) and real pytest; only Claude Code is faked."""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest

from veronica.automation.daily_builds import DailyBuilder, slugify
from veronica.config import DailyBuildsConfig
from veronica.tools.daily_build_tools import DailyBuildsTool

IDEA = json.dumps({"title": "Word Counter", "slug": "word-counter", "kind": "python tool",
                   "summary": "Counts words in a text file.", "spec": "- count words\n- tests"})

GOOD_FILES = {
    "README.md": "# Word Counter\nBuilt with Claude Code, picked by VERONICA.\n",
    "wc.py": "def count(text):\n    return len(text.split())\n",
    "tests/test_wc.py": "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent.parent))\n"
                        "from wc import count\n\ndef test_count():\n    assert count('a b c') == 3\n",
}


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def remote(tmp_path):
    bare = tmp_path / "remote.git"
    _git("init", "--bare", "-q", str(bare), cwd=tmp_path)
    return bare


class FakeClaude:
    """Stands in for `claude -p`: writes the given files into its working folder."""

    def __init__(self, *plans):
        self.plans = list(plans)          # each: dict of files, or ("fail", text)
        self.prompts: list[str] = []

    def __call__(self, cmd, **kw):
        if cmd[0] != "claude":
            return subprocess.run(cmd, **kw)
        self.prompts.append(kw.get("input", ""))
        assert "dontAsk" in cmd and "WebFetch" in cmd           # locked down every time
        plan = self.plans.pop(0) if self.plans else GOOD_FILES
        if isinstance(plan, tuple):
            return subprocess.CompletedProcess(cmd, 1, json.dumps({"is_error": True, "result": plan[1]}), "")
        for rel, text in plan.items():
            p = Path(kw["cwd"]) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"subtype": "success", "result": "done"}), "")


def _builder(tmp_path, remote, claude, **cfg):
    c = DailyBuildsConfig(enabled=True, repo_url=str(remote), local_dir=str(tmp_path / "clone"), **cfg)
    b = DailyBuilder(c, tmp_path / "state.json", idea_maker=lambda prompt: IDEA, run=claude,
                     now=lambda: dt.datetime(2026, 11, 9, 20, 0), battery_ok=lambda p: True)
    return b


def _remote_files(remote, tmp_path):
    check = tmp_path / "check"
    _git("clone", "-q", str(remote), str(check), cwd=tmp_path)
    return check


def test_a_project_goes_from_idea_to_the_repo(tmp_path, remote):
    b = _builder(tmp_path, remote, FakeClaude())
    said = []
    b.on_done = said.append
    assert b.step() == "pushed"
    pushed = _remote_files(remote, tmp_path)
    assert (pushed / "word-counter" / "wc.py").is_file()
    assert "Word Counter" in (pushed / "README.md").read_text(encoding="utf-8")
    log = _git("log", "-1", "--format=%B", cwd=pushed).stdout
    assert "Add Word Counter" in log and "Claude Code" in log
    assert said == ["By the way, I built Word Counter and added it to your GitHub repo."]
    assert b.news() == ""                                  # said once only
    assert b.step() == "done for today"                    # one a day


def test_a_shutdown_mid_build_is_resumed_next_start(tmp_path, remote):
    half = {"README.md": "# Word Counter\n"}               # no tests yet, then the laptop went off
    b = _builder(tmp_path, remote, FakeClaude(half))
    assert b.step().startswith("rejected")
    # Simulate the hard stop: a fresh VERONICA reads the same saved state.
    claude = FakeClaude()
    b2 = _builder(tmp_path, remote, claude)
    assert b2.state["current"]["slug"] == "word-counter"
    assert b2.step() == "pushed"
    assert "earlier, unfinished attempt" in claude.prompts[0] and "no tests" in claude.prompts[0]


def test_killed_while_claude_was_running_resumes(tmp_path, remote):
    b = _builder(tmp_path, remote, FakeClaude())
    b._ensure_repo()
    b._pick()
    b.state["current"]["status"], b.state["current"]["attempts"] = "building", 1
    b._save()                                               # what the file says after a power cut
    claude = FakeClaude()
    b2 = _builder(tmp_path, remote, claude)
    assert b2.step() == "pushed" and b2.state["history"][-1]["slug"] == "word-counter"


def test_a_claude_code_limit_waits_without_using_up_a_try(tmp_path, remote):
    b = _builder(tmp_path, remote, FakeClaude(("fail", "Claude usage limit reached")))
    assert b.step() == "build interrupted"
    assert b.state["current"]["attempts"] == 0
    assert b.step() == "waiting (Claude Code limit)"


@pytest.mark.parametrize("bad, why", [
    ({**GOOD_FILES, "../outside.txt": "x"}, "outside its folder"),
    ({**GOOD_FILES, "notes.txt": "card 4111 1111 1111 1111"}, "secret or personal"),
    ({**GOOD_FILES, "tests/test_wc.py": "def test_no():\n    assert False\n"}, "tests fail"),
])
def test_bad_work_is_not_committed(tmp_path, remote, bad, why):
    b = _builder(tmp_path, remote, FakeClaude(bad))
    result = b.step()
    assert result.startswith("rejected") and why in result
    assert _git("ls-remote", str(remote), cwd=tmp_path).stdout == ""     # nothing reached GitHub


def test_gives_up_after_the_tries_and_cleans_up(tmp_path, remote):
    broken = {"README.md": "# x\n"}
    b = _builder(tmp_path, remote, FakeClaude(broken, broken), max_attempts=2)
    b.step(); b.step()
    assert b.step() == "gave up"
    assert not (b.repo_dir / "word-counter").exists()
    assert b.state["history"][-1]["status"] == "failed"


def test_push_failure_is_retried_later(tmp_path, remote):
    b = _builder(tmp_path, remote, FakeClaude())
    b._ensure_repo()
    _git("remote", "set-url", "origin", str(tmp_path / "nowhere.git"), cwd=b.repo_dir)
    assert b.step() == "committed"
    _git("remote", "set-url", "origin", str(remote), cwd=b.repo_dir)
    assert b.step() == "pushed earlier work"
    assert b.state["history"][-1]["status"] == "pushed"


def test_paused_and_battery(tmp_path, remote):
    b = _builder(tmp_path, remote, FakeClaude())
    b.pause()
    assert b.step() == "paused"
    b.resume()
    b._battery_ok = lambda p: False
    assert b.step() == "waiting for the charger"


def test_no_repo_means_nothing_happens(tmp_path):
    b = DailyBuilder(DailyBuildsConfig(), tmp_path / "s.json", idea_maker=lambda p: IDEA)
    assert b.step() == "no repo set"
    assert "aren't set up" in DailyBuildsTool(b).run(action="status").message


def test_slugify():
    assert slugify("Retro Snake: Game!") == "retro-snake-game"


@pytest.mark.parametrize("text, action, kind", [
    ("what did you build today", "status", ""),
    ("how's the daily build going", "status", ""),
    ("pause daily builds", "pause", ""),
    ("stop the daily builds", "pause", ""),
    ("resume daily builds", "resume", ""),
    ("build something now", "now", ""),
    ("build a game next", "prefer", "game"),
    ("next project should be a web app", "prefer", "web app"),
])
def test_voice_phrases(text, action, kind):
    tool = DailyBuildsTool()
    assert tool.matches(text)
    args = tool.extract_arguments(text)
    assert args["action"] == action and args["kind"] == kind


@pytest.mark.parametrize("text", ["build a website for my shop", "what's the weather", "write code for a calculator"])
def test_other_requests_are_not_daily_builds(text):
    assert not DailyBuildsTool().matches(text)


def test_leak_check_fits_code():
    from veronica.automation.daily_builds import find_leak
    assert find_leak('password = getpass.getpass("Password: ")\nif not password:\n    return 0\n') == ""
    assert find_leak("Bearer tokens expire; the secret: rotate them") == ""
    assert find_leak('KEY = "sk-' + "a" * 30 + '"') == "an API key"
    assert find_leak("pay with 4111 1111 1111 1111") == "a card number"
