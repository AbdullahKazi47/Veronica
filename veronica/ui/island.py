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
import json
import math
import socket
import sys
import time

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
WIN_W, WIN_H, TOP = 600, 290, 8
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
                if m.get("you"):
                    self.you, self.reply = str(m["you"])[:300], ""
                if m.get("say"):
                    if self.piece:
                        self.prev_piece, self.prev_t = self.piece, time.monotonic()
                    self.piece, self.piece_t = str(m["say"])[:400], time.monotonic()
                    self.shift = 0.0
                    self.reply = (self.reply + " " + self.piece).strip()[-600:]

        def send(self, cmd: str) -> None:
            if self.reply_to:
                try:
                    self.sock.sendto(json.dumps({"cmd": cmd}).encode("utf-8"), self.reply_to)
                except OSError:
                    pass

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
            if self.card and now - self.card_touched > 12 and self.hover < 0.5:
                self.card = False                          # left alone: fold back
            if self.card:
                tw, th, tr = CARD
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
            self.content.target = 0.0 if (self.state == "idle" and not self.card) else 1.0
            self.content.step(dt)
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
            resting = (self.state == "idle" and not self.card and self.hover < 0.5
                       and abs(self.w.value - ORB[0]) < 0.3 and abs(self.w.v) < 0.5)
            want = 33 if resting else 16                  # only the slow breathing left: half the frames, half the CPU
            if self.timer.interval() != want:
                self.timer.setInterval(want)
            self.update()

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

            # soft shadow
            for i in range(10, 0, -2):
                sh = QPainterPath()
                sh.addRoundedRect(rect.adjusted(-i, -i * 0.3, i, i * 1.1), radius + i, radius + i)
                p.fillPath(sh, QColor(0, 0, 0, 7))
            # the glass: dark frosted base, a wash of her colour, a top sheen
            base = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            solid = self.cardness.value * 26                   # the card is for reading: less see-through
            base.setColorAt(0.0, QColor(20, 20, 22, int(232 + solid * 0.8)))
            base.setColorAt(1.0, QColor(4, 4, 5, int(244 + solid * 0.4)))
            p.fillPath(path, QBrush(base))
            wash = QRadialGradient(QPointF(rect.left() + rect.height() * 0.6, rect.center().y()),
                                   max(rect.width(), rect.height()) * 0.9)
            glow = 8 + 26 * self.voice + (6 if self.state == "speaking" else 0)
            wash.setColorAt(0.0, rgba(tint, glow))
            wash.setColorAt(0.55, rgba(tint, glow * 0.25))
            wash.setColorAt(1.0, rgba(tint, 0))
            p.fillPath(path, QBrush(wash))
            sheen = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.top() + min(rect.height(), 40)))
            sheen.setColorAt(0.0, QColor(255, 255, 255, 22))
            sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.fillPath(path, QBrush(sheen))
            edge = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            edge.setColorAt(0.0, QColor(255, 255, 255, 46))
            edge.setColorAt(1.0, QColor(255, 255, 255, 12))
            p.setPen(QPen(QBrush(edge), 1.0))
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
            alpha = max(0.0, min(1.0, (c - 0.35) / 0.65)) * (1.0 - self.cardness.value)
            line = QRectF(orb_c.x() + orb_r + 12, rect.top(), rect.right() - (orb_c.x() + orb_r + 12) - 18,
                          min(rect.height(), 52))
            if alpha > 0.01:
                if self.state in ("listening", "open"):
                    self.draw_waves(p, line, t, tint, alpha)
                elif self.state == "thinking":
                    self.draw_label(p, line, "Thinking", t, tint, alpha, shimmer=True)
                elif self.state == "speaking":
                    self.draw_lyric(p, line, tint, alpha)
            if self.cardness.value > 0.02:
                self.draw_card(p, rect, t, tint, self.cardness.value)
            p.end()

        def draw_orb(self, p, center, r, t, tint) -> None:
            pulse = 1.0 + 0.04 * math.sin(t * 2.2) + 0.22 * self.voice
            rr = r * pulse
            p.setPen(Qt.PenStyle.NoPen)
            if self.voice > 0.02:                                   # a soft white glow as you speak
                halo = QRadialGradient(center, rr * 2.0)
                halo.setColorAt(0.0, QColor(255, 255, 255, int(70 * self.voice)))
                halo.setColorAt(1.0, QColor(255, 255, 255, 0))
                p.setBrush(QBrush(halo))
                p.drawEllipse(center, rr * 2.0, rr * 2.0)
            body = QRadialGradient(QPointF(center.x() - rr * 0.25, center.y() - rr * 0.3), rr * 1.25)
            body.setColorAt(0.0, QColor(58, 58, 62))
            body.setColorAt(0.55, QColor(14, 14, 16))
            body.setColorAt(1.0, QColor(0, 0, 0))
            p.setBrush(QBrush(body))
            p.drawEllipse(center, rr, rr)
            breath = 0.5 + 0.5 * math.sin(t * 1.6)
            p.setPen(QPen(QColor(255, 255, 255, int(55 + 45 * breath + 90 * self.voice)), 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(center, rr, rr)
            spot = QRadialGradient(QPointF(center.x() - rr * 0.35, center.y() - rr * 0.42), rr * 0.5)
            spot.setColorAt(0.0, QColor(255, 255, 255, 150))
            spot.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(spot))
            p.drawEllipse(center, rr, rr)
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
    args = ap.parse_args(argv)
    if sys.platform != "win32":
        return 0
    app = build(args)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
