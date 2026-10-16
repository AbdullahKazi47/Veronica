"""Background tasks: started by voice, run in a private session, announced when done,
and never allowed to ask for permission."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from veronica.core.background import (
    BackgroundTasks, in_background, is_status_request, split_background_request,
)
from veronica.safety.permissions import PermissionLevel
from veronica.ui import cli as cli_mod


@pytest.mark.parametrize("text,task", [
    ("research cheap flights to Goa in the background", "research cheap flights to Goa"),
    ("find the best biryani places in Bangalore while I play my game", "find the best biryani places in Bangalore"),
    ("summarize today's tech news and let me know when you're done", "summarize today's tech news"),
    ("Veronica, compare iPhone and Pixel cameras, I'll go get lunch", "compare iPhone and Pixel cameras"),
    ("please look up train times to Mysuru while I'm away", "look up train times to Mysuru"),
    ("check the weather in Delhi and do it in the background", "check the weather in Delhi"),
])
def test_background_phrases(text, task):
    assert split_background_request(text) == task


@pytest.mark.parametrize("text", ["what's the weather", "tell me about the background of the Mona Lisa",
                                  "what time is it"])
def test_ordinary_requests_are_not_background(text):
    assert split_background_request(text) is None


@pytest.mark.parametrize("text", ["what are you working on", "are you done yet", "status of my tasks",
                                  "what did you do while I was away"])
def test_status_phrases(text):
    assert is_status_request(text)


class FakeOrchestrator:
    def __init__(self, reply="Found 3 flights under 5000 rupees.", delay=0.0):
        self.reply, self.delay, self.sessions, self.flags = reply, delay, [], []

    def handle(self, text, session):
        self.sessions.append(session)
        self.flags.append(in_background())
        time.sleep(self.delay)
        return SimpleNamespace(text=self.reply)


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.01)
    return cond()


def test_task_runs_in_a_private_session_and_is_announced(tmp_path):
    orch, announced = FakeOrchestrator(), []
    bg = BackgroundTasks(orch, announced.append, store_path=tmp_path / "tasks.json")
    task = bg.submit("research cheap flights to Goa")
    assert wait_for(lambda: announced)
    assert announced[0].status == "done" and announced[0].result == "Found 3 flights under 5000 rupees."
    assert orch.flags == [True]                          # ran flagged as background
    assert orch.sessions[0].short_term._path is None     # private, never persisted
    assert not in_background()                           # flag doesn't leak to this thread
    assert "Finished: research cheap flights to Goa" in bg.status_report()


def test_running_tasks_show_in_status_and_survive_restart_as_failed(tmp_path):
    orch = FakeOrchestrator(delay=0.5)
    bg = BackgroundTasks(orch, lambda t: None, store_path=tmp_path / "tasks.json")
    bg.submit("long job")
    assert wait_for(lambda: bg.tasks()[0].status == "running")
    assert "still working on long job" in bg.status_report()
    again = BackgroundTasks(FakeOrchestrator(), lambda t: None, store_path=tmp_path / "tasks.json")
    assert again.tasks()[0].status == "failed"           # VERONICA closed mid-task: said honestly


def test_a_failing_task_is_reported_and_the_worker_keeps_going(tmp_path):
    class Boom(FakeOrchestrator):
        def handle(self, text, session):
            if text == "bad":
                raise RuntimeError("model down")
            return super().handle(text, session)
    announced = []
    bg = BackgroundTasks(Boom(), announced.append, store_path=tmp_path / "t.json")
    bg.submit("bad")
    bg.submit("good")
    assert wait_for(lambda: len(announced) == 2)
    assert [t.status for t in announced] == ["failed", "done"]


def test_permission_prompts_are_refused_inside_background_tasks(tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(cli_mod, "_voice_confirmer", lambda d, lvl: asked.append(d) or True)

    class NeedsPermission(FakeOrchestrator):
        def handle(self, text, session):
            ok = cli_mod._confirm_in_terminal("write_file({'path': 'notes.txt'})", PermissionLevel.SENSITIVE)
            return SimpleNamespace(text=f"write allowed={ok}")
    announced = []
    bg = BackgroundTasks(NeedsPermission(), announced.append, store_path=tmp_path / "t.json")
    bg.submit("save notes")
    assert wait_for(lambda: announced)
    assert announced[0].result == "write allowed=False"
    assert announced[0].refused == ["write_file({'path': 'notes.txt'})"]
    assert asked == []                                   # the user was never asked
    # ...while the same prompt in the foreground still asks normally.
    assert cli_mod._confirm_in_terminal("write_file({})", PermissionLevel.SENSITIVE) is True and asked


def test_cli_starts_tasks_reports_status_and_announces(tmp_path):
    app = cli_mod.CLIApp.__new__(cli_mod.CLIApp)
    app.config = SimpleNamespace(assistant=SimpleNamespace(name="VERONICA"))
    app.voice_mode = False
    app._capturing, app._stop, app._speak_lock = threading.Event(), threading.Event(), threading.RLock()
    said = []
    app._say, app._beep = said.append, lambda: None
    app.background = BackgroundTasks(FakeOrchestrator(), app._announce_task, store_path=tmp_path / "t.json")
    assert app._background_reply("what time is it") is None
    assert app._background_reply("research flights to Goa in the background").startswith("On it.")
    assert wait_for(lambda: any("background task is done" in s for s in said))
    assert said[-1] == "Your background task is done: research flights to Goa. Found 3 flights under 5000 rupees."
    assert "Finished: research flights to Goa" in app._background_reply("what are you working on")


@pytest.mark.parametrize("text,is_file", [
    ("find three interesting facts about Mysore Palace", False), ("find cheap flights to goa", False),
    ("find the best biryani places", False), ("find my resume file", True), ("find report.pdf", True),
    ("find the photos from goa", True), ("find my file budget", True),
])
def test_find_only_fast_paths_file_requests(text, is_file):
    from veronica.tools.file_tools import FindFileTool
    assert FindFileTool().matches(text) is is_file
