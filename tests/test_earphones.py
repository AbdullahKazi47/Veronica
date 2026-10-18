"""Earphones mode: a quiet "Veronica" through the earphone mic, in public."""
import numpy as np

from veronica.config import Config
from veronica.voice import earphones, microphone

APIS = [{"name": "MME"}]
LAPTOP = {"name": "Microphone Array (Realtek)", "max_input_channels": 2, "hostapi": 0}
JACK = {"name": "Jack Mic (Realtek)", "max_input_channels": 2, "hostapi": 0}
BT = {"name": "Headset (WH-1000XM4)", "max_input_channels": 1, "hostapi": 0}
OUT = {"name": "Headphones (Realtek)", "max_input_channels": 0, "hostapi": 0}
NAMES = Config().voice.earphones.mic_names


def test_bluetooth_headset_is_found_even_when_not_default():
    assert earphones.find_mic(NAMES, [LAPTOP, BT, OUT], APIS, default_input=0) == "Headset (WH-1000XM4)"


def test_jack_mic_only_when_windows_made_it_the_default():
    assert earphones.find_mic(NAMES, [LAPTOP, JACK, OUT], APIS, default_input=1) == "Jack Mic (Realtek)"
    assert earphones.find_mic(NAMES, [LAPTOP, JACK, OUT], APIS, default_input=0) is None


def test_apply_switches_mic_and_lowers_the_bars():
    cfg = Config()
    try:
        note = earphones.apply(cfg, finder=lambda names: "Headset (WH-1000XM4)")
        assert "quietly" in note
        assert cfg.stt.mic_device_name == "Headset (WH-1000XM4)"
        assert cfg.voice.wake_threshold <= 0.28
        assert cfg.stt.speech_over_ambient == 1.8
        assert microphone._gain == 3.0
    finally:
        microphone.set_input_gain(1.0)


def test_auto_without_earphones_changes_nothing():
    cfg = Config()
    before = (cfg.stt.mic_device_name, cfg.voice.wake_threshold, cfg.stt.min_speech_rms)
    assert earphones.apply(cfg, finder=lambda names: None) is None
    assert (cfg.stt.mic_device_name, cfg.voice.wake_threshold, cfg.stt.min_speech_rms) == before
    assert microphone._gain == 1.0


def test_off_never_switches():
    cfg = Config()
    cfg.voice.earphones.mode = "off"
    assert earphones.apply(cfg, finder=lambda names: "Headset") is None


def test_boost_makes_quiet_speech_louder_but_never_clips():
    microphone.set_input_gain(3.0)
    try:
        quiet = (np.sin(np.linspace(0, 20, 320)) * 1000).astype(np.int16)
        loud = (np.sin(np.linspace(0, 20, 320)) * 20000).astype(np.int16)
        q = np.frombuffer(microphone.boost(quiet.tobytes()), dtype=np.int16)
        l = np.frombuffer(microphone.boost(loud.tobytes()), dtype=np.int16)
        assert abs(int(q.max()) - 3 * int(quiet.max())) <= 3
        assert int(np.abs(l.astype(np.int32)).max()) <= 24000
    finally:
        microphone.set_input_gain(1.0)
