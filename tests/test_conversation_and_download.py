"""Sticky conversation mode (stay open until an ending phrase) and the file downloader
(asks first, saves to Downloads, size cap, never bypasses logins)."""
from __future__ import annotations

import pytest

from veronica.config import Config
from veronica.safety.permissions import PermissionGate, PermissionLevel
from veronica.tools.download_tools import DownloadFileTool, _clean_name, _human
from veronica.tools.registry import build_default_registry


# ---------------------------------------------------------------- conversation mode

class _FakeResult:
    def __init__(self, text, success=True):
        self.text, self.success, self.error = text, success, ""


def _cli_in_conversation(monkeypatch, scripted):
    """A CLIApp wired so _listen() returns each scripted line in turn, conversation on."""
    from veronica.ui import cli as climod
    app = climod.CLIApp.__new__(climod.CLIApp)
    app.config = Config()
    app.config.voice.conversation_mode = True
    app.config.voice.conversation_idle_timeout_s = 300
    app.voice_mode = True
    app.wake_gate = object()
    said = []
    lines = list(scripted)
    app._say = lambda t: said.append(t)
    app._status = lambda *a, **k: None
    app._listen = lambda timeout_s=6: _FakeResult(lines.pop(0)) if lines else _FakeResult("", success=False)
    app.said = said
    return app


def test_conversation_stays_open_across_turns(monkeypatch):
    import time as _t
    app = _cli_in_conversation(monkeypatch, ["what's the weather", "and tomorrow", "thanks"])
    app._open_follow_up()                       # a reply just happened
    assert app._follow_up() == "what's the weather"
    app._open_follow_up()
    assert app._follow_up() == "and tomorrow"
    app._open_follow_up()
    assert app._follow_up() == ""               # "thanks" ends it
    assert app.said[-1] == "Anytime."


def test_conversation_rides_through_silence(monkeypatch):
    # silence (failed listen) then a real line: must NOT drop out on the first silence
    app = _cli_in_conversation(monkeypatch, ["", "", "play some music"])
    app._open_follow_up()
    assert app._follow_up() == "play some music"


def test_conversation_idle_timeout_returns_to_wake(monkeypatch):
    app = _cli_in_conversation(monkeypatch, [])   # only silence
    app.config.voice.conversation_idle_timeout_s = 0.05
    app._open_follow_up()
    # The loop rides silence until the idle deadline, then drops back to the wake word.
    assert app._follow_up() == ""


def test_non_conversation_mode_uses_short_window(monkeypatch):
    app = _cli_in_conversation(monkeypatch, ["something"])
    app.config.voice.conversation_mode = False
    app.config.voice.follow_up_seconds = 8
    app._open_follow_up()
    assert app._follow_up() == "something"
    app._open_follow_up()
    # a single silence drops back to the wake word in the old timed mode
    app._listen = lambda timeout_s=6: _FakeResult("", success=False)
    assert app._follow_up() == ""


# ---------------------------------------------------------------- downloader

def test_routing_and_permission():
    reg = build_default_registry(PermissionGate())
    for t in ("download https://example.com/report.pdf",
              "grab the file from https://site.org/data.csv",
              "save https://x.io/installer.exe"):
        assert reg.find_fast_match(t).name == "download_file", t
    assert DownloadFileTool.permission >= PermissionLevel.SENSITIVE   # always asks


def test_rejects_non_http():
    assert not DownloadFileTool().run(url="ftp://x/y").success
    assert not DownloadFileTool().run(url="").success


def test_filename_sanitized_and_size_formatted():
    traversal = _clean_name("../../etc/passwd")      # no separators survive -> can't escape Downloads
    assert "/" not in traversal and "\\" not in traversal
    assert _clean_name("my report.pdf?x=1") == "my report.pdf_x=1"
    assert _human(1536) == "1.5 KB" and _human(None) == "unknown size"


class _Resp:
    def __init__(self, body=b"hello world", headers=None, status=200):
        self._body, self.headers, self.status_code = body, headers or {}, status
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError("bad")
    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i+chunk_size]


def _tool_with(monkeypatch, resp, tmp_path):
    import requests
    cfg = Config()
    cfg.download.dir = str(tmp_path)
    monkeypatch.setattr(requests, "get", lambda *a, **k: resp)
    return DownloadFileTool(config=cfg)


def test_downloads_to_folder(monkeypatch, tmp_path):
    resp = _Resp(b"PDFDATA", headers={"Content-Type": "application/pdf"})
    tool = _tool_with(monkeypatch, resp, tmp_path)
    res = tool.run(url="https://example.com/report.pdf")
    assert res.success and (tmp_path / "report.pdf").read_bytes() == b"PDFDATA"
    assert "Downloads folder" in res.message


def test_uses_content_disposition_name(monkeypatch, tmp_path):
    resp = _Resp(b"x", headers={"Content-Disposition": 'attachment; filename="real name.zip"'})
    tool = _tool_with(monkeypatch, resp, tmp_path)
    res = tool.run(url="https://example.com/dl?id=9")
    assert (tmp_path / "real name.zip").exists() and res.success


def test_size_cap_before_download(monkeypatch, tmp_path):
    resp = _Resp(b"x" * 10, headers={"Content-Length": str(5 * 1024 * 1024)})
    cfg = Config(); cfg.download.dir = str(tmp_path); cfg.download.max_mb = 1
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: resp)
    res = DownloadFileTool(config=cfg).run(url="https://x/big.bin")
    assert not res.success and "limit" in res.message
    assert not any(tmp_path.iterdir())          # nothing saved


def test_size_cap_during_stream(monkeypatch, tmp_path):
    resp = _Resp(b"x" * (2 * 1024 * 1024), headers={})   # no Content-Length, overflows mid-stream
    cfg = Config(); cfg.download.dir = str(tmp_path); cfg.download.max_mb = 1
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: resp)
    res = DownloadFileTool(config=cfg).run(url="https://x/stream")
    assert not res.success and "Nothing was kept" in res.message
    assert not any(tmp_path.iterdir())


def test_does_not_overwrite_existing(monkeypatch, tmp_path):
    (tmp_path / "report.pdf").write_text("old")
    resp = _Resp(b"new", headers={"Content-Type": "application/pdf"})
    tool = _tool_with(monkeypatch, resp, tmp_path)
    res = tool.run(url="https://example.com/report.pdf")
    assert (tmp_path / "report (2).pdf").read_bytes() == b"new"
    assert (tmp_path / "report.pdf").read_text() == "old" and res.success


@pytest.mark.parametrize("url", ["magnet:?xt=urn:btih:abc", "ftp://x/y.zip", "file:///c/secret"])
def test_rejects_non_web_schemes(url):
    # magnet / ftp / file must be refused, not coerced to https
    res = DownloadFileTool().run(url=url)
    assert not res.success and "http" in res.message.lower()
