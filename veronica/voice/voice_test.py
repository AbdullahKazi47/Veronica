"""
"Run a voice test": a one-minute guided check of the real microphone and speaker.

  1. room   — stay quiet; measure the background level (fan, TV, people)
  2. voice  — say a test sentence; report what was heard, how sure the recognizer was,
              and how much louder you are than the room. In an open conversation VERONICA
              ignores speech that isn't `conversation_speech_ratio` times louder than the
              room — if you only just clear that bar, she offers to lower it.
  3. talk-over — she reads a long sentence; say "Veronica" to stop her; report whether
              (and how fast) she stopped.

The steps are plain functions over injected say/listen/measure callables, so they can be
tested without hardware. Changing a setting is only ever offered, and asked.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

TEST_SENTENCE = "Veronica, what's the weather like today?"
LONG_SENTENCE = ("This is a test of talking over me. I'll keep going for a little while, reading this long "
                 "sentence slowly and clearly, so you have plenty of time to say my name. Whenever you're "
                 "ready, just say Veronica, and I should stop right away and listen to you instead of "
                 "finishing this sentence.")


@dataclass
class VoiceTestReport:
    room_rms: Optional[float] = None
    room_label: str = ""
    heard: str = ""
    confidence: Optional[float] = None
    speech_rms: Optional[float] = None
    ratio: Optional[float] = None
    needed_ratio: float = 3.0
    interrupted: Optional[bool] = None
    interrupt_after_s: Optional[float] = None
    suggestions: list[str] = field(default_factory=list)
    suggested_ratio: Optional[float] = None


def room_label(rms: Optional[float]) -> str:
    if rms is None:
        return "unknown"
    if rms < 80:
        return "quiet"
    if rms < 300:
        return "a bit noisy"
    return "noisy"


def judge_voice(report: VoiceTestReport) -> None:
    """Fill in the ratio and suggestions from the measured levels."""
    if report.speech_rms and report.room_rms:
        report.ratio = round(report.speech_rms / max(report.room_rms, 1.0), 1)
        if report.ratio < report.needed_ratio:
            # Comfortably below what this user produces, but never so low that a TV passes.
            report.suggested_ratio = max(1.5, round(report.ratio * 0.75, 1))
            report.suggestions.append(
                f"Your voice was {report.ratio} times the room level, under the {report.needed_ratio} I need in "
                f"an open conversation, so I might ignore you there. I can lower it to {report.suggested_ratio}.")
        elif report.ratio < report.needed_ratio * 1.3:
            report.suggestions.append("You only just cleared the bar; speaking a little closer to the laptop helps.")
    if report.confidence is not None and report.confidence < 0.6:
        report.suggestions.append("I wasn't very sure of what I heard; a quieter room or speaking closer helps, "
                                  "and you can teach me words I get wrong: 'when I say X, I mean Y'.")


def run_voice_test(*, say: Callable[[str], None], listen: Callable[[], object],
                   measure_room: Callable[[float], Optional[float]],
                   speak_interruptible: Optional[Callable[[str], tuple[bool, float]]],
                   needed_ratio: float = 3.0, room_seconds: float = 3.0) -> VoiceTestReport:
    """Run the three steps. `listen()` returns an STTResult-like object (text, confidence,
    stats). `speak_interruptible(text)` returns (completed, seconds_spoken)."""
    report = VoiceTestReport(needed_ratio=needed_ratio)

    say("Voice test. First, please stay quiet for a few seconds while I listen to the room.")
    report.room_rms = measure_room(room_seconds)
    report.room_label = room_label(report.room_rms)
    level = f" (level {report.room_rms:.0f})" if report.room_rms is not None else ""
    say(f"The room is {report.room_label}{level}. Now say: {TEST_SENTENCE}")

    heard = listen()
    if getattr(heard, "success", False):
        report.heard = (getattr(heard, "text", "") or "").strip()
        report.confidence = getattr(heard, "confidence", None)
        stats = getattr(heard, "stats", None) or {}
        report.speech_rms = stats.get("speech_rms")
        if stats.get("ambient_rms") and not report.room_rms:
            report.room_rms = stats["ambient_rms"]
    judge_voice(report)
    if report.heard:
        say(f"I heard: {report.heard}." + (f" You were {report.ratio} times louder than the room." if report.ratio else ""))
    else:
        report.suggestions.append("I didn't hear you at all; check the microphone isn't muted and is the one "
                                  "set in stt.mic_device_name.")
        say("I didn't catch anything.")

    if speak_interruptible is not None:
        say("Last test. I'll read a long sentence. Say Veronica to stop me.")
        completed, spoken_s = speak_interruptible(LONG_SENTENCE)
        report.interrupted = not completed
        report.interrupt_after_s = round(spoken_s, 1) if not completed else None
        if completed:
            report.suggestions.append("You didn't stop me. Say 'Veronica' clearly while I talk; if it still doesn't "
                                      "work, the wake word may need more training recordings.")
    return report


def summarize(report: VoiceTestReport) -> str:
    parts = [f"Room: {report.room_label}."]
    if report.heard:
        sure = f", {round(report.confidence * 100)} percent sure" if report.confidence is not None else ""
        parts.append(f"Heard you as \"{report.heard}\"{sure}.")
    if report.interrupted:
        parts.append(f"Talk-over worked; I stopped after {report.interrupt_after_s} seconds.")
    elif report.interrupted is False:
        parts.append("Talk-over didn't trigger.")
    parts += report.suggestions or ["Everything looks good."]
    return " ".join(parts)


def persist_number(config_path, key: str, value: float) -> bool:
    """Write `key: value` into config.yaml, keeping the line's comment. Only called after
    the user said yes. Returns False if the key isn't in the file."""
    import re
    from pathlib import Path
    path = Path(config_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    pattern = re.compile(rf"^(\s*{re.escape(key)}:\s*)([-+0-9.]+)", re.M)
    if not pattern.search(text):
        return False
    path.write_text(pattern.sub(lambda m: f"{m.group(1)}{value}", text, count=1), encoding="utf-8")
    return True
