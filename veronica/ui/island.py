"""
The island: VERONICA's presence at the top of the screen, just under the camera.
Frosted glass tinted in her current colour, morphing with spring motion between:

    idle        a small glass orb that breathes: she's waiting for her name
    listening   a glass bar with your voice as flowing waves (cyan)
    open        conversation mode: still listening for a follow-up
    thinking    a shimmer sweeping across "Thinking" (amber)
    speaking    her words lighting up one by one as she says them, like live lyrics (violet)

Click it and it drops into a glass card: what you said, what she's saying, and buttons for her
terminal and for stopping her mid-sentence. Click again (or leave it) and it folds back.

Her terminal starts hidden while the island is up and comes back by itself if she stops
unexpectedly, so an error is never hidden. The island is its own process (Qt must own its
thread), so it can't slow down or crash her voice loop. VERONICA talks to it over a local UDP
socket (127.0.0.1 only); its buttons talk back the same way.

    python -m veronica.ui.island --port 47531 --reply-port 47532 --parent <pid> --console <hwnd> --hide
"""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import math
import socket
import sys
import time
from pathlib import Path

KEY = "#010203"                              # kept for the tests' colour helper
# Black and white on purpose (the user asked for no colour): states differ by motion, not hue.
TINT = {"idle": (255, 255, 255), "listening": (255, 255, 255), "open": (255, 255, 255),
        "thinking": (255, 255, 255), "speaking": (255, 255, 255)}
LABEL = {"listening": "Listening", "open": "I'm listening", "thinking": "Thinking", "speaking": "", "idle": ""}

# shapes (logical px): width, height, corner radius
ORB = (34, 34, 17)
BAR = (400, 44, 22)
BAR_TALL = (460, 52, 26)
CARD = (520, 214, 30)
GRID = (520, 318, 28)                         # the hover tool grid (music, timer, system, toggles)
WIN_W, WIN_H, TOP = 600, 350, 8
WORDS_PER_S = 2.7                            # her speaking pace (Kokoro / Supertonic / ElevenLabs ~2.5-3)


# -- helpers kept small and pure (tested) ------------------------------------------------------
def level_to_height(level: float, ambient: float) -> float:
    """0..1 against the room's own level, log scale (like a VU meter), as the terminal meter does."""
    floor = max(float(ambient or 0.0), 60.0)
    ratio = (level or 0.0) / floor
    if ratio < 1.3:
        return 0.0
    return max(0.0, min(1.0, (math.log(ratio) - math.log(1.3)) / (math.log(12.0) - math.log(1.3))))


def _mix(a: str, b: str, f: float) -> str:
    f = max(0.0, min(1.0, f))
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    out = "#" + "".join(f"{round(x + (y - x) * f):02x}" for x, y in zip(ca, cb))
    return "#020304" if out == KEY else out


def spoken_words(text: str, elapsed: float, rate: float = WORDS_PER_S) -> float:
    """How many of the sentence's words she has said after `elapsed` seconds (fractional)."""
    n = len((text or "").split())
    return max(0.0, min(float(n), elapsed * rate))


class Spring:
    """Critically-damped-ish spring: smooth, quick, a touch of overshoot."""
    def __init__(self, value: float, stiffness: float = 170.0, damping: float = 0.0) -> None:
        self.value = self.target = value
        self.v = 0.0
        self.k = stiffness
        self.d = damping or 2 * math.sqrt(stiffness) * 0.9       # just under critical: glides, no wobble

    def step(self, dt: float) -> float:
        for _ in range(2):                       # two half-steps: stable and smooth at any frame rate
            h = dt / 2
            a = self.k * (self.target - self.value) - self.d * self.v
            self.v += a * h
            self.value += self.v * h
        return self.value


def ease(current: float, target: float, rate: float, dt: float) -> float:
    """Frame-rate independent exponential approach (rate = 1/seconds)."""
    return target + (current - target) * math.exp(-rate * dt)


# -- Windows bits -----------------------------------------------------------------------------
def _alive(pid: int) -> bool:
    if pid <= 0:
        return True
    try:
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(handle, ctypes.byref(code))
        k32.CloseHandle(handle)
        return code.value == 259
    except Exception:  # noqa: BLE001
        return True


def _terminal_window(console: int, title: str) -> int:
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
        user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
    else:
        user32.ShowWindow(hwnd, 0)


def _terminal_visible(hwnd: int) -> bool:
    try:
        return bool(hwnd) and bool(ctypes.windll.user32.IsWindowVisible(hwnd))
    except Exception:  # noqa: BLE001
        return False


def hide_from_capture(hwnd: int) -> bool:
    """Keep this window out of screen captures (Windows 10 2004+). The glass needs it: the island
    photographs the screen behind itself, and must not photograph itself. Side effect: the island
    doesn't appear in screenshots or screen recordings."""
    try:
        fn = ctypes.windll.user32.SetWindowDisplayAffinity
        fn.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        return bool(fn(hwnd, 0x11))                      # WDA_EXCLUDEFROMCAPTURE
    except Exception:  # noqa: BLE001
        return False


# -- the window -------------------------------------------------------------------------------
def build(args):
    from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
    from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPainterPath, QPen,
                             QRadialGradient, QConicalGradient)
    from PyQt6.QtWidgets import QApplication, QWidget

    def rgba(c, a):
        return QColor(int(c[0]), int(c[1]), int(c[2]), int(max(0, min(255, a))))

    class Island(QWidget):
        def __init__(self) -> None:
            super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                             | Qt.WindowType.Tool | Qt.WindowType.NoDropShadowWindowHint)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self.setMouseTracking(True)
            screen = QApplication.primaryScreen().geometry()
            self.setGeometry(screen.x() + (screen.width() - WIN_W) // 2, screen.y(), WIN_W, WIN_H)
            self.state, self.card = "idle", False
            self.level, self.ambient, self.voice = 0.0, 60.0, 0.0
            self.you, self.reply, self.piece, self.piece_t = "", "", "", 0.0
            self.w, self.h, self.r = Spring(ORB[0]), Spring(ORB[1]), Spring(ORB[2])
            self.tint = [list(TINT["idle"]), list(TINT["idle"])]
            self.content = Spring(0.0, 110)               # 0 = orb only, 1 = bar contents shown
            self.cardness = Spring(0.0, 130)
            self.phase = 0.0                              # wave phase, accumulated (speed changes never jump it)
            self.spin, self.spin_speed = 0.0, 0.0         # the arc around the orb
            self.label_a = 1.0                            # "Listening" fades as you start talking
            self.shift = 0.0                              # how far her line has scrolled
            self.prev_piece, self.prev_t = "", 0.0        # the sentence before, fading up and out
            self.shift_prev = 0.0
            self.hover = 0.0
            self.t0 = self.last = time.monotonic()
            self.card_touched = 0.0
            self.buttons: dict[str, QRectF] = {}
            self.mouse = None
            # the hover tool grid
            from veronica.ui import island_tools as tools
            self.tools = tools
            self.grid, self.gridness = False, Spring(0.0, 150)
            self.hover_since: float | None = None
            self.left_since: float | None = None
            self.hits: dict[str, QRectF] = {}
            self.power_armed = 0.0                        # the power button: press, then press again to confirm
            self.stats = tools.Stats()
            self.music = tools.NowPlaying()
            self.awake = False
            self.muted = False
            self.next_up = ""
            self.toast, self.toast_until = "", 0.0
            self.phone_on, self.phone_last = False, ""        # from VERONICA's phone page (ui/remote/transfer.py)
            self.setAcceptDrops(True)
            self.console = _terminal_window(args.console, args.title)
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind(("127.0.0.1", args.port))
            self.sock.setblocking(False)
            self.reply_to = ("127.0.0.1", args.reply_port) if args.reply_port else None
            self.font_ui = QFont("Segoe UI Variable Display", 11)
            self.font_ui.setWeight(QFont.Weight.DemiBold)
            self.font_small = QFont("Segoe UI Variable Text", 9)
            self.font_big = QFont("Segoe UI Variable Display", 14)
            self.font_big.setWeight(QFont.Weight.DemiBold)
            try:
                ctypes.windll.winmm.timeBeginPeriod(1)    # 1 ms timer resolution for this process
            except Exception:  # noqa: BLE001
                pass
            self.timer = QTimer(self)
            self.timer.setTimerType(Qt.TimerType.PreciseTimer)
            self.timer.timeout.connect(self.tick)
            self.timer.start(16)                          # a steady 60 fps: what the screen can show
            self.watch = QTimer(self)
            self.watch.timeout.connect(self.check_parent)
            self.watch.start(500)
            if args.hide:
                QTimer.singleShot(900, lambda: show_terminal(self.console, False))
            QTimer.singleShot(1500, self.warm_up)
            # Glass (the iPhone look): Windows' own blur can't follow a rounded, changing shape (tested:
            # it always fills the window's whole rectangle), so the island blurs the screen behind
            # itself: small snapshots, shrunk and stretched back, painted inside its own shape.
            self.glass = False
            self.backdrop = None                          # the blurred screen behind the window
            self.backdrop_at = 0.0
            if getattr(args, "glass", "on") != "off":
                QTimer.singleShot(0, lambda: setattr(self, "glass", hide_from_capture(int(self.winId()))))

        def warm_up(self) -> None:
            """Draw the hover grid once, off screen, soon after start. The first draw looks up fonts
            for its symbols and imports psutil, which froze the first hover for about a second."""
            try:
                from PyQt6.QtGui import QImage
                self.stats.sample()
                img = QImage(WIN_W, WIN_H, QImage.Format.Format_ARGB32_Premultiplied)
                img.fill(0)
                q = QPainter(img)
                q.setRenderHint(QPainter.RenderHint.Antialiasing)
                q.setRenderHint(QPainter.RenderHint.TextAntialiasing)
                rect = QRectF((WIN_W - GRID[0]) / 2, TOP, GRID[0], GRID[1])
                self.draw_grid(q, rect, 0.0, 1.0)
                for f in (self.font_ui, self.font_small, self.font_big):
                    q.setFont(f)
                    q.drawText(QPointF(0, 20), "Thinking Listening 0123456789 ↓↑↺⚡·“”")
                q.end()
                self.hits = {}
            except Exception:  # noqa: BLE001 - only a speed-up
                pass

        # -- messages ---------------------------------------------------------------------
        def read(self) -> None:
            while True:
                try:
                    data, _ = self.sock.recvfrom(8192)
                except (BlockingIOError, OSError):
                    return
                try:
                    m = json.loads(data.decode("utf-8"))
                except ValueError:
                    continue
                s = m.get("s")
                if s == "quit":
                    QApplication.quit()
                    return
                if s in TINT:
                    if s != self.state and s in ("listening", "open"):
                        self.voice = 0.0
                    self.state = s
                if "l" in m:
                    self.level = float(m.get("l") or 0.0)
                    self.ambient = float(m.get("a") or self.ambient or 60.0)
                if m.get("snap"):                             # save what the island is showing (it hides from screenshots)
                    try:
                        self.grab().save(str(m["snap"]))
                    except Exception:  # noqa: BLE001
                        pass
                if "mic" in m:
                    self.muted = not bool(m["mic"])
                if "next" in m:
                    self.next_up = str(m.get("next") or "")[:120]
                if isinstance(m.get("phone"), dict):
                    self.phone_on = bool(m["phone"].get("on"))
                    self.phone_last = str(m["phone"].get("last") or "")[:80]
                if m.get("you"):
                    self.you, self.reply = str(m["you"])[:300], ""
                if m.get("say"):
                    if self.piece:
                        self.prev_piece, self.prev_t = self.piece, time.monotonic()
                    self.piece, self.piece_t = str(m["say"])[:400], time.monotonic()
                    self.shift = 0.0
                    self.reply = (self.reply + " " + self.piece).strip()[-600:]

        def send(self, cmd: str, **extra) -> None:
            if self.reply_to:
                try:
                    self.sock.sendto(json.dumps({"cmd": cmd, **extra}).encode("utf-8"), self.reply_to)
                except OSError:
                    pass

        # -- the Phone tile: clipboard, files, drag and drop ------------------------------
        def send_clipboard(self) -> None:
            import tempfile
            cb = QApplication.clipboard()
            img = cb.image()
            if not img.isNull():
                path = Path(tempfile.gettempdir()) / f"veronica-clip-{int(time.time() * 1000)}.png"
                img.save(str(path), "PNG")
                self.send("phone_image", path=str(path))
                self.flash("Sending the picture to your phone")
                return
            text = (cb.text() or "").strip()
            if not text:
                self.flash("Your clipboard is empty")
                return
            if len(text.encode("utf-8")) > 6000:                # too big for one message: hand over a file
                path = Path(tempfile.gettempdir()) / f"veronica-clip-{int(time.time() * 1000)}.txt"
                path.write_text(text, encoding="utf-8")
                self.send("phone_text_file", path=str(path))
            else:
                self.send("phone_text", text=text)
            self.flash("Sending your clipboard to your phone")

        def pick_file(self) -> None:
            from PyQt6.QtWidgets import QFileDialog
            self.grid = False
            path, _ = QFileDialog.getOpenFileName(None, "Send a file to your phone", str(Path.home()))
            if path:
                self.send("phone_file", path=path)
                self.flash(f"Sending {Path(path).name} to your phone")

        def dragEnterEvent(self, e) -> None:
            if e.mimeData().hasUrls() and self.state == "idle":
                self.grid = True                                  # open the grid so the Phone tile is there
                self.hover_since = time.monotonic()
                e.acceptProposedAction()

        def dragMoveEvent(self, e) -> None:
            if e.mimeData().hasUrls():
                self.grid, self.left_since = True, None
                e.acceptProposedAction()

        def dropEvent(self, e) -> None:
            files = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
            for path in files[:10]:
                if Path(path).is_file():
                    self.send("phone_file", path=path)
            if files:
                self.flash(f"Sending {len(files)} file{'s' if len(files) != 1 else ''} to your phone")
            e.acceptProposedAction()

        def check_parent(self) -> None:
            if not _alive(args.parent):
                show_terminal(self.console, True)          # she stopped unexpectedly: show why
                QApplication.quit()

        # -- motion -----------------------------------------------------------------------
        def tick(self) -> None:
            self.read()
            now = time.monotonic()
            dt = min(0.05, now - self.last)
            self.last = now
            # Where the mouse is, read every frame: movement events alone miss a cursor that jumps
            # onto the island (and a "leave" that never arrives keeps it open).
            from PyQt6.QtGui import QCursor
            local = self.mapFromGlobal(QCursor.pos())
            inside = self.shape().adjusted(-4, -4, 4, 4).contains(QPointF(local))
            self.hover = 1.0 if inside else 0.0
            self.mouse = QPointF(local) if inside else None
            if self.card and now - self.card_touched > 12 and self.hover < 0.5:
                self.card = False                          # left alone: fold back
            if self.state != "idle" or self.card:
                self.grid, self.hover_since = False, None  # she's listening / talking: the tools make way
            elif self.hover > 0.5:
                self.left_since = None
                self.hover_since = self.hover_since or now
                if not self.grid and now - self.hover_since > 0.12:
                    self.grid = True
            else:
                self.hover_since = None
                if self.grid:
                    self.left_since = self.left_since or now
                    if now - self.left_since > 0.35:
                        self.grid = False
            if self.gridness.value > 0.05:
                self.stats.sample(now)
                self.music.poll(now)
            toast = self.toast and now < self.toast_until and self.state == "idle" and not self.grid
            if self.card:
                tw, th, tr = CARD
            elif self.grid:
                tw, th, tr = GRID
            elif toast:
                tw, th, tr = BAR
            elif self.state == "idle":
                tw, th, tr = ORB
            elif self.state == "speaking":
                tw, th, tr = BAR_TALL
            else:
                tw, th, tr = BAR
            if self.hover > 0.5 and not self.card:
                tw, th = tw + 8, th + 2                    # a little lift under the mouse
            self.w.target, self.h.target, self.r.target = tw, th, tr
            for sp in (self.w, self.h, self.r):
                sp.step(dt)
            self.content.target = 0.0 if (self.state == "idle" and not self.card and not self.grid and not toast) else 1.0
            self.content.step(dt)
            self.gridness.target = 1.0 if self.grid else 0.0
            self.gridness.step(dt)
            self.cardness.target = 1.0 if self.card else 0.0
            self.cardness.step(dt)
            want = TINT.get(self.state, TINT["idle"])
            self.tint[0] = [ease(a, b, 6, dt) for a, b in zip(self.tint[0], want)]
            target_voice = level_to_height(self.level, self.ambient) if self.state in ("listening", "open") else 0.0
            self.voice = ease(self.voice, target_voice, 14 if target_voice > self.voice else 4.5, dt)
            self.phase += dt * (2.6 + 3.0 * self.voice)                     # waves speed up with you, smoothly
            want_spin = {"thinking": 6.5, "speaking": 2.4}.get(self.state, 0.0)
            self.spin_speed = ease(self.spin_speed, want_spin, 4, dt)
            self.spin += dt * self.spin_speed
            self.label_a = ease(self.label_a, 1.0 if self.voice < 0.1 else 0.0, 7, dt)
            resting = (self.state == "idle" and not self.card and not self.grid and not toast and self.hover < 0.5
                       and abs(self.w.value - ORB[0]) < 0.3 and abs(self.w.v) < 0.5)
            want = 33 if resting else 16                  # only the slow breathing left: half the frames, half the CPU
            if self.timer.interval() != want:
                self.timer.setInterval(want)
            if self.glass and now - self.backdrop_at >= (0.5 if resting else 0.08):
                self.backdrop_at = now                       # ~12 looks a second while active, 2 while resting
                self.refresh_backdrop()
            self.update()

        def refresh_backdrop(self) -> None:
            """Photograph the screen behind the island's current shape and blur it (shrink, then stretch)."""
            try:
                rect = self.shape().adjusted(-6, -6, 6, 6)
                origin = self.mapToGlobal(rect.topLeft().toPoint())
                screen = self.screen() or QApplication.primaryScreen()
                shot = screen.grabWindow(0, origin.x() - screen.geometry().x(), origin.y() - screen.geometry().y(),
                                         int(rect.width()), int(rect.height()))
                if shot.isNull():
                    return
                w, h = max(2, int(rect.width() / 5)), max(2, int(rect.height() / 5))
                small = shot.toImage().scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation)
                small = small.scaled(max(2, w // 3), max(2, h // 3), Qt.AspectRatioMode.IgnoreAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
                self.backdrop = (small, rect)
            except Exception:  # noqa: BLE001 - the glass is decoration; never break the island
                self.backdrop = None

        # -- input ------------------------------------------------------------------------
        def shape(self) -> QRectF:
            w, h = max(8.0, self.w.value), max(8.0, self.h.value)
            return QRectF((WIN_W - w) / 2, TOP, w, h)

        def mouseMoveEvent(self, e) -> None:
            inside = self.shape().contains(e.position())
            self.mouse = e.position() if inside else None
            self.hover = 1.0 if inside else 0.0
            self.setCursor(Qt.CursorShape.PointingHandCursor if inside else Qt.CursorShape.ArrowCursor)
            if self.card and inside:
                self.card_touched = time.monotonic()

        def leaveEvent(self, e) -> None:
            self.hover = 0.0
            self.mouse = None

        def mousePressEvent(self, e) -> None:
            p = e.position()
            if not self.shape().contains(p):
                if self.card:
                    self.card = False
                return
            self.card_touched = time.monotonic()
            if self.grid and self.gridness.value > 0.6:
                for name, rect in self.hits.items():
                    if rect.contains(p):
                        self.grid_action(name)
                        return
                return
            if self.card and self.cardness.value > 0.7:
                for name, rect in self.buttons.items():
                    if rect.contains(p):
                        if name == "terminal":
                            show_terminal(self.console, not _terminal_visible(self.console))
                        elif name == "stop":
                            self.send("stop")
                        return
                self.card = False
                return
            self.card = True

        def grid_action(self, name: str) -> None:
            now = time.monotonic()
            if name == "orb":                                  # the globe opens the conversation card
                self.grid, self.card, self.card_touched = False, True, now
            elif name in ("previous", "play_pause", "next"):
                self.tools.media_key(name)
                self.music.pressed(name)
            elif name == "terminal":
                show_terminal(self.console, not _terminal_visible(self.console))
            elif name == "power":
                if now < self.power_armed:                     # second press: close VERONICA
                    self.power_armed = 0.0
                    self.grid = False
                    self.flash("Goodbye")
                    self.send("quit")
                else:
                    self.power_armed = now + 3.0               # first press: ask once more
            elif name == "awake":
                self.awake = not self.awake
                self.tools.keep_awake(self.awake)
                self.flash("Staying awake" if self.awake else "Sleep allowed again")
            elif name == "mic":
                self.muted = not self.muted
                self.send("mic_off" if self.muted else "mic_on")
            elif name == "screenshot":
                self.grid = False
                QTimer.singleShot(420, self.shoot)             # let the grid fold away first
            elif name == "phone_clip":
                self.send_clipboard()
            elif name == "phone_file":
                QTimer.singleShot(50, self.pick_file)

        def shoot(self) -> None:
            path = self.tools.take_screenshot()
            if path is None:
                self.flash("Couldn't take a screenshot")
                return
            try:
                from PyQt6.QtGui import QImage
                QApplication.clipboard().setImage(QImage(str(path)))
            except Exception:  # noqa: BLE001
                pass
            self.flash("Screenshot saved and copied")

        def flash(self, text: str) -> None:
            self.toast, self.toast_until = text, time.monotonic() + 2.4

        # -- drawing ----------------------------------------------------------------------
        def paintEvent(self, _e) -> None:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            t = time.monotonic() - self.t0
            rect = self.shape()
            radius = max(4.0, min(self.r.value, rect.height() / 2))
            tint = self.tint[0]
            path = QPainterPath()
            path.addRoundedRect(rect, radius, radius)

            if self.glass and self.backdrop is not None:
                # Glass, like an iPhone's: your screen shows through, blurred. Over it, a smoky tint
                # so white text stays readable, a soft light from above, and a bright rim.
                small, _ = self.backdrop
                p.save()
                p.setClipPath(path)
                p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                p.drawImage(rect.adjusted(-6, -6, 6, 6), small)
                p.restore()
                busy = max(self.gridness.value, self.cardness.value)        # more to read: a little darker
                p.fillPath(path, QColor(10, 10, 12, int(105 + 60 * busy)))
                light = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.top() + min(rect.height(), 60)))
                light.setColorAt(0.0, QColor(255, 255, 255, 46))
                light.setColorAt(1.0, QColor(255, 255, 255, 0))
                p.fillPath(path, QBrush(light))
                rim = QLinearGradient(rect.topLeft(), rect.bottomLeft())
                rim.setColorAt(0.0, QColor(255, 255, 255, 150))
                rim.setColorAt(0.5, QColor(255, 255, 255, 40))
                rim.setColorAt(1.0, QColor(255, 255, 255, 90))
                inner = QPainterPath()
                inner.addRoundedRect(rect.adjusted(0.8, 0.8, -0.8, -0.8), max(2.0, radius - 0.8), max(2.0, radius - 0.8))
                p.setPen(QPen(QBrush(rim), 1.3))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(inner)
            else:
                # no glass available: flat black, one solid surface and a hairline edge
                for i in range(8, 0, -2):
                    sh = QPainterPath()
                    sh.addRoundedRect(rect.adjusted(-i, -i * 0.3, i, i), radius + i, radius + i)
                    p.fillPath(sh, QColor(0, 0, 0, 6))
                p.fillPath(path, QColor(0, 0, 0, 248))
                p.setPen(QPen(QColor(255, 255, 255, 30), 1.0))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(path)
            p.setClipPath(path)

            c = self.content.value
            orb_r = min(rect.height(), 44) * 0.30 if c > 0.05 else rect.height() * 0.34
            orb_c = QPointF(rect.left() + min(rect.height(), 44) / 2 + (2 if c > 0.05 else 0), rect.top() + min(rect.height(), 52) / 2) \
                if c > 0.05 else rect.center()
            self.draw_orb(p, orb_c, orb_r, t, tint)
            if c < 0.05:
                p.end()
                return
            alpha = max(0.0, min(1.0, (c - 0.35) / 0.65)) * (1.0 - self.cardness.value) * (1.0 - self.gridness.value)
            line = QRectF(orb_c.x() + orb_r + 12, rect.top(), rect.right() - (orb_c.x() + orb_r + 12) - 18,
                          min(rect.height(), 52))
            if alpha > 0.01:
                if self.state in ("listening", "open"):
                    self.draw_waves(p, line, t, tint, alpha)
                elif self.state == "thinking":
                    self.draw_label(p, line, "Thinking", t, tint, alpha, shimmer=True)
                elif self.state == "speaking":
                    self.draw_lyric(p, line, tint, alpha)
                elif self.toast and time.monotonic() < self.toast_until:
                    self.draw_label(p, line, self.toast, t, tint, alpha, shimmer=False)
            if self.gridness.value > 0.02:
                self.draw_grid(p, rect, t, self.gridness.value)
            if self.cardness.value > 0.02:
                self.draw_card(p, rect, t, tint, self.cardness.value)
            p.end()

        def draw_orb(self, p, center, r, t, tint) -> None:
            """Plain and quiet: a thin white ring with a small dot in the middle. The ring breathes
            slowly while she waits; the dot swells with your voice."""
            rr = r * (1.0 + 0.10 * self.voice)
            breath = 0.5 + 0.5 * math.sin(t * 1.5)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, int(95 + 55 * breath + 100 * self.voice)), 1.4))
            p.drawEllipse(center, rr, rr)
            dot = rr * (0.26 + 0.34 * self.voice)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, int(205 + 50 * self.voice)))
            p.drawEllipse(center, dot, dot)
            if self.muted:                                          # a small slash: she isn't listening
                p.setPen(QPen(QColor(255, 255, 255, 200), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(center.x() - rr * 0.55, center.y() + rr * 0.55),
                           QPointF(center.x() + rr * 0.55, center.y() - rr * 0.55))
            arc_a = max(0.0, min(1.0, self.spin_speed / 2.4))            # fades in and out with the spin
            if arc_a > 0.02:
                arc = QRectF(center.x() - rr - 3, center.y() - rr - 3, (rr + 3) * 2, (rr + 3) * 2)
                p.setPen(QPen(QColor(255, 255, 255, int(200 * arc_a)), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawArc(arc, int(-math.degrees(self.spin) % 360 * 16), int((60 + 40 * arc_a) * 16))

        def draw_waves(self, p, box, t, tint, alpha) -> None:
            label = LABEL.get(self.state, "")
            p.setFont(self.font_ui)
            fm = QFontMetricsF(self.font_ui)
            lw = (fm.horizontalAdvance(label) + 16) * self.label_a if label else 0.0
            if label and self.label_a > 0.02:
                y = box.center().y() + fm.ascent() / 2 - 2
                p.setPen(rgba((232, 232, 232), 180 * alpha * self.label_a))
                p.drawText(QPointF(box.left() + 2 - (1 - self.label_a) * 10, y), label)
            waves = QRectF(box.left() + lw, box.top(), max(10.0, box.width() - lw), box.height())
            mid = waves.center().y()
            amp = 2.2 + self.voice * (box.height() * 0.34)
            p.save()
            p.setClipRect(waves)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            layers = [((235, 235, 235), 1.0, 1.0, 0.0, 2.0), ((150, 150, 150), 0.72, 1.55, 1.9, 1.6),
                      ((95, 95, 95), 0.5, 2.2, 3.4, 1.4)]
            for color, scale, freq, offset, width in layers:
                path = QPainterPath()
                steps = 120
                for i in range(steps + 1):
                    x = i / steps
                    env = (1 - (2 * x - 1) ** 2) ** 2
                    y = mid + math.sin(x * math.pi * 2 * freq + self.phase * (1 + 0.15 * freq) + offset) * amp * scale * env
                    pt = QPointF(waves.left() + x * waves.width(), y)
                    path.moveTo(pt) if i == 0 else path.lineTo(pt)
                p.setPen(QPen(rgba(color, 205 * alpha), width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(path)
            p.restore()

        def draw_label(self, p, box, text, t, tint, alpha, shimmer, right=False) -> None:
            p.setFont(self.font_ui)
            fm = QFontMetricsF(self.font_ui)
            wtxt = fm.horizontalAdvance(text)
            x = box.right() - wtxt if right else box.left() + 2
            y = box.center().y() + fm.ascent() / 2 - 2
            if shimmer:
                g = QLinearGradient(QPointF(x, 0), QPointF(x + wtxt, 0))
                sweep = (t * 0.8) % 1.6 - 0.3
                for stop, a in ((max(0, sweep - 0.25), 110), (max(0, min(1, sweep)), 255), (min(1, sweep + 0.25), 110)):
                    g.setColorAt(stop, rgba((255, 255, 255), a * alpha))
                g.setColorAt(0.0, rgba((255, 255, 255), 110 * alpha))
                g.setColorAt(1.0, rgba((255, 255, 255), 110 * alpha))
                p.setPen(QPen(QBrush(g), 1))
                p.drawText(QPointF(x, y), text)          # the light sweeping across it is the motion
            else:
                p.setPen(rgba((230, 230, 230), 170 * alpha))
                p.drawText(QPointF(x, y), text)

        def draw_lyric(self, p, box, tint, alpha) -> None:
            """Her line on its own layer, then a soft mask fades both ends: words glide in and out
            of view instead of being sliced, and nothing can reach the orb."""
            from PyQt6.QtGui import QImage
            dpr = self.devicePixelRatioF()
            layer = QImage(int(box.width() * dpr) + 2, int(box.height() * dpr) + 2,
                           QImage.Format.Format_ARGB32_Premultiplied)
            layer.setDevicePixelRatio(dpr)
            layer.fill(0)
            q = QPainter(layer)
            q.setRenderHint(QPainter.RenderHint.Antialiasing)
            q.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            q.setFont(self.font_ui)
            fm = QFontMetricsF(self.font_ui)
            now = time.monotonic()
            base_y = box.height() / 2 + fm.ascent() / 2 - 2
            out = min(1.0, (now - self.prev_t) / 0.35) if self.prev_piece else 1.0
            if self.prev_piece and out < 1.0:                    # the last sentence lifts away
                q.setPen(rgba((250, 250, 250), 255 * alpha * (1 - out) ** 2))
                q.drawText(QPointF(-self.shift_prev, base_y - 14 * _smooth(out)), self.prev_piece)
            words = self.piece.split()
            if words:
                said = spoken_words(self.piece, now - self.piece_t)
                space = fm.horizontalAdvance(" ")
                widths = [fm.horizontalAdvance(w) for w in words]
                cur = min(len(words) - 1, int(said))
                upto = sum(widths[:cur + 1]) + space * cur
                want = max(0.0, upto - box.width() * 0.8)           # keep the current word in view
                self.shift = self.shift + (want - self.shift) * 0.1   # glide, don't jump
                self.shift_prev = self.shift
                rise = 1.0 - _smooth(min(1.0, (now - self.piece_t) / 0.35))
                y = base_y + 12 * rise
                x = -self.shift
                for i, (word, w) in enumerate(zip(words, widths)):
                    if x + w >= -2 and x <= box.width() + 2:
                        if i < int(said):
                            col, a = (250, 250, 250), 255
                        elif i == int(said):
                            f = _smooth(said - int(said))
                            col, a = tuple(140 + (255 - 140) * f for _ in range(3)), 160 + 95 * f
                        else:
                            col, a = (200, 200, 200), 90
                        q.setPen(rgba(col, a * alpha * (1 - rise * 0.8)))
                        q.drawText(QPointF(x, y), word)
                    x += w + space
            # the mask: transparent at both ends, solid in the middle
            q.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            mask = QLinearGradient(QPointF(0, 0), QPointF(box.width(), 0))
            left_fade = min(0.12, 30 / max(box.width(), 1)) if self.shift > 1 else 0.0
            right_fade = min(0.1, 24 / max(box.width(), 1))
            mask.setColorAt(0.0, QColor(0, 0, 0, 0 if left_fade else 255))
            mask.setColorAt(max(0.001, left_fade), QColor(0, 0, 0, 255))
            mask.setColorAt(1.0 - right_fade, QColor(0, 0, 0, 255))
            mask.setColorAt(1.0, QColor(0, 0, 0, 0))
            q.fillRect(QRectF(0, 0, box.width() + 2, box.height() + 2), QBrush(mask))
            q.end()
            p.drawImage(box.topLeft(), layer)

        def draw_card(self, p, rect, t, tint, k) -> None:
            a = max(0.0, min(1.0, (k - 0.45) / 0.55))
            if a <= 0.01:
                return
            left = rect.left() + 64
            top = rect.top() + 18
            p.setFont(self.font_small)
            p.setPen(rgba((205, 205, 205), 190 * a))
            status = {"idle": "Waiting for “Veronica”", "listening": "Listening", "open": "Listening for a follow-up",
                      "thinking": "Thinking", "speaking": "Speaking"}.get(self.state, "")
            p.drawText(QPointF(left, top + 10), f"VERONICA  ·  {status}")
            box = QRectF(left, top + 22, rect.right() - left - 22, 26)
            if self.you:
                p.setPen(rgba((165, 165, 165), 170 * a))
                p.drawText(box, Qt.TextFlag.TextSingleLine | Qt.AlignmentFlag.AlignVCenter,
                           QFontMetricsF(self.font_small).elidedText("You: " + self.you, Qt.TextElideMode.ElideRight, box.width()))
            p.setFont(self.font_big)
            p.setPen(rgba((248, 248, 248), 245 * a))
            body = QRectF(left, top + 50, rect.right() - left - 70, rect.bottom() - (top + 50) - 14)
            text = self.reply or ("Say “Veronica” to start." if self.state == "idle" else "…")
            p.drawText(body, Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignTop, _fit(text, 150))
            # icon buttons: terminal, and stop while she speaks
            self.buttons = {}
            size = 34.0
            by = rect.bottom() - size - 12
            bx = rect.right() - 18 - size
            icons = ["terminal"] + (["stop"] if self.state == "speaking" else [])
            for name in reversed(icons):
                r = QRectF(bx, by, size, size)
                on = name == "terminal" and _terminal_visible(self.console)
                hot = self.mouse is not None and r.contains(self.mouse)
                bp = QPainterPath()
                bp.addEllipse(r)
                p.fillPath(bp, QColor(255, 255, 255, int((235 if on else 44 if hot else 22) * a)))
                p.setPen(QPen(QColor(255, 255, 255, int((90 if hot else 46) * a)), 1))
                p.setBrush(Qt.BrushStyle.NoBrush)       # the stop icon's fill must not leak into this outline
                p.drawPath(bp)
                self.draw_icon(p, name, r, a, dark=on)        # on (terminal showing): black icon on white
                self.buttons[name] = r
                bx -= size + 10

        def draw_grid(self, p, rect, t, k) -> None:
            """Header (time, date, what's next) over three tiles; tiles spring in one after another."""
            self.hits = {}
            head_a = max(0.0, min(1.0, (k - 0.35) / 0.5))
            orb_hit = QRectF(rect.left() + 4, rect.top() + 4, 48, 48)
            self.hits["orb"] = orb_hit
            if head_a > 0.01:
                now = dt.datetime.now()
                p.setFont(self.font_big)
                p.setPen(rgba((250, 250, 250), 245 * head_a))
                fm = QFontMetricsF(self.font_big)
                clock = now.strftime("%I:%M").lstrip("0")
                p.drawText(QPointF(rect.left() + 58, rect.top() + 33), clock)
                p.setFont(self.font_small)
                p.setPen(rgba((170, 170, 170), 220 * head_a))
                p.drawText(QPointF(rect.left() + 64 + fm.horizontalAdvance(clock), rect.top() + 32),
                           now.strftime("%p  ·  %a %d %b"))
                if self.next_up:
                    right = QRectF(rect.left() + 250, rect.top() + 18, rect.width() - 270, 20)
                    p.setPen(rgba((200, 200, 200), 200 * head_a))
                    p.drawText(right, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                               QFontMetricsF(self.font_small).elidedText("Next: " + self.next_up,
                                                                        Qt.TextElideMode.ElideLeft, right.width()))
            L, R = rect.left() + 14, rect.right() - 14
            phone_h = 56.0
            T, B = rect.top() + 56, rect.bottom() - 14 - phone_h - 10
            gap = 10.0
            side = (R - L - gap) / 3
            main_w = R - L - gap - side
            mid = (T + B - gap) / 2
            tiles = [("music", QRectF(L, T, main_w, mid - T)),
                     ("toggles", QRectF(L, mid + gap, main_w, B - mid - gap)),
                     ("system", QRectF(R - side, T, side, B - T)),
                     ("phone", QRectF(L, B + 10, R - L, phone_h))]
            for i, (name, box) in enumerate(tiles):
                a = max(0.0, min(1.0, (k - 0.45 - 0.07 * i) / 0.35))        # one after another
                if a <= 0.01:
                    continue
                box = box.translated(0, 10 * (1 - _smooth(a)))
                tile = QPainterPath()
                tile.addRoundedRect(box, 16, 16)
                p.fillPath(tile, QColor(255, 255, 255, int(14 * a)))       # one quiet surface, no outline
                getattr(self, f"tile_{name}")(p, box, t, a)

        def tile_music(self, p, box, t, a) -> None:
            info = self.music.info
            playing = bool(info.get("playing"))
            # equaliser: moves while music plays, rests flat when paused
            ex, ey = box.left() + 16, box.center().y() - 2
            for i in range(4):
                hgt = 4 + (10 * abs(math.sin(t * (5.5 + i * 1.3) + i)) if playing else 0)
                p.fillRect(QRectF(ex + i * 5, ey + 7 - hgt, 3, hgt), QColor(255, 255, 255, int(200 * a)))
            text_l = box.left() + 46
            controls_w = 3 * 30 + 8
            width = box.width() - (text_l - box.left()) - controls_w - 10
            p.setFont(self.font_ui)
            p.setPen(rgba((250, 250, 250), 245 * a))
            title = info.get("title") or "Nothing playing"
            p.drawText(QPointF(text_l, box.center().y() - 2),
                       QFontMetricsF(self.font_ui).elidedText(title, Qt.TextElideMode.ElideRight, width))
            p.setFont(self.font_small)
            p.setPen(rgba((160, 160, 160), 220 * a))
            sub_line = " · ".join(x for x in (info.get("artist"), info.get("app")) if x) or "Spotify, YouTube, any player"
            p.drawText(QPointF(text_l, box.center().y() + 15),
                       QFontMetricsF(self.font_small).elidedText(sub_line, Qt.TextElideMode.ElideRight, width))
            x = box.right() - controls_w
            for name in ("previous", "play_pause", "next"):
                r = QRectF(x, box.center().y() - 15, 30, 30)
                self.round_button(p, r, a, big=name == "play_pause")
                self.media_icon(p, name, r.center(), a, playing)
                self.hits[name] = r
                x += 33

        def tile_toggles(self, p, box, t, a) -> None:
            size = 34.0
            armed = time.monotonic() < self.power_armed
            shown = _terminal_visible(self.console)
            names = [("awake", self.awake, "Awake"), ("screenshot", False, "Shot"),
                     ("mic", self.muted, "Muted" if self.muted else "Mic"),
                     ("terminal", shown, "Terminal"), ("power", armed, "Sure?" if armed else "Power")]
            step = (box.width() - 24) / len(names)
            for i, (name, on, label) in enumerate(names):
                cx = box.left() + 12 + step * (i + 0.5)
                r = QRectF(cx - size / 2, box.center().y() - size / 2 - 7, size, size)
                self.round_button(p, r, a, on=on)
                if name == "terminal":
                    self.draw_icon(p, "terminal", r, a, dark=on)
                else:
                    self.toggle_icon(p, name, r.center(), a, dark=on)
                self.hits[name] = r
                p.setFont(self.font_small)
                p.setPen(rgba((235, 235, 235) if on else (150, 150, 150), 220 * a))
                p.drawText(QRectF(cx - step / 2, r.bottom() + 3, step, 14), Qt.AlignmentFlag.AlignCenter, label)

        def tile_system(self, p, box, t, a) -> None:
            st = self.stats
            p.setFont(self.font_small)
            y = box.top() + 16
            for label, value in (("CPU", st.cpu), ("Memory", st.mem)):
                p.setPen(rgba((165, 165, 165), 220 * a))
                p.drawText(QPointF(box.left() + 14, y + 10), label)
                p.setPen(rgba((245, 245, 245), 240 * a))
                p.drawText(QRectF(box.left() + 14, y, box.width() - 28, 14),
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{value:.0f}%")
                bar = QRectF(box.left() + 14, y + 17, box.width() - 28, 4)
                track = QPainterPath()
                track.addRoundedRect(bar, 2, 2)
                p.fillPath(track, QColor(255, 255, 255, int(28 * a)))
                fill = QPainterPath()
                shown = getattr(self, f"_shown_{label}", 0.0)
                shown += (value - shown) * 0.12                       # bars glide to new values
                setattr(self, f"_shown_{label}", shown)
                fill.addRoundedRect(QRectF(bar.left(), bar.top(), bar.width() * max(0.02, shown / 100), 4), 2, 2)
                p.fillPath(fill, QColor(255, 255, 255, int(220 * a)))
                y += 34
            if st.battery:
                pct, plugged = st.battery
                p.setPen(rgba((165, 165, 165), 220 * a))
                p.drawText(QPointF(box.left() + 14, y + 10), "Battery")
                p.setPen(rgba((245, 245, 245), 240 * a))
                p.drawText(QRectF(box.left() + 14, y, box.width() - 28, 14),
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                           f"{pct:.0f}%" + (" ⚡" if plugged else ""))
                y += 22
            if st.down:
                p.setPen(rgba((165, 165, 165), 220 * a))
                p.drawText(QRectF(box.left() + 14, y, box.width() - 28, 14),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f"↓ {st.down}")
                p.drawText(QRectF(box.left() + 14, y + 14, box.width() - 28, 14),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f"↑ {st.up}")
            # CPU history along the bottom
            graph = QRectF(box.left() + 12, box.bottom() - 30, box.width() - 24, 20)
            line = QPainterPath()
            pts = list(st.history)
            for i, v in enumerate(pts):
                pt = QPointF(graph.left() + graph.width() * i / (len(pts) - 1), graph.bottom() - graph.height() * v)
                line.moveTo(pt) if i == 0 else line.lineTo(pt)
            p.setPen(QPen(QColor(255, 255, 255, int(150 * a)), 1.3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(line)

        def tile_phone(self, p, box, t, a) -> None:
            # a phone outline with a live dot
            c = QPointF(box.left() + 26, box.center().y())
            p.setPen(QPen(QColor(245, 245, 245, int(220 * a)), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(c.x() - 7, c.y() - 11, 14, 22), 3.5, 3.5)
            p.drawLine(QPointF(c.x() - 2, c.y() + 7.5), QPointF(c.x() + 2, c.y() + 7.5))
            dot = QColor(255, 255, 255, int((235 if self.phone_on else 70) * a))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(dot)
            pulse = 1.0 + (0.25 * math.sin(t * 3.0) if self.phone_on else 0.0)
            p.drawEllipse(QPointF(c.x() + 9, c.y() - 10), 3.2 * pulse, 3.2 * pulse)
            p.setFont(self.font_ui)
            p.setPen(rgba((250, 250, 250), 245 * a))
            p.drawText(QPointF(box.left() + 48, box.center().y() - 3), "My phone")
            p.setFont(self.font_small)
            p.setPen(rgba((160, 160, 160), 220 * a))
            sub_line = self.phone_last or ("Connected, phone page open" if self.phone_on
                                           else "Open your phone page to connect \u00b7 drop files here")
            width = box.width() - 48 - 210
            p.drawText(QPointF(box.left() + 48, box.center().y() + 14),
                       QFontMetricsF(self.font_small).elidedText(sub_line, Qt.TextElideMode.ElideRight, width))
            x = box.right() - 12
            for name, label in (("phone_file", "Send file"), ("phone_clip", "Send clipboard")):
                w = QFontMetricsF(self.font_small).horizontalAdvance(label) + 26
                r = QRectF(x - w, box.center().y() - 14, w, 28)
                hot = self.mouse is not None and r.contains(self.mouse)
                pill = QPainterPath()
                pill.addRoundedRect(r, 14, 14)
                p.fillPath(pill, QColor(255, 255, 255, int((48 if hot else 22) * a)))
                p.setPen(QPen(QColor(255, 255, 255, int((80 if hot else 30) * a)), 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(pill)
                p.setPen(rgba((240, 240, 240), 235 * a))
                p.drawText(r, Qt.AlignmentFlag.AlignCenter, label)
                self.hits[name] = r
                x -= w + 8

        def round_button(self, p, r, a, big=False, on=False) -> None:
            hot = self.mouse is not None and r.contains(self.mouse)
            path = QPainterPath()
            path.addEllipse(r)
            p.fillPath(path, QColor(255, 255, 255, int((240 if on else 52 if hot else 26 if big else 20) * a)))

        def media_icon(self, p, name, c, a, playing) -> None:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(245, 245, 245, int(235 * a)))

            def tri(x, y, w, h, left=False):
                path = QPainterPath()
                if left:
                    path.moveTo(x + w, y - h / 2); path.lineTo(x, y); path.lineTo(x + w, y + h / 2)
                else:
                    path.moveTo(x, y - h / 2); path.lineTo(x + w, y); path.lineTo(x, y + h / 2)
                path.closeSubpath()
                p.drawPath(path)
            if name == "play_pause":
                if playing:
                    p.drawRoundedRect(QRectF(c.x() - 4.5, c.y() - 5.5, 3.2, 11), 1, 1)
                    p.drawRoundedRect(QRectF(c.x() + 1.3, c.y() - 5.5, 3.2, 11), 1, 1)
                else:
                    tri(c.x() - 3, c.y(), 8.5, 11)
            elif name == "next":
                tri(c.x() - 5, c.y(), 6, 9); tri(c.x(), c.y(), 6, 9)
            else:
                tri(c.x() - 1, c.y(), 6, 9, left=True); tri(c.x() - 6, c.y(), 6, 9, left=True)

        def toggle_icon(self, p, name, c, a, dark=False) -> None:
            ink = QColor(10, 10, 10, int(255 * a)) if dark else QColor(245, 245, 245, int(235 * a))
            p.setPen(QPen(ink, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.setBrush(Qt.BrushStyle.NoBrush)
            if name == "awake":                                   # a coffee cup with steam
                p.drawRoundedRect(QRectF(c.x() - 6, c.y() - 2, 9, 8), 2, 2)
                p.drawArc(QRectF(c.x() + 1.5, c.y() - 0.5, 5, 5), -90 * 16, 180 * 16)
                p.drawLine(QPointF(c.x() - 3, c.y() - 4.5), QPointF(c.x() - 3, c.y() - 7))
                p.drawLine(QPointF(c.x() + 0.5, c.y() - 4.5), QPointF(c.x() + 0.5, c.y() - 7))
            elif name == "screenshot":                            # a camera
                p.drawRoundedRect(QRectF(c.x() - 7.5, c.y() - 4.5, 15, 10.5), 2.5, 2.5)
                p.drawEllipse(QPointF(c.x(), c.y() + 0.7), 3, 3)
                p.drawLine(QPointF(c.x() - 3, c.y() - 4.5), QPointF(c.x() - 1.5, c.y() - 6.5))
                p.drawLine(QPointF(c.x() - 1.5, c.y() - 6.5), QPointF(c.x() + 1.5, c.y() - 6.5))
                p.drawLine(QPointF(c.x() + 1.5, c.y() - 6.5), QPointF(c.x() + 3, c.y() - 4.5))
            elif name == "power":                                 # the power symbol
                p.drawArc(QRectF(c.x() - 6.5, c.y() - 5.5, 13, 13), 125 * 16, -290 * 16)
                p.drawLine(QPointF(c.x(), c.y() - 7.5), QPointF(c.x(), c.y() - 0.5))
            elif name == "mic":                                   # a microphone (struck through when muted)
                p.drawRoundedRect(QRectF(c.x() - 2.6, c.y() - 7, 5.2, 9), 2.6, 2.6)
                p.drawArc(QRectF(c.x() - 5.5, c.y() - 4, 11, 9), 200 * 16, 140 * 16)
                p.drawLine(QPointF(c.x(), c.y() + 5), QPointF(c.x(), c.y() + 7.5))
                if dark:
                    p.drawLine(QPointF(c.x() - 6.5, c.y() + 6), QPointF(c.x() + 6.5, c.y() - 7))

        def draw_icon(self, p, name, r, a, dark=False) -> None:
            c = r.center()
            ink = QColor(10, 10, 10, int(255 * a)) if dark else QColor(245, 245, 245, int(235 * a))
            pen = QPen(ink, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                       Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            if name == "terminal":                                  # a little window with  >_
                box = QRectF(c.x() - 8, c.y() - 6.5, 16, 13)
                p.drawRoundedRect(box, 2.5, 2.5)
                p.drawLine(QPointF(c.x() - 4.5, c.y() - 2.5), QPointF(c.x() - 1.5, c.y()))
                p.drawLine(QPointF(c.x() - 1.5, c.y()), QPointF(c.x() - 4.5, c.y() + 2.5))
                p.drawLine(QPointF(c.x() + 0.5, c.y() + 3), QPointF(c.x() + 4.5, c.y() + 3))
            elif name == "stop":                                    # a rounded square
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(ink)
                p.drawRoundedRect(QRectF(c.x() - 5.5, c.y() - 5.5, 11, 11), 2.5, 2.5)

    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(True)
    app.island = Island()          # kept on the app: a local would be garbage-collected (window gone at once)
    app.island.show()
    return app


def _smooth(x: float) -> float:
    """Smoothstep: eases in and out."""
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def _fit(text: str, limit: int) -> str:
    """The end of a long reply (what she's saying now), cut at a word."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    return "…" + cut[cut.find(" ") + 1:] if " " in cut else "…" + cut


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--reply-port", type=int, default=0)
    ap.add_argument("--parent", type=int, default=0)
    ap.add_argument("--console", type=int, default=0)
    ap.add_argument("--title", default="VERONICA")
    ap.add_argument("--hide", action="store_true")
    ap.add_argument("--glass", default="on", choices=["on", "off"])
    args = ap.parse_args(argv)
    if sys.platform != "win32":
        return 0
    app = build(args)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
