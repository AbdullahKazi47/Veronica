"""Finding folders by name anywhere: temp directory trees stand in for drive roots — no real disk."""
from __future__ import annotations

from pathlib import Path

import pytest

from veronica.core.understanding import Aliases
from veronica.tools.folder_search import FolderFinder, tier
from veronica.tools.system_tools import parse_open_request
from veronica.tools.window_tools import OpenPathTool


@pytest.fixture
def world(tmp_path):
    """home = tmp/Users/me, 'C:' = tmp/C, 'D:' = tmp/D (drive roots live outside home)."""
    home, c, d = tmp_path / "Users" / "me", tmp_path / "C", tmp_path / "D"
    for p in (home / "Documents", home / "Downloads", home / "Desktop", c, d):
        p.mkdir(parents=True)
    return home, c, d


def make_tool(world, tmp_path, **kw):
    home, c, d = world
    opened: list[str] = []
    tool = OpenPathTool(home=home, drives=[c, d], opener=opened.append, aliases=Aliases(tmp_path / "aliases.json"), **kw)
    return tool, opened


# ---------------------------------------------------------------- matching

@pytest.mark.parametrize("query,name,expected", [
    ("placement", "placement", 3), ("placement", "Placement", 3), ("placement", "PLACEMENT", 3),
    ("placement", "Placements", 2), ("placements", "placement", 2),
    ("my project", "my_project", 3), ("my project", "my-project", 3), ("my project", "MyProject", 2),
    ("placement", "placement_docs", 1), ("placement", "old placement files", 1),
    ("placement", "displacement", 0), ("placement", "notes", 0), ("", "x", 0),
])
def test_tiers(query, name, expected):
    assert tier(query, name) == expected


# ---------------------------------------------------------------- where it looks

def test_a_folder_at_a_drive_root_outside_home_is_found(world, tmp_path):
    home, c, d = world
    (c / "placement").mkdir()
    tool, opened = make_tool(world, tmp_path)
    res = tool.run(name="placement")
    assert res.success and opened == [str(c / "placement")]


def test_other_drives_are_searched_too(world, tmp_path):
    _home, _c, d = world
    (d / "Placements").mkdir()
    tool, opened = make_tool(world, tmp_path)
    assert tool.run(name="placement").success and opened == [str(d / "Placements")]


def test_user_folders_come_before_drive_roots(world, tmp_path):
    home, c, _d = world
    (home / "Documents" / "placement").mkdir()
    (c / "placement").mkdir()
    tool, opened = make_tool(world, tmp_path)
    assert tool.run(name="placement").success
    assert opened == [str(home / "Documents" / "placement")]          # no question: the first stage decided


def test_exact_outranks_a_longer_name_that_only_contains_it(world, tmp_path):
    _home, c, _d = world
    (c / "placement_docs").mkdir()
    (c / "Placement").mkdir()
    tool, opened = make_tool(world, tmp_path)
    assert tool.run(name="placement").success and opened == [str(c / "Placement")]


def test_a_weaker_match_is_still_better_than_nothing_when_it_is_the_only_one(world, tmp_path):
    _home, c, _d = world
    (c / "placement_docs").mkdir()
    tool, opened = make_tool(world, tmp_path)
    assert tool.run(name="placement").success and opened == [str(c / "placement_docs")]


def test_spaces_underscores_and_hyphens_do_not_matter(world, tmp_path):
    _home, c, _d = world
    (c / "college_notes-2024").mkdir()
    tool, opened = make_tool(world, tmp_path)
    assert tool.run(name="college notes 2024").success and opened == [str(c / "college_notes-2024")]


def test_system_and_bulk_folders_are_never_entered(world, tmp_path):
    home, c, _d = world
    for skipped in ("Windows", "Program Files", "Program Files (x86)", "ProgramData", "$Recycle.Bin",
                    "System Volume Information", "node_modules", ".git"):
        (c / skipped / "placement").mkdir(parents=True)
    (home / "AppData" / "Local" / "placement").mkdir(parents=True)
    tool, opened = make_tool(world, tmp_path)
    res = tool.run(name="placement")
    assert not res.success and opened == [] and "couldn't find" in res.message


def test_a_deeper_folder_is_found_by_the_last_stage(world, tmp_path):
    _home, c, _d = world
    (c / "a" / "b" / "c" / "d" / "Thesis").mkdir(parents=True)
    tool, opened = make_tool(world, tmp_path)
    assert tool.run(name="thesis").success and opened == [str(c / "a" / "b" / "c" / "d" / "Thesis")]


def test_inside_a_named_parent_and_on_a_named_drive(world, tmp_path):
    home, c, d = world
    (home / "Documents" / "Projects" / "Veronica").mkdir(parents=True)
    (c / "Veronica").mkdir()
    (d / "Veronica").mkdir()
    tool, opened = make_tool(world, tmp_path)
    assert tool.run(name="veronica", inside="projects").success
    assert opened[-1] == str(home / "Documents" / "Projects" / "Veronica")


def test_documents_are_found_on_the_drives_too(world, tmp_path):
    _home, c, _d = world
    (c / "papers").mkdir()
    (c / "papers" / "Resume 2024.pdf").write_text("x")
    tool, opened = make_tool(world, tmp_path)
    assert tool.run(name="resume 2024", kind="pdf").success and opened == [str(c / "papers" / "Resume 2024.pdf")]


def test_programs_are_still_never_run(world, tmp_path):
    _home, c, _d = world
    (c / "setup.exe").write_text("x")
    tool, opened = make_tool(world, tmp_path)
    assert not tool.run(name="setup.exe", kind="file").success and opened == []


# ---------------------------------------------------------------- ambiguity

def test_several_equally_good_folders_are_asked_about_not_guessed(world, tmp_path):
    home, c, d = world
    (c / "placement").mkdir()
    (d / "Placement").mkdir()
    (d / "work" / "placement").mkdir(parents=True)
    tool, opened = make_tool(world, tmp_path)
    res = tool.run(name="placement")
    assert not res.success and opened == []
    assert "3 folders called placement" in res.message and "Which one?" in res.message
    assert "on the" in res.message and res.data["ask"] == "path" and len(res.data["choices"]) == 3


def test_at_most_three_are_read_out(world, tmp_path):
    _home, c, d = world
    for i in range(5):
        (c / f"x{i}" / "dup").mkdir(parents=True)
    tool, _opened = make_tool(world, tmp_path)
    res = tool.run(name="dup")
    assert "5 folders" in res.message and "(and 2 more)" in res.message and len(res.data["choices"]) == 3


def test_answering_which_one_opens_it_and_remembers_the_choice(world, tmp_path):
    _home, c, d = world
    (c / "placement").mkdir()
    (d / "placement").mkdir()
    tool, opened = make_tool(world, tmp_path)
    ask = tool.run(name="placement")
    pick_text = "the second one"
    assert tool.matches(pick_text) and tool.extract_arguments(pick_text) == {"pick": 2}
    assert tool.run(**tool.extract_arguments(pick_text)).success
    assert opened == [ask.data["choices"][1]]
    assert tool.run(name="placement").success and opened[-1] == ask.data["choices"][1]        # asked once, never again


def test_a_choice_is_only_waited_for_a_little_while(world, tmp_path):
    _home, c, d = world
    (c / "placement").mkdir()
    (d / "placement").mkdir()
    now = [1000.0]
    tool, _opened = make_tool(world, tmp_path, clock=lambda: now[0])
    tool.run(name="placement")
    assert tool.matches("the first one")
    now[0] += 600
    assert not tool.matches("the first one")
    assert not tool.run(pick=1).success


def test_a_pick_with_nothing_pending_is_not_this_tools(world, tmp_path):
    tool, _opened = make_tool(world, tmp_path)
    assert not tool.matches("the second one") and not tool.matches("search deeper")


# ---------------------------------------------------------------- nothing found

def test_nothing_found_says_so_and_offers_a_deeper_look(world, tmp_path):
    tool, opened = make_tool(world, tmp_path)
    res = tool.run(name="zzz nothing")
    assert not res.success and opened == []
    assert "couldn't find a zzz nothing folder" in res.message and "deeper" in res.message and "drive" in res.message


def test_search_deeper_and_try_the_d_drive_continue_the_last_search(world, tmp_path):
    _home, c, d = world
    (d / "1" / "2" / "3" / "4" / "5" / "6" / "Archive2020").mkdir(parents=True)
    tool, opened = make_tool(world, tmp_path)
    assert not tool.run(name="archive2020").success
    assert tool.matches("search deeper") and tool.extract_arguments("search deeper") == {"deep": True}
    assert tool.run(**tool.extract_arguments("search deeper")).success
    assert opened[-1].endswith("Archive2020")


def test_a_named_drive_limits_the_search(world, tmp_path):
    _home, c, d = world
    (c / "Photos").mkdir()
    (d / "Photos").mkdir()
    tool, opened = make_tool(world, tmp_path, drive_map={"D": d, "C": c})
    tool.run(name="photos", kind="folder", drive="D")                     # "photos folder on the D drive"
    assert opened == [str(d / "Photos")]
    assert not tool.run(name="photos", drive="Q").success


# ---------------------------------------------------------------- memory

def test_a_resolved_name_is_remembered_and_instant_next_time(world, tmp_path):
    _home, c, _d = world
    (c / "placement").mkdir()
    tool, opened = make_tool(world, tmp_path)
    assert tool.run(name="placement").success
    assert Aliases(tmp_path / "aliases.json").place("placement") == str(c / "placement")
    tool.finder.find = lambda *a, **k: (_ for _ in ()).throw(AssertionError("searched again"))
    res = tool.run(name="Placement")
    assert res.success and opened[-1] == str(c / "placement") and "remembered" in res.message


def test_a_remembered_folder_that_is_gone_is_forgotten_and_searched_for_again(world, tmp_path):
    _home, c, d = world
    (c / "placement").mkdir()
    tool, opened = make_tool(world, tmp_path)
    tool.run(name="placement")
    (c / "placement").rmdir()
    (d / "placement").mkdir()
    assert tool.run(name="placement").success and opened[-1] == str(d / "placement")
    assert Aliases(tmp_path / "aliases.json").place("placement") == str(d / "placement")


def test_places_do_not_disturb_the_spoken_aliases(tmp_path):
    a = Aliases(tmp_path / "aliases.json")
    assert a.add("my notes", "Obsidian") and a.remember_place("placement", "C:\\placement")
    assert a.all() == {"my notes": "Obsidian"}
    assert a.apply("open my notes") == "open Obsidian"
    assert a.add("sheets", "Excel") and a.place("placement") == "C:\\placement"      # adding keeps them
    assert not a.remove("__places__") and a.place("placement")


# ---------------------------------------------------------------- limits

def test_the_search_stops_at_its_time_budget():
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        for i in range(40):
            (root / f"d{i}" / "inner").mkdir(parents=True)
        ticks = [0.0]

        def clock():
            ticks[0] += 1.0                  # every look at the clock costs a second
            return ticks[0]
        finder = FolderFinder(home=root / "none", drives=[root], max_seconds=1.8, clock=clock)
        res = finder.find("no such name")
        assert res.timed_out and res.matches == []


def test_the_search_stops_at_its_entry_budget(tmp_path):
    for i in range(60):
        (tmp_path / f"d{i}").mkdir()
        for j in range(5):
            (tmp_path / f"d{i}" / f"x{j}").mkdir()
    finder = FolderFinder(home=tmp_path / "none", drives=[tmp_path], max_entries=50)
    res = finder.find("zzz")
    assert res.timed_out


def test_a_big_tree_is_searched_quickly(tmp_path):
    import time
    for i in range(30):
        for j in range(30):
            (tmp_path / f"a{i}" / f"b{j}").mkdir(parents=True)
    (tmp_path / "a29" / "b29" / "target folder").mkdir()
    finder = FolderFinder(home=tmp_path / "none", drives=[tmp_path])
    t0 = time.monotonic()
    res = finder.find("target folder")
    assert res.best() and time.monotonic() - t0 < 2.0


def test_no_real_drives_are_touched_when_a_test_gives_its_own_home(tmp_path):
    assert FolderFinder(home=tmp_path).drives() == []


# ---------------------------------------------------------------- routing and verification

class _Index:
    """What parse_open_request needs from the app index."""

    def __init__(self, place=None, site=None, app=None):
        self._place, self._site, self._app = place, site, app

    def find_browser(self, via): return None
    def looks_like_domain(self, t): return False
    def find_folder(self, t): return None
    def find_app(self, t): return {"name": t} if self._app == t else None
    def find_place(self, t): return self._place if t == "placement" else None
    def site_for(self, t): return self._site


def test_bare_open_name_reaches_open_path_when_no_app_has_that_name():
    parsed = parse_open_request("open placement", _Index(place=Path("C:/placement"), site="https://www.placement.com"))
    assert parsed == {"kind": "place", "target": "placement"}        # beats the website with the same name


def test_an_app_by_that_name_still_wins_and_unknown_names_are_unchanged():
    assert parse_open_request("open placement", _Index(place=Path("C:/placement"), app="placement"))["kind"] == "app"
    assert parse_open_request("open netflix", _Index(site="https://www.netflix.com"))["kind"] == "site"
    assert parse_open_request("open nothing", _Index()) is None


def test_open_path_claims_the_routed_place():
    from veronica.tools.system_tools import _OpenRouting

    class R(_OpenRouting):
        def __init__(self):
            self.index, self._last = _Index(place=Path("C:/placement")), ("", None)
    routing = R()
    tool = OpenPathTool(routing=routing, opener=lambda p: None)
    assert tool.matches("open placement") and tool.extract_arguments("open placement") == {"name": "placement", "kind": "folder"}
    assert not tool.matches("open netflix")


def test_names_an_unknown_app_ignores_a_folder_she_can_find():
    from veronica.tools.app_index import names_an_unknown_app
    assert not names_an_unknown_app(_Index(place=Path("C:/placement")), "open placement")
    assert names_an_unknown_app(_Index(), "open placement")


def test_an_open_she_cannot_see_is_not_a_success(world, tmp_path):
    _home, c, _d = world
    (c / "placement").mkdir()
    seen = {"v": False}
    tool, opened = make_tool(world, tmp_path, verify=lambda path, is_dir: seen["v"])
    res = tool.run(name="placement")
    assert opened and not res.success and "can't confirm" in res.message and res.data["unconfirmed"]
    seen["v"] = True
    res = tool.run(name="placement")
    assert res.success and res.data["verified"]


def test_an_absolute_path_that_is_not_there_is_said_plainly(world, tmp_path):
    tool, opened = make_tool(world, tmp_path)
    res = tool.run(name="Q:\\nowhere\\at all")
    assert not res.success and opened == [] and "nothing at" in res.message
