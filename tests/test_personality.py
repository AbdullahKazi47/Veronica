"""Her character in the instant answers, her own blended voice, Alice back as the main
voice with the blend as backup, and the live check kept out of her real memory."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from veronica.core import flair


@pytest.mark.parametrize("when,said", [
    (dt.datetime(2026, 10, 17, 18, 42), "It's 6:42 in the evening, Saturday the 17th."),
    (dt.datetime(2026, 10, 17, 9, 0), "It's 9 o'clock in the morning, Saturday the 17th."),
    (dt.datetime(2026, 10, 17, 12, 0), "It's noon, Saturday the 17th."),
    (dt.datetime(2026, 10, 1, 0, 5), "It's 12:05 at night, Thursday the 1st."),
    (dt.datetime(2026, 10, 13, 14, 30), "It's 2:30 in the afternoon, Tuesday the 13th."),
])
def test_time_said_like_a_person(when, said):
    assert flair.spoken_time(when) == said


def test_dress_only_touches_short_successful_answers(monkeypatch):
    monkeypatch.setattr(flair._rng, "random", lambda: 0.0)          # always add the touch
    assert flair.dress("Opening Spotify.") == "Opening Spotify, boss."
    assert flair.dress("Couldn't find Spotify.", ok=False) == "Couldn't find Spotify."
    assert flair.dress("Which one, the first or the second?") == "Which one, the first or the second?"
    assert flair.dress("Right away, boss.") == "Right away, boss."
    long = "Here is a long answer that goes on and on about the weather in Bhatkal and the sea breeze tonight."
    assert flair.dress(long) == long
    monkeypatch.setattr(flair._rng, "random", lambda: 0.99)         # most of the time: unchanged
    assert flair.dress("Opening Spotify.") == "Opening Spotify."
    assert flair.dress("Done.") in flair._ACKS


def test_goodbyes_vary():
    assert {flair.goodbye() for _ in range(60)} - {"Okay."}


def test_time_tool_sounds_natural():
    from veronica.tools.utility_tools import CurrentTimeTool
    msg = CurrentTimeTool().run().message
    assert msg.startswith("It's ") and ("morning" in msg or "afternoon" in msg or "evening" in msg
                                        or "night" in msg or "noon" in msg or "midnight" in msg)


def test_blend_mixes_style_vectors():
    import numpy as np
    from veronica.speech import kokoro_voice as k

    class E:
        def get_voice_style(self, name):
            return np.full(4, {"bf_isabella": 1.0, "bf_emma": 2.0, "af_heart": 4.0}[name], dtype=np.float32)
    mixed = k._style(E(), "veronica")
    assert np.allclose(mixed, 0.45 * 1 + 0.35 * 2 + 0.20 * 4)
    assert k._style(E(), "bf_emma") == "bf_emma" and k.lang_for("veronica") == "en-gb"


def test_alice_runs_out_then_the_blend_speaks(tmp_path):
    from unittest.mock import patch
    from veronica.config import TTSConfig
    from veronica.speech.tts import TextToSpeech
    tts = TextToSpeech(TTSConfig(backend="elevenlabs", backup_backend="kokoro", backup_voice="veronica"),
                       cache_dir=tmp_path / "c")
    tts._mark_primary_out("quota")
    with patch.object(tts, "_kokoro_file", return_value=tmp_path / "k.wav") as kokoro, \
            patch.object(tts, "_edge_file") as edge:
        assert tts._voice_file("Hello.") == tmp_path / "k.wav"
    kokoro.assert_called_once_with("Hello.", "veronica")
    edge.assert_not_called()


def test_voice_picker_has_her_own_voice():
    from veronica.tools.voice_tools import OPTIONS, find_option
    assert any(o.name == "Veronica" and o.voice == "veronica" for o in OPTIONS)
    assert find_option("use the female too voice").name == "Female 2"            # how speech hears "Female 2"
    assert find_option("female 1").name == "Female 1"
    assert find_option("use your own voice").name == "Veronica"
    assert find_option("change your voice to Emma").name == "Emma"


def test_feature_check_writes_to_a_scratch_folder():
    import importlib.util
    from pathlib import Path
    from veronica.config import load_config
    path = Path(__file__).resolve().parent.parent / "scripts" / "feature_check.py"
    spec = importlib.util.spec_from_file_location("feature_check_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cfg = load_config()
    real = cfg.resolve_path("data/conversation.json")
    mod._scratch_data(cfg)
    assert cfg.resolve_path("data/conversation.json") != real
    assert cfg.resolve_path("data/reminders.json").parent.name.startswith("veronica-check-")
    assert cfg.resolve_path("data/models/kokoro") == real.parent / "models" / "kokoro"   # models stay real
    assert cfg.memory.persist_conversation is False and cfg.mem0.enabled is False


def test_alice_runs_out_then_female_2_speaks(tmp_path):
    """Supertonic F2 ("Female 2" on freetts.com) is the backup the user picked."""
    from unittest.mock import patch
    from veronica.config import TTSConfig
    from veronica.speech.tts import TextToSpeech
    tts = TextToSpeech(TTSConfig(backend="elevenlabs", backup_backend="supertonic", backup_voice="F2"),
                       cache_dir=tmp_path / "c")
    tts._mark_primary_out("quota")
    with patch.object(tts, "_supertonic_file", return_value=tmp_path / "s.wav") as st, \
            patch.object(tts, "_edge_file") as edge:
        assert tts._voice_file("Hello.") == tmp_path / "s.wav"
    st.assert_called_once_with("Hello.", "F2")
    edge.assert_not_called()


def test_supertonic_missing_falls_back_to_the_online_voice(tmp_path):
    from unittest.mock import patch
    from veronica.config import TTSConfig
    from veronica.speech.tts import TextToSpeech
    tts = TextToSpeech(TTSConfig(backend="supertonic", voice="F2", backup_backend="edge_tts"),
                       cache_dir=tmp_path / "c")                     # no model under tmp_path
    with patch.object(tts, "_edge_file", return_value=tmp_path / "e.mp3") as edge:
        assert tts._voice_file("Hello.") == tmp_path / "e.mp3"
    edge.assert_called_once()


def test_config_backup_is_female_2():
    from veronica.config import load_config
    c = load_config()
    assert (c.tts.backend, c.tts.backup_backend, c.tts.backup_voice) == ("elevenlabs", "supertonic", "F2")
