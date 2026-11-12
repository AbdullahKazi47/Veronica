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
TINT = {"idle": (64, 196, 255), "listening": (56, 210, 255), "open": (56, 210, 255),
        "thinking": (255, 176, 64), "speaking": (176, 132, 255)}
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
    def __init__(self, value: float, stiffness: float = 180.0, damping: float = 22.0) -> None:
        self.value = self.target = value
        self.v = 0.0
        self.k, self.d = stiffness, damping

    def step(self, dt: float) -> float:
        a = self.k * (self.target - self.value) - self.d * self.v
        self.v += a * dt
        self.value += self.v * dt
        return self.value


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
            self.content = Spring(0.0, 120, 20)           # 0 = orb only, 1 = bar contents shown
            self.cardness = Spring(0.0, 140, 22)
            self.hover = 0.0
            self.t0 = self.last = time.monotonic()
            self.card_touched = 0.0
            self.buttons: dict[str, QRectF] = {}
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
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.tick)
            self.timer.start(16)
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
                    self.piece, self.piece_t = str(m["say"])[:400], time.monotonic()
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
            self.tint[0] = [a + (b - a) * min(1.0, dt * 6) for a, b in zip(self.tint[0], want)]
            target_voice = level_to_height(self.level, self.ambient) if self.state in ("listening", "open") else 0.0
            self.voice += (target_voice - self.voice) * min(1.0, dt * (18 if target_voice > self.voice else 6))
            self.update()

        # -- input ------------------------------------------------------------------------
        def shape(self) -> QRectF:
            w, h = max(8.0, self.w.value), max(8.0, self.h.value)
            return QRectF((WIN_W - w) / 2, TOP, w, h)

        def mouseMoveEvent(self, e) -> None:
            inside = self.shape().contains(e.position())
            self.hover = 1.0 if inside else 0.0
            self.setCursor(Qt.CursorShape.PointingHandCursor if inside else Qt.CursorShape.ArrowCursor)
            if self.card and inside:
                self.card_touched = time.monotonic()

        def leaveEvent(self, e) -> None:
            self.hover = 0.0

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
            base.setColorAt(0.0, QColor(30, 32, 42, int(222 + solid)))
            base.setColorAt(1.0, QColor(10, 11, 16, int(236 + solid * 0.7)))
            p.fillPath(path, QBrush(base))
            wash = QRadialGradient(QPointF(rect.left() + rect.height() * 0.6, rect.center().y()),
                                   max(rect.width(), rect.height()) * 0.9)
            glow = 70 + 50 * self.voice + (25 if self.state == "speaking" else 0)
            wash.setColorAt(0.0, rgba(tint, glow))
            wash.setColorAt(0.55, rgba(tint, glow * 0.25))
            wash.setColorAt(1.0, rgba(tint, 0))
            p.fillPath(path, QBrush(wash))
            sheen = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.top() + min(rect.height(), 40)))
            sheen.setColorAt(0.0, QColor(255, 255, 255, 34))
            sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.fillPath(path, QBrush(sheen))
            edge = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            edge.setColorAt(0.0, QColor(255, 255, 255, 70))
            edge.setColorAt(1.0, rgba(tint, 40))
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
            pulse = 1.0 + 0.06 * math.sin(t * 2.2) + 0.25 * self.voice
            rr = r * pulse
            halo = QRadialGradient(center, rr * 2.2)
            halo.setColorAt(0.0, rgba(tint, 90))
            halo.setColorAt(1.0, rgba(tint, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(halo))
            p.drawEllipse(center, rr * 2.2, rr * 2.2)
            spin = QConicalGradient(center, (t * (220 if self.state == "thinking" else 70)) % 360)
            spin.setColorAt(0.0, rgba(tint, 255))
            spin.setColorAt(0.33, QColor(120, 90, 255, 230))
            spin.setColorAt(0.66, QColor(60, 230, 210, 230))
            spin.setColorAt(1.0, rgba(tint, 255))
            p.setBrush(QBrush(spin))
            p.drawEllipse(center, rr, rr)
            core = QRadialGradient(QPointF(center.x() - rr * 0.3, center.y() - rr * 0.35), rr * 1.1)
            core.setColorAt(0.0, QColor(255, 255, 255, 200))
            core.setColorAt(0.35, QColor(255, 255, 255, 40))
            core.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.setBrush(QBrush(core))
            p.drawEllipse(center, rr, rr)

        def draw_waves(self, p, box, t, tint, alpha) -> None:
            mid = box.center().y()
            amp = (3.0 + self.voice * (box.height() * 0.36))
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            layers = [(tint, 1.0, 1.0, 0.0), ((120, 100, 255), 0.75, 1.6, 1.7), ((70, 235, 205), 0.55, 2.3, 3.1)]
            for color, scale, freq, phase in layers:
                path = QPainterPath()
                steps = 80
                for i in range(steps + 1):
                    x = i / steps
                    env = (1 - (2 * x - 1) ** 2) ** 2
                    y = mid + math.sin(x * math.pi * 2 * freq + t * (5.0 + 3 * self.voice) + phase) * amp * scale * env
                    pt = QPointF(box.left() + x * box.width(), y)
                    path.moveTo(pt) if i == 0 else path.lineTo(pt)
                p.setPen(QPen(rgba(color, 200 * alpha), 2.2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(path)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            label = LABEL.get(self.state, "")
            if label and self.voice < 0.08:
                self.draw_label(p, box, label, t, tint, alpha * 0.85 * (1 - self.voice * 10), shimmer=False, right=True)

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
                    g.setColorAt(stop, rgba((255, 236, 205), a * alpha))
                g.setColorAt(0.0, rgba((255, 236, 205), 110 * alpha))
                g.setColorAt(1.0, rgba((255, 236, 205), 110 * alpha))
                p.setPen(QPen(QBrush(g), 1))
                dots = "." * (1 + int(t * 2.5) % 3)
                p.drawText(QPointF(x, y), text + dots)
            else:
                p.setPen(rgba((225, 240, 255), 170 * alpha))
                p.drawText(QPointF(x, y), text)

        def draw_lyric(self, p, box, tint, alpha) -> None:
            words = self.piece.split()
            if not words:
                return
            said = spoken_words(self.piece, time.monotonic() - self.piece_t)
            p.setFont(self.font_ui)
            fm = QFontMetricsF(self.font_ui)
            space = fm.horizontalAdvance(" ")
            widths = [fm.horizontalAdvance(w) for w in words]
            cur = min(len(words) - 1, int(said))
            upto = sum(widths[:cur + 1]) + space * cur
            shift = max(0.0, upto - box.width() + 24)          # keep the current word in view
            x = box.left() - shift
            y = box.center().y() + fm.ascent() / 2 - 2
            for i, (word, w) in enumerate(zip(words, widths)):
                if x + w > box.left() - 4 and x < box.right() + 4:
                    if i < int(said):
                        col = rgba((246, 244, 255), 255 * alpha)
                    elif i == int(said):
                        frac = said - int(said)
                        col = rgba([a + (b - a) * frac for a, b in zip((150, 140, 190), (255, 255, 255))], 255 * alpha)
                    else:
                        col = rgba((200, 196, 225), 95 * alpha)
                    p.setPen(col)
                    p.drawText(QPointF(x, y), word)
                x += w + space
            fade = QLinearGradient(QPointF(box.left(), 0), QPointF(box.left() + 18, 0))
            if shift > 0:
                fade.setColorAt(0.0, QColor(14, 14, 22, int(220 * alpha)))
                fade.setColorAt(1.0, QColor(14, 14, 22, 0))
                p.fillRect(QRectF(box.left(), box.top(), 18, box.height()), QBrush(fade))

        def draw_card(self, p, rect, t, tint, k) -> None:
            a = max(0.0, min(1.0, (k - 0.45) / 0.55))
            if a <= 0.01:
                return
            left = rect.left() + 64
            top = rect.top() + 18
            p.setFont(self.font_small)
            p.setPen(rgba((200, 215, 235), 190 * a))
            status = {"idle": "Waiting for “Veronica”", "listening": "Listening", "open": "Listening for a follow-up",
                      "thinking": "Thinking", "speaking": "Speaking"}.get(self.state, "")
            p.drawText(QPointF(left, top + 10), f"VERONICA  ·  {status}")
            box = QRectF(left, top + 22, rect.right() - left - 22, 26)
            if self.you:
                p.setPen(rgba((170, 185, 210), 170 * a))
                p.drawText(box, Qt.TextFlag.TextSingleLine | Qt.AlignmentFlag.AlignVCenter,
                           QFontMetricsF(self.font_small).elidedText("You: " + self.you, Qt.TextElideMode.ElideRight, box.width()))
            p.setFont(self.font_big)
            p.setPen(rgba((246, 246, 255), 245 * a))
            body = QRectF(left, top + 50, rect.right() - left - 22, rect.bottom() - (top + 50) - 52)
            text = self.reply or ("Say “Veronica” to start." if self.state == "idle" else "…")
            p.drawText(body, Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignTop, _fit(text, 150))
            # buttons
            self.buttons = {}
            bx = left
            by = rect.bottom() - 42
            labels = [("terminal", "Hide terminal" if _terminal_visible(self.console) else "Show terminal")]
            if self.state == "speaking":
                labels.append(("stop", "Stop talking"))
            p.setFont(self.font_small)
            for name, label in labels:
                bw = QFontMetricsF(self.font_small).horizontalAdvance(label) + 28
                r = QRectF(bx, by, bw, 28)
                bp = QPainterPath()
                bp.addRoundedRect(r, 14, 14)
                p.fillPath(bp, rgba((255, 255, 255), 26 * a))
                p.setPen(QPen(rgba((255, 255, 255), 50 * a), 1))
                p.drawPath(bp)
                p.setPen(rgba((235, 242, 255), 230 * a))
                p.drawText(r, Qt.AlignmentFlag.AlignCenter, label)
                self.buttons[name] = r
                bx += bw + 10

    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(True)
    app.island = Island()          # kept on the app: a local would be garbage-collected (window gone at once)
    app.island.show()
    return app


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
