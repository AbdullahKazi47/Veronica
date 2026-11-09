"""Daily builds: pick -> build -> check -> commit -> new repo -> push, resumed after interruptions.
Real git (local bare repos stand in for new GitHub repos) and real pytest; only Claude Code and
the GitHub CLI are faked."""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest

from veronica.automation.daily_builds import DailyBuilder, GitHub, find_leak, slugify
from veronica.config import DailyBuildsConfig
from veronica.tools.daily_build_tools import DailyBuildsTool

IDEA = json.dumps({"title": "Word Counter", "slug": "word-counter-cli", "kind": "python tool",
                   "summary": "Counts words in a text file.", "spec": "- count words\n- tests"})

GOOD_FILES = {
    "README.md": "# Word Counter\nBuilt with Claude Code, picked by VERONICA.\n",
    "wc.py": "def count(text):\n    return len(text.split())\n",
    "tests/test_wc.py": "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent.parent))\n"
                        "from wc import count\n\ndef test_count():\n    assert count('a b c') == 3\n",
}


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


class FakeGitHub:
    """Each 'new GitHub repo' is a local bare repo."""

    def __init__(self, root: Path, existing=()):
        self.root, self.created, self.not_ready = root, [], ""
        root.mkdir(parents=True, exist_ok=True)
        for name in existing:
            _git("init", "--bare", "-q", str(root / name), cwd=root)
        self.fail_create = False

    def ready(self):
        return self.not_ready

    def exists(self, name):
        return (self.root / name).is_dir()

    def url(self, name):
        return str(self.root / name) if self.exists(name) else ""

    def create(self, name, description):
        if self.fail_create or self.exists(name):
            return ""
        _git("init", "--bare", "-q", str(self.root / name), cwd=self.root)
        self.created.append((name, description))
        return str(self.root / name)


class FakeClaude:
    """Stands in for `claude -p`: writes the given files into its working folder."""

    def __init__(self, *plans):
        self.plans = list(plans)          # each: dict of files, or ("fail", text)
        self.prompts: list[str] = []
        self.names: list[str] = []

    def __call__(self, cmd, **kw):
        if cmd[0] != "claude":
            return subprocess.run(cmd, **kw)
        self.prompts.append(kw.get("input", ""))
        assert "dontAsk" in cmd and "WebFetch" in cmd                     # locked down every time
        assert not {"-c", "--continue", "-r", "--resume"} & set(cmd)     # a new chat every time
        self.names.append(cmd[cmd.index("--name") + 1])
        plan = self.plans.pop(0) if self.plans else GOOD_FILES
        if isinstance(plan, tuple):
            return subprocess.CompletedProcess(cmd, 1, json.dumps({"is_error": True, "result": plan[1]}), "")
        for rel, text in plan.items():
            p = Path(kw["cwd"]) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"subtype": "success", "result": "done"}), "")


def _builder(tmp_path, claude, host=None, **cfg):
    c = DailyBuildsConfig(enabled=True, projects_dir=str(tmp_path / "projects"), **cfg)
    host = host or FakeGitHub(tmp_path / "github")
    return DailyBuilder(c, tmp_path / "state.json", idea_maker=lambda prompt: IDEA, host=host, run=claude,
                        now=lambda: dt.datetime(2026, 11, 9, 20, 0), battery_ok=lambda p: True)


def _on_github(b, name, tmp_path):
    check = tmp_path / f"check-{name}"
    _git("clone", "-q", "-b", "main", b.host.url(name), str(check), cwd=tmp_path)
    return check


def test_each_project_gets_its_own_new_repo_named_after_it(tmp_path):
    b = _builder(tmp_path, FakeClaude())
    said = []
    b.on_done = said.append
    assert b.step() == "pushed"
    assert b.host.created == [("word-counter-cli", "Counts words in a text file.")]
    pushed = _on_github(b, "word-counter-cli", tmp_path)
    assert (pushed / "wc.py").is_file() and (pushed / "tests" / "test_wc.py").is_file()
    log = _git("log", "-1", "--format=%B", cwd=pushed).stdout
    assert log.startswith("Word Counter") and "Claude Code" in log
    assert said == ["By the way, I built Word Counter and put it in a new GitHub repo called word counter cli."]
    assert b.news() == ""                                  # said once only
    assert b.step() == "done for today"                    # one a day
    assert b.state["history"][-1]["repo_url"] == b.host.url("word-counter-cli")


def test_next_day_is_a_second_new_repo(tmp_path):
    day = [dt.datetime(2026, 11, 9, 20, 0)]
    b = _builder(tmp_path, FakeClaude())
    b._now = lambda: day[0]
    b.step()
    day[0] = dt.datetime(2026, 11, 10, 9, 0)
    assert b.step() == "pushed"
    assert [n for n, _ in b.host.created] == ["word-counter-cli", "word-counter-cli-2"]


def test_never_reuses_a_repo_name_you_already_have(tmp_path):
    host = FakeGitHub(tmp_path / "github", existing=["word-counter-cli"])
    b = _builder(tmp_path, FakeClaude(), host=host)
    assert b.step() == "pushed"
    assert host.created[0][0] == "word-counter-cli-2"
    assert _git("ls-remote", host.url("word-counter-cli"), cwd=tmp_path).stdout == ""   # untouched


def test_waits_until_github_is_signed_in(tmp_path):
    b = _builder(tmp_path, FakeClaude())
    b.host.not_ready = "GitHub isn't signed in yet (run: gh auth login)"
    assert b.step().startswith("not ready")
    assert "isn't signed in" in b.status()
    b.host.not_ready = ""
    assert b.step() == "pushed"


def test_a_shutdown_mid_build_is_resumed_next_start(tmp_path):
    half = {"README.md": "# Word Counter\n"}               # no tests yet, then the laptop went off
    host = FakeGitHub(tmp_path / "github")
    b = _builder(tmp_path, FakeClaude(half), host=host)
    assert b.step().startswith("rejected")
    claude = FakeClaude()
    b2 = _builder(tmp_path, claude, host=host)               # a fresh VERONICA, same saved state
    assert b2.state["current"]["slug"] == "word-counter-cli"
    assert b2.step() == "pushed"
    assert "earlier, unfinished attempt" in claude.prompts[0] and "no tests" in claude.prompts[0]


def test_killed_while_claude_was_running_resumes(tmp_path):
    host = FakeGitHub(tmp_path / "github")
    b = _builder(tmp_path, FakeClaude(), host=host)
    b._pick()
    b.state["current"]["status"], b.state["current"]["attempts"] = "building", 1
    b._save()                                               # what the file says after a power cut
    b2 = _builder(tmp_path, FakeClaude(), host=host)
    assert b2.step() == "pushed" and b2.state["history"][-1]["slug"] == "word-counter-cli"


def test_died_right_after_creating_the_repo_reuses_it(tmp_path):
    host = FakeGitHub(tmp_path / "github")
    b = _builder(tmp_path, FakeClaude(), host=host, push=False)
    assert b.step() == "committed"                          # built + committed, not on GitHub yet
    job = b.state["history"][-1]
    job["creating"] = True                                   # the create call went through, then power cut
    host.create(job["slug"], "")
    b.cfg.push = True
    assert b._push_pending() == "pushed earlier work"
    assert len(host.created) == 1                            # not a second repo
    assert (_on_github(b, "word-counter-cli", tmp_path) / "wc.py").is_file()


def test_github_down_keeps_the_commit_and_publishes_later(tmp_path):
    b = _builder(tmp_path, FakeClaude())
    b.host.fail_create = True
    assert b.step() == "committed"
    b.host.fail_create = False
    assert b.step() == "pushed earlier work"
    assert b.state["history"][-1]["status"] == "pushed"


def test_a_claude_code_limit_waits_without_using_up_a_try(tmp_path):
    b = _builder(tmp_path, FakeClaude(("fail", "Claude usage limit reached")))
    assert b.step() == "build interrupted"
    assert b.state["current"]["attempts"] == 0
    assert b.step() == "waiting (Claude Code limit)"


@pytest.mark.parametrize("bad, why", [
    ({**GOOD_FILES, "notes.txt": "card 4111 1111 1111 1111"}, "secret or personal"),
    ({**GOOD_FILES, "config.py": 'KEY = "sk-' + "a" * 30 + '"'}, "secret or personal"),
    ({**GOOD_FILES, "tests/test_wc.py": "def test_no():\n    assert False\n"}, "tests fail"),
])
def test_bad_work_never_reaches_github(tmp_path, bad, why):
    b = _builder(tmp_path, FakeClaude(bad))
    result = b.step()
    assert result.startswith("rejected") and why in result
    assert b.host.created == []


def test_gives_up_after_the_tries_and_cleans_up(tmp_path):
    broken = {"README.md": "# x\n"}
    b = _builder(tmp_path, FakeClaude(broken, broken), max_attempts=2)
    b.step(); b.step()
    assert b.step() == "gave up"
    assert not b.folder("word-counter-cli").exists()
    assert b.state["history"][-1]["status"] == "failed" and b.host.created == []


def test_paused_and_battery(tmp_path):
    b = _builder(tmp_path, FakeClaude())
    b.pause()
    assert b.step() == "paused"
    b.resume()
    b._battery_ok = lambda p: False
    assert b.step() == "waiting for the charger"


def test_switched_off_in_config(tmp_path):
    b = DailyBuilder(DailyBuildsConfig(enabled=False), tmp_path / "s.json", host=FakeGitHub(tmp_path / "g"))
    assert "switched off" in DailyBuildsTool(b).run(action="status").message


def test_real_github_cli_wrapper_reports_missing_sign_in():
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "You are not logged into any GitHub hosts.")
    assert "gh auth login" in GitHub(run=fake_run).ready()

    def created(cmd, **kw):
        assert cmd[1:4] == ["repo", "create", "my-tool"] and "--public" in cmd
        return subprocess.CompletedProcess(cmd, 0, "https://github.com/someone/my-tool\n", "")
    assert GitHub(run=created).create("my-tool", "x") == "https://github.com/someone/my-tool"


def test_slugify():
    assert slugify("Retro Snake: Game!") == "retro-snake-game"


def test_every_build_and_retry_is_its_own_named_chat(tmp_path):
    claude = FakeClaude({"README.md": "# half\n"}, GOOD_FILES)
    b = _builder(tmp_path, claude)
    b.step(); b.step()
    assert claude.names == ["Daily build 2026-11-09: Word Counter (try 1)",
                            "Daily build 2026-11-09: Word Counter (try 2)"]


def test_leak_check_fits_code():
    assert find_leak('password = getpass.getpass("Password: ")\nif not password:\n    return 0\n') == ""
    assert find_leak("Bearer tokens expire; the secret: rotate them") == ""
    assert find_leak('KEY = "sk-' + "a" * 30 + '"') == "an API key"
    assert find_leak("pay with 4111 1111 1111 1111") == "a card number"


# ---------------------------------------------------------------- voice

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


@pytest.mark.parametrize("text", [
    "don't do anything for today", "Don't do anything today.", "do nothing today", "skip today",
    "skip today's build", "no builds today", "don't build anything today", "take the day off",
    "stop building for today", "Veronica, don't do anything else today",
])
def test_day_off_phrases(text):
    tool = DailyBuildsTool()
    assert tool.matches(text) and tool.extract_arguments(text)["action"] == "skip_today"


@pytest.mark.parametrize("text", ["I'm going to take a day off", "not today", "what's on today",
                                  "That's all for today, Veronica"])
def test_not_a_day_off(text):
    assert DailyBuildsTool().extract_arguments(text)["action"] != "skip_today"


def test_day_off_reaches_her_through_the_full_registry():
    from veronica.safety.permissions import PermissionGate
    from veronica.tools.registry import build_default_registry
    reg = build_default_registry(PermissionGate())
    assert reg.find_fast_match("don't do anything for today").name == "daily_builds"


def test_day_off_stops_a_running_build_and_carries_on_tomorrow(tmp_path):
    day = [dt.datetime(2026, 11, 9, 20, 0)]

    class StoppedMidway(FakeClaude):
        def __call__(self, cmd, **kw):
            if cmd[0] == "claude" and not self.prompts:
                self.prompts.append(kw.get("input", ""))
                b.skip_today()                        # the user said it while Claude was working
                (Path(kw["cwd"]) / "README.md").write_text("# half done\n", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 1, "", "killed")
            return super().__call__(cmd, **kw)

    claude = StoppedMidway()
    b = _builder(tmp_path, claude)
    b._now = lambda: day[0]
    assert b.step() == "stopped by you"
    assert b.state["current"]["attempts"] == 0           # not counted as a failed try
    assert b.step() == "day off"
    assert "day off" in b.status()
    day[0] = dt.datetime(2026, 11, 10, 9, 0)              # next morning
    assert b.step() == "pushed"
    assert "earlier, unfinished attempt" in claude.prompts[-1]
    assert b.state["history"][-1]["date"] == "2026-11-10"
    assert b.step() == "done for today"


def test_day_off_by_voice(tmp_path):
    b = _builder(tmp_path, FakeClaude())
    msg = DailyBuildsTool(b).run(action="skip_today").message
    assert "tomorrow" in msg and b.step() == "day off"
    DailyBuildsTool(b).run(action="resume")
    assert b.step() == "pushed"                           # "resume daily builds" undoes it
