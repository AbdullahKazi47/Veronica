"""The guided voice test: room level, how she hears you, talk-over — no hardware."""
from __future__ import annotations

from veronica.speech.stt import STTResult
from veronica.voice.voice_test import (
    VoiceTestReport, judge_voice, persist_number, room_label, run_voice_test, summarize,
)


def _heard(text="Veronica what's the weather like today", conf=0.9, speech=600.0, ambient=100.0):
    return STTResult(success=True, text=text, confidence=conf, stats={"speech_rms": speech, "ambient_rms": ambient})


def test_room_labels():
    assert room_label(40) == "quiet" and room_label(150) == "a bit noisy" and room_label(900) == "noisy"


def test_full_run_all_good():
    said = []
    report = run_voice_test(say=said.append, listen=lambda: _heard(speech=600),
                            measure_room=lambda s: 100.0,
                            speak_interruptible=lambda text: (False, 2.4), needed_ratio=3.0)
    assert report.ratio == 6.0 and report.interrupted and report.interrupt_after_s == 2.4
    assert not report.suggested_ratio
    assert "Talk-over worked" in summarize(report) and "Everything looks good" in summarize(report)
    assert any("stay quiet" in s for s in said)


def test_quiet_voice_gets_a_lower_bar_offered():
    report = run_voice_test(say=lambda t: None, listen=lambda: _heard(speech=220), measure_room=lambda s: 100.0,
                            speak_interruptible=None, needed_ratio=3.0)
    assert report.ratio == 2.2 and report.suggested_ratio == 1.7
    assert "lower it to 1.7" in summarize(report)


def test_bar_never_goes_below_a_floor():
    r = VoiceTestReport(room_rms=100.0, speech_rms=120.0, needed_ratio=3.0)
    judge_voice(r)
    assert r.suggested_ratio == 1.5


def test_not_heard_and_no_talk_over():
    report = run_voice_test(say=lambda t: None, listen=lambda: STTResult(success=False, error="none"),
                            measure_room=lambda s: 50.0, speak_interruptible=lambda t: (True, 9.0))
    s = summarize(report)
    assert "didn't hear you" in s and "didn't stop me" in s


def test_persist_number_keeps_comment(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("voice:\n  conversation_speech_ratio: 3.0       # louder than the room\n", encoding="utf-8")
    assert persist_number(p, "conversation_speech_ratio", 1.6)
    assert p.read_text() == "voice:\n  conversation_speech_ratio: 1.6       # louder than the room\n"
    assert not persist_number(p, "no_such_key", 1)


def test_tool_routing_and_text_mode():
    from veronica.safety.permissions import PermissionGate
    from veronica.tools.registry import build_default_registry
    from veronica.tools.voice_test_tools import VoiceTestTool, set_voice_tester
    reg = build_default_registry(PermissionGate())
    for t in ("run a voice test", "test your microphone", "voice check", "can you hear me properly"):
        assert reg.find_fast_match(t).name == "voice_test", t
    set_voice_tester(None)
    assert not VoiceTestTool().run().success                     # text mode: honest refusal
    assert VoiceTestTool(tester=lambda: "Room: quiet.").run().message == "Room: quiet."
