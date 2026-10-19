"""
Reference text/voice front-end. Deliberately thin: it does nothing but collect input
(typed or spoken), hand it to `Orchestrator.handle()`, and present the output (printed
and/or spoken). A GUI, the 3D orb HUD, or a Slack bot would all look the same shape.
"""
from __future__ import annotations

import ast
import datetime as dt
import re
import sys
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
    from veronica.ui.remote.server import remote_confirmer
    remote = remote_confirmer()
    if remote is not None:
        # Asked from the phone page: the question goes to that device, never to the
        # PC's mic or terminal (where nobody may be). No answer in time means no.
        return remote(description, level)
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
        try:
            from veronica.tools.voice_tools import apply_saved_voice
            apply_saved_voice(config)            # the voice chosen by voice last time
        except Exception:  # noqa: BLE001
            logger.exception("Couldn't load the saved voice.")
        self._earphones_note = None
        if voice_mode:
            try:
                from veronica.voice.earphones import apply as apply_earphones
                self._earphones_note = apply_earphones(config)   # quiet "Veronica" in public
            except Exception:  # noqa: BLE001
                logger.exception("Couldn't check for earphones.")
        self.tts = TextToSpeech(config.tts, cache_dir=config.resolve_path(config.tts.cache_dir))
        if voice_mode:
            self.tts.warm()
        self.quick_ack = None
        if voice_mode and getattr(config.voice, "quick_ack", False):
            from veronica.voice.quick_ack import QuickAck
            self.quick_ack = QuickAck(self.tts, getattr(config.voice, "quick_ack_phrases", None) or ())
            self.quick_ack.prepare()
        self.speaker = SynchronousSpeaker(self.tts)
        self.stt = SpeechToText(config.stt, corrections=self._make_corrections()) if voice_mode else None
        if self.stt is not None:
            from veronica.speech.vocabulary import build_echo_terms, build_vocabulary
            self.stt.vocabulary = lambda: build_vocabulary(config)   # spelling hint for Whisper
            # Her name and the local place names, not the app names: see build_echo_terms.
            self.stt.echo_terms = lambda: build_echo_terms(config)

            def names_unknown_app(text: str) -> bool:
                from veronica.tools.app_index import names_an_unknown_app, shared_index
                return names_an_unknown_app(shared_index(config), text)
            self.stt.unknown_target = names_unknown_app
            # Loading the offline Whisper model takes 13-19 s on this PC. Off the main
            # thread, so it happens while she greets you instead of before your first word.
            threading.Thread(target=self.stt.warm_up, name="stt-warm", daemon=True).start()
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
            try:
                from veronica.tools.voiceprint_tools import set_voice_enroller
                from veronica.voice.voiceprint import get_voiceprint
                set_voice_enroller(self._learn_voice)
                vp = get_voiceprint(self.config)
                if self.stt is not None and getattr(self.config.voice, "speaker_check", True):
                    self.stt.voiceprint = vp     # only the owner's voice, once it's learned
                if vp.enrolled:
                    vp.warm()
            except Exception:  # noqa: BLE001 - the voice check is optional
                logger.exception("Voice check not set up.")

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
        if getattr(self, "_earphones_note", None):
            self._notice(self._earphones_note)
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
        try:
            from veronica.logging_setup import quiet_third_party
            quiet_third_party()        # libraries that set up their own console logging on import
        except Exception:  # noqa: BLE001
            pass
        try:
            from veronica.memory.mem0_memory import get_mem0
            mem = get_mem0(self.config)
            if mem is not None:
                mem.warm()
        except Exception:  # noqa: BLE001 - Mem0 is optional
            pass
        try:
            from veronica.integrations.browser_agent import set_announcer
            set_announcer(self._announce_browser)
        except Exception:  # noqa: BLE001 - browser-use is optional
            pass
        try:
            from veronica.integrations.gps import start_gps
            start_gps(self.config)               # a USB GPS or the phone, if there is one
        except Exception:  # noqa: BLE001 - GPS is optional
            logger.exception("Couldn't start GPS.")
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
                    from veronica.core.flair import goodbye
                    self._say(goodbye())
                    self._follow_up_until = 0.0
                    continue
                if not self.voice_mode and text.strip().lower().strip(".!") in _EXIT_WORDS:
                    self._say("Goodbye.")
                    break

                live = None
                try:
                    reply = self._background_reply(text)
                    if reply is None:
                        self._acknowledge(text)
                        reply, live = self._answer(text)
                except Exception:  # noqa: BLE001 - one failed request must never take VERONICA down
                    logger.exception("Request failed: %r", text)
                    reply = "Sorry, something went wrong on my end with that one. The details are in the log."
                if live is None:
                    self._say(reply)
                else:
                    rest = live.remainder(reply)   # e.g. a figure she couldn't confirm, added after
                    if rest:
                        self._say(rest)
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
        if not task:
            from veronica.core.understanding import is_multistep_task
            if is_multistep_task(text) and getattr(getattr(self.config, "understanding", None), "enabled", False):
                task = text.strip()          # research-and-save jobs take a while: always in the background
        if task:
            background.submit(task)
            self._status(f"[background] started: {task}")
            return "On it. I'll work on that in the background and tell you when it's done."
        return None

    def _announce_browser(self, text: str) -> None:
        while self._capturing.is_set() and not self._stop.is_set():
            time.sleep(0.2)  # never talk over the user's own capture
        self._status("[browser] done")
        self._beep()
        self._say(text)

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
        meter = self._start_meter()
        try:
            return self.stt.listen_once(timeout_s=timeout_s)
        finally:
            self._capturing.clear()
            if meter is not None:
                meter.join(timeout=1.0)

    def _listen_addressed(self, timeout_s: int = 6):
        """Listen right after her name: an unclear read gets a cloud re-read, not the bin."""
        self.stt.addressed = True
        try:
            return self._listen(timeout_s=timeout_s)
        finally:
            self.stt.addressed = False

    def _start_meter(self):
        """A one-line live mic level while she listens, like a voice-mode waveform:
        dots when it's quiet, bars as you speak. Erased when she stops listening, so the
        conversation lines stay clean. Off when the terminal can't show it."""
        hub = getattr(self, "hub", None)
        if not self.voice_mode or hub is None or not getattr(getattr(self.config, "voice", None), "show_mic_meter", True):
            return None
        try:
            if not sys.stdout.isatty():
                return None
        except Exception:  # noqa: BLE001
            return None
        from veronica.voice.meter import render

        def run():
            history: list[float] = []
            width = 0
            try:
                while self._capturing.is_set() and not self._stop.is_set():
                    history = (history + [float(getattr(hub, "last_level", 0.0) or 0.0)])[-24:]
                    line = render(history, hub.ambient_rms())
                    width = max(width, len(line))
                    print("\r" + line.ljust(width), end="", flush=True)
                    time.sleep(0.06)
            except Exception:  # noqa: BLE001 - the meter must never break listening
                pass
            finally:
                if width:
                    print("\r" + " " * width + "\r", end="", flush=True)
        t = threading.Thread(target=run, name="mic-meter", daemon=True)
        t.start()
        return t

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
                if self._just_my_name(result):
                    # "Veronica" on its own: she's being called. Answer with the cue, keep listening.
                    self._beep()
                    self._status("[yes? listening]")
                    continue
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
            result = self._listen_addressed()
            if (result.error or "").startswith("Not your voice"):
                self._status(f"[STT] {result.error}")      # someone else (TV, a video): stay quiet
                return ""
            if not result.success and not (result.error or "").startswith("No speech detected"):
                # She heard her name, so silence back would look like she's ignoring you.
                self._status(f"[STT] {result.error}")
                self._say("Sorry, I didn't catch that. Say it again?")
                result = self._listen_addressed()
                if not result.success:
                    self._status(f"[STT] {result.error}")
                    return ""
            elif not result.success:
                self._status(f"[STT] {result.error}")
                return ""
            return self._accept(result, open_conversation=False) or ""
        return input("You: ")

    _VOICE_SENTENCES = (
        "Veronica, what's the weather like today?",
        "Open YouTube and play some music.",
        "Remind me to call my mother at seven.",
        "What's the latest news from India?",
        "Thanks Veronica, that's all for now.",
    )

    def _learn_voice(self) -> str:
        """'Learn my voice': record a few sentences, build the owner's voiceprint."""
        from veronica.voice.voiceprint import get_voiceprint
        vp = get_voiceprint(self.config)
        self._say("Sure. I'll learn your voice. After each sentence I read out, say it back to me in your "
                  "normal voice. There are five.")
        clips: list[bytes] = []
        self.stt.voice_check = False            # don't judge the sentences that build the voiceprint
        try:
            for line in self._VOICE_SENTENCES:
                self._say(f"Say: {line}")
                for _attempt in range(2):
                    heard = self._listen(timeout_s=8)
                    pcm = getattr(self.stt, "last_pcm", None) or b""
                    if heard.success and len(pcm) >= 16000 * 2:      # at least a second of you
                        clips.append(pcm)
                        break
                    self._say("I didn't get that one. Once more, please.")
        finally:
            self.stt.voice_check = True
        if len(clips) < 3:
            return "I couldn't get enough clear sentences to learn your voice. Let's try again later."
        bar = vp.enroll(clips)
        self.stt.voiceprint = vp
        logger.info("Voiceprint enrolled (%d clips, bar %.2f).", len(clips), bar)
        return ("Got it. I know your voice now, and I'll only respond to you. If I ever ignore you, "
                "say 'stop checking my voice', or 'learn my voice' to do this again.")

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
            # Speech aimed at her (a command she knows, or her name) gets a lower bar: a TV
            # rarely says "open Doraemon". Seen live: the user at 2.5x the room was ignored.
            if ratio and self._addressed(text, matches):
                ratio = min(ratio, 2.0)
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

    def _names(self) -> list[str]:
        voice = getattr(self.config, "voice", None)
        names = [self.config.assistant.name] + list(getattr(voice, "wake_words", None) or [])
        return [n for n in dict.fromkeys(n.strip() for n in names) if n]

    def _addressed(self, text: str, matches=None) -> bool:
        """Is this said TO her: her name first, or a command she knows?"""
        names = "|".join(re.escape(n) for n in self._names())
        if names and re.match(rf"^\s*(?:hey\s+|ok(?:ay)?\s+)?(?:{names})(?![\w'])", text or "", re.I):
            return True
        return bool(matches and matches(text))

    def _just_my_name(self, result) -> bool:
        """Only her name was heard, and it was really the user (loud against the room)."""
        m = re.search(r"heard '([^']*)'", getattr(result, "error", "") or "")
        if not m:
            return False
        said = re.sub(r"[^a-z]", "", m.group(1).lower())
        if said not in {re.sub(r"[^a-z]", "", n.lower()) for n in self._names()}:
            return False
        stats = getattr(result, "stats", None) or {}
        ambient, speech = stats.get("ambient_rms"), stats.get("speech_rms")
        ratio = min(2.0, float(getattr(self.config.stt, "speech_over_ambient", 2.0) or 2.0))
        return bool(ambient and speech and speech >= ambient * ratio)

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

    def _acknowledge(self, text: str) -> Optional[threading.Thread]:
        """Say "Mm-hm." at once while the model thinks (voice/quick_ack.py). Not for
        commands she runs herself: those answer quickly anyway. Holds the speaking lock,
        so her reply starts right after the clip and never on top of it."""
        ack = getattr(self, "quick_ack", None)
        tools = getattr(getattr(self, "orchestrator", None), "tools", None)
        if ack is None or (tools is not None and tools.find_fast_match(text) is not None):
            return None
        clip = ack.pick()
        if clip is None:
            return None
        started = threading.Event()

        def play() -> None:
            with self._speak_lock:
                started.set()
                hub = getattr(self, "hub", None)
                if hub is not None:
                    hub.self_speaking = True   # her voice is not the room's noise level
                try:
                    from veronica.voice.quick_ack import play as play_ack
                    play_ack(clip)
                except Exception:  # noqa: BLE001 - a missing "Mm-hm." must never stop the reply
                    logger.debug("Quick acknowledgement failed.", exc_info=True)
                finally:
                    if hub is not None:
                        hub.self_speaking = False
        t = threading.Thread(target=play, name="quick-ack-play", daemon=True)
        t.start()
        started.wait(0.2)          # it holds the lock before the reply can take it
        return t

    def _live_speech_on(self) -> bool:
        cfg = getattr(self.config, "streaming", None)
        return (self.voice_mode and bool(getattr(cfg, "enabled", False))
                and bool(getattr(cfg, "speak_while_writing", True))
                and hasattr(self.orchestrator, "handle_stream")
                and getattr(self.speaker, "speak_stream", None) is not None)

    def _answer(self, text: str):
        """(reply, LiveSpeech or None). In voice mode with streaming on, she starts speaking
        the first sentence while the model is still writing the rest; the caller only
        speaks the reply again when the live speech didn't already say it."""
        if not self._live_speech_on():
            return self.orchestrator.handle(text, self.session).text, None
        from veronica.voice.live_speech import LiveSpeech
        live = LiveSpeech()
        speaker: list[threading.Thread] = []

        def start_speaking() -> None:
            t = threading.Thread(target=self._say, args=("",), kwargs={"pieces": live.pieces()},
                                 name="live-speech", daemon=True)
            speaker.append(t)
            t.start()
        live.on_first_piece = start_speaking
        self.orchestrator.stream_event_callback = live.on_event
        try:
            reply = self.orchestrator.handle_stream(text, self.session).text
        finally:
            self.orchestrator.stream_event_callback = None
            live.close()
            for t in speaker:
                t.join()
        return reply, (live if live.started else None)

    def _say(self, text: str, pieces=None) -> None:
        """Print and speak a reply. `pieces` (voice mode): sentences still arriving from a
        reply being written (voice/live_speech.py); spoken as they come."""
        with self._speak_lock:  # replies and reminder announcements never overlap
            speak_i = getattr(self.speaker, "speak_interruptible", None) if self.voice_mode else None
            speak_s = getattr(self.speaker, "speak_stream", None) if pieces is not None else None
            if speak_i is None:
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
                shown: list[str] = []

                def on_piece(piece: str) -> None:
                    # Each sentence appears as she starts saying it, so the text never runs
                    # ahead of her voice.
                    print((f"{self.config.assistant.name}: " if not shown else " ") + piece, end="", flush=True)
                    shown.append(piece)
                    if monitor is not None:
                        monitor.arm_for(piece)
                try:
                    if speak_s is not None:
                        completed = speak_s(pieces, stop, on_piece=on_piece)
                    elif speak_i is not None:
                        completed = speak_i(text, stop, on_piece=on_piece)
                    else:
                        self.speaker.speak(text)
                except Exception as e:
                    logger.warning("TTS playback failed: %s", e)
                finally:
                    if speak_i is not None:
                        print("\n" if shown else (f"{self.config.assistant.name}: {text}\n" if text else ""))
                    try:            # e.g. "my ElevenLabs credits ran out": after her sentence, not inside it
                        from veronica.speech.tts import TextToSpeech
                        for notice in TextToSpeech.take_notices():
                            print(notice + "\n")
                    except Exception:  # noqa: BLE001
                        pass
                    if monitor is not None:
                        monitor.stop()
                    if hub is not None:
                        hub.self_speaking = False
                    self._tts_active = False
                if not completed:
                    self._barged = True
                    self._status("[you talked over me — listening]")
                    self._beep()
                # Stage 3: let the room's echo of her voice die down before listening; the
                # echo transcribed by Whisper was being taken as the user speaking.
                time.sleep(0.35)
