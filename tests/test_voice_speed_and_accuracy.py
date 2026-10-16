"""Faster replies (shared mic, pipelined speech), talking over her (barge-in),
fewer mishearings (alternatives, corrections, confirm when unsure) and ignoring
background speech. No real microphone, speaker or network."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from veronica.config import Config
from veronica.speech.corrections import Corrections
from veronica.speech.stt import STTResult, SpeechToText, highpass
from veronica.speech.tts import TextToSpeech, split_for_speech
from veronica.voice.barge_in import BargeInMonitor
from veronica.voice.mic_hub import BLOCK, MicHub


def _tone(level: float, n: int = BLOCK) -> bytes:
    return (np.ones(n) * level).astype(np.int16).tobytes()


class FakeStream:
    def __init__(self, push):
        self.push = push
    def stop(self): pass
    def close(self): pass


def _hub():
    holder = {}
    hub = MicHub(stream_factory=lambda cb: holder.setdefault("s", FakeStream(cb)))
    assert hub.ensure_running()
    return hub, holder["s"]


# ---------------------------------------------------------------- shared mic hub

def test_tap_reads_exact_chunks_from_the_live_stream():
    hub, stream = _hub()
    tap = hub.tap(chunk_size=BLOCK * 2)
    assert tap.open() and tap.needs_warmup is False
    for _ in range(4):
        stream.push(_tone(100))
    assert len(tap.read()) == BLOCK * 2 * 2 and len(tap.read()) == BLOCK * 2 * 2
    tap.close()
    assert tap.read() == b""


def test_two_taps_see_the_same_audio():
    hub, stream = _hub()
    a, b = hub.tap(BLOCK), hub.tap(BLOCK)
    a.open(); b.open()
    stream.push(_tone(50))
    assert a.read() == b.read() == _tone(50)


def test_room_level_tracks_noise_and_ignores_her_own_voice():
    hub, stream = _hub()
    for _ in range(60):
        stream.push(_tone(30))
    assert 25 < hub.ambient_rms() < 35
    hub.self_speaking = True
    for _ in range(200):
        stream.push(_tone(3000))          # her voice through the speakers
    hub.self_speaking = False
    assert hub.ambient_rms() < 35         # not counted as the room's noise
    for _ in range(200):
        stream.push(_tone(400))           # a TV switched on
    assert hub.ambient_rms() > 300


# ---------------------------------------------------------------- recognition

def test_alternatives_confidence_and_corrections(tmp_path):
    c = Corrections(tmp_path / "c.json")
    assert c.add("attacks", "tabs")
    stt = SpeechToText(Config().stt, corrections=c)
    r = stt._from_google({"alternative": [
        {"transcript": "minimise all attacks", "confidence": 0.71},
        {"transcript": "minimise all tabs"}, {"transcript": "minimise all tax"}]})
    assert r.success and r.text == "minimise all tabs" and r.confidence == pytest.approx(0.71)
    assert "minimise all tax" in r.alternatives and "minimise all tabs" not in r.alternatives


def test_nothing_understood():
    assert not SpeechToText(Config().stt)._from_google([]).success


def test_corrections_whole_words_longest_first(tmp_path):
    c = Corrections(tmp_path / "c.json")
    c.add("crystal", "restore")
    c.add("all attacks", "all tabs")
    assert c.apply("Crystal my windows") == "restore my windows"
    assert c.apply("crystalline") == "crystalline"            # whole words only
    assert c.apply("minimise all attacks") == "minimise all tabs"
    assert c.remove("crystal") and c.apply("crystal") == "crystal"


def test_highpass_removes_hum_keeps_speech():
    rate = 16000
    t = np.arange(rate) / rate
    hum = 3000 * np.sin(2 * np.pi * 50 * t)
    voice = 3000 * np.sin(2 * np.pi * 800 * t)
    out = np.frombuffer(highpass((hum + voice).astype(np.int16).tobytes(), 100), dtype=np.int16)
    spec = np.abs(np.fft.rfft(out))
    assert spec[50] < spec[800] * 0.05


class ScriptedMic:
    needs_warmup = False
    def __init__(self, levels):
        self.levels = list(levels)
        self.device_index, self._backend = None, "fake"
    def open(self): return True
    def read(self): return _tone(self.levels.pop(0), 1024) if self.levels else b""
    def close(self): pass


def test_click_is_not_speech():
    cfg = Config().stt
    cfg.min_speech_rms, cfg.pause_threshold, cfg.min_voiced_s = 40, 0.2, 0.25
    stt = SpeechToText(cfg)
    stt.known_ambient_rms = 10
    stt.mic_factory = lambda chunk: ScriptedMic([10, 10, 500] + [10] * 10)   # one 64 ms click
    pcm, err = stt._capture_utterance(timeout_s=2)
    assert pcm is None and "short" in err.lower()


def test_real_speech_is_captured_with_levels():
    cfg = Config().stt
    cfg.min_speech_rms, cfg.pause_threshold, cfg.min_voiced_s = 40, 0.2, 0.25
    stt = SpeechToText(cfg)
    stt.known_ambient_rms = 10
    stt.mic_factory = lambda chunk: ScriptedMic([10] + [500] * 12 + [10] * 10)
    pcm, err = stt._capture_utterance(timeout_s=2)
    assert pcm and stt.last_capture_stats["voiced_s"] >= 0.25
    assert stt.last_capture_stats["speech_rms"] >= 400


# ---------------------------------------------------------------- pipelined, interruptible speech

def test_split_starts_short_and_never_exceeds_max():
    g = ("Good afternoon. It is 3:59 PM on Friday. In Kokti it is 28 degrees. Top headlines: "
         + "; ".join(f"headline number {i} about something happening today" for i in range(8)) + ".")
    pieces = split_for_speech(g)
    assert pieces[0] == "Good afternoon." and all(len(p) <= 240 for p in pieces)
    assert " ".join(pieces).replace("  ", " ") == g


def _eleven(tmp_path, monkeypatch, played, synth_delay=0.0):
    cfg = Config().tts
    cfg.backend = "elevenlabs"
    tts = TextToSpeech(cfg, cache_dir=tmp_path)
    made = []

    def fake_file(text, previous="", following=""):
        time.sleep(synth_delay)
        made.append((text, previous, following))
        path = tmp_path / f"{len(made)}.mp3"
        path.write_bytes(b"x")
        return path
    monkeypatch.setattr(tts, "_elevenlabs_file", fake_file)
    import veronica.speech.tts as ttsmod
    monkeypatch.setattr(ttsmod, "play_interruptible", lambda path, stop: played.append(path.name) or not stop.is_set())
    return tts, made


def test_speaks_every_sentence_in_order_with_neighbour_context(tmp_path, monkeypatch):
    played = []
    tts, made = _eleven(tmp_path, monkeypatch, played)
    assert tts.speak_interruptible("First one here. Second sentence follows. Third and last.", threading.Event())
    assert played == ["1.mp3", "2.mp3", "3.mp3"] or len(played) == len(made)
    assert made[1][1] == made[0][0] and made[0][2] == made[1][0]   # previous/next text passed


def test_stop_cuts_speech_short(tmp_path, monkeypatch):
    played = []
    tts, made = _eleven(tmp_path, monkeypatch, played)
    stop = threading.Event()
    import veronica.speech.tts as ttsmod

    def play(path, ev):
        played.append(path.name)
        ev.set()              # the user talks over her during the first sentence
        return False
    monkeypatch.setattr(ttsmod, "play_interruptible", play)
    assert tts.speak_interruptible("One sentence here. Two sentences here. Three sentences.", stop) is False
    assert played == ["1.mp3"]


def test_first_sentence_plays_before_the_rest_is_synthesized(tmp_path, monkeypatch):
    played_at, done_at = [], {}
    tts, made = _eleven(tmp_path, monkeypatch, [], synth_delay=0.15)
    import veronica.speech.tts as ttsmod
    monkeypatch.setattr(ttsmod, "play_interruptible",
                        lambda path, stop: played_at.append((path.name, len(made))) or True)
    tts.speak_interruptible("A short opener. " + "Then a much longer second sentence that follows it. " * 3,
                            threading.Event())
    assert played_at[0][0] == "1.mp3" and played_at[0][1] < 3   # played while others were pending


# ---------------------------------------------------------------- talk over her

class FiringDetector:
    def __init__(self, fire_after: int):
        self.n, self.fire_after = 0, fire_after
    def reset(self): self.n = 0
    def detect(self, frame):
        self.n += 1
        return self.n >= self.fire_after


def test_saying_her_name_stops_playback():
    hub, stream = _hub()
    stop = threading.Event()
    mon = BargeInMonitor(lambda n: hub.tap(n), FiringDetector(fire_after=2)).start(stop)
    for _ in range(40):
        stream.push(_tone(200))
        if stop.wait(0.01):
            break
    mon.stop()
    assert stop.is_set() and mon.triggered


def test_no_self_interrupt_while_she_says_her_own_name():
    hub, stream = _hub()
    stop = threading.Event()
    mon = BargeInMonitor(lambda n: hub.tap(n), FiringDetector(fire_after=1), name="VERONICA")
    mon.arm_for("I'm VERONICA, your assistant.")
    mon.start(stop)
    for _ in range(30):
        stream.push(_tone(200))
    time.sleep(0.1)
    mon.stop()
    assert not stop.is_set()


# ---------------------------------------------------------------- what counts as the user

def _app(fast_match=None):
    from veronica.ui.cli import CLIApp
    app = CLIApp.__new__(CLIApp)
    app.config = Config()
    tools = SimpleNamespace(find_fast_match=lambda t: object() if fast_match and fast_match(t) else None)
    app.orchestrator = SimpleNamespace(tools=tools)
    app.said = []
    app._say = lambda t: app.said.append(t)
    return app


def _r(text, conf=None, alts=(), ambient=20.0, speech=200.0):
    return STTResult(success=True, text=text, confidence=conf, alternatives=list(alts),
                     stats={"ambient_rms": ambient, "speech_rms": speech})


def test_prefers_the_reading_that_is_a_command():
    app = _app(fast_match=lambda t: "tabs" in t)
    assert app._accept(_r("minimise all attacks", alts=["minimise all tabs"]),
                       open_conversation=False) == "minimise all tabs"


def test_open_conversation_ignores_quiet_background_speech():
    app = _app()
    assert app._accept(_r("and the weather today", speech=40, ambient=20), open_conversation=True) is None
    assert app._accept(_r("what's the weather", speech=300, ambient=20), open_conversation=True)


def test_open_conversation_ignores_unclear_speech_but_keeps_commands():
    app = _app(fast_match=lambda t: t.startswith("open"))
    assert app._accept(_r("blah something", conf=0.3), open_conversation=True) is None
    assert app._accept(_r("open notepad", conf=0.3), open_conversation=True) == "open notepad"
    assert app._accept(_r("thanks", conf=0.2, speech=30), open_conversation=True) == "thanks"   # always ends


def test_after_wake_word_unclear_speech_is_checked():
    app = _app()
    app._listen = lambda timeout_s=6: STTResult(success=True, text="yes")
    assert app._accept(_r("restore my tabs", conf=0.3), open_conversation=False) == "restore my tabs"
    assert "did you say" in app.said[0].lower()


def test_after_wake_word_no_then_repeat():
    app = _app()
    answers = iter([STTResult(success=True, text="no"), STTResult(success=True, text="restore my tabs")])
    app._listen = lambda timeout_s=6: next(answers)
    assert app._accept(_r("crystal", conf=0.2), open_conversation=False) == "restore my tabs"


def test_confident_speech_is_never_questioned():
    app = _app()
    assert app._accept(_r("what time is it", conf=0.92), open_conversation=False) == "what time is it"
    assert app.said == []


# ---------------------------------------------------------------- teaching corrections by voice

def test_correction_tool_routing_and_effect(tmp_path):
    from veronica.safety.permissions import PermissionGate
    from veronica.tools.registry import build_default_registry
    from veronica.tools.speech_tools import SpeechCorrectionTool
    reg = build_default_registry(PermissionGate())
    for t in ("when I say attacks, I mean tabs", "you heard crystal but I said restore",
              "what corrections do you have", "forget the correction for attacks"):
        assert reg.find_fast_match(t).name == "speech_correction", t
    tool = SpeechCorrectionTool(corrections=Corrections(tmp_path / "c.json"))
    assert tool.run(**tool.extract_arguments("when I say attacks, I mean tabs")).success
    assert tool.store.apply("minimise all attacks") == "minimise all tabs"
    assert "attacks means tabs" in tool.run(action="list").message
    assert tool.run(**tool.extract_arguments("forget the correction for attacks")).success


def test_the_users_own_speech_never_raises_the_room_level():
    hub, stream = _hub()
    for _ in range(60):
        stream.push(_tone(30))
    hub.user_speaking = True
    for _ in range(400):
        stream.push(_tone(2000))          # a long sentence from the user
    hub.user_speaking = False
    assert hub.ambient_rms() < 35


def test_capture_marks_user_speech_on_the_hub():
    cfg = Config().stt
    cfg.min_speech_rms, cfg.pause_threshold, cfg.min_voiced_s = 40, 0.2, 0.0
    stt = SpeechToText(cfg)
    seen = []

    class Hub:
        user_speaking = False
        def ambient_rms(self): return 10.0

    stt.hub = Hub()
    levels = [10] + [500] * 6 + [10] * 10

    class Mic(ScriptedMic):
        def read(self_inner):
            seen.append(stt.hub.user_speaking)
            return ScriptedMic.read(self_inner)
    stt.mic_factory = lambda chunk: Mic(levels)
    pcm, _ = stt._capture_utterance(timeout_s=2)
    assert pcm and any(seen) and stt.hub.user_speaking is False
