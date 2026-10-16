"""Conversation mode (follow-ups without the wake word) and precise location."""
from __future__ import annotations

import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from veronica.tools.productivity_tools import Profile
from veronica.tools.weather_tools import WeatherTool
from veronica.ui.cli import CLIApp


def cli(heard, follow_up_seconds=8, wake=True):
    app = CLIApp.__new__(CLIApp)
    app.config = SimpleNamespace(assistant=SimpleNamespace(name="VERONICA"),
                                 voice=SimpleNamespace(follow_up_seconds=follow_up_seconds, wake_threshold=0.35))
    app.voice_mode = True
    app._capturing, app._stop, app._speak_lock = threading.Event(), threading.Event(), threading.RLock()
    app._follow_up_until = 0.0
    queue, said, waits = list(heard), [], []
    app.stt = SimpleNamespace(listen_once=lambda timeout_s=6: (
        SimpleNamespace(success=bool(queue[0]), text=queue.pop(0), error="no speech") if queue
        else SimpleNamespace(success=False, text="", error="no speech")))
    app.wake_gate = SimpleNamespace(wait=lambda on_near_miss=None: waits.append(1) or True, reason="") if wake else None
    app._beep = lambda: None
    app._say = said.append
    return app, said, waits


def test_follow_up_needs_no_wake_word():
    app, said, waits = cli(["what's the weather", "and tomorrow?"])
    assert app._get_input() == "what's the weather"   # first turn: wake word
    assert waits == [1]
    app._open_follow_up()
    assert app._get_input() == "and tomorrow?"        # follow-up: straight in
    assert waits == [1]


def test_silence_returns_to_the_wake_word():
    app, said, waits = cli([""])
    app._open_follow_up()
    assert app._get_input() == ""                      # nothing said in the window
    assert app._follow_up_until == 0.0                 # next turn waits for "Veronica" again


@pytest.mark.parametrize("words", ["thanks", "thank you", "okay thanks", "that's all", "never mind", "no", "stop"])
def test_ending_phrases_close_the_conversation(words):
    app, said, _ = cli([words])
    app._open_follow_up()
    assert app._get_input() == "" and said == ["Okay."]


def test_conversation_mode_can_be_turned_off_and_needs_the_wake_word_mode():
    app, _, _ = cli(["x"], follow_up_seconds=0)
    app._open_follow_up()
    assert app._follow_up_until == 0.0
    plain, _, _ = cli(["x"], wake=False)                # plain --voice already listens every turn
    plain._open_follow_up()
    assert plain._follow_up_until == 0.0


# ---------------------------------------------------------------- location

def cfg(**assistant):
    return SimpleNamespace(assistant=SimpleNamespace(**{"home_city": None, "auto_detect_location": True, **assistant}),
                           resolve_path=lambda p: Path(p))


@pytest.fixture(autouse=True)
def in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def test_windows_location_gives_locality_and_exact_coordinates():
    p = Profile(cfg(), detector=lambda: {"name": "Kengeri, Bengaluru", "lat": 12.91, "lon": 77.48,
                                         "via": "Windows location"})
    assert p.city_and_source() == ("Kengeri, Bengaluru", "detected")
    assert p.detected_coords() == (12.91, 77.48)
    assert "from Windows location" in p.detection_notice()


def test_ip_guess_has_no_coordinates_and_voice_city_wins():
    p = Profile(cfg(), detector=lambda: "Bengaluru")
    assert p.detected_coords() is None
    p.set_home_city("Mysuru")
    assert p.city_and_source() == ("Mysuru", "user") and p.detected_coords() is None


def test_weather_uses_exact_coordinates_from_windows_location():
    p = Profile(cfg(), detector=lambda: {"name": "Kengeri, Bengaluru", "lat": 12.91, "lon": 77.48,
                                         "via": "Windows location"})
    get = MagicMock(return_value=SimpleNamespace(json=lambda: {
        "current_weather": {"temperature": 24, "windspeed": 5, "weathercode": 3}}))
    with patch.dict(sys.modules, {"requests": types.SimpleNamespace(get=get, RequestException=Exception)}):
        msg = WeatherTool(profile=p).run().message
    assert msg.startswith("Kengeri, Bengaluru: 24°C, overcast")
    assert get.call_count == 1                          # no city-name geocoding step
    assert get.call_args.kwargs["params"]["latitude"] == 12.91


def test_named_city_still_geocodes_by_name():
    p = Profile(cfg(), detector=lambda: {"name": "Kengeri, Bengaluru", "lat": 12.91, "lon": 77.48, "via": "x"})
    get = MagicMock(side_effect=[
        SimpleNamespace(json=lambda: {"results": [{"latitude": 28.6, "longitude": 77.2, "name": "Delhi"}]}),
        SimpleNamespace(json=lambda: {"current_weather": {"temperature": 30, "windspeed": 4, "weathercode": 0}})])
    with patch.dict(sys.modules, {"requests": types.SimpleNamespace(get=get, RequestException=Exception)}):
        assert WeatherTool(profile=p).run(city="Delhi").message.startswith("Delhi: 30°C")


# ---------------------------------------------------------------- room level handoff

import numpy as np


def _frame(level, n):
    return (np.ones(n, dtype=np.int16) * int(level)).tobytes()


def test_wake_gate_measures_the_room_before_the_wake_word():
    from veronica.config import Config
    from veronica.voice.wake_detector import MockWakeDetector
    from veronica.voice.wake_gate import WakeGate

    frames = [_frame(0, 1280)] * 7 + [_frame(300, 1280)] * 30 + [_frame(3000, 1280)] * 9  # warm-up, room, "Veronica"
    class Mic:
        def open(self): return True
        def read(self): return frames.pop(0) if frames else b""
        def close(self): pass
    class FireAtEnd(MockWakeDetector):
        def detect(self, f): return not frames
    g = WakeGate(Config(), mic_factory=Mic, detector=FireAtEnd())
    assert g.wait(timeout_s=5)
    assert g.ambient_rms == 300.0          # the wake word itself didn't inflate it


def test_stt_uses_the_handed_over_room_level_instead_of_measuring_the_user(monkeypatch):
    import veronica.voice.microphone as micmod
    from veronica.config import STTConfig
    from veronica.speech.stt import SpeechToText

    # The user starts talking right after the beep: what follows the warm-up is speech (2500).
    stream = [_frame(0, 1024)] * 7 + [_frame(2500, 1024)] * 20 + [_frame(50, 1024)] * 40
    class Mic:
        device_index, _backend = 2, "sounddevice"
        def __init__(self, *a, **k): pass
        def open(self): return True
        def read(self): return stream.pop(0) if stream else b""
        def close(self): pass
    monkeypatch.setattr(micmod, "MicrophoneInput", Mic)
    monkeypatch.setattr(micmod, "resolve_input_device", lambda name, index: 2)
    stt = SpeechToText(STTConfig(backend="google_free", min_speech_rms=40))
    stt.known_ambient_rms = 800.0           # measured by the wake gate before "Veronica"
    pcm, err = stt._capture_utterance(timeout_s=2)
    assert err == "" and pcm
    assert stt.last_capture_stats["threshold"] == 2000.0   # 2.5 x room, not 2.5 x the user
