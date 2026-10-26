"""
Remote access: talk to VERONICA from your phone or another laptop.

How it is reached (and why it is safe to run):
  * This server binds to 127.0.0.1 only — nothing on your Wi-Fi or the internet can
    connect to it directly.
  * Tailscale publishes it to YOUR devices only, over HTTPS, with:
        tailscale serve --bg 7272
    which gives an address like https://<pc-name>.<tailnet>.ts.net (valid certificate,
    reachable only from devices signed in to your Tailscale account). HTTPS matters:
    phone browsers only allow the microphone on secure pages.
  * Every API call also needs the access token (data/remote_token.txt), compared in
    constant time, with a lockout after repeated wrong tokens. The page itself carries
    no secrets.

What the page can do:
  * POST /api/chat   {"text": ...}        -> {"reply", "tool"}
  * POST /api/voice  (audio/wav, 16 kHz mono 16-bit, recorded in the browser)
                                          -> {"heard", "reply", "tool"}
  * POST /api/speak  {"text": ...}        -> the reply as audio (her configured voice)
  * GET  /api/pending                     -> a permission question waiting for you
  * POST /api/confirm {"id", "allow"}     -> answer it
Every request goes through the same Orchestrator and PermissionGate as anything said
at the PC. A permission question raised by a remote request is asked on the device that
sent it (never by voice at the PC, never auto-approved); no answer within the timeout
means NO.
"""
from __future__ import annotations

import ast
import hmac
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

from veronica.logging_setup import get_logger

logger = get_logger("ui.remote")

PAGE = Path(__file__).with_name("phone.html")
# The home-screen icon (so "Add to Home Screen" shows VERONICA's own icon, not a blank page
# glyph) — static files next to phone.html, served read-only and cached.
_ICONS = {f"/{name}": (Path(__file__).with_name(name), "image/png") for name in
          ("icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png")}
MAX_BODY = 6 * 1024 * 1024              # ~3 min of 16 kHz audio; far above a spoken request
MAX_FAILED_AUTH = 20                    # wrong tokens per window before locking out
FAILED_AUTH_WINDOW_S = 600

# --- remote permission questions -------------------------------------------------------
# The PermissionGate is built before any front-end exists and calls one function
# (ui.cli._confirm_in_terminal). That function asks `remote_confirmer()` first: when the
# CURRENT THREAD is serving a remote request, the question goes to that device.
_local = threading.local()


def remote_confirmer() -> Optional[Callable[[str, Any], bool]]:
    return getattr(_local, "confirmer", None)


def split_description(description: str) -> tuple[str, dict]:
    """"shutdown_pc({'action': 'sign_out'})" -> ("shutdown_pc", {"action": "sign_out"}).
    Anything that isn't a literal dict comes back as no arguments (never evaluated)."""
    m = re.match(r"^\s*(\w+)\((.*)\)\s*$", description or "", re.S)
    if not m:
        return "", {}
    try:
        args = ast.literal_eval(m.group(2)) if m.group(2).strip() else {}
    except (ValueError, SyntaxError):
        return m.group(1), {}
    if not isinstance(args, dict):
        return m.group(1), {}
    return m.group(1), {str(k)[:40]: (v if isinstance(v, (int, float, bool)) else str(v)[:300])
                        for k, v in list(args.items())[:12]}


class PendingQuestions:
    """Permission questions waiting for an answer from the remote device."""

    def __init__(self, timeout_s: float = 60.0) -> None:
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}

    def ask(self, description: str, level) -> bool:
        qid = uuid.uuid4().hex[:12]
        event = threading.Event()
        item = {"id": qid, "description": description,
                "level": getattr(level, "name", str(level)), "event": event, "answer": None,
                "asked_at": time.time()}
        with self._lock:
            self._items[qid] = item
        logger.info("Remote permission asked (%s): %s", item["level"], description)
        answered = event.wait(self.timeout_s)
        with self._lock:
            self._items.pop(qid, None)
        allow = bool(answered and item["answer"])
        logger.info("Remote permission %s: %s", "approved" if allow else
                    ("denied" if answered else "timed out (denied)"), description)
        return allow

    def pending(self) -> list[dict]:
        with self._lock:
            out = []
            for i in self._items.values():
                tool, args = split_description(i["description"])
                out.append({"id": i["id"], "description": i["description"], "level": i["level"],
                            "tool": tool, "args": args,
                            "expires_in": max(0, int(self.timeout_s - (time.time() - i["asked_at"])))})
            return out

    def answer(self, qid: str, allow: bool) -> bool:
        with self._lock:
            item = self._items.get(qid)
            if item is None:
                return False
            item["answer"] = bool(allow)
            item["event"].set()
            return True


# --- token ------------------------------------------------------------------------------
def load_or_create_token(path: Path, env_value: str = "") -> str:
    """The access token: config/.env VERONICA_REMOTE_TOKEN if set, else data/remote_token.txt
    (created on first run, 32 random bytes)."""
    if env_value and len(env_value) >= 16:
        return env_value.strip()
    try:
        tok = path.read_text(encoding="utf-8").strip()
        if len(tok) >= 16:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tok + "\n", encoding="utf-8")
    return tok


def tailscale_https_name() -> str:
    """This PC's Tailscale DNS name (e.g. pc.tail1234.ts.net), or "" if unknown."""
    exe = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
    if not os.path.exists(exe) and not shutil.which("tailscale"):
        return ""
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True,
                             timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        name = (json.loads(out.stdout or "{}").get("Self") or {}).get("DNSName", "")
        return name.rstrip(".")
    except Exception:  # noqa: BLE001 - only used to print a friendly address
        return ""


# --- audio --------------------------------------------------------------------------------
def wav_to_pcm16k(data: bytes) -> bytes:
    """Validate a browser-recorded WAV and return its raw 16 kHz mono 16-bit samples."""
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != 16000:
            raise ValueError(f"expected 16 kHz mono 16-bit WAV, got {w.getframerate()} Hz / "
                             f"{w.getnchannels()} ch / {w.getsampwidth() * 8}-bit")
        return w.readframes(w.getnframes())


# --- the service (testable without HTTP) ----------------------------------------------------
class RemoteService:
    def __init__(self, orchestrator, config, token: str, stt=None, tts=None,
                 confirm_timeout_s: float = 60.0) -> None:
        self.orchestrator = orchestrator
        self.config = config
        self.token = token
        self.stt = stt
        self.tts = tts
        self.questions = PendingQuestions(confirm_timeout_s)
        self.session = orchestrator.new_session()
        self._turn_lock = threading.Lock()      # one remote request at a time
        self._fails: list[float] = []
        self._fail_lock = threading.Lock()

    # auth
    def authorized(self, header: str) -> bool:
        now = time.time()
        with self._fail_lock:
            self._fails = [t for t in self._fails if now - t < FAILED_AUTH_WINDOW_S]
            if len(self._fails) >= MAX_FAILED_AUTH:
                return False                    # locked out for the rest of the window
        given = (header or "").removeprefix("Bearer ").strip()
        ok = bool(given) and hmac.compare_digest(given.encode(), self.token.encode())
        if not ok:
            with self._fail_lock:
                self._fails.append(now)
            logger.warning("Remote: rejected a request with a wrong or missing token.")
        return ok

    # turns
    def chat(self, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "Empty message."}
        with self._turn_lock:
            _local.confirmer = self.questions.ask
            try:
                response = self.orchestrator.handle(text, self.session)
            finally:
                _local.confirmer = None
        return {"ok": True, "reply": response.text, "tool": response.tool_used}

    def voice(self, wav: bytes) -> dict:
        if self.stt is None:
            return {"ok": False, "error": "Speech recognition isn't available on the PC."}
        pcm = wav_to_pcm16k(wav)
        if len(pcm) < 16000 * 2 * 0.3:
            return {"ok": False, "error": "That was too short — hold the button and speak."}
        result = self.stt.transcribe_pcm(pcm)
        if not result.success:
            return {"ok": False, "error": result.error or "I couldn't make that out."}
        heard = result.text
        reply = self.chat(heard)
        reply["heard"] = heard
        return reply

    def speak(self, text: str) -> tuple[bytes, str]:
        if self.tts is None or not (text or "").strip():
            raise ValueError("Nothing to speak.")
        path = self.tts._voice_file(text.strip()[:2000])
        if path is None:
            raise RuntimeError("No voice available right now.")
        kind = "audio/wav" if str(path).lower().endswith(".wav") else "audio/mpeg"
        return Path(path).read_bytes(), kind


# --- HTTP -----------------------------------------------------------------------------------
_PAGE_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                "style-src 'self' 'unsafe-inline'; media-src 'self' blob:; "
                                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "VERONICA-remote"
    sys_version = ""

    @property
    def service(self) -> RemoteService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("remote %s - " + fmt, self.address_string(), *args)

    # helpers
    def _send(self, code: int, body: bytes, kind: str, extra: Optional[dict] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for k, v in {**_PAGE_HEADERS, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _body(self) -> Optional[bytes]:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            self._json({"ok": False, "error": "Request too large."}, 413)
            return None
        return self.rfile.read(n) if n else b""

    def _auth(self) -> bool:
        if self.service.authorized(self.headers.get("Authorization", "")):
            return True
        time.sleep(0.5)                         # slow down guessing
        self._json({"ok": False, "error": "unauthorized"}, 401)
        return False

    # routes
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/manifest.webmanifest":
            name = getattr(getattr(self.service.config, "assistant", None), "name", "VERONICA")
            self._send(200, json.dumps({"name": name, "short_name": name, "start_url": "/",
                                        "display": "standalone", "orientation": "portrait-primary",
                                        "background_color": "#05070c", "theme_color": "#05070c",
                                        "icons": [
                                            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                                            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                                            {"src": "/icon-maskable-512.png", "sizes": "512x512", "type": "image/png",
                                             "purpose": "maskable"},
                                        ]}).encode(),
                       "application/manifest+json")
            return
        if path in _ICONS:
            # Static, served once "Add to Home Screen" installs the manifest; safe to cache.
            self._send(200, _ICONS[path][0].read_bytes(), _ICONS[path][1], {"Cache-Control": "public, max-age=604800"})
            return
        if not path.startswith("/api/"):
            self._json({"ok": False, "error": "not found"}, 404)
            return
        if not self._auth():
            return
        if path == "/api/ping":
            name = getattr(getattr(self.service.config, "assistant", None), "name", "VERONICA")
            self._json({"ok": True, "name": name, "voice_in": self.service.stt is not None,
                        "voice_out": self.service.tts is not None})
        elif path == "/api/pending":
            self._json({"ok": True, "questions": self.service.questions.pending()})
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/"):
            self._json({"ok": False, "error": "not found"}, 404)
            return
        if not self._auth():
            return
        body = self._body()
        if body is None:
            return
        try:
            if path == "/api/chat":
                self._json(self.service.chat(json.loads(body or b"{}").get("text", "")))
            elif path == "/api/voice":
                self._json(self.service.voice(body))
            elif path == "/api/speak":
                audio, kind = self.service.speak(json.loads(body or b"{}").get("text", ""))
                self._send(200, audio, kind)
            elif path == "/api/confirm":
                req = json.loads(body or b"{}")
                ok = self.service.questions.answer(str(req.get("id", "")), bool(req.get("allow")))
                self._json({"ok": ok, "error": None if ok else "That question has expired."})
            else:
                self._json({"ok": False, "error": "not found"}, 404)
        except (ValueError, wave.Error) as e:
            self._json({"ok": False, "error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001 - the page gets a message, never a stack trace
            logger.exception("Remote %s failed", path)
            self._json({"ok": False, "error": f"Something went wrong on the PC: {e}"}, 500)


class RemoteServer:
    def __init__(self, service: RemoteService, host: str = "127.0.0.1", port: int = 7272) -> None:
        self.service = service
        self.host, self.port = host, port
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.service = service  # type: ignore[attr-defined]
        self.port = self._httpd.server_address[1]

    def serve_in_background(self) -> threading.Thread:
        t = threading.Thread(target=self._httpd.serve_forever, name="remote-server", daemon=True)
        t.start()
        return t

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def start_remote(config, orchestrator) -> Optional[RemoteServer]:
    """Start the remote server next to the normal front-end. Never raises: a remote
    failure must not stop VERONICA from starting."""
    rc = getattr(config, "remote", None)
    try:
        host = getattr(rc, "host", "127.0.0.1") or "127.0.0.1"
        port = int(getattr(rc, "port", 7272) or 7272)
        token_path = config.resolve_path(getattr(rc, "token_path", "data/remote_token.txt"))
        first_run = not token_path.exists()
        token = load_or_create_token(token_path, env_value=config.secret("VERONICA_REMOTE_TOKEN") or "")
        stt = tts = None
        try:
            from veronica.speech.stt import SpeechToText
            stt = SpeechToText(config.stt)
            try:
                from veronica.speech.vocabulary import build_vocabulary
                stt.vocabulary = lambda: build_vocabulary(config)
            except Exception:  # noqa: BLE001
                pass
            threading.Thread(target=stt.warm_up, name="remote-stt-warm", daemon=True).start()
        except Exception:  # noqa: BLE001
            logger.exception("Remote: speech recognition unavailable; text only.")
            stt = None
        try:
            from veronica.speech.tts import TextToSpeech
            tts = TextToSpeech(config.tts, cache_dir=config.resolve_path(config.tts.cache_dir))
        except Exception:  # noqa: BLE001
            logger.exception("Remote: voice output unavailable; text only.")
            tts = None
        service = RemoteService(orchestrator, config, token, stt=stt, tts=tts,
                                confirm_timeout_s=float(getattr(rc, "confirm_timeout_s", 60) or 60))
        server = RemoteServer(service, host=host, port=port)
        server.serve_in_background()
        name = tailscale_https_name()
        base = f"https://{name}" if name else "https://<your-pc>.<tailnet>.ts.net"
        link = f"{base}/?t={token}"
        link_file = config.resolve_path("data/remote_link.txt")
        try:
            link_file.write_text(link + "\n", encoding="utf-8")
        except OSError:
            pass
        # The link contains the access token: show it in full only the first time (it may
        # be on screen while you record or share), afterwards point to the file instead.
        if first_run:
            print(f"[remote] Phone access link (open once on each device, keep it private):\n  {link}")
        else:
            print(f"[remote] Phone access on {base} - your link is in data\\remote_link.txt")
        if not name:
            print(f"[remote] Tailscale not detected. On the PC run once:  tailscale serve --bg {port}")
        logger.info("Remote server on %s:%s (Tailscale name: %s)", host, port, name or "unknown")
        return server
    except OSError as e:
        print(f"[remote] Couldn't start phone access: {e}")
        logger.exception("Remote server failed to start")
        return None
    except Exception:  # noqa: BLE001
        logger.exception("Remote server failed to start")
        return None
