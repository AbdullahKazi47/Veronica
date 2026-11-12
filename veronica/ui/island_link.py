"""VERONICA's side of the island (ui/island.py): start it, tell it her state, keep it alive.

States: idle (waiting for her name), listening, open (conversation mode, waiting for a
follow-up), thinking, speaking. The mic level is sent about 25 times a second while she listens.
If the island stops, her terminal is shown again, so she is never left invisible.
"""
from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from veronica.logging_setup import get_logger

logger = get_logger("ui.island")

STATES = ("idle", "listening", "open", "thinking", "speaking")


def _console_hwnd() -> int:
    try:
        return int(ctypes.windll.kernel32.GetConsoleWindow() or 0)
    except Exception:  # noqa: BLE001
        return 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pythonw() -> str:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w) if w.is_file() else str(exe)


class IslandLink:
    def __init__(self, level: Callable[[], tuple[float, float]] = lambda: (0.0, 60.0), *, hide_terminal: bool = True,
                 title: str = "VERONICA", spawn=subprocess.Popen) -> None:
        self._level = level                  # () -> (mic level, room level)
        self.hide_terminal = hide_terminal
        self.title = title
        self._spawn = spawn
        self.state = "idle"
        self.proc = None
        self.port = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._stop = threading.Event()
        self._restarts = 0
        self.console = _console_hwnd()
        self.on_command: Optional[Callable[[str], None]] = None   # the island's buttons ("stop")
        self._inbox = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._inbox.bind(("127.0.0.1", 0))
        self._inbox.settimeout(0.5)
        self.reply_port = self._inbox.getsockname()[1]

    # -- life ----------------------------------------------------------------------------
    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        if not self._launch():
            return False
        threading.Thread(target=self._pump, name="island", daemon=True).start()
        threading.Thread(target=self._listen, name="island-buttons", daemon=True).start()
        import atexit
        atexit.register(self.close)
        return True

    def _launch(self) -> bool:
        self.port = _free_port()
        cmd = [_pythonw(), "-m", "veronica.ui.island", "--port", str(self.port), "--reply-port", str(self.reply_port),
               "--parent", str(os.getpid()), "--console", str(self.console), "--title", self.title]
        if self.hide_terminal:
            cmd.append("--hide")
        try:
            self.proc = self._spawn(cmd, cwd=str(Path(__file__).resolve().parents[2]),
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            logger.exception("Couldn't start the island.")
            self.proc = None
            return False
        logger.info("Island started (port %d).", self.port)
        return True

    def close(self) -> None:
        """A clean exit ('go to sleep'): the island goes and her window stays shut."""
        self._stop.set()
        self._send({"s": "quit"})

    # -- state ---------------------------------------------------------------------------
    def set(self, state: str) -> None:
        if state in STATES and state != self.state:
            self.state = state
            self._send({"s": state})

    def you(self, text: str) -> None:
        """What the user said (shown on the island's card)."""
        if text:
            self._send({"you": str(text)[:300]})

    def say(self, piece: str) -> None:
        """A sentence she is starting to say: its words light up on the island as she speaks."""
        if piece:
            self._send({"s": "speaking", "say": str(piece)[:400]})

    def _listen(self) -> None:
        """The island's buttons. Only 127.0.0.1 can reach this socket; only known commands count."""
        while not self._stop.is_set():
            try:
                data, addr = self._inbox.recvfrom(1024)
            except (socket.timeout, OSError):
                continue
            if addr[0] != "127.0.0.1":
                continue
            try:
                cmd = json.loads(data.decode("utf-8")).get("cmd")
            except (ValueError, AttributeError):
                continue
            if cmd in ("stop",) and self.on_command is not None:
                try:
                    self.on_command(cmd)
                except Exception:  # noqa: BLE001
                    logger.exception("Island command failed.")

    def _send(self, msg: dict) -> None:
        if not self.port:
            return
        try:
            self._sock.sendto(json.dumps(msg).encode("utf-8"), ("127.0.0.1", self.port))
        except OSError:
            pass

    def _pump(self) -> None:
        while not self._stop.is_set():
            if self.state in ("listening", "open"):
                try:
                    level, ambient = self._level()
                except Exception:  # noqa: BLE001
                    level, ambient = 0.0, 60.0
                self._send({"s": self.state, "l": round(float(level or 0.0), 1), "a": round(float(ambient or 60.0), 1)})
                self._stop.wait(0.04)
            else:
                self._send({"s": self.state})       # a heartbeat, so a restarted island catches up
                self._stop.wait(0.25)
            self._keep_alive()

    def _keep_alive(self) -> None:
        proc = self.proc
        if proc is None or proc.poll() is None:
            return
        logger.warning("The island stopped (exit %s).", proc.returncode)
        self.proc = None
        if self._restarts < 3 and not self._stop.is_set():
            self._restarts += 1
            if self._launch():
                return
        self.show_terminal()                          # never leave her invisible

    def show_terminal(self) -> None:
        try:
            if self.console:
                ctypes.windll.user32.ShowWindow(self.console, 9)
        except Exception:  # noqa: BLE001
            pass
