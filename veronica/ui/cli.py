"""
Reference text/voice front-end. Deliberately thin: it does nothing but collect input
(typed or spoken), hand it to `Orchestrator.handle()`, and present the output (printed
and/or spoken). A GUI, the 3D orb HUD, or a Slack bot would all look the same shape.
"""
from __future__ import annotations

import ast
import datetime as dt
import re
import threading
import time
from typing import Callable, Optional

from veronica.config import Config
from veronica.core.orchestrator import Orchestrator
from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel
from veronica.speech.stt import SpeechToText
from veronica.speech.tts import TextToSpeech, SynchronousSpeaker

logger = get_logger("ui.cli")

_EXIT_WORDS = {"exit", "quit", "bye", "goodbye", "shutdown"}

# Set by a voice-mode CLIApp so permission questions are asked and answered by voice.
# The PermissionGate is built (in main.build_app) before the CLI exists, so it keeps
# calling _confirm_in_terminal, which delegates here when a voice confirmer is active.
_voice_confirmer: Optional[Callable[[str, PermissionLevel], bool]] = None

_PAUSE_ALERTS = re.compile(
    r"\b(?:pause|mute|stop|silence|hold)\s+(?:the\s+|your\s+|my\s+)?(?:alerts|notifications\s+from\s+you|interruptions)"
    r"(?:\s+for\s+(?:an?\s+|(\d+(?:\.\d+)?)\s+)?(hours?|hrs?|minutes?|mins?))?\b|"
    r"\bbe\s+quiet\s+for\s+(?:an?\s+|(\d+)\s+)?(hours?|minutes?)\b", re.I)
_RESUME_ALERTS = re.compile(r"\b(?:resume|unmute|restart|turn\s+on)\s+(?:the\s+|your\s+)?alerts\b", re.I)


def _duration(minutes: float) -> str:
    if minutes >= 60 and minutes % 60 == 0:
        h = int(minutes // 60)
        return "an hour" if h == 1 else f"{h} hours"
    return f"{int(minutes)} minutes"


# Said during a follow-up: end the conversation instead of sending it to the brain.
_ENDS_CONVERSATION = re.compile(
    r"^\s*(?:ok(?:ay)?[,\s]+)?(?:thanks?(?:\s+you)?|thank\s+you(?:\s+\w+)?|that'?s\s+(?:all|it)|never\s*mind|"
    r"nothing(?:\s+else)?|no(?:\s+thanks?)?|stop|cancel|bye|i'?m\s+done|that\s+will\s+be\s+all)\s*[.!]?\s*$", re.I)

_NO_WORDS = re.compile(r"\b(no|nope|nah|cancel|stop|don'?t|do not|deny|negative|abort|wait)\b")
_YES_WORDS = re.compile(r"\b(yes|yeah|yep|yup|sure|ok|okay|go ahead|do it|proceed|confirm|confirmed|affirmative|allow)\b")


def _confirm_in_terminal(description: str, level: PermissionLevel) -> bool:
    from veronica.core.background import in_background, note_permission_refused
    if in_background():
        # Background tasks never ask: the user may be away, and a "yes" meant for the
        # conversation must not approve them. Refuse and report it when the task ends.
        note_permission_refused(description)
        logger.info("Background task needed %s permission for %s; refused (not asked).", level.name, description)
        return False
    if _voice_confirmer is not None:
        return _voice_confirmer(description, level)
    answer = input(f"\n[VERONICA needs {level.name} permission] {description}\nProceed? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def parse_confirmation(text: str, level: PermissionLevel) -> Optional[bool]:
    """Spoken answer -> True / False / None (unclear). Any 'no' wins. CRITICAL actions
    (delete, shell) need the word 'confirm' — a stray 'yes' from a TV must not approve them."""
    t = (text or "").lower()
    if _NO_WORDS.search(t):
        return False
    if level >= PermissionLevel.CRITICAL:
        return True if re.search(r"\bconfirm(ed)?\b", t) else None
    return True if _YES_WORDS.search(t) else None


def spoken_permission(description: str) -> str:
    """'write_file({'path': 'a.txt', ...})' -> 'write file, path a.txt, ...' (short, speakable)."""
    m = re.match(r"^\s*(\w+)\((.*)\)\s*$", description or "", re.S)
    if not m:
        return (description or "do that").splitlines()[0][:140]
    action = m.group(1).replace("_", " ")
    try:
        args = ast.literal_eval(m.group(2)) if m.group(2).strip() else {}
    except (ValueError, SyntaxError):
        args = {}
    if isinstance(args, dict) and args:
        details = ", ".join(f"{k.replace('_', ' ')} {str(v)[:60]}" for k, v in args.items())
        return f"{action}, {details}"[:160]
    return action


class CLIApp:
    def __init__(self, config: Config, orchestrator: Orchestrator, voice_mode: bool = False,
                 wake_mode: bool = False, wake_gate=None) -> None:
        self.config = config
        self.orchestrator = orchestrator
        self.voice_mode = voice_mode
        self.session = orchestrator.new_session()
        self.tts = TextToSpeech(config.tts, cache_dir=config.resolve_path(config.tts.cache_dir))
        self.speaker = SynchronousSpeaker(self.tts)
        self.stt = SpeechToText(config.stt) if voice_mode else None
        self._tts_active = False  # prevents self-listening during playback
        self.wake_gate = self._make_wake_gate(wake_gate) if (voice_mode and wake_mode) else None
        self._speak_lock = threading.RLock()     # one voice at a time (replies + reminders)
        self._capturing = threading.Event()      # set while STT is recording the user
        self._stop = threading.Event()
        self._follow_up_until = 0.0              # conversation mode window (monotonic time)
        if voice_mode:
            global _voice_confirmer
            _voice_confirmer = self._confirm_by_voice

    def _make_wake_gate(self, gate):
        if gate is None:
            from veronica.voice.wake_gate import WakeGate
            gate = WakeGate(self.config)
        if not gate.available:
            # Honest fallback: say why, then keep working as plain voice mode.
            print(f"[wake] wake word unavailable: {gate.reason}. Listening without a wake word.")
            return None
        return gate

    def banner(self) -> None:
        print(f"\n{'=' * 60}")
        print(f"  {self.config.assistant.name} — online.")
        print(f"  Type your request, or 'exit' to quit. Voice mode: {'ON' if self.voice_mode else 'off (text only)'}")
        if self.wake_gate is not None:
            print(f"  Wake word: ON — say \"{self.config.assistant.name}\", wait for the beep, then speak.")
        if self.voice_mode:
            self._report_output()
        print(f"{'=' * 60}\n")

    @staticmethod
    def _report_output() -> None:
        """Say where the voice will come out — a muted or headphone output looks
        exactly like 'VERONICA has no voice'."""
        from veronica.tools.system_tools import output_status
        out = output_status()
        if out is None:
            return
        print(f"  Voice output: {out['device']} at {out['level']}%{' (MUTED)' if out['muted'] else ''}")
        if out["muted"] or out["level"] < 15:
            print("  !! You won't hear me: " + ("the sound is muted" if out["muted"] else "the volume is very low")
                  + ". Say 'unmute' / 'volume to 60', or use the speaker icon on the taskbar.")

    def run(self) -> None:
        self.banner()
        threading.Thread(target=self._announce_reminders, name="reminder-announcer", daemon=True).start()
        # Locating via Windows can take ~10 s the first time; do it now, not on the first question.
        threading.Thread(target=self._warm_location, name="locate", daemon=True).start()
        from veronica.core.background import BackgroundTasks
        self.background = BackgroundTasks(self.orchestrator, self._announce_task,
                                          store_path=self.config.resolve_path("data/background_tasks.json"))
        self.proactive = self._start_proactive()
        try:
            while True:
                try:
                    text = self._get_input()
                except (KeyboardInterrupt, EOFError):
                    print("\nShutting down.")
                    break

                if not text:
                    continue
                if text.strip().lower().strip(".!") in _EXIT_WORDS:
                    self._say("Goodbye.")
                    break

                reply = self._background_reply(text)
                if reply is None:
                    reply = self.orchestrator.handle(text, self.session).text
                self._say(reply)
                self._open_follow_up()
        finally:
            self._stop.set()

    # -- proactive -----------------------------------------------------------------------
    def _start_proactive(self):
        pcfg = getattr(self.config, "proactive", None)
        if not pcfg or not pcfg.enabled:
            return None
        from veronica.core.proactive import ProactiveMonitor, rain_probability_for
        from veronica.tools.productivity_tools import Profile
        briefing_tool = self.orchestrator.tools.get("daily_briefing")
        monitor = ProactiveMonitor(
            pcfg, self._proactive_say,
            rain_probability=rain_probability_for(Profile(self.config)),
            briefing=(lambda: briefing_tool.run().message) if briefing_tool else None)
        monitor.start()
        return monitor

    def _proactive_say(self, text: str, urgent: bool = False) -> None:
        while self._capturing.is_set() and not self._stop.is_set():
            time.sleep(0.2)  # never talk over the user
        print(f"[proactive{' URGENT' if urgent else ''}] {text}")
        self._beep()
        self._say(text)

    # -- background tasks ----------------------------------------------------------------
    def _background_reply(self, text: str) -> Optional[str]:
        """Start a background task / report on them, or pause/resume proactive alerts;
        None for an ordinary request."""
        from veronica.core.background import is_status_request, split_background_request
        monitor = getattr(self, "proactive", None)
        if monitor is not None:
            m = _PAUSE_ALERTS.search(text)
            if m:
                number, unit = (m.group(1) or m.group(3)), (m.group(2) or m.group(4) or "hour")
                amount = float(number) if number else 1.0
                minutes = amount * (60 if unit.lower().startswith("h") else 1)
                monitor.pause(minutes)
                return (f"Okay, alerts paused for {_duration(minutes)}. I'll only interrupt for a critically "
                        "low battery. Say 'resume alerts' any time.")
            if _RESUME_ALERTS.search(text):
                monitor.resume()
                return "Alerts are back on."
        background = getattr(self, "background", None)
        if background is None:
            return None
        if is_status_request(text):
            return background.status_report()
        task = split_background_request(text)
        if task:
            background.submit(task)
            print(f"[background] started: {task}")
            return "On it. I'll work on that in the background and tell you when it's done."
        return None

    def _announce_task(self, task) -> None:
        while self._capturing.is_set() and not self._stop.is_set():
            time.sleep(0.2)  # never talk over the user's own capture
        sentences = re.split(r"(?<=[.!?])\s+", task.result)
        result = " ".join(sentences[:3])
        if task.status == "done":
            msg = f"Your background task is done: {task.request}. {result}"
        else:
            msg = f"I couldn't finish {task.request}. {result}"
        if task.refused:
            n = len(task.refused)
            msg += (f" I skipped {n} step{'s' if n != 1 else ''} that needed your permission; "
                    "ask me again now if you want me to do that.")
        print(f"[background] {task.status}: {task.request}")
        self._beep()
        self._say(msg)

    def _warm_location(self) -> None:
        try:
            from veronica.tools.productivity_tools import Profile
            Profile(self.config).city_and_source()
        except Exception:  # noqa: BLE001 — location is a convenience, never a blocker
            logger.debug("Background location lookup failed", exc_info=True)

    # -- reminders ---------------------------------------------------------------
    def _announce_reminders(self, poll_s: float = 5.0) -> None:
        """Speak reminders when due (and ones missed while VERONICA was off)."""
        from veronica.tools.productivity_tools import ReminderStore
        store = ReminderStore(self.config)
        while not self._stop.is_set():
            try:
                for r in store.pop_due():
                    # Never talk over the user's own capture: it would be transcribed as input.
                    while self._capturing.is_set() and not self._stop.is_set():
                        time.sleep(0.2)
                    due = dt.datetime.fromisoformat(r["due"])
                    late = dt.datetime.now() - due > dt.timedelta(minutes=2)
                    when = due.strftime("%I:%M %p").lstrip("0")
                    self._beep()
                    self._say(f"Missed reminder from {when}: {r['text']}." if late else f"Reminder: {r['text']}.")
            except Exception:
                logger.exception("Reminder announcer failed; will retry.")
            self._stop.wait(poll_s)

    # -- voice permission ----------------------------------------------------------
    def _listen(self, timeout_s: int = 6):
        self._capturing.set()
        try:
            return self.stt.listen_once(timeout_s=timeout_s)
        finally:
            self._capturing.clear()

    def _confirm_by_voice(self, description: str, level: PermissionLevel) -> bool:
        critical = level >= PermissionLevel.CRITICAL
        how = "Say confirm to go ahead, or no to cancel." if critical else "Say yes or no."
        print(f"\n[permission {level.name}] {description}")
        prompt = f"I need your permission to {spoken_permission(description)}. {how}"
        for attempt in range(2):
            self._say(prompt)
            heard = self._listen()
            answer = parse_confirmation(heard.text if heard.success else "", level)
            if heard.success:
                print(f"You (voice): {heard.text}")
            if answer is not None:
                logger.info("Voice %s for %s (%s)", "approval" if answer else "denial", description, level.name)
                return answer
            prompt = f"Sorry, I didn't catch that. {how}"
        self._say("I'll take that as a no.")  # unclear or silent: deny
        return False

    # -- conversation mode -----------------------------------------------------------
    def _open_follow_up(self) -> None:
        """After a reply, listen for a follow-up WITHOUT the wake word for a few seconds."""
        secs = float(getattr(self.config.voice, "follow_up_seconds", 0) or 0)
        if self.voice_mode and self.wake_gate is not None and secs > 0:
            self._follow_up_until = time.monotonic() + secs

    def _follow_up(self) -> Optional[str]:
        """The follow-up turn: the user's words, '' to drop back to the wake word, or
        None when no follow-up window is open."""
        if time.monotonic() >= getattr(self, "_follow_up_until", 0.0):
            return None
        secs = float(getattr(self.config.voice, "follow_up_seconds", 0) or 0)
        self._follow_up_until = 0.0
        print(f"[listening for a follow-up... just speak ({secs:.0f}s)]")
        result = self._listen(timeout_s=max(1, int(round(secs))))
        if not result.success or not (result.text or "").strip():
            print(f"[back to waiting for \"{self.config.assistant.name}\"]")
            return ""
        print(f"You (voice): {result.text}")
        if _ENDS_CONVERSATION.match(result.text):
            self._say("Okay.")
            return ""
        return result.text

    def _get_input(self) -> str:
        if self.voice_mode and self.stt is not None:
            # Stage -1: conversation mode — a follow-up right after a reply needs no wake word.
            follow = self._follow_up() if self.wake_gate is not None else None
            if follow is not None:
                return follow
            # Stage 0 (--wake only): wait for the wake word; the gate releases the mic for STT.
            if self.wake_gate is not None:
                print(f"[waiting for \"{self.config.assistant.name}\"...]")
                threshold = self.config.voice.wake_threshold
                near_miss = lambda s: print(  # noqa: E731
                    f"[wake] heard something close (score {s:.2f}, needs {threshold:.2f}) "
                    f"- say \"{self.config.assistant.name}\" a little clearer or closer")
                if not self.wake_gate.wait(on_near_miss=near_miss):
                    print(f"[wake] {self.wake_gate.reason or 'no wake word heard'}")
                    time.sleep(1.0)  # don't spin if the mic keeps failing
                    return ""
                # Hand STT the room level heard before the wake word (used for follow-ups too).
                ambient = getattr(self.wake_gate, "ambient_rms", None)
                if ambient is not None:
                    self.stt.known_ambient_rms = ambient
                self._beep()
            # Stage 1: LISTEN — microphone is active
            print("[listening...]")
            result = self._listen()
            if not result.success:
                print(f"[STT] {result.error}")
                return ""
            print(f"You (voice): {result.text}")
            return result.text
        return input("You: ")

    @staticmethod
    def _beep() -> None:
        """Short cue that the wake word was heard (Windows only; silent elsewhere)."""
        try:
            import winsound
            winsound.Beep(880, 120)
        except Exception:
            pass

    def _say(self, text: str) -> None:
        with self._speak_lock:  # replies and reminder announcements never overlap
            print(f"{self.config.assistant.name}: {text}\n")
            if self.voice_mode:
                # Stage 2: Block microphone while TTS plays — prevents self-listening feedback
                self._tts_active = True
                try:
                    self.speaker.speak(text)
                except Exception as e:
                    logger.warning("TTS playback failed: %s", e)
                finally:
                    self._tts_active = False
                # Stage 3: Short cooldown + buffer flush before re-listening
                time.sleep(0.2)
