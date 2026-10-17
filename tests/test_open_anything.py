"""Nothing hard-coded: apps, browsers, folders and sites come from discovery.
Fake app/browser lists stand in for Windows; nothing is actually launched."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from veronica.tools import system_tools
from veronica.tools.app_index import AppIndex
from veronica.tools.system_tools import (
    OpenApplicationTool, SetVolumeTool, _OpenRouting, parse_open_request,
)
from veronica.tools.web_tools import OpenWebsiteTool, WebSearchTool, split_browser
from veronica.tools.youtube_tools import YouTubeSearchTool

APPS = [{"name": "WhatsApp", "app_id": "WA!App"}, {"name": "WhatsApp Web", "app_id": "Chrome._crx_wa"},
        {"name": "Notepad", "app_id": "Notepad!App"}, {"name": "Notepad++", "app_id": "npp"},
        {"name": "Calculator", "app_id": "Calc!App"}, {"name": "Brave", "app_id": "Brave"},
        {"name": "Settings", "app_id": "settings!App"}]
BROWSERS = [{"name": "Brave", "exe": r"C:\b\brave.exe"}, {"name": "Google Chrome", "exe": r"C:\g\chrome.exe"},
            {"name": "Microsoft Edge", "exe": r"C:\m\msedge.exe"}, {"name": "Mozilla Firefox", "exe": r"C:\f\firefox.exe"}]


@pytest.fixture
def index(tmp_path):
    home = tmp_path / "home"
    for d in ("Downloads", "Documents", "Music"):
        (home / d).mkdir(parents=True)
    idx = AppIndex(app_loader=lambda: list(APPS), browser_loader=lambda: list(BROWSERS),
                   site_checker=lambda url: "youtube" in url or "netflix" in url, home=home)
    idx._app_cache = tmp_path / "app_index.json"
    idx._site_cache = tmp_path / "site_cache.json"
    return idx


@pytest.fixture(autouse=True)
def domains_resolve(monkeypatch):
    # "github.com" resolves, "notes.txt" doesn't — without touching real DNS.
    monkeypatch.setattr(system_tools, "_resolves", lambda host: not host.endswith((".txt", ".pdf")))


@pytest.mark.parametrize("name,expected", [
    ("whatsapp", "WhatsApp"), ("WhatsApp app", "WhatsApp"), ("notepad", "Notepad"),
    ("notepad plus plus", None), ("calculater", "Calculator"), ("spotify", None),
])
def test_find_app_prefers_exact_then_shortest(index, name, expected):
    hit = index.find_app(name)
    assert (hit["name"] if hit else None) == expected


@pytest.mark.parametrize("name,expected", [
    ("brave", "Brave"), ("chrome", "Google Chrome"), ("edge", "Microsoft Edge"),
    ("firefox", "Mozilla Firefox"), ("safari", None),
])
def test_find_browser_by_any_part_of_its_name(index, name, expected):
    hit = index.find_browser(name)
    assert (hit["name"] if hit else None) == expected


@pytest.mark.parametrize("text,kind,target,browser", [
    ("open youtube in brave", "site", "youtube", "brave"),
    ("open YouTube on Chrome please", "site", "YouTube", "Chrome"),
    ("open whatsapp", "app", "whatsapp", None),
    ("launch the calculator app", "app", "calculator", None),
    ("open downloads", "folder", "downloads", None),
    ("open my documents folder", None, None, None),     # "my ..." is never guessed
    ("go to netflix", "site", "netflix", ""),
    ("open github.com in edge", "site", "github.com", "edge"),
    ("open notes.txt", None, None, None),              # file name, not a domain
    ("open youtube on my phone", None, None, None),    # not a browser: LLM decides
    ("start writing a poem about the sea", None, None, None),
    ("launch spotify", None, None, None),              # not installed: LLM explains
    ("open camera and America", None, None, None),     # misheard sentence, not a website (real log)
    ("go to some random place", None, None, None),
])
def test_parse_open_request(index, text, kind, target, browser):
    p = parse_open_request(text, index)
    if kind is None:
        assert p is None
    else:
        assert p["kind"] == kind and p["target"] == target
        if browser is not None:
            assert p.get("browser") == browser


def test_open_website_in_named_browser(index):
    tool = OpenWebsiteTool(routing=_OpenRouting(index=index))
    assert tool.matches("open youtube in brave")
    with patch("veronica.tools.app_index.subprocess.Popen") as popen:
        result = tool.run(**tool.extract_arguments("open youtube in brave"))
    assert result.success and result.message == "Opening youtube in Brave."
    popen.assert_called_once_with([r"C:\b\brave.exe", "https://www.youtube.com"])


def test_unknown_browser_says_which_ones_exist(index):
    result = OpenWebsiteTool(routing=_OpenRouting(index=index)).run(site="youtube", browser="safari")
    assert result.success is False and "Brave" in result.message and "Google Chrome" in result.message


def test_default_browser_comes_from_config_or_system(index):
    with patch("veronica.tools.app_index.os.startfile", create=True) as startfile, \
            patch("veronica.tools.app_index.platform.system", return_value="Windows"):
        ok, where = index.open_url("https://example.org")
    assert ok and where == "your default browser"
    startfile.assert_called_once_with("https://example.org")
    index.default_browser = "Firefox"
    with patch("veronica.tools.app_index.subprocess.Popen") as popen:
        assert index.open_url("https://example.org") == (True, "Mozilla Firefox")
    popen.assert_called_once_with([r"C:\f\firefox.exe", "https://example.org"])


def test_unknown_site_names_resolve_without_a_list(index):
    assert index.resolve_site("netflix") == "https://www.netflix.com"
    # Not a site: a search on the default engine (Google), never a DuckDuckGo guess.
    assert index.site_for("irctc") is None
    assert index.resolve_site("irctc") == "https://www.google.com/search?q=irctc"
    assert index.resolve_site("github.com/me") == "https://github.com/me"
    index._site_checker = lambda url: (_ for _ in ()).throw(AssertionError("cached, must not re-check"))
    assert index.resolve_site("netflix") == "https://www.netflix.com"


def test_open_application_launches_discovered_app_and_folder(index, tmp_path):
    tool = OpenApplicationTool(routing=_OpenRouting(index=index))
    with patch("veronica.tools.app_index.subprocess.Popen") as popen:
        assert tool.run(app_name="whatsapp").message == "Opening WhatsApp."
    popen.assert_called_once_with(["explorer.exe", "shell:AppsFolder\\WA!App"])
    with patch("veronica.tools.system_tools.os.startfile", create=True) as startfile, \
            patch("veronica.tools.system_tools.platform.system", return_value="Windows"):
        assert tool.run(app_name="music").message == "Opening your Music folder."
    assert startfile.call_args.args[0].endswith("Music")
    assert "couldn't find an app called spotify" in tool.run(app_name="spotify").message.lower()


def test_new_app_found_after_refresh(index):
    installed = list(APPS)
    index._app_loader = lambda: installed
    assert index.find_app("spotify") is None
    installed.append({"name": "Spotify", "app_id": "Spotify!App"})
    index._last_refresh = 0  # allow the refresh-on-miss
    assert index.find_app("spotify")["name"] == "Spotify"


def test_play_and_search_take_a_service_and_a_browser(index):
    index._site_checker = lambda url: any(s in url for s in ("youtube", "spotify", "soundcloud"))
    yt = YouTubeSearchTool(index=index, scheme_lookup=lambda name: None)
    assert yt.extract_arguments("play lofi beats on youtube in brave") == \
        {"query": "lofi beats", "service": "youtube", "browser": "brave"}
    assert yt.extract_arguments("play music in spotify") == {"query": "music", "service": "spotify", "browser": ""}
    assert yt.extract_arguments("play songs in hindi") == {"query": "songs in hindi", "service": "", "browser": ""}
    search = WebSearchTool(index=index)
    assert search.extract_arguments("search for cheap flights in firefox") == \
        {"query": "cheap flights", "engine": "", "browser": "firefox"}
    with patch("veronica.tools.app_index.subprocess.Popen") as popen:
        assert yt.run(query="lofi", browser="brave").message == "Playing lofi on YouTube in Brave."
    exe, url = popen.call_args.args[0]
    assert exe == r"C:\b\brave.exe"
    assert url == "https://duckduckgo.com/?q=%5Clofi+site%3Ayoutube.com"  # first result = the video plays
    assert split_browser("music in hindi", index) == ("music in hindi", "")


def test_play_on_web_service_when_app_not_installed(index):
    index._site_checker = lambda url: "spotify" in url
    tool = YouTubeSearchTool(index=index, scheme_lookup=lambda name: None)
    with patch("veronica.tools.app_index.os.startfile", create=True) as startfile, \
            patch("veronica.tools.app_index.platform.system", return_value="Windows"):
        result = tool.run(**tool.extract_arguments("play arijit singh on spotify"))
    assert result.message == "Playing arijit singh on spotify."
    assert startfile.call_args.args[0] == "https://duckduckgo.com/?q=%5Carijit+singh+site%3Aspotify.com"


def test_play_opens_installed_app_through_its_own_link_type(index):
    index._apps = APPS + [{"name": "Spotify", "app_id": "Spotify!App"}]
    tool = YouTubeSearchTool(index=index, scheme_lookup=lambda name: name.lower() if name == "Spotify" else None)
    with patch("os.startfile", create=True) as startfile:
        result = tool.run(query="lofi beats", service="spotify")
    assert result.message == "Playing lofi beats on Spotify."
    startfile.assert_called_once_with("spotify:search:lofi%20beats")


def test_default_play_service_is_configurable(index):
    from types import SimpleNamespace
    cfg = SimpleNamespace(assistant=SimpleNamespace(default_play_service="SoundCloud"))
    index._site_checker = lambda url: "soundcloud" in url
    tool = YouTubeSearchTool(config=cfg, index=index, scheme_lookup=lambda n: None)
    with patch("veronica.tools.app_index.os.startfile", create=True) as startfile, \
            patch("veronica.tools.app_index.platform.system", return_value="Windows"):
        assert tool.run(query="chill").message == "Playing chill on SoundCloud."
    assert "site%3Asoundcloud.com" in startfile.call_args.args[0]


# ---------------------------------------------------------------- volume

@pytest.mark.parametrize("text,args", [
    ("set volume to 60", {"action": "set", "level": 60}),
    ("volume to 30 percent", {"action": "set", "level": 30}),
    ("turn the volume up", {"action": "up"}),
    ("volume down", {"action": "down"}),
    ("lower the volume", {"action": "down"}),
    ("louder", {"action": "up"}),
    ("mute", {"action": "mute"}),
    ("unmute the sound", {"action": "unmute"}),
])
def test_volume_phrases(text, args):
    tool = SetVolumeTool()
    assert tool.matches(text), text
    assert tool.extract_arguments(text) == args


class FakeVolume:
    def __init__(self, level=0.18, muted=True):
        self.level, self.muted = level, muted

    def GetMasterVolumeLevelScalar(self):
        return self.level

    def SetMasterVolumeLevelScalar(self, v, ctx):
        self.level = v

    def GetMute(self):
        return self.muted

    def SetMute(self, m, ctx):
        self.muted = bool(m)


def test_volume_up_unmutes_and_clamps(monkeypatch):
    vol = FakeVolume(level=0.95, muted=True)
    monkeypatch.setattr(system_tools, "_endpoint_volume", lambda: (vol, "Speakers"))
    monkeypatch.setattr(system_tools.platform, "system", lambda: "Windows")
    tool = SetVolumeTool()
    assert tool.run(action="up").message == "Volume 100 percent."
    assert vol.muted is False and vol.level == 1.0
    assert tool.run(action="mute").message == "Muted." and vol.muted is True
    assert system_tools.output_status() == {"device": "Speakers", "level": 100, "muted": True}


def test_unknown_play_service_searches_google_not_duckduckgo(index):
    index._site_checker = lambda url: "youtube" in url
    tool = YouTubeSearchTool(index=index, scheme_lookup=lambda name: None)
    with patch("veronica.tools.app_index.os.startfile", create=True) as startfile, \
            patch("veronica.tools.app_index.platform.system", return_value="Windows"):
        tool.run(query="songs", service="blorptune")
    assert startfile.call_args.args[0] == "https://www.google.com/search?q=songs+blorptune"


# -- "did I mishear that app name?" — the speech escalation trigger ----------------
@pytest.mark.parametrize("text, escalate", [
    ("close Crow", True),                      # the real miss: Chrome heard as Crow
    ("open Lozenge", True),
    ("close Chrome", False),                   # a browser she has
    ("open Notepad", False),
    ("open Notepadd", False),                  # fuzzy match rescues it; no cloud needed
    ("open WhatsApp and Calculator", False),   # two programs, not one odd name
    ("open Brave, Notepad and Calculator", False),
    ("open WhatsApp and Lozenge", True),       # one good name does not excuse the other
    ("open the downloads folder", False),      # folders have their own lookup
    ("open Music", False),                     # a real folder in the fake home
    ("close that", False),
    ("close all windows", False),
    ("what's on my calendar tomorrow", False),
    ("thanks", False),
])
def test_only_an_unfindable_program_asks_for_a_second_opinion(index, text, escalate):
    from veronica.tools.app_index import names_an_unknown_app
    assert names_an_unknown_app(index, text) is escalate


def test_a_broken_app_index_never_forces_a_cloud_request(index):
    """Escalation is an optimisation; a discovery failure must not send audio off the PC."""
    from veronica.tools.app_index import names_an_unknown_app
    index.find_app = lambda name: (_ for _ in ()).throw(OSError("registry unavailable"))
    assert names_an_unknown_app(index, "open Lozenge") is False


def test_weather_codes_cover_drizzle_and_showers():
    from veronica.tools.weather_tools import _WEATHER_CODES
    for code in (53, 55, 81, 82, 96, 99):
        assert code in _WEATHER_CODES
