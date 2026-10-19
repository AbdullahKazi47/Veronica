"""'Only listen to me': learning the owner's voice and dropping other voices before STT.
The embedder is stubbed: a clip's first byte picks its 'voice'."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from veronica.voice.voiceprint import FLOOR, Voiceprint

ONE_SECOND = 16000 * 2


def clip(voice: int, seconds: float = 1.5) -> bytes:
    return bytes([voice]) * int(ONE_SECOND * seconds)


VOICES = {1: [1.0, 0.1, 0.0], 2: [0.95, 0.2, 0.05], 3: [0.0, 1.0, 0.2], 4: [0.1, 0.0, 1.0]}


def stub_embed(pcm: bytes):
    return VOICES[pcm[0]]


@pytest.fixture
def vp(tmp_path):
    return Voiceprint(tmp_path / "voiceprint.json", tmp_path / "model.onnx", embedder=stub_embed)


def test_enrol_then_only_the_owner_passes(vp):
    bar = vp.enroll([clip(1), clip(2), clip(1), clip(2), clip(1)])
    assert FLOOR <= bar <= 0.65 and vp.active
    assert vp.check(clip(1))[0] and vp.check(clip(2))[0]          # the owner, two ways of speaking
    ok, score = vp.check(clip(3))
    assert not ok and score < bar                                   # someone else
    assert not vp.check(clip(4))[0]


def test_short_clips_and_no_voiceprint_are_not_judged(vp):
    assert vp.check(clip(3)) == (True, None)                       # nothing learned yet
    vp.enroll([clip(1)] * 4)
    assert vp.check(clip(3, seconds=0.5)) == (True, None)          # too short to tell


def test_needs_three_clear_sentences(vp):
    with pytest.raises(ValueError):
        vp.enroll([clip(1), clip(1, seconds=0.3)])


def test_off_on_forget_and_persisted(vp, tmp_path):
    vp.enroll([clip(1)] * 4)
    again = Voiceprint(tmp_path / "voiceprint.json", tmp_path / "model.onnx", embedder=stub_embed)
    assert again.active
    again.set_enabled(False)
    assert again.check(clip(3)) == (True, None)
    assert again.forget() and not again.enrolled and not (tmp_path / "voiceprint.json").exists()


def test_a_broken_check_never_deafens_her(tmp_path):
    def boom(pcm):
        raise RuntimeError("model gone")
    vp = Voiceprint(tmp_path / "v.json", tmp_path / "m.onnx", embedder=stub_embed)
    vp.enroll([clip(1)] * 4)
    vp._embedder = boom
    assert vp.check(clip(3)) == (True, None)


def test_stt_drops_other_voices_before_transcribing(vp):
    from veronica.speech.stt import SpeechToText
    vp.enroll([clip(1)] * 4)
    stt = SpeechToText.__new__(SpeechToText)
    stt.config = SimpleNamespace(highpass_hz=0)
    stt.last_capture_stats = {}
    stt._capture_utterance = lambda timeout_s: (clip(3), None)
    stt.voiceprint = vp
    transcribed = []
    stt._engines = lambda: [("offline", lambda pcm: transcribed.append(pcm))]
    r = stt._listen_chain(6)
    assert not r.success and r.error.startswith("Not your voice") and transcribed == []
    assert "voice_match" in r.stats


def test_voiceprint_tool_phrases(vp):
    from veronica.tools.voiceprint_tools import VoiceprintTool
    tool = VoiceprintTool(voiceprint=vp, enroller=lambda: "learned")
    for said, action in [("learn my voice", "learn"), ("only listen to me", "learn"),
                         ("stop checking my voice", "off"), ("start checking my voice", "on"),
                         ("forget my voice", "forget"), ("is voice check on", "status")]:
        assert tool.matches(said), said
        assert tool.extract_arguments(said)["action"] == action, said
    assert not tool.matches("what's the weather")
    assert tool.run(action="learn").message == "learned"
    assert "haven't learned" in tool.run(action="off").message
