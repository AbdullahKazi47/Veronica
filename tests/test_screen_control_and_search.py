"""Screen control opens any Start-menu app (not just Notepad); search defaults to Google
and uses any engine the user names. Fake windows/apps — nothing is launched."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from veronica.tools.web_tools import WebSearchTool
from veronica.vision.app_windows import is_safe_app_name, open_or_activate, window_matches

APPS = {"whatsapp": {"name": "WhatsApp", "app_id": "WA!App"},
        "notepad": {"name": "Notepad", "app_id": "Notepad!App"},
        "chrome": {"name": "Google Chrome", "app_id": "Chrome"}}


class FakeIndex:
    def __init__(self):
        self.launched = []

    def find_app(self, name):
        return next((a for k, a in APPS.items() if k in name.lower() or name.lower() in a["name"].lower()), None)

    def launch_app(self, entry):
        self.launched.append(entry["name"])


@pytest.mark.parametrize("app,title,proc,ok", [
    ("Google Chrome", "Inbox - Gmail - Google Chrome", "chrome.exe", True),
    ("Google Chrome", "anything", "chrome.exe", True),
    ("Notepad", "Untitled - Notepad", "Notepad.exe", True),
    ("WhatsApp", "WhatsApp", "WhatsApp.Root.exe", True),
    ("WhatsApp", "Untitled - Notepad", "notepad.exe", False),
    ("Microsoft Edge", "Settings", "SystemSettings.exe", False),   # generic words don't match
])
def test_window_matching_is_by_the_apps_own_name(app, title, proc, ok):
    assert window_matches(app, title, proc) is ok


def test_switches_to_an_already_open_window_without_launching():
    index, activated = FakeIndex(), []
    windows = lambda: [(11, "Untitled - Notepad", "notepad.exe"), (22, "WhatsApp", "WhatsApp.exe")]
    ok, msg, hwnd = open_or_activate("whatsapp", index, windows, activated.append)
    assert ok and hwnd == 22 and activated == [22] and index.launched == []
    assert msg == "Switched to WhatsApp (WhatsApp)."


def test_launches_then_waits_for_the_window():
    index, activated = FakeIndex(), []
    appeared = {"calls": 0}

    def windows():
        appeared["calls"] += 1
        return [(33, "WhatsApp", "WhatsApp.exe")] if appeared["calls"] >= 3 else []
    ok, msg, hwnd = open_or_activate("WhatsApp", index, windows, activated.append, sleep=lambda s: None)
    assert ok and index.launched == ["WhatsApp"] and activated == [33]
    assert msg.startswith("Opened WhatsApp")


@pytest.mark.parametrize("name", [r"C:\Windows\System32\cmd.exe", "notepad.exe", "calc & del *", "powershell; rm -rf",
                                  "../../evil", "x" * 100])
def test_paths_commands_and_executables_are_refused(name):
    index = FakeIndex()
    ok, msg, _ = open_or_activate(name, index, lambda: [], lambda h: None)
    assert ok is False and index.launched == []
    assert not is_safe_app_name(name)


def test_not_installed_is_refused_not_guessed():
    index = FakeIndex()
    ok, msg, _ = open_or_activate("spotify", index, lambda: [], lambda h: None)
    assert ok is False and "isn't an installed Start-menu app" in msg and index.launched == []


def test_computer_use_open_application_is_no_longer_notepad_only():
    from veronica.vision import computer_use
    loop = computer_use.ComputerUseLoop.__new__(computer_use.ComputerUseLoop)
    loop.app_index = FakeIndex()
    action = SimpleNamespace(action_type="OPEN_APPLICATION", target_name="WhatsApp")
    windows = [(5, "WhatsApp", "WhatsApp.exe")]
    with patch("veronica.vision.app_windows._list_windows", return_value=windows), \
            patch("veronica.vision.app_windows._activate") as activate:
        result = loop._execute(action)
        assert result.success and result.message == "Switched to WhatsApp (WhatsApp)."
        activate.assert_called_once_with(5)
        blocked = loop._execute(SimpleNamespace(action_type="OPEN_APPLICATION", target_name=r"C:\x\evil.exe"))
    assert blocked.success is False and loop.app_index.launched == []


def test_editor_detection_accepts_modern_document_controls():
    from veronica.vision.text_entry import find_editor_target
    focused = SimpleNamespace(control_type="Document")
    layer = SimpleNamespace(uia_provider=None,
                            get_current_state=lambda: SimpleNamespace(focused_element=focused))
    assert find_editor_target(layer) is focused


# ---------------------------------------------------------------- search engines

class SiteIndex:
    """resolve_site: real-looking names resolve to their own site, others don't."""
    real = {"bing", "duckduckgo", "youtube", "amazon", "brave", "ecosia"}

    def site_for(self, name):
        n = name.lower().replace(" ", "")
        return f"https://www.{n}.com" if n in self.real else None

    def find_browser(self, name):
        return {"name": "Brave"} if name.lower() == "brave" else None

    def open_url(self, url, browser=""):
        self.opened = (url, browser)
        return True, "Brave" if browser else "your default browser"


GOOGLE = SimpleNamespace(assistant=SimpleNamespace(search_engine="Google",
                                                   search_url="https://www.google.com/search?q={query}"))


@pytest.mark.parametrize("text,query,engine,browser", [
    ("search for cheap flights", "cheap flights", "", ""),
    ("browse best laptops 2026", "best laptops 2026", "", ""),
    ("look up the weather in Goa", "the weather in Goa", "", ""),
    ("google biryani recipe", "biryani recipe", "Google", ""),
    ("search for cats on bing", "cats", "bing", ""),
    ("search bing for cats", "cats", "bing", ""),
    ("search for lofi using youtube", "lofi", "youtube", ""),
    ("search for flights on monday", "flights on monday", "", ""),          # not an engine
    ("search for my files on my laptop", "my files on my laptop", "", ""),  # not an engine
    ("search for shoes on amazon in brave", "shoes", "amazon", "brave"),
])
def test_search_phrases(text, query, engine, browser):
    tool = WebSearchTool(config=GOOGLE, index=SiteIndex())
    assert tool.matches(text), text
    assert tool.extract_arguments(text) == {"query": query, "engine": engine, "browser": browser}


def test_default_is_google_directly():
    tool = WebSearchTool(config=GOOGLE, index=SiteIndex())
    result = tool.run(query="cheap flights")
    assert result.message == "Searching Google for cheap flights."
    assert tool.index.opened[0] == "https://www.google.com/search?q=cheap+flights"


def test_named_engine_is_reached_by_name():
    tool = WebSearchTool(config=GOOGLE, index=SiteIndex())
    assert tool.run(query="cats", engine="bing").message == "Searching bing for cats."
    assert tool.index.opened[0] == "https://duckduckgo.com/?q=%21bing+cats"
    tool.run(query="privacy", engine="Brave Search", browser="brave")
    assert tool.index.opened == ("https://duckduckgo.com/?q=%21brave+privacy", "brave")


def test_without_config_google_is_still_the_default():
    tool = WebSearchTool(index=SiteIndex())
    assert tool.run(query="x").message == "Searching Google for x."
    assert tool.index.opened[0] == "https://duckduckgo.com/?q=%21google+x"
