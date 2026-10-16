"""Everyday PC control: right permission level per action, nothing real touched."""
from __future__ import annotations

import pytest

from veronica.safety.permissions import PermissionGate, PermissionLevel
from veronica.tools.registry import build_default_registry
from veronica.tools.window_tools import (
    CancelShutdownTool,
    CloseWindowTool,
    LockOrSleepTool,
    OpenPathTool,
    ShutdownTool,
    WindowControlTool,
)


@pytest.fixture(scope="module")
def registry():
    return build_default_registry(PermissionGate())


# ---------------------------------------------------------------- routing

@pytest.mark.parametrize("text,tool", [
    ("minimize all my tabs", "window_control"),
    ("minimise everything", "window_control"),
    ("minimise all attacks", "window_control"),
    ("show desktop", "window_control"),
    ("restore all windows", "window_control"),
    ("minimize chrome", "window_control"),
    ("switch to WhatsApp", "window_control"),
    ("close spotify", "close_window"),
    ("lock my pc", "lock_or_sleep"),
    ("put the laptop to sleep", "lock_or_sleep"),
    ("shut down my pc", "shutdown_pc"),
    ("restart the computer in 5 minutes", "shutdown_pc"),
    ("cancel shutdown", "cancel_shutdown"),
    ("open the projects folder", "open_path"),
    ("open my resume pdf", "open_path"),
    ("open the photos folder in documents", "open_path"),
])
def test_fast_path_routing(registry, text, tool):
    assert registry.find_fast_match(text).name == tool


def test_bare_go_to_sleep_does_not_sleep_the_pc():
    assert not LockOrSleepTool().matches("go to sleep")
    assert not CloseWindowTool().matches("close it")


def test_permission_levels_only_destructive_ones_ask():
    gate_level = PermissionLevel.SENSITIVE  # config: require_confirmation_above
    for t in (WindowControlTool(), LockOrSleepTool(), OpenPathTool(), CancelShutdownTool()):
        assert t.permission < gate_level, t.name
    for t in (CloseWindowTool(), ShutdownTool()):
        assert t.permission >= gate_level, t.name


# ---------------------------------------------------------------- behaviour

def test_minimize_all_and_one_window():
    calls = []

    class Shell:
        def MinimizeAll(self): calls.append("min_all")
        def UndoMinimizeALL(self): calls.append("undo")

    tool = WindowControlTool(shell=Shell, find_window=lambda app: (42, "Chrome") if app == "chrome" else None,
                             show=lambda h, how: calls.append((h, how)), list_windows=lambda: [])
    assert tool.run(**tool.extract_arguments("minimize all my tabs")).success
    assert tool.run(action="restore_all").success
    assert tool.run(**tool.extract_arguments("maximize chrome")).success
    assert not tool.run(action="focus", app="notepad").success
    assert calls == ["min_all", "undo", (42, "maximize")]


def test_shutdown_has_cancellable_delay_and_cancel_works():
    ran = []
    tool = ShutdownTool(runner=ran.append)
    res = tool.run(**tool.extract_arguments("shut down my pc"))
    assert res.success and ran[-1][:4] == ["shutdown", "/s", "/t", "30"]
    tool.run(**tool.extract_arguments("restart in 2 minutes"))
    assert ran[-1][:4] == ["shutdown", "/r", "/t", "120"]
    CancelShutdownTool(runner=ran.append).run()
    assert ran[-1] == ["shutdown", "/a"]


def test_lock_and_sleep():
    did = []
    tool = LockOrSleepTool(lock=lambda: did.append("lock"), sleep=lambda: did.append("sleep"))
    tool.run(**tool.extract_arguments("lock my computer"))
    tool.run(**tool.extract_arguments("put my pc to sleep"))
    assert did == ["lock", "sleep"]


def test_open_path_finds_nested_folder_and_documents(tmp_path):
    (tmp_path / "Documents" / "Projects" / "Veronica").mkdir(parents=True)
    (tmp_path / "AppData" / "Projects").mkdir(parents=True)          # skipped
    (tmp_path / "Documents" / "My Resume.pdf").write_text("x")
    (tmp_path / "Downloads").mkdir()
    (tmp_path / "Downloads" / "setup.exe").write_text("x")
    opened = []
    tool = OpenPathTool(home=tmp_path, opener=opened.append)

    assert tool.run(**tool.extract_arguments("open the projects folder")).success
    assert opened[-1] == str(tmp_path / "Documents" / "Projects")
    assert tool.run(**tool.extract_arguments("open veronica folder in projects")).success
    assert opened[-1].endswith("Veronica")
    assert tool.run(**tool.extract_arguments("open my resume pdf")).success
    assert opened[-1].endswith("My Resume.pdf")
    assert not tool.run(name="nothing here", kind="folder").success


def test_open_path_never_runs_programs(tmp_path):
    exe = tmp_path / "setup.exe"
    exe.write_text("x")
    opened = []
    tool = OpenPathTool(home=tmp_path, opener=opened.append)
    assert not tool.run(name=str(exe), kind="file").success
    assert not tool.run(**tool.extract_arguments("open setup.exe")).success
    assert opened == []


def test_close_window_reports_missing_app():
    closed = []
    tool = CloseWindowTool(find_window=lambda app: (7, "Spotify") if app == "spotify" else None,
                           close=closed.append)
    assert tool.run(app="spotify").success and closed == [7]
    assert not tool.run(app="notepad").success


@pytest.mark.parametrize("text,args", [
    ("restore my tabs", {"action": "restore_all"}),
    ("bring back my tabs", {"action": "restore_all"}),
    ("bring back all my windows", {"action": "restore_all"}),
    ("restore", {"action": "restore_all"}),
    ("restore them", {"action": "restore_all"}),
    ("undo that", {"action": "restore_all"}),
    ("show my tabs again", {"action": "restore_all"}),
    ("restore spotify", {"action": "focus", "app": "spotify"}),
])
def test_restore_phrasings(text, args):
    tool = WindowControlTool()
    assert tool.matches(text) and tool.extract_arguments(text) == args


def test_restore_brings_back_what_minimize_all_hid():
    state = {1: False, 2: True, 3: False}          # hwnd -> minimized; 2 was minimized before
    shown = []

    class Shell:
        def MinimizeAll(self):
            for h in state:
                state[h] = True
        def UndoMinimizeALL(self): shown.append("undo")

    tool = WindowControlTool(shell=Shell, list_windows=lambda: list(state), is_minimized=lambda h: state[h],
                             show=lambda h, how: shown.append(h))
    tool.run(action="minimize_all")
    res = tool.run(action="restore_all")
    assert res.success and sorted(shown) == [1, 3]   # not the one the user had minimized themselves


def test_restore_after_restart_restores_every_minimized_window():
    shown = []
    tool = WindowControlTool(shell=lambda: None, list_windows=lambda: [5, 6, 7],
                             is_minimized=lambda h: h != 6, show=lambda h, how: shown.append(h))
    assert tool.run(action="restore_all").success and sorted(shown) == [5, 7]


def test_dispatch_accepts_a_tool_argument_called_name(tmp_path):
    """Regression: 'open placement folder' crashed — dispatch(name, **{'name': ...})."""
    from veronica.tools.registry import ToolRegistry
    (tmp_path / "Placement").mkdir()
    opened = []
    reg = ToolRegistry(PermissionGate(threshold=PermissionLevel.SENSITIVE))
    reg.register(OpenPathTool(home=tmp_path, opener=opened.append))
    res = reg.dispatch("open_path", name="placement", kind="folder")
    assert res.success and opened == [str(tmp_path / "Placement")]
