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
    r"nothing(?:\s+else)?|no(?:\s+thanks?)?|stop|cancel|bye|i'?m\s+done|that\s+will\s+be\s+all|"
    # "go to sleep" said to her means stop listening — never put the PC to sleep
    r"(?:you\s+can\s+)?(?:go\s+(?:to\s+)?)?sleep(?:\s+now)?|good\s*night|go\s+(?:to\s+)?rest|"
    r"(?:ok(?:ay)?\s+)?that'?s\s+enough|see\s+you(?:\s+later)?|goodbye)\s*[.!]?\s*$", re.I)

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
        self.stt = SpeechToText(config.stt, corrections=self._make_corrections()) if voice_mode else None
        if self.stt is not None:
            from veronica.speech.vocabulary import build_vocabulary
            self.stt.vocabulary = lambda: build_vocabulary(config)   # spelling hint for Whisper
        self._tts_active = False  # prevents self-listening during playback
        self.hub = self._make_mic_hub() if voice_mode else None
        if self.hub is not None and self.stt is not None:
            self.stt.mic_factory = lambda chunk: self.hub.tap(chunk)
            self.stt.hub = self.hub
        self.wake_gate = self._make_wake_gate(wake_gate) if (voice_mode and wake_mode) else None
        self._barged = False                     # the user just talked over her: listen at once
        self._speak_lock = threading.RLock()     # one voice at a time (replies + reminders)
        self._capturing = threading.Event()      # set while STT is recording the user
        self._stop = threading.Event()
        self._follow_up_until = 0.0              # conversation mode window (monotonic time)
        if voice_mode:
            global _voice_confirmer
            _voice_confirmer = self._confirm_by_voice
            from veronica.tools.voice_test_tools import set_voice_tester
            set_voice_tester(self._voice_test)

    def _make_corrections(self):
        from veronica.speech.corrections import Corrections
        return Corrections(self.config.resolve_path(
            getattr(self.config.stt, "corrections_path", "data/stt_corrections.json")))

    def _make_mic_hub(self):
        """One always-open mic stream for wake word, speech recognition and talk-over.
        Falls back to opening the device per listen if the stream can't start."""
        if not getattr(self.config.voice, "shared_mic", True):
            return None
        try:
            from veronica.voice.mic_hub import MicHub
            from veronica.voice.microphone import resolve_input_device
            device = resolve_input_device(getattr(self.config.stt, "mic_device_name", None),
                                          self.config.stt.mic_device_index)
            hub = MicHub(device)
            if hub.ensure_running():
                return hub
            logger.warning("Shared microphone unavailable (%s); opening it per listen instead.", hub.failed)
        except Exception:  # noqa: BLE001
            logger.exception("Shared microphone failed to start; opening it per listen instead.")
        return None

    def _make_wake_gate(self, gate):
        if gate is None:
            from veronica.voice.wake_gate import FRAME_SAMPLES, WakeGate
            factory = (lambda: self.hub.tap(FRAME_SAMPLES)) if self.hub is not None else None
            gate = WakeGate(self.config, mic_factory=factory)
        if not gate.available:
            # Honest fallback: say why, then keep working as plain voice mode.
            self._notice(f"My wake word isn't working ({gate.reason}), so I'll listen to everything you say.")
            return None
        return gate

    # -- console ---------------------------------------------------------------------
    @property
    def _verbose(self) -> bool:
        return getattr(self.config, "console_mode", "full") != "conversation"

    def _status(self, message: str) -> None:
        """Housekeeping (listening..., wake misses, background progress): always in the
        log file; on screen only in console_mode 'full'."""
        logger.info(message)
        if self._verbose:
            print(message)

    def _notice(self, message: str) -> None:
        """Something the user needs to know, shown as VERONICA saying it (never dropped)."""
        logger.warning(message)
        print(f"{self.config.assistant.name}: {message}")

    def banner(self) -> None:
        if not self._verbose:
            print(f"{self.config.assistant.name}: Online.")
            if self.voice_mode:
                self._report_output()
            return
        print(f"\n{'=' * 60}")
        print(f"  {self.config.assistant.name} — online.")
        print(f"  Type your request, or 'exit' to quit. Voice mode: {'ON' if self.voice_mode else 'off (text only)'}")
        if self.wake_gate is not None:
            print(f"  Wake word: ON — say \"{self.config.assistant.name}\", wait for the beep, then speak.")
        if self.voice_mode:
            self._report_output()
        print(f"{'=' * 60}\n")

    def _report_output(self) -> None:
        """Say where the voice will come out — a muted or headphone output looks
        exactly like 'VERONICA has no voice'."""
        from veronica.tools.system_tools import output_status
        out = output_status()
        if out is None:
            return
        self._status(f"  Voice output: {out['device']} at {out['level']}%{' (MUTED)' if out['muted'] else ''}")
        if out["muted"] or out["level"] < 15:
            self._notice("You won't hear me: " + ("the sound is muted" if out["muted"] else "the volume is very low")
                         + ". Say 'unmute' or 'volume to 60', or use the speaker icon on the taskbar.")

    def run(self) -> None:
        self.banner()
        threading.Thread(target=self._announce_reminders, name="reminder-announcer", daemon=True).start()
        # Locating via Windows can take ~10 s the first time; do it now, not on the first question.
        threading.Thread(target=self._warm_location, name="locate", daemon=True).start()
        from veronica.core.background import BackgroundTasks
        self.background = BackgroundTasks(self.orchestrator, self._announce_task,
                                          store_path=self.config.resolve_path("data/background_tasks.json"))
        self.proactive = self._start_proactive()
        self.health_monitor = self._start_self_heal()
        try:
            while True:
                try:
                    text = self._get_input()
                except (KeyboardInterrupt, EOFError):
                    self._status("Shutting down.")
                    break

                if not text:
                    continue
                if self.voice_mode and self._ends_conversation(text):
                    # Voice: "bye" / "go to sleep" / "thanks" end the conversation; she
                    # goes back to waiting for her name. She is never quit by voice, and
                    # nothing here touches the PC itself.
                    self._say("Okay.")
                    self._follow_up_until = 0.0
                    continue
                if not self.voice_mode and text.strip().lower().strip(".!") in _EXIT_WORDS:
                    self._say("Goodbye.")
                    break

                try:
                    reply = self._background_reply(text)
                    if reply is None:
                        reply = self.orchestrator.handle(text, self.session).text
                except Exception:  # noqa: BLE001 - one failed request must never take VERONICA down
                    logger.exception("Request failed: %r", text)
                    reply = "Sorry, something went wrong on my end with that one. The details are in the log."
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
            pcfg, self._proactive_say, state_path=self.config.resolve_path("data/proactive_state.json"),
            rain_probability=rain_probability_for(Profile(self.config)),
            briefing=(lambda: briefing_tool.run(include_tasks=False).message) if briefing_tool else None)
        monitor.start()
        return monitor

    def _proactive_say(self, text: str, urgent: bool = False) -> None:
        while self._capturing.is_set() and not self._stop.is_set():
            time.sleep(0.2)  # never talk over the user
        self._status(f"[proactive{' URGENT' if urgent else ''}]")
        self._beep()
        self._say(text)

    # -- self-heal -----------------------------------------------------------------------
    def _start_self_heal(self):
        """Watch VERONICA's own workers and memory files; restart/restore what breaks."""
        try:
            from veronica.core.self_heal import HealthMonitor
            from veronica.tools.self_heal_tools import _data_files, set_live_monitor
            llm = getattr(self.orchestrator, "llm", None)
            monitor = HealthMonitor(speak=self._heal_say, data_files=_data_files(self.config), llm=llm)
            monitor.watch_threads("background-", 1, lambda: self.background.ensure_workers(), "background worker")
            if self.proactive is not None:
                monitor.watch_threads("proactive", 1, self.proactive.start, "proactive monitor")
            monitor.start()
            set_live_monitor(lambda: monitor)
            return monitor
        except Exception:  # noqa: BLE001 — self-heal must never stop VERONICA from running
            logger.exception("Couldn't start the self-heal monitor.")
            return None

    def _heal_say(self, text: str, urgent: bool = False) -> None:
        while self._capturing.is_set() and not self._stop.is_set():
            time.sleep(0.2)
        self._status("[self-heal]")
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
            self._status(f"[background] started: {task}")
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
        self._status(f"[background] {task.status}: {task.request}")
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
        lock = getattr(self, "_speak_lock", None)
        if lock is not None:
            with lock:              # never open the mic while she (or an announcement) is talking
                pass
        self._capturing.set()
        try:
            return self.stt.listen_once(timeout_s=timeout_s)
        finally:
            self._capturing.clear()

    def _confirm_by_voice(self, description: str, level: PermissionLevel) -> bool:
        critical = level >= PermissionLevel.CRITICAL
        how = "Say confirm to go ahead, or no to cancel." if critical else "Say yes or no."
        self._status(f"[permission {level.name}] {description}")
        prompt = f"I need your permission to {spoken_permission(description)}. {how}"
        for attempt in range(2):
            self._say(prompt)
            heard = self._listen()
            answer = parse_confirmation(heard.text if heard.success else "", level)
            if heard.success:
                print(f"You: {heard.text}")
            if answer is not None:
                logger.info("Voice %s for %s (%s)", "approval" if answer else "denial", description, level.name)
                return answer
            prompt = f"Sorry, I didn't catch that. {how}"
        self._say("I'll take that as a no.")  # unclear or silent: deny
        return False

    # -- conversation mode -----------------------------------------------------------
    def _ends_conversation(self, text: str) -> bool:
        """An ending phrase ("thanks", "good night") — unless it is the name of one of the
        user's routines, which then runs instead."""
        if not _ENDS_CONVERSATION.match(text or ""):
            return False
        tools = getattr(getattr(self, "orchestrator", None), "tools", None)
        routine = tools.get("run_routine") if tools is not None and hasattr(tools, "get") else None
        try:
            return not (routine is not None and routine.matches(text))
        except Exception:  # noqa: BLE001
            return True

    def _conversation_mode(self) -> bool:
        return bool(getattr(self.config.voice, "conversation_mode", False))

    def _open_follow_up(self) -> None:
        """After a reply, keep listening WITHOUT the wake word. In conversation mode the
        window stays open until the user ends it (a long idle timeout is the only safety
        fallback); otherwise it's the short follow_up_seconds window."""
        if not (self.voice_mode and self.wake_gate is not None):
            return
        if self._conversation_mode():
            idle = float(getattr(self.config.voice, "conversation_idle_timeout_s", 300) or 0)
            self._follow_up_until = time.monotonic() + idle if idle > 0 else float("inf")
        else:
            secs = float(getattr(self.config.voice, "follow_up_seconds", 0) or 0)
            if secs > 0:
                self._follow_up_until = time.monotonic() + secs

    def _follow_up(self) -> Optional[str]:
        """The follow-up turn: the user's words, '' to drop back to the wake word, or
        None when no follow-up window is open."""
        if time.monotonic() >= getattr(self, "_follow_up_until", 0.0):
            return None
        if self._conversation_mode():
            return self._conversation_turn()
        secs = float(getattr(self.config.voice, "follow_up_seconds", 0) or 0)
        self._follow_up_until = 0.0
        self._status(f"[listening for a follow-up... just speak ({secs:.0f}s)]")
        result = self._listen(timeout_s=max(1, int(round(secs))))
        text = self._accept(result, open_conversation=True) if result.success else None
        if not text:
            self._status(f"[back to waiting for \"{self.config.assistant.name}\"]")
            return ""
        if self._ends_conversation(text):
            self._say("Okay.")
            return ""
        return text

    def _conversation_turn(self) -> str:
        """Conversation mode: stay listening (no wake word) through silences until the
        user speaks, says an ending phrase, or goes quiet past the idle timeout."""
        deadline = getattr(self, "_follow_up_until", 0.0)
        name = self.config.assistant.name
        self._status("[conversation — just speak; say 'thanks' or 'bye' to end]")
        while time.monotonic() < deadline:
            result = self._listen(timeout_s=6)
            if not result.success and result.error and not result.error.startswith("No speech detected"):
                logger.info("Conversation: nothing usable heard (%s).", result.error)
            text = self._accept(result, open_conversation=True) if result.success else None
            if not text:
                continue                        # silence or background speech: keep it open
            logger.info("Conversation: heard %r", text)
            if self._ends_conversation(text):
                self._say("Anytime.")
                self._follow_up_until = 0.0
                return ""                       # user ended it: back to the wake word
            return text
        logger.info("Conversation ended: quiet for %s s.", getattr(self.config.voice, "conversation_idle_timeout_s", 300))
        self._status(f"[quiet for a while — back to waiting for \"{name}\"]")
        self._follow_up_until = 0.0
        return ""

    def _get_input(self) -> str:
        if self.voice_mode and self.stt is not None and getattr(self, "_barged", False):
            # The user talked over her: listen straight away, no wake word.
            self._barged = False
            self._status("[listening — you interrupted me]")
            result = self._listen()
            text = self._accept(result, open_conversation=False) if result.success else None
            return text or ""
        if self.voice_mode and self.stt is not None:
            # Stage -1: conversation mode — a follow-up right after a reply needs no wake word.
            follow = self._follow_up() if self.wake_gate is not None else None
            if follow is not None:
                return follow
            # Stage 0 (--wake only): wait for the wake word; the gate releases the mic for STT.
            if self.wake_gate is not None:
                self._status(f"[waiting for \"{self.config.assistant.name}\"...]")
                threshold = self.config.voice.wake_threshold
                near_miss = lambda s: self._status(  # noqa: E731
                    f"[wake] heard something close (score {s:.2f}, needs {threshold:.2f}) "
                    f"- say \"{self.config.assistant.name}\" a little clearer or closer")
                if not self.wake_gate.wait(on_near_miss=near_miss):
                    reason = self.wake_gate.reason or "no wake word heard"
                    if "microphone" in reason and reason != getattr(self, "_last_mic_notice", ""):
                        self._last_mic_notice = reason  # say it once, not every second
                        self._notice(f"I can't hear you: {reason}.")
                    else:
                        self._status(f"[wake] {reason}")
                    time.sleep(1.0)  # don't spin if the mic keeps failing
                    return ""
                # Hand STT the room level heard before the wake word (used for follow-ups too).
                ambient = getattr(self.wake_gate, "ambient_rms", None)
                if ambient is not None:
                    self.stt.known_ambient_rms = ambient
                self._beep()
            # Stage 1: LISTEN — microphone is active
            self._status("[listening...]")
            result = self._listen()
            if not result.success:
                self._status(f"[STT] {result.error}")
                return ""
            return self._accept(result, open_conversation=False) or ""
        return input("You: ")

    def _voice_test(self) -> str:
        """The guided voice check on the real mic and speaker (voice/voice_test.py)."""
        from veronica.voice.mic_hub import rms
        from veronica.voice.voice_test import persist_number, run_voice_test, summarize
        hub = getattr(self, "hub", None)

        def measure(seconds: float):
            if hub is None:
                return getattr(self.stt, "known_ambient_rms", None)
            tap = hub.tap(320)
            if not tap.open():
                return None
            levels, end = [], time.monotonic() + seconds
            try:
                while time.monotonic() < end:
                    b = tap.read()
                    if b:
                        levels.append(rms(b))
            finally:
                tap.close()
            levels.sort()
            return levels[len(levels) // 4] if levels else None

        def speak_and_watch(text: str):
            stop = threading.Event()
            monitor = self._start_barge_in(stop)
            started = time.monotonic()
            if hub is not None:
                hub.self_speaking = True
            try:
                completed = self.speaker.speak_interruptible(text, stop, on_piece=monitor.arm_for if monitor else None)
            finally:
                if monitor is not None:
                    monitor.stop()
                if hub is not None:
                    hub.self_speaking = False
            return completed, time.monotonic() - started

        can_talk_over = hub is not None and getattr(self, "wake_gate", None) is not None             and hasattr(self.speaker, "speak_interruptible")
        ratio = float(getattr(self.config.voice, "conversation_speech_ratio", 3.0) or 3.0)
        report = run_voice_test(say=self._say, listen=lambda: self._listen(timeout_s=8), measure_room=measure,
                                speak_interruptible=speak_and_watch if can_talk_over else None, needed_ratio=ratio)
        summary = summarize(report)
        if report.suggested_ratio:
            self._say(f"Want me to lower that bar to {report.suggested_ratio}? Say yes or no.")
            heard = self._listen()
            answer = parse_confirmation(heard.text if heard.success else "", PermissionLevel.SENSITIVE)
            if answer:
                self.config.voice.conversation_speech_ratio = report.suggested_ratio
                saved = persist_number(self.config.resolve_path("config/config.yaml"),
                                       "conversation_speech_ratio", report.suggested_ratio)
                summary += (f" Done: lowered to {report.suggested_ratio}"
                            + (" and saved in config." if saved else " for now."))
            else:
                summary += " I left the setting as it was."
        return summary

    def _start_barge_in(self, stop: threading.Event):
        """The talk-over listener for one reply (None when unavailable or switched off)."""
        if (getattr(self, "hub", None) is None or getattr(self, "wake_gate", None) is None
                or getattr(self.config.voice, "barge_in", "wake_word") != "wake_word"):
            return None
        try:
            from veronica.voice.barge_in import BargeInMonitor
            return BargeInMonitor(lambda n: self.hub.tap(n), self.wake_gate.detector,
                                  name=self.config.assistant.name).start(stop)
        except Exception:  # noqa: BLE001 - never let it stop her speaking
            logger.exception("Couldn't start the talk-over listener.")
            return None

    def _accept(self, result, *, open_conversation: bool) -> Optional[str]:
        """Decide what the user actually said, or None to ignore it.

        * Prefer the recognizer's alternative reading that is a real command when the top
          reading isn't one ("minimise all attacks" vs "minimise all tabs").
        * In an open conversation (no wake word this turn): ignore speech that isn't aimed
          at her — too quiet for how loud the room is (a TV across the room), or low
          recognition confidence.
        * After the wake word: if confidence is very low, ask "did you say ...?"."""
        text = (getattr(result, "text", "") or "").strip()
        if not text:
            return None
        tools = getattr(getattr(self, "orchestrator", None), "tools", None)
        matches = (lambda t: tools.find_fast_match(t) is not None) if tools is not None else (lambda t: False)
        if not matches(text):
            for alt in list(getattr(result, "alternatives", []) or [])[:3]:
                if matches(alt):
                    logger.info("Heard %r; using the reading %r, which is a command.", text, alt)
                    text = alt
                    break
        conf = getattr(result, "confidence", None)
        if open_conversation:
            stats = getattr(result, "stats", None) or {}
            ambient, speech = stats.get("ambient_rms"), stats.get("speech_rms")
            voice = getattr(self.config, "voice", None)
            ratio = float(getattr(voice, "conversation_speech_ratio", 0) or 0)
            # The same "is this really the user?" checks apply to ending phrases: Whisper
            # turns the echo of her own last word, or a short noise, into "Thank you." —
            # which used to end the conversation by itself.
            if ratio and ambient and speech and speech < ambient * ratio:
                logger.info("Ignored background speech %r (level %s vs room %s).", text, speech, ambient)
                return None
            if self._ends_conversation(text):
                voiced = stats.get("voiced_s")
                logprob = stats.get("whisper_logprob")
                if (voiced is not None and voiced < 0.45) or (logprob is not None and logprob < -0.5):
                    logger.info("Ignored a doubtful ending phrase %r (voiced %ss, whisper score %s).",
                                text, voiced, logprob)
                    return None
                print(f"You: {text}")
                logger.info("Conversation ended by the user: %r", text)
                return text
            floor = float(getattr(voice, "conversation_min_confidence", 0) or 0)
            if floor and conf is not None and conf < floor and not matches(text):
                logger.info("Ignored unclear speech %r (confidence %.2f).", text, conf)
                return None
            print(f"You: {text}")
            return text
        print(f"You: {text}")
        ask_below = float(getattr(getattr(self.config, "stt", None), "confirm_below_confidence", 0) or 0)
        if ask_below and conf is not None and conf < ask_below:
            return self._check_what_was_said(text)
        return text

    def _check_what_was_said(self, text: str) -> Optional[str]:
        """Low confidence: "Did you say ...?" — yes keeps it, no asks once more."""
        self._say(f"Sorry, did you say: {text}?")
        heard = self._listen()
        reply = (heard.text or "").strip() if heard.success else ""
        if reply:
            print(f"You: {reply}")
        if _YES_WORDS.search(reply.lower()) and not _NO_WORDS.search(reply.lower()):
            return text
        if reply and not _NO_WORDS.search(reply.lower()):
            return reply                         # they just said it again, more clearly
        self._say("Okay, say it again.")
        again = self._listen()
        if again.success and (again.text or "").strip():
            print(f"You: {again.text}")
            return again.text.strip()
        return None

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
                # Stage 2: speak sentence by sentence; "Veronica" mid-reply stops her.
                self._tts_active = True
                stop = threading.Event()
                monitor = self._start_barge_in(stop)
                hub = getattr(self, "hub", None)
                if hub is not None:
                    hub.self_speaking = True     # her voice is not the room's noise level
                completed = True
                try:
                    speak_i = getattr(self.speaker, "speak_interruptible", None)
                    if speak_i is not None:
                        completed = speak_i(text, stop, on_piece=monitor.arm_for if monitor else None)
                    else:
                        self.speaker.speak(text)
                except Exception as e:
                    logger.warning("TTS playback failed: %s", e)
                finally:
                    if monitor is not None:
                        monitor.stop()
                    if hub is not None:
                        hub.self_speaking = False
                    self._tts_active = False
                if not completed:
                    self._barged = True
                    self._status("[you talked over me — listening]")
                    self._beep()
                # Stage 3: short cooldown so the room's echo dies down before listening
                time.sleep(0.1)
