"""
The island: VERONICA's presence at the top of the screen, just under the camera.

    idle        a small dark circle with a slow glow: she's waiting for her name
    listening   it opens into a pill with your voice as a live waveform
    open        (conversation mode) still a pill, still listening for a follow-up
    thinking    amber shimmer while she works it out
    speaking    her own voice as a moving wave

Click it to show her terminal; click again to hide it. Her terminal starts hidden while the
island is up, and comes back by itself if she stops unexpectedly, so an error is never hidden.

It runs as its own small process (Tk must own its thread), so nothing here can slow down or
crash her voice loop. VERONICA sends it her state over a local UDP socket (127.0.0.1 only):

    python -m veronica.ui.island --port 47531 --parent <pid> --console <hwnd> --hide
"""
from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import random
import socket
import sys
import time
from collections import deque

PILL_W, PILL_H, DOT = 248, 40, 28            # logical pixels (scaled for the screen's DPI)
TOP_GAP = 6
BARS = 26
KEY = "#010203"                              # the transparent colour (never drawn otherwise)
BODY = "#050608"
COLORS = {"listening": "#3fd2ff", "open": "#3fd2ff", "thinking": "#ffb547", "speaking": "#e9f7ff",
          "idle": "#2aa7d8"}


def _dpi_scale() -> float:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)        # crisp, and real pixel positions
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            return 1.0
    try:
        return max(1.0, ctypes.windll.user32.GetDpiForSystem() / 96.0)
    except Exception:  # noqa: BLE001
        return 1.0


def _alive(pid: int) -> bool:
    if pid <= 0:
        return True
    try:
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(handle, ctypes.byref(code))
        k32.CloseHandle(handle)
        return code.value == 259                              # STILL_ACTIVE
    except Exception:  # noqa: BLE001
        return True


def _terminal_window(console: int, title: str) -> int:
    """Her terminal's window: the console's own, or (Windows Terminal hosts consoles in a hidden
    pseudo window) the top-level window with her title."""
    user32 = ctypes.windll.user32
    if console:
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(console, cls, 64)
        if cls.value != "PseudoConsoleWindow":
            return console
    found = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def each(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if buf.value.strip().lower().startswith(title.lower()):
            found.append(hwnd)
        return True
    user32.EnumWindows(proto(each), 0)
    return found[0] if found else console


def show_terminal(hwnd: int, show: bool) -> None:
    if not hwnd:
        return
    user32 = ctypes.windll.user32
    if show:
        user32.ShowWindow(hwnd, 9)                            # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    else:
        user32.ShowWindow(hwnd, 0)                            # SW_HIDE


def level_to_height(level: float, ambient: float) -> float:
    """0..1 against the room's own level, log scale (like a VU meter), as the terminal meter does."""
    floor = max(float(ambient or 0.0), 60.0)
    ratio = (level or 0.0) / floor
    if ratio < 1.3:
        return 0.0
    return max(0.0, min(1.0, (math.log(ratio) - math.log(1.3)) / (math.log(12.0) - math.log(1.3))))


class Island:
    def __init__(self, port: int, parent: int, console: int, hide: bool, title: str) -> None:
        import tkinter as tk
        self.s = _dpi_scale()
        self.parent, self.title = parent, title
        self.console = _terminal_window(console, title)
        self.hide_at_start = hide
        self.state = "idle"
        self.level, self.ambient = 0.0, 60.0
        self.history = deque([0.0] * BARS, maxlen=BARS)
        self.shown = [0.0] * BARS
        self.w = self.h = DOT * self.s
        self.last_msg = time.monotonic()
        self.terminal_visible = not hide
        self.hover = False
        self.quitting = False

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", port))
        self.sock.setblocking(False)

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", KEY)
        self.root.attributes("-toolwindow", True)              # no taskbar button
        self.root.configure(bg=KEY)
        self.cw, self.ch = int(PILL_W * self.s) + 4, int(PILL_H * self.s) + 4
        self.screen_w = self.root.winfo_screenwidth()
        x = (self.screen_w - self.cw) // 2
        self.root.geometry(f"{self.cw}x{self.ch}+{x}+{int(TOP_GAP * self.s)}")
        self.canvas = tk.Canvas(self.root, width=self.cw, height=self.ch, bg=KEY, highlightthickness=0, bd=0,
                                cursor="hand2")
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<Enter>", lambda e: setattr(self, "hover", True))
        self.canvas.bind("<Leave>", lambda e: setattr(self, "hover", False))
        self.root.deiconify()
        self.t0 = time.monotonic()
        self.root.after(30, self.frame)
        self.root.after(500, self.watch)
        if hide:
            self.root.after(800, lambda: show_terminal(self.console, False))

    # -- input -----------------------------------------------------------------------
    def on_click(self, _event=None) -> None:
        self.terminal_visible = not self.terminal_visible
        show_terminal(self.console, self.terminal_visible)

    def read(self) -> None:
        while True:
            try:
                data, _ = self.sock.recvfrom(2048)
            except (BlockingIOError, OSError):
                return
            try:
                msg = json.loads(data.decode("utf-8"))
            except ValueError:
                continue
            self.last_msg = time.monotonic()
            state = msg.get("s")
            if state == "quit":                                # a clean "go to sleep": leave her window shut
                self.quitting = True
                self.root.after(10, self.root.destroy)
                return
            if state in COLORS:
                self.state = state
            if "l" in msg:
                self.level = float(msg.get("l") or 0.0)
                self.ambient = float(msg.get("a") or self.ambient or 60.0)

    def watch(self) -> None:
        """She stopped without saying goodbye (crash, closed window): bring her terminal back so
        any error is visible, then go."""
        if not _alive(self.parent):
            show_terminal(self.console, True)
            self.root.destroy()
            return
        self.root.after(500, self.watch)

    # -- drawing ---------------------------------------------------------------------
    def frame(self) -> None:
        self.read()
        s, t = self.s, time.monotonic() - self.t0
        wide = self.state != "idle"
        tw, th = (PILL_W * s, PILL_H * s) if wide else (DOT * s, DOT * s)
        k = 0.22                                               # spring toward the target shape
        self.w += (tw - self.w) * k
        self.h += (th - self.h) * k
        if self.state in ("listening", "open"):
            self.history.append(level_to_height(self.level, self.ambient))
        elif self.state == "speaking":
            self.history.append(0.25 + 0.6 * abs(math.sin(t * 7.0)) * random.uniform(0.55, 1.0))
        else:
            self.history.append(0.0)
        self.draw(t)
        self.root.after(16, self.frame)

    def pill(self, x0, y0, x1, y1, color) -> None:
        c, r = self.canvas, (y1 - y0) / 2
        if x1 - x0 <= 2 * r + 0.5:
            c.create_oval(x0, y0, x1, y1, fill=color, outline=color)
            return
        c.create_oval(x0, y0, x0 + 2 * r, y1, fill=color, outline=color)
        c.create_oval(x1 - 2 * r, y0, x1, y1, fill=color, outline=color)
        c.create_rectangle(x0 + r, y0, x1 - r, y1, fill=color, outline=color)

    def draw(self, t: float) -> None:
        c, s = self.canvas, self.s
        c.delete("all")
        cx = self.cw / 2
        x0, x1 = cx - self.w / 2, cx + self.w / 2
        y0, y1 = 2, 2 + self.h
        color = COLORS.get(self.state, COLORS["idle"])
        body = "#0c0f14" if self.hover else BODY
        self.pill(x0, y0, x1, y1, body)
        openness = max(0.0, min(1.0, (self.w - DOT * s) / ((PILL_W - DOT) * s)))
        if openness < 0.35:                                    # the circle: a slow breathing ring
            glow = 0.45 + 0.35 * (0.5 + 0.5 * math.sin(t * 1.6))
            ring = _mix(BODY, COLORS["idle"], glow)
            pad = 5 * s
            c.create_oval(cx - self.w / 2 + pad, y0 + pad, cx + self.w / 2 - pad, y1 - pad, outline=ring, width=max(1, int(2 * s)))
            return
        # the pill: a status dot, then the waveform
        mid = (y0 + y1) / 2
        dot_r = 4.5 * s
        dx = x0 + self.h / 2
        pulse = 0.65 + 0.35 * math.sin(t * (5.0 if self.state == "thinking" else 2.4))
        c.create_oval(dx - dot_r, mid - dot_r, dx + dot_r, mid + dot_r, fill=_mix(BODY, color, pulse * openness), outline="")
        left = dx + dot_r + 9 * s
        right = x1 - self.h / 2
        if right - left < 20 * s:
            return
        step = (right - left) / BARS
        bar_w = max(2.0, step * 0.5)
        for i, v in enumerate(self.history):
            if self.state == "thinking":
                v = 0.08 + 0.10 * (0.5 + 0.5 * math.sin(t * 4.5 - i * 0.45))
            self.shown[i] += (v - self.shown[i]) * 0.35
            hh = max(2.0 * s, self.shown[i] * (self.h - 14 * s)) / 2
            bx = left + i * step + (step - bar_w) / 2
            shade = _mix(BODY, color, (0.45 + 0.55 * min(1.0, self.shown[i] * 1.6)) * openness)
            self.pill(bx, mid - hh, bx + bar_w, mid + hh, shade) if hh * 2 >= bar_w else \
                c.create_rectangle(bx, mid - hh, bx + bar_w, mid + hh, fill=shade, outline=shade)

    def run(self) -> None:
        self.root.mainloop()


def _mix(a: str, b: str, f: float) -> str:
    f = max(0.0, min(1.0, f))
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    out = "#" + "".join(f"{round(x + (y - x) * f):02x}" for x, y in zip(ca, cb))
    return "#020304" if out == KEY else out


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--parent", type=int, default=0)
    p.add_argument("--console", type=int, default=0)
    p.add_argument("--title", default="VERONICA")
    p.add_argument("--hide", action="store_true")
    a = p.parse_args(argv)
    if sys.platform != "win32":
        return 0
    Island(a.port, a.parent, a.console, a.hide, a.title).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
