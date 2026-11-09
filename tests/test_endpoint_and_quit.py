"""She stops listening when you finish, and "go to sleep" closes her (fakes only)."""
from __future__ import annotations

import numpy as np
import pytest

from veronica.config import Config
from veronica.speech.stt import SpeechToText
from veronica.ui.cli import quits_veronica

CHUNK_S = 1024 / 16000          # one fake mic read


def _tone(level: float) -> bytes:
    return (np.ones(1024) * level).astype(np.int16).tobytes()


class ScriptedMic:
    needs_warmup = False

    def __init__(self, levels):
        self.levels = list(levels)
        self.device_index, self._backend = None, "fake"

    def open(self): return True
    def read(self): return _tone(self.levels.pop(0)) if self.levels else b""
    def close(self): pass


def _earphones_stt(levels, room=62.8):
    """The case from the real log: earphones bar (1.8x room), room 62.8, an 8 s cap."""
    cfg = Config().stt
    cfg.min_speech_rms, cfg.speech_over_ambient = 25, 1.8
    cfg.pause_threshold, cfg.max_utterance_s, cfg.min_voiced_s = 0.65, 8.0, 0.25
    stt = SpeechToText(cfg)
    stt.known_ambient_rms = room
    stt.mic_factory = lambda chunk: ScriptedMic(levels)
    return stt


def _secs(n: float) -> int:
    return int(round(n / CHUNK_S))


def test_small_room_noise_after_you_finish_ends_the_sentence():
    # 2 s of speech, then 8 s of a fan/room noise only a little above the room level.
    # Before: anything over 0.6x the bar (about 1.08x the room) counted as speech, so she
    # recorded to the 8 s cap and you had to mute the mic.
    stt = _earphones_stt([60] + [600] * _secs(2) + [75] * _secs(8))
    pcm, err = stt._capture_utterance(timeout_s=3)
    assert pcm, err
    assert stt.last_capture_stats["duration_s"] < 3.2


def test_noise_just_under_the_speech_bar_still_ends_it():
    # Something that hovers above "silence" but never reaches speech level (a TV, a fan close by)
    # ends the sentence once nothing speech-level has been heard for 1.5 s.
    stt = _earphones_stt([60] + [600] * _secs(2) + [105] * _secs(8))
    pcm, err = stt._capture_utterance(timeout_s=3)
    assert pcm, err
    assert stt.last_capture_stats["duration_s"] < 4.0
    assert stt.last_capture_stats.get("ended_by") == "no speech-level sound"


def test_a_short_breath_mid_sentence_does_not_cut_you_off():
    levels = [60] + [600] * _secs(1.5) + [62] * _secs(0.3) + [600] * _secs(1.5) + [62] * _secs(2)
    stt = _earphones_stt(levels)
    pcm, err = stt._capture_utterance(timeout_s=3)
    assert pcm, err
    assert stt.last_capture_stats["duration_s"] >= 3.2      # both halves were kept


def test_long_speech_still_stops_at_the_cap():
    stt = _earphones_stt([60] + [600] * _secs(12))
    pcm, _ = stt._capture_utterance(timeout_s=3)
    assert pcm and stt.last_capture_stats["duration_s"] <= 8.5


@pytest.mark.parametrize("text", [
    "go to sleep", "Go to sleep.", "you can shut down now", "You can go to sleep now.",
    "shut down", "Veronica, go to sleep", "okay you can go to sleep for now",
    "shut yourself down", "turn yourself off", "close yourself", "quit", "exit veronica",
    "That's all for today, Veronica. You can go to sleep now.", "go offline",
])
def test_these_close_veronica(text):
    assert quits_veronica(text)


@pytest.mark.parametrize("text", [
    "shut down my pc", "shut down the computer", "put the pc to sleep", "I can't go to sleep",
    "remind me to sleep at 11", "thanks", "bye", "good night", "close chrome",
    "how do I go to sleep faster", "sleep timer for 10 minutes", "exit full screen",
])
def test_these_do_not_close_veronica(text):
    assert not quits_veronica(text)
