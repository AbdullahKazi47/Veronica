"""PC <-> phone through the phone page: real HTTP against the real handler, with the token."""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from veronica.ui.remote import transfer
from veronica.ui.remote.server import RemoteServer, RemoteService
from veronica.ui.remote.transfer import PhoneLink, safe_name

TOKEN = "t0ken-for-tests"


class Orch:
    def new_session(self):
        return object()


@pytest.fixture
def phone(tmp_path, monkeypatch):
    link = PhoneLink(outbox=tmp_path / "out", inbox=tmp_path / "From phone")
    monkeypatch.setattr(transfer, "_link", link)
    from veronica.config import Config
    server = RemoteServer(RemoteService(Orch(), Config(), TOKEN), port=0)
    server.serve_in_background()
    yield link, f"http://127.0.0.1:{server.port}"
    server.shutdown()


def call(url, data=None, headers=None, token=TOKEN, method=None):
    h = {**(headers or {})}
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.headers, r.read()


def test_clipboard_text_reaches_the_phone_page(phone):
    link, base = phone
    link.send_text("https://example.com/recipe")
    status, _, body = call(base + "/api/inbox")
    items = json.loads(body)["items"]
    assert status == 200 and items[0]["kind"] == "text" and items[0]["text"] == "https://example.com/recipe"
    assert link.connected()                                   # the page asked: the island shows "connected"


def test_a_shared_file_downloads_and_no_path_is_ever_shown(phone, tmp_path):
    link, base = phone
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.7 hello")
    link.send_file(str(f))
    item = json.loads(call(base + "/api/inbox")[2])["items"][0]
    assert "path" not in item and str(tmp_path) not in json.dumps(item)
    status, headers, body = call(base + "/api/file?id=" + item["id"])
    assert status == 200 and body == b"%PDF-1.7 hello"
    assert "report.pdf" in headers["Content-Disposition"]


def test_only_shared_items_can_be_downloaded_and_only_with_the_token(phone, tmp_path):
    link, base = phone
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base + "/api/file?id=../../config/.env")
    assert e.value.code == 404
    f = tmp_path / "a.txt"
    f.write_text("x")
    item = link.send_file(str(f))
    with pytest.raises(urllib.error.HTTPError) as e:
        call(base + "/api/file?id=" + item["id"], token="wrong")
    assert e.value.code == 401


def test_shared_items_expire_after_an_hour(tmp_path):
    t = [1000.0]
    link = PhoneLink(outbox=tmp_path, inbox=tmp_path, clock=lambda: t[0])
    item = link.send_text("hi")
    assert link.items_for_phone()
    t[0] += 3601
    assert link.items_for_phone() == [] and link.file_for(item["id"]) is None


def test_a_file_from_the_phone_lands_in_downloads_from_phone_without_overwriting(phone):
    link, base = phone
    for _ in range(2):
        status, _, body = call(base + "/api/upload", data=b"photo-bytes", method="POST",
                               headers={"X-Filename": "IMG%201.jpg", "Content-Type": "application/octet-stream"})
        assert status == 200 and json.loads(body)["ok"]
    names = sorted(p.name for p in link.inbox.iterdir())
    assert names == ["IMG 1 (2).jpg", "IMG 1.jpg"]
    assert (link.inbox / "IMG 1.jpg").read_bytes() == b"photo-bytes"
    assert "from your phone" in link.last


@pytest.mark.parametrize("bad, good", [("../../Windows/system32/evil.dll", "evil.dll"), ("..\\..\\x.bat", "x.bat"),
                                       ("CON.txt", "_CON.txt"), ('a<b>:"c|d?.txt', "a_b___c_d_.txt"), ("", "file")])
def test_upload_names_cannot_escape_the_folder(bad, good):
    assert safe_name(bad) == good


def test_uploads_are_size_capped(tmp_path):
    link = PhoneLink(outbox=tmp_path, inbox=tmp_path / "in")
    with pytest.raises(ValueError):
        link.receive("big.bin", io.BytesIO(b""), transfer.MAX_UPLOAD + 1)
    with pytest.raises(ValueError):
        link.receive("short.bin", io.BytesIO(b"abc"), 10)        # stopped early: nothing kept
    assert not any((tmp_path / "in").glob("*"))


def test_the_island_hands_clipboard_and_files_to_the_phone(tmp_path, monkeypatch):
    import tempfile
    from pathlib import Path
    from veronica.ui.cli import CLIApp
    link = PhoneLink(outbox=tmp_path / "out", inbox=tmp_path / "in")
    monkeypatch.setattr(transfer, "_link", link)
    app = CLIApp.__new__(CLIApp)
    app._island_command("phone_text", {"text": "hello phone"})
    clip = Path(tempfile.gettempdir()) / "veronica-clip-test.png"
    clip.write_bytes(b"\x89PNG fake")
    app._island_command("phone_image", {"path": str(clip)})
    assert not clip.exists()                                  # read once, removed
    outside = tmp_path / "secret.txt"
    outside.write_text("no")
    app._island_command("phone_image", {"path": str(outside)})   # not the island's clipboard file: refused
    kinds = [i["kind"] for i in link.items_for_phone()]
    assert kinds == ["text", "image"] and outside.exists() and "Couldn't send" in link.last
