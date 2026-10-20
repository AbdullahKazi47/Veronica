"""Tabs: fake UI Automation strips and window lists — no real window, browser or keyboard."""
from __future__ import annotations

import pytest

from veronica.safety.permissions import PermissionGate, PermissionLevel
from veronica.tools.registry import ToolRegistry
from veronica.tools.tab_tools import CloseTabTool, ListTabsTool, SwitchTabTool, match_tabs
from veronica.vision.uia_tabs import (
    RawTab, TabReader, WindowGone, canonical_app, clean_title, titles_removed,
)


class FakeUIA:
    """Tab strips by window handle. Closing removes the tab unless its title is in `stubborn`
    (a tab that asks to save stays open); `unreadable` windows raise when read."""

    def __init__(self, windows: dict[int, list[str]], selected: dict[int, int] | None = None) -> None:
        self.tabs = {h: [RawTab(t, False, ref=(h, t), close_ref=(h, t)) for t in titles] for h, titles in windows.items()}
        for h, i in (selected or {}).items():
            self.tabs[h][i].selected = True
        for h, ts in self.tabs.items():
            if ts and not any(t.selected for t in ts):
                ts[0].selected = True
        self.stubborn: set[str] = set()
        self.unreadable: set[int] = set()
        self.select_fails = False
        self.close_fails = False
        self.calls: list[tuple] = []
        self.on_empty = None

    def read(self, hwnd, kind):
        if hwnd in self.unreadable:
            raise RuntimeError("COM busy")
        if hwnd not in self.tabs:
            raise WindowGone(str(hwnd))
        return list(self.tabs[hwnd])

    def select(self, hwnd, tab):
        self.calls.append(("select", tab.title))
        if self.select_fails:
            raise RuntimeError("no SelectionItem")
        for t in self.tabs[hwnd]:
            t.selected = t is tab

    def close(self, hwnd, tab):
        self.calls.append(("close", tab.title))
        if self.close_fails:
            raise RuntimeError("no close button")
        if tab.title in self.stubborn:
            return
        self.tabs[hwnd].remove(tab)
        if self.tabs[hwnd] and not any(t.selected for t in self.tabs[hwnd]):
            self.tabs[hwnd][0].selected = True


class World:
    """Windows + a fake backend wired into a TabReader."""

    def __init__(self, layout: dict[int, tuple[str, str, list[str]]], front: int = 0, selected=None) -> None:
        self.layout = layout       # hwnd -> (window title, process, tab titles)
        self.uia = FakeUIA({h: v[2] for h, v in layout.items()}, selected)
        self.front = front
        self.keys: list[tuple] = []
        self.shown: list[tuple] = []
        self.minimized: set[int] = set()
        self.reader = TabReader(
            backend=self.uia, list_windows=self.windows, is_minimized=lambda h: h in self.minimized,
            show=lambda h, how: self.shown.append((h, how)) or (self.minimized.discard(h) if how == "restore" else None),
            foreground=lambda: self.front, exists=lambda h: h in self.layout, press=self.keys.append,
            sleep=lambda s: None, settle_s=0.0)
        self._wrap_close()

    def windows(self):
        return [(h, v[0], v[1]) for h, v in self.layout.items()]

    def _wrap_close(self):
        orig = self.uia.close

        def close(hwnd, tab):
            orig(hwnd, tab)
            proc = self.layout[hwnd][1].lower()
            if not self.uia.tabs[hwnd] and proc != "code.exe":
                self.layout.pop(hwnd)                      # closing the last browser tab closes the window
        self.uia.close = close


def chrome_edge_world(**kw) -> World:
    return World({
        10: ("Inbox - Google Chrome", "chrome.exe", ["Inbox", "YouTube", "Docs - project", "Docs - notes", "GitHub"]),
        20: ("News - Microsoft Edge", "msedge.exe", ["News", "Weather"]),
        30: ("Notepad", "notepad.exe", []),
    }, front=10, **kw)


# ---------------------------------------------------------------- names

def test_titles_are_cleaned_of_noise_and_marks():
    assert clean_title("Gmail​ - Memory usage - 120 MB") == "Gmail"
    assert clean_title("Edge two - Sleeping") == "Edge two"
    assert clean_title("  Docs   -  Audio playing") == "Docs"


@pytest.mark.parametrize("word,label", [("chrome", "Chrome"), ("Google Chrome", "Chrome"), ("the edge browser", "Edge"),
                                        ("explorer", "File Explorer"), ("file explorer", "File Explorer"),
                                        ("vs code", "VS Code"), ("Visual Studio Code", "VS Code"), ("notepad", None)])
def test_app_names(word, label):
    assert canonical_app(word) == label


def test_match_tabs_prefers_exact_then_phrase_then_words():
    r = TabReader(backend=FakeUIA({1: ["Docs", "Docs - project", "Project docs"]}), list_windows=lambda: [(1, "x", "chrome.exe")],
                  foreground=lambda: 1, is_minimized=lambda h: False)
    tabs = r.strips()[0].tabs
    assert [t.title for t in match_tabs(tabs, "docs")] == ["Docs"]
    assert [t.title for t in match_tabs(tabs, "docs project")] == ["Docs - project"]
    assert [t.title for t in match_tabs(tabs, "project")] == ["Docs - project", "Project docs"]
    assert match_tabs(tabs, "nothing like it") == []


def test_titles_removed_counts_duplicates():
    assert titles_removed(["a", "b", "a"], ["a"]) == ["b", "a"]


# ---------------------------------------------------------------- reading

def test_strips_only_from_tabbed_apps_with_tabs():
    w = chrome_edge_world()
    strips = w.reader.strips()
    assert [(s.app, len(s.tabs)) for s in strips] == [("Chrome", 5), ("Edge", 2)]      # Notepad has none
    assert strips[0].current.title == "Inbox"
    assert [s.app for s in w.reader.strips("Edge")] == ["Edge"]


def test_minimized_window_without_readable_tabs_is_brought_up_for_the_read_and_put_back():
    w = World({5: ("Edge", "msedge.exe", ["One", "Two"])})
    w.minimized.add(5)
    real_read = w.uia.read
    state = {"n": 0}

    def read(h, k):
        state["n"] += 1
        return [] if state["n"] == 1 else real_read(h, k)   # first read: minimized, nothing exposed
    w.uia.read = read
    strip = w.reader.strips()[0]
    assert [t.title for t in strip.tabs] == ["One", "Two"]
    assert [how for _h, how in w.shown] == ["restore", "minimize"]


def test_unreadable_window_is_skipped_not_fatal():
    w = chrome_edge_world()
    w.uia.unreadable.add(10)
    assert [s.app for s in w.reader.strips()] == ["Edge"]


# ---------------------------------------------------------------- list_tabs

def test_list_tabs_says_what_is_open_and_which_is_current():
    w = chrome_edge_world()
    tool = ListTabsTool(w.reader)
    res = tool.run()
    assert res.success and "Chrome has 5 tabs" in res.message and "current: Inbox" in res.message and "Edge has 2 tabs" in res.message
    one = tool.run(app="edge")
    assert "Edge has 2 tabs" in one.message and "Chrome" not in one.message
    assert not tool.run(app="firefox").success


def test_list_tabs_when_nothing_has_tabs():
    w = World({1: ("Notepad", "notepad.exe", [])})
    assert not ListTabsTool(w.reader).run().success


@pytest.mark.parametrize("text,args", [
    ("list my tabs", {}), ("what tabs are open", {}), ("what tabs are open in chrome", {"app": "chrome"}),
    ("how many tabs do I have", {}), ("list the tabs in edge", {"app": "edge"}),
])
def test_list_phrases(text, args):
    t = ListTabsTool()
    assert t.matches(text) and t.extract_arguments(text) == args


# ---------------------------------------------------------------- switch_tab

def test_switch_by_name_is_checked_afterwards():
    w = chrome_edge_world()
    res = SwitchTabTool(w.reader).run(name="youtube")
    assert res.success and "YouTube" in res.message
    assert w.reader.strips("Chrome")[0].current.title == "YouTube"
    assert (10, "focus") in w.shown                       # the window is brought forward first


def test_switch_by_number_and_relative():
    w = chrome_edge_world()
    tool = SwitchTabTool(w.reader)
    assert tool.run(number=3).success and w.reader.strips("Chrome")[0].current.title == "Docs - project"
    assert tool.run(relative="next").success and w.reader.strips("Chrome")[0].current.title == "Docs - notes"
    assert tool.run(relative="previous").success and w.reader.strips("Chrome")[0].current.title == "Docs - project"
    assert not tool.run(number=9).success


def test_switch_does_not_guess_between_matches():
    w = chrome_edge_world()
    res = SwitchTabTool(w.reader).run(name="docs")
    assert not res.success and "2 tabs match" in res.message
    assert w.uia.calls == []


def test_switch_unknown_tab():
    w = chrome_edge_world()
    assert "I don't see a tab called" in SwitchTabTool(w.reader).run(name="spreadsheet").message


def test_switch_without_confirmation_is_not_a_success():
    w = chrome_edge_world()
    w.uia.select = lambda h, tab: w.uia.calls.append(("select", tab.title))      # presses, nothing changes
    res = SwitchTabTool(w.reader).run(name="github")
    assert not res.success and "can't confirm" in res.message


def test_switch_falls_back_to_the_keyboard_after_focusing_the_window():
    w = chrome_edge_world()
    w.uia.select_fails = True                       # UI Automation refuses; Ctrl+3 is what is left
    SwitchTabTool(w.reader).run(number=3)
    assert w.keys == [("ctrl", "3")] and (10, "focus") in w.shown


@pytest.mark.parametrize("text,args", [
    ("switch to the youtube tab", {"name": "youtube"}), ("go to tab 3", {"number": 3}),
    ("switch to the second tab in chrome", {"number": 2, "app": "chrome"}), ("next tab", {"relative": "next"}),
    ("go to the previous tab", {"relative": "previous"}),
])
def test_switch_phrases(text, args):
    t = SwitchTabTool()
    assert t.matches(text) and t.extract_arguments(text) == args


def test_switch_to_an_app_without_the_word_tab_is_the_window_tools():
    assert not SwitchTabTool().matches("switch to chrome")


# ---------------------------------------------------------------- close_tab

def test_close_current_tab():
    w = chrome_edge_world()
    res = CloseTabTool(w.reader).run(scope="current")
    assert res.success and res.message == "Closed 1 Chrome tab."
    assert [t.title for t in w.reader.strips("Chrome")[0].tabs][0] == "YouTube"


def test_close_named_tab_and_report_from_what_is_left():
    w = chrome_edge_world()
    res = CloseTabTool(w.reader).run(scope="named", name="github")
    assert res.success and "Closed 1 Chrome tab" in res.message
    assert "GitHub" not in [t.title for t in w.reader.strips("Chrome")[0].tabs]


def test_close_named_ambiguous_closes_nothing():
    w = chrome_edge_world()
    res = CloseTabTool(w.reader).run(scope="named", name="docs")
    assert not res.success and "closed nothing" in res.message.lower()
    assert not [c for c in w.uia.calls if c[0] == "close"]
    both = CloseTabTool(w.reader).run(scope="named", name="docs", match_all=True)
    assert both.success and "Closed 2 Chrome tabs" in both.message


def test_close_all_with_two_apps_asks_which_and_closes_nothing():
    w = chrome_edge_world()
    res = CloseTabTool(w.reader).run(scope="all")
    assert not res.success and "Chrome and Edge" in res.message and res.data.get("ask") == "app"
    assert not [c for c in w.uia.calls if c[0] == "close"]


def test_close_all_with_one_app_uses_it_and_says_the_window_went_too():
    w = World({10: ("Inbox - Google Chrome", "chrome.exe", ["A", "B", "C"])}, front=10)
    res = CloseTabTool(w.reader).run(scope="all")
    assert res.success and res.message.startswith("Closed 3 Chrome tabs") and "closed the Chrome window too" in res.message
    assert 10 not in w.layout


def test_close_all_in_a_named_app_leaves_the_others():
    w = chrome_edge_world()
    res = CloseTabTool(w.reader).run(scope="all", app="edge")
    assert res.success and "Closed 2 Edge tabs" in res.message
    assert [s.app for s in w.reader.strips()] == ["Chrome"]


def test_a_tab_that_asks_to_save_is_reported_as_still_open_and_is_not_a_success():
    w = World({10: ("x - Google Chrome", "chrome.exe", ["A", "Draft", "B", "C", "D", "E"])}, front=10)
    w.uia.stubborn.add("Draft")
    res = CloseTabTool(w.reader).run(scope="all", app="chrome")
    assert not res.success
    assert "Closed 5 of 6 Chrome tabs" in res.message and "1 is still open" in res.message and "asking to save" in res.message
    assert [t.title for t in w.reader.strips("Chrome")[0].tabs] == ["Draft"]
    assert 10 in w.layout                                 # so the window is not claimed closed


def test_nothing_closed_is_said_plainly():
    w = World({10: ("x - Google Chrome", "chrome.exe", ["Draft", "Other"])}, front=10)
    w.uia.stubborn.add("Draft")
    res = CloseTabTool(w.reader).run(scope="named", name="draft")
    assert not res.success and "still open" in res.message and "Closed" not in res.message


def test_vs_code_with_every_editor_closed_is_verified_and_the_window_stays():
    w = World({7: ("a.py - proj - Visual Studio Code", "Code.exe", ["a.py", "b.py"])}, front=7)
    res = CloseTabTool(w.reader).run(scope="all", app="vs code")
    assert res.success and res.message == "Closed 2 VS Code tabs." and 7 in w.layout


def test_unreadable_afterwards_is_never_reported_as_closed():
    w = chrome_edge_world()
    orig = w.uia.close

    def close_then_break(hwnd, tab):
        orig(hwnd, tab)
        w.uia.unreadable.add(hwnd)                         # the window stops answering
    w.uia.close = close_then_break
    res = CloseTabTool(w.reader).run(scope="current")
    assert not res.success and "can't confirm" in res.message and res.data.get("unconfirmed")
    assert "Closed" not in res.message


def test_close_uses_the_keyboard_only_after_focusing_and_only_for_the_current_tab():
    w = chrome_edge_world()
    w.uia.close_fails = True
    CloseTabTool(w.reader).run(scope="current")
    assert w.keys == [("ctrl", "w")] and (10, "focus") in w.shown


def test_close_a_numbered_tab():
    w = chrome_edge_world()
    res = CloseTabTool(w.reader).run(scope="number", number=2)
    assert res.success and "YouTube" not in [t.title for t in w.reader.strips("Chrome")[0].tabs]
    assert not CloseTabTool(w.reader).run(scope="number", number=40).success


@pytest.mark.parametrize("text,args", [
    ("close this tab", {"scope": "current"}),
    ("close the current tab", {"scope": "current"}),
    ("close all tabs", {"scope": "all"}),
    ("close all my tabs in chrome", {"scope": "all", "app": "chrome"}),
    ("close every tab", {"scope": "all"}),
    ("close the youtube tab", {"scope": "named", "name": "youtube"}),
    ("close tab named inbox", {"scope": "named", "name": "inbox"}),
    ("close tab 3", {"scope": "number", "number": 3}),
    ("close the second tab", {"scope": "number", "number": 2}),
    ("close all github tabs", {"scope": "named", "name": "github", "match_all": True}),
    ("close the chrome tab", {"scope": "current", "app": "chrome"}),
])
def test_close_phrases(text, args):
    t = CloseTabTool()
    assert t.matches(text) and t.extract_arguments(text) == args


def test_close_window_no_longer_takes_tab_phrases():
    from veronica.tools.window_tools import CloseWindowTool
    cw = CloseWindowTool()
    for text in ("close all tabs", "close this tab", "close the youtube tab", "close my tabs"):
        assert not cw.matches(text), text
    assert cw.matches("close spotify")
    assert not cw.run(app="all tabs").success


# ---------------------------------------------------------------- permission and routing

def test_permissions_listing_and_switching_are_safe_closing_asks():
    assert ListTabsTool().permission == PermissionLevel.SAFE and SwitchTabTool().permission == PermissionLevel.SAFE
    assert CloseTabTool().permission >= PermissionLevel.SENSITIVE


def test_closing_tabs_is_refused_when_the_user_says_no():
    w = chrome_edge_world()
    asked = []
    gate = PermissionGate(threshold=PermissionLevel.SENSITIVE, confirm_callback=lambda d, l: asked.append(d) or False)
    reg = ToolRegistry(gate)
    reg.register(CloseTabTool(w.reader))
    res = reg.dispatch("close_tab", scope="all", app="chrome")
    assert asked and not res.success and "NOT DONE" in res.message
    assert not [c for c in w.uia.calls if c[0] == "close"]


def test_routing_through_the_real_registry(monkeypatch):
    from veronica.tools.registry import build_default_registry
    from veronica.vision.desktop_state import Desktop
    monkeypatch.setattr(Desktop, "explorer_windows", lambda self: [])
    reg = build_default_registry(PermissionGate())
    for text, tool in [("close all tabs", "close_tab"), ("close this tab", "close_tab"), ("close the youtube tab", "close_tab"),
                       ("list my tabs", "list_tabs"), ("switch to the docs tab", "switch_tab"), ("close spotify", "close_window"),
                       ("switch to chrome", "window_control"), ("minimize all my tabs", "window_control")]:
        assert reg.find_fast_match(text).name == tool, text
