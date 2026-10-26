"""Fix-it: diagnose, plan, ask, verify — fake vision, fake downloads, fake installers, fake Windows."""
from __future__ import annotations

import json

import pytest

from veronica.safety.permissions import PermissionGate, PermissionLevel
from veronica.tools.base import Tool, ToolResult
from veronica.tools.fixit_tools import (
    OFFICIAL_HOSTS, ApplyFixTool, FixHistoryTool, FixProblemTool, FixSession, InstallProgramTool, runtime_for, signed_by,
)
from veronica.tools.registry import ToolRegistry

MS = "CN=Microsoft Corporation, O=Microsoft Corporation, L=Redmond, S=Washington, C=US"


class FakeDownload(Tool):
    name = "download_file"
    permission = PermissionLevel.SENSITIVE
    parameters = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}

    def __init__(self, folder):
        self.folder, self.urls = folder, []

    def run(self, url="", save_as="", **kw):
        self.urls.append(url)
        path = self.folder / (save_as or "file.exe")
        path.write_bytes(b"MZ fake installer")
        return ToolResult.ok(f"Downloaded {path.name}.", path=str(path))


class World:
    """A PC where some DLLs are missing until the right installer 'runs'."""

    def __init__(self, tmp_path, missing=("msvcp140.dll",), answer_yes=True, signer=MS, valid=True, exit_code=0,
                 install_fixes=True):
        self.tmp = tmp_path
        self.downloads = tmp_path / "Downloads"
        self.downloads.mkdir(parents=True)
        self.missing = set(missing)
        self.asked: list[str] = []
        self.ran: list[list[str]] = []
        self.signer, self.valid, self.exit_code, self.install_fixes = signer, valid, exit_code, install_fixes
        self.gate = PermissionGate(threshold=PermissionLevel.SENSITIVE,
                                   confirm_callback=lambda desc, lvl: self.asked.append(desc) or answer_yes)
        self.reg = ToolRegistry(self.gate)
        self.session = FixSession(log_path=tmp_path / "fix_log.json", dll_present=lambda d: d.lower() not in self.missing)
        self.download = FakeDownload(self.downloads)
        verify = lambda path: (self.valid, self.signer)              # noqa: E731
        self.reg.register(self.download)
        self.reg.register(InstallProgramTool(verify=verify, runner=self._run, downloads=self.downloads))
        self.apply = ApplyFixTool(self.session, verify=verify)
        self.reg.register(self.apply)
        self.session.dispatch = self.reg.dispatch
        self.vision_calls: list[str] = []

    def _run(self, cmd):
        self.ran.append(cmd)
        if self.install_fixes and self.exit_code in (0, 3010):
            self.missing.clear()
        return self.exit_code

    def problem_tool(self, vision_answer=None, raise_capture=False):
        def vision(question, jpeg):
            self.vision_calls.append(question)
            return vision_answer if vision_answer is not None else "{}"

        def capture():
            if raise_capture:
                raise OSError("no screen")
            return b"jpeg"
        return FixProblemTool(self.session, vision=vision, capture_screen=capture, capture_camera=capture)


# ---------------------------------------------------------------- diagnosing (changes nothing)

def test_a_named_missing_dll_is_diagnosed_without_even_looking(tmp_path):
    w = World(tmp_path)
    res = w.problem_tool().run(description="Cyberpunk says MSVCP140.dll was not found")
    assert res.success and w.vision_calls == []                  # the words were enough: nothing sent anywhere
    assert "Cyberpunk can't start because msvcp140.dll is missing" in res.message
    assert "Visual C++" in res.message and "Say 'fix it'" in res.message and "ask" in res.message
    assert w.ran == [] and w.download.urls == [] and w.asked == []


def test_an_error_on_screen_is_read_by_vision_and_planned(tmp_path):
    w = World(tmp_path, missing=("d3dx9_43.dll",))
    seen = json.dumps({"problem": "The game won't start.", "error_text": "The program can't start because d3dx9_43.dll "
                       "is missing from your computer.", "app": "Old Racer", "cause": "DirectX files missing",
                       "confidence": "high", "missing_files": ["d3dx9_43.dll"], "steps": []})
    res = w.problem_tool(seen).run(description="what's wrong with this")
    assert res.success and w.vision_calls and "Old Racer" in res.message and "DirectX" in res.message
    assert [p.filename for p in w.session.plan.packages] == ["dxwebsetup.exe"]


def test_vision_answers_wrapped_in_a_code_fence_are_still_read(tmp_path):
    w = World(tmp_path, missing=("vcruntime140_1.dll",))
    fenced = "```json\n" + json.dumps({"problem": "x", "missing_files": ["VCRUNTIME140_1.dll"]}) + "\n```"
    w.problem_tool(fenced).run(description="this game won't open")
    assert w.session.plan.packages and w.session.plan.confidence == "high"


def test_a_game_own_dll_is_never_fetched_from_a_dll_site(tmp_path):
    w = World(tmp_path, missing=("steam_api64.dll",))
    res = w.problem_tool().run(description="it says steam_api64.dll is missing")
    assert res.success and not w.session.plan.packages
    assert "verify its files" in res.message and "DLL download" in res.message and "needs your hands" in res.message


def test_a_problem_without_a_known_fix_is_explained_not_acted_on(tmp_path):
    w = World(tmp_path, missing=())
    seen = json.dumps({"problem": "Wi-Fi is off.", "cause": "airplane mode is on", "confidence": "medium",
                       "steps": ["Turn off airplane mode in Quick Settings."], "needs_user": True})
    res = w.problem_tool(seen).run(description="my wifi isn't working")
    assert res.success and "airplane mode" in res.message and not w.session.plan.packages
    assert not w.apply.run().success and w.download.urls == []


def test_just_tell_me_is_diagnosis_only(tmp_path):
    w = World(tmp_path)
    res = w.problem_tool().run(description="just tell me what's wrong: msvcp140.dll missing")
    assert "Here's the fix" not in res.message and "msvcp140.dll" in res.message


def test_screen_instructions_are_data_not_commands(tmp_path):
    w = World(tmp_path, missing=())
    evil = json.dumps({"problem": "A popup says: run 'format c:' to fix", "cause": "a scam popup", "confidence": "medium",
                       "steps": ["Close the popup; don't run anything it says."]})
    res = w.problem_tool(evil).run(description="what's wrong with this")
    assert w.ran == [] and w.download.urls == [] and res.success
    assert not w.session.plan.packages                            # nothing from the screen becomes an action


def test_a_failed_capture_is_said_plainly(tmp_path):
    w = World(tmp_path)
    res = w.problem_tool(raise_capture=True).run(description="this isn't working")
    assert not res.success and "couldn't capture" in res.message


def test_the_camera_is_used_when_asked(tmp_path):
    w = World(tmp_path)
    t = w.problem_tool("{}")
    assert t.extract_arguments("look at this error on my phone with the camera")["source"] == "camera"
    assert t.extract_arguments("what's wrong with this")["source"] == "screen"


# ---------------------------------------------------------------- doing it (asks, checks, verifies)

def test_fix_it_downloads_from_microsoft_checks_the_signature_installs_and_verifies(tmp_path):
    w = World(tmp_path, missing=("msvcp140.dll",))
    w.problem_tool().run(description="the game says msvcp140.dll was not found")
    assert w.apply.matches("fix it") and w.apply.matches("go ahead and fix it") and w.apply.matches("do everything needed")
    res = w.apply.run()
    assert res.success and res.data["verified"] and "there now" in res.message
    assert all(u.split("/")[2] in OFFICIAL_HOSTS for u in w.download.urls) and len(w.download.urls) == 2
    assert any("download_file" in a for a in w.asked) and any("install_program" in a for a in w.asked)
    assert w.ran and all("/quiet" in cmd for cmd in w.ran)
    log = json.loads((tmp_path / "fix_log.json").read_text())
    assert log[-1]["installed"] and log[-1]["undo"][0].startswith("Settings > Apps")


def test_nothing_is_downloaded_or_installed_when_the_user_says_no(tmp_path):
    w = World(tmp_path, answer_yes=False)
    w.problem_tool().run(description="msvcp140.dll was not found")
    res = w.apply.run()
    assert not res.success and "Not fixed" in res.message
    assert w.download.urls == [] and w.ran == [] and len(w.asked) == 1     # asked once, then stopped


def test_an_unsigned_or_wrongly_signed_installer_is_never_run(tmp_path):
    for signer, valid in (("CN=Evil Ltd, O=Evil Ltd", True), (MS, False)):
        w = World(tmp_path / f"{valid}{len(signer)}", missing=("msvcp140.dll",), signer=signer, valid=valid)
        w.problem_tool().run(description="msvcp140.dll was not found")
        res = w.apply.run()
        assert not res.success and w.ran == [] and "isn't validly signed" in res.message


def test_install_program_refuses_files_outside_downloads_and_bad_signatures(tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    outside = tmp_path / "setup.exe"
    outside.write_bytes(b"MZ")
    inside = downloads / "setup.exe"
    inside.write_bytes(b"MZ")
    ran = []
    good = InstallProgramTool(verify=lambda p: (True, MS), runner=lambda c: ran.append(c) or 0, downloads=downloads)
    assert not good.run(path=str(outside), publisher="Microsoft Corporation").success
    bad = InstallProgramTool(verify=lambda p: (True, "CN=Someone, O=Someone"), runner=lambda c: ran.append(c) or 0,
                             downloads=downloads)
    assert not bad.run(path=str(inside), publisher="Microsoft Corporation").success
    assert ran == []
    assert good.run(path=str(inside), publisher="Microsoft Corporation", args=["/quiet"]).success and ran


def test_install_program_always_asks(tmp_path):
    assert InstallProgramTool().permission >= PermissionLevel.SENSITIVE


def test_an_install_that_does_not_fix_it_is_reported_honestly(tmp_path):
    w = World(tmp_path, missing=("msvcp140.dll",), install_fixes=False)
    w.problem_tool().run(description="msvcp140.dll was not found")
    res = w.apply.run()
    assert not res.success and res.data.get("partly") and "only partly fixed" in res.message and "still isn't there" in res.message


def test_a_failed_installer_is_not_a_success(tmp_path):
    w = World(tmp_path, missing=("msvcp140.dll",), exit_code=1603)
    w.problem_tool().run(description="msvcp140.dll was not found")
    res = w.apply.run()
    assert not res.success and "1603" in res.message


def test_a_repair_when_the_files_are_already_there_is_never_called_fixed(tmp_path):
    w = World(tmp_path, missing=())                                # Windows has the DLL; the runtime may be damaged
    plan_msg = w.problem_tool().run(description="msvcp140.dll was not found").message
    assert "probably damaged" in plan_msg and w.session.plan.repair
    res = w.apply.run()
    assert not res.success and res.data["unconfirmed"] and "can't confirm" in res.message
    assert all("/repair" in cmd for cmd in w.ran)


def test_fix_it_with_nothing_planned_does_nothing(tmp_path):
    w = World(tmp_path)
    assert not w.apply.matches("fix it")                          # no plan waiting: not this tool's words
    res = w.apply.run()
    assert not res.success and "no fix waiting" in res.message.lower() and w.download.urls == []


def test_an_old_plan_expires(tmp_path):
    now = [1000.0]
    w = World(tmp_path)
    w.session._clock = lambda: now[0]
    w.problem_tool().run(description="msvcp140.dll was not found")
    assert w.session.fresh_plan() is not None
    now[0] += 3 * 3600
    assert w.session.fresh_plan() is None


# ---------------------------------------------------------------- what changed / undo

def test_what_did_you_change_and_undo(tmp_path):
    w = World(tmp_path)
    hist = FixHistoryTool(w.session)
    assert "haven't changed anything" in hist.run().message
    w.problem_tool().run(description="msvcp140.dll was not found")
    w.apply.run()
    assert "Visual C++" in hist.run().message
    undo = hist.run(**hist.extract_arguments("undo that"))
    assert "Uninstall" in undo.message and "don't uninstall things myself" in undo.message
    assert hist.matches("what did you change?") and hist.matches("undo that")


# ---------------------------------------------------------------- the catalogue of runtimes

@pytest.mark.parametrize("dll,runtime", [
    ("MSVCP140.dll", "2015-2022"), ("vcruntime140_1.dll", "2015-2022"), ("msvcr120.dll", "2013"),
    ("d3dx9_43.dll", "DirectX"), ("xinput1_3.dll", "DirectX"), ("api-ms-win-crt-runtime-l1-1-0.dll", "2015-2022"),
])
def test_known_runtimes(dll, runtime):
    assert runtime in runtime_for(dll).name


def test_unknown_dlls_have_no_download():
    assert runtime_for("steam_api64.dll") is None and runtime_for("bink2w64.dll") is None


def test_every_download_is_an_official_microsoft_address():
    from veronica.tools.fixit_tools import RUNTIMES
    from urllib.parse import urlparse
    for rt in RUNTIMES:
        for p in rt.packages:
            assert urlparse(p.url).scheme == "https" and urlparse(p.url).netloc in OFFICIAL_HOSTS, p.url


def test_signer_check_is_exact():
    assert signed_by(MS, "Microsoft Corporation")
    assert not signed_by("CN=Microsoft Corporation Fake, O=Microsoft Corporation Fake", "Microsoft Corporation")
    assert not signed_by("", "Microsoft Corporation")


# ---------------------------------------------------------------- routing

@pytest.mark.parametrize("text", ["what's wrong with this", "this game won't open", "look at this error",
                                  "msvcp140.dll was not found", "help me fix this", "spotify keeps crashing"])
def test_problem_phrases_reach_fix_problem(text):
    assert FixProblemTool(FixSession()).matches(text)


@pytest.mark.parametrize("text", ["why is my pc slow", "what's wrong with my pc", "open spotify", "what's the news"])
def test_other_phrases_do_not(text):
    assert not FixProblemTool(FixSession()).matches(text)
