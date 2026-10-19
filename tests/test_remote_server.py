"""Remote access server: auth, chat, voice, permission questions (no network, no mic)."""
from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from veronica.ui.remote import server as remote
from veronica.ui.remote.server import (PendingQuestions, RemoteServer, RemoteService,
                                       load_or_create_token, remote_confirmer, wav_to_pcm16k)

TOKEN = "t" * 40


@dataclass
class _Resp:
    text: str
    tool_used: str | None = None


class _Orch:
    """Echoes the text; if it says 'delete', asks permission through the remote hook,
    the same way the PermissionGate -> _confirm_in_terminal path does."""

    def __init__(self):
        self.seen = []

    def new_session(self):
        return object()

    def handle(self, text, session):
        self.seen.append(text)
        if "delete" in text:
            confirm = remote_confirmer()
            assert confirm is not None, "remote request must route permission to the device"
            allowed = confirm("delete file report.txt", SimpleNamespace(name="CRITICAL"))
            return _Resp("Deleted." if allowed else "Okay, I left it.", "delete_file")
        return _Resp(f"echo: {text}")


class _STT:
    def transcribe_pcm(self, pcm):
        return SimpleNamespace(success=True, text="what time is it", error="")


def _wav(seconds=1.0, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


@pytest.fixture
def live():
    cfg = SimpleNamespace(assistant=SimpleNamespace(name="VERONICA"))
    orch = _Orch()
    svc = RemoteService(orch, cfg, TOKEN, stt=_STT(), tts=None, confirm_timeout_s=3)
    srv = RemoteServer(svc, host="127.0.0.1", port=0)
    srv.serve_in_background()
    yield f"http://127.0.0.1:{srv.port}", svc, orch
    srv.shutdown()


def _call(base, path, body=None, token=TOKEN, kind="application/json"):
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method="POST" if data is not None else "GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", kind)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_page_is_public_but_api_needs_token(live):
    base, _, orch = live
    with urllib.request.urlopen(base + "/") as r:
        assert r.status == 200 and b"VERONICA" in r.read()
    assert _call(base, "/api/ping", token=None)[0] == 401
    assert _call(base, "/api/chat", {"text": "hi"}, token="wrong" * 8)[0] == 401
    assert orch.seen == []                     # nothing reached the brain


def test_chat_round_trip(live):
    base, _, _ = live
    code, r = _call(base, "/api/chat", {"text": "hello"})
    assert code == 200 and r["ok"] and r["reply"] == "echo: hello"


def test_voice_transcribes_then_answers(live):
    base, _, orch = live
    code, r = _call(base, "/api/voice", _wav(1.0), kind="audio/wav")
    assert code == 200 and r["heard"] == "what time is it" and r["reply"] == "echo: what time is it"


def test_voice_rejects_wrong_format(live):
    base, _, orch = live
    code, r = _call(base, "/api/voice", _wav(1.0, rate=44100), kind="audio/wav")
    assert code == 400 and not r["ok"] and orch.seen == []


def test_permission_is_asked_on_the_device_and_can_be_allowed(live):
    base, svc, _ = live
    out = {}
    t = threading.Thread(target=lambda: out.update(r=_call(base, "/api/chat", {"text": "delete report"})[1]))
    t.start()
    for _ in range(50):
        qs = _call(base, "/api/pending")[1]["questions"]
        if qs:
            break
        time.sleep(0.05)
    assert qs and qs[0]["level"] == "CRITICAL"
    assert _call(base, "/api/confirm", {"id": qs[0]["id"], "allow": True})[1]["ok"]
    t.join(5)
    assert out["r"]["reply"] == "Deleted."


def test_unanswered_permission_means_no():
    q = PendingQuestions(timeout_s=0.2)
    assert q.ask("shut down the PC", SimpleNamespace(name="CRITICAL")) is False
    assert q.pending() == []


def test_confirmer_is_only_set_during_a_remote_turn(live):
    _, svc, _ = live
    assert remote_confirmer() is None
    svc.chat("hello")
    assert remote_confirmer() is None


def test_lockout_after_repeated_wrong_tokens():
    svc = RemoteService(_Orch(), SimpleNamespace(), TOKEN)
    for _ in range(remote.MAX_FAILED_AUTH):
        assert not svc.authorized("Bearer nope")
    assert not svc.authorized(f"Bearer {TOKEN}")   # locked out even with the right token


def test_token_created_once_and_reused(tmp_path):
    p = tmp_path / "tok.txt"
    a = load_or_create_token(p)
    assert len(a) >= 32 and load_or_create_token(p) == a
    assert load_or_create_token(p, env_value="x" * 20) == "x" * 20


def test_wav_to_pcm():
    assert len(wav_to_pcm16k(_wav(0.5))) == 16000


# ---------------------------------------------------------------- the phone's permission sheet (2026-10-19)

def test_permission_questions_carry_the_tool_and_arguments():
    """The phone page words the question in plain language from tool + args, never from raw text."""
    from veronica.ui.remote.server import split_description
    assert split_description("shutdown_pc({'action': 'sign_out', 'delay_seconds': 30})") == (
        "shutdown_pc", {"action": "sign_out", "delay_seconds": 30})
    assert split_description("run_code({})") == ("run_code", {})
    assert split_description("not a call") == ("", {})
    # only a literal dict is read; nothing is ever evaluated
    assert split_description("x(__import__('os').system('echo hi'))") == ("x", {})
    assert split_description("x({'a': 'b' * 5000})") == ("x", {})
    long = split_description("x({'path': '" + "p" * 900 + "'})")[1]["path"]
    assert len(long) == 300


def test_pending_lists_tool_args_and_time_left():
    q = PendingQuestions(timeout_s=5)
    t = threading.Thread(target=lambda: q.ask("shutdown_pc({'action': 'restart'})", SimpleNamespace(name="SENSITIVE")))
    t.start()
    for _ in range(50):
        if q.pending():
            break
        time.sleep(0.02)
    item = q.pending()[0]
    assert item["tool"] == "shutdown_pc" and item["args"] == {"action": "restart"} and item["level"] == "SENSITIVE"
    assert 0 < item["expires_in"] <= 5
    assert q.answer(item["id"], False)
    t.join(2)


def test_phone_page_has_the_sheet_and_sets_text_safely():
    import re
    from pathlib import Path
    html = (Path(remote.__file__).with_name("phone.html")).read_text(encoding="utf-8")
    for needed in ('id="sheet"', 'role="alertdialog"', 'id="confirmBtn"', 'id="denyBtn"', 'id="menu"', 'id="powerBtn"',
                   'id="timerBar"', "Hold to shut down", "Hold to sign out", "Hold to delete", "Cancel shutdown",
                   "prefers-color-scheme: light", "env(safe-area-inset-bottom)", "prefers-reduced-motion"):
        assert needed in html, needed
    assert 'class="ask"' not in html and ".ask {" not in html            # the small in-chat card is gone
    # whatever came from the PC (descriptions, file names, commands) is shown with textContent;
    # innerHTML only ever receives constants and the page's own icons
    for rhs in re.findall(r"innerHTML\s*=\s*([^;]+);", html):
        assert rhs.strip().startswith(("icon(", "'<span class=\"typing\">")), rhs
    assert 'textContent = q.description' in html
