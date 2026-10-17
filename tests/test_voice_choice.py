"""Choosing her voice by voice, the Kokoro voice on this PC, and text that keeps pace with speech."""
from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from veronica.config import Config, TTSConfig
from veronica.speech import tts as tts_module
from veronica.speech.tts import TextToSpeech
from veronica.tools.voice_tools import OPTIONS, ChangeVoiceTool, apply_saved_voice, current, find_option


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.resolve_path = lambda rel: tmp_path / rel
    c.tts.backend, c.tts.voice = "edge_tts", "en-IE-EmilyNeural"
    return c


def _tool(cfg, everything=True):
    return ChangeVoiceTool(config=cfg, available=lambda o: everything or o.backend != "elevenlabs")


@pytest.mark.parametrize("said", ["what voices do you have", "give me some voice options",
                                  "change your voice to Emma", "switch to the Irish voice", "try another voice",
                                  "go back to Alice's voice", "use voice number 3"])
def test_voice_requests_match(cfg, said):
    assert _tool(cfg).matches(said)


@pytest.mark.parametrize("said", ["test my voice", "turn the volume up", "check the voice recognition",
                                  "what's the weather"])
def test_other_requests_dont(cfg, said):
    assert not _tool(cfg).matches(said)


def test_list_names_every_usable_voice(cfg):
    msg = _tool(cfg, everything=False).run().message
    assert "Emma" in msg and "Emily" in msg and "Alice" not in msg and "using Emily now" in msg


def test_change_applies_live_and_is_remembered(cfg):
    tool = _tool(cfg)
    res = tool.run(**tool.extract_arguments("change your voice to Emma"))
    assert res.success and cfg.tts.backend == "kokoro" and cfg.tts.voice == "bf_emma"
    fresh = Config()
    fresh.resolve_path = cfg.resolve_path
    assert apply_saved_voice(fresh).name == "Emma" and fresh.tts.backend == "kokoro"


def test_alice_is_elevenlabs_and_warns_about_credits(cfg):
    tool = _tool(cfg)
    res = tool.run(voice="Alice")
    assert cfg.tts.backend == "elevenlabs" and cfg.tts.elevenlabs_voice_id == "Xb7hH8MSUJpSbSDYk0k2"
    assert "credits" in res.message


def test_next_voice_cycles(cfg):
    tool = _tool(cfg)
    before = current(cfg)
    tool.run(voice="next")
    after = current(cfg)
    assert after is not None and after != before
    assert OPTIONS.index(after) == (OPTIONS.index(before) + 1) % len(OPTIONS)


def test_unknown_or_missing_voice(cfg):
    assert not _tool(cfg).run(voice="Gandalf").success
    assert "isn't available" in _tool(cfg, everything=False).run(voice="Alice").message


def test_find_option_by_accent_and_number():
    assert find_option("the american one").name == "Heart"
    assert find_option("voice number 1").name == OPTIONS[0].name


def test_kokoro_backend_is_pipelined(tmp_path):
    tts = TextToSpeech(TTSConfig(backend="kokoro", voice="bf_emma"), cache_dir=tmp_path / "cache")
    made, played = [], []

    def kokoro(text, voice):
        made.append(voice)
        p = tmp_path / f"{len(made)}.wav"
        p.write_bytes(b"x")
        return p
    with patch.object(tts, "_kokoro_file", side_effect=kokoro), \
            patch.object(tts_module, "play_interruptible", side_effect=lambda p, s: played.append(p) or True):
        assert tts.speak_interruptible("One sentence. Two sentences.", threading.Event())
    assert made and set(made) == {"bf_emma"} and len(played) == len(made)


def test_kokoro_missing_falls_back_to_the_backup_voice(tmp_path):
    tts = TextToSpeech(TTSConfig(backend="kokoro", voice="bf_emma"), cache_dir=tmp_path / "cache")
    with patch.object(tts, "_backup_file", return_value=tmp_path / "b.mp3") as backup:
        assert tts._voice_file("Hello.") == tmp_path / "b.mp3"     # no model files in tmp_path
    backup.assert_called_once()


def test_text_appears_with_the_voice_not_before(cfg, capsys):
    from veronica.ui.cli import CLIApp
    app = CLIApp.__new__(CLIApp)
    app.config, app.voice_mode, app._speak_lock = cfg, True, threading.Lock()
    app._tts_active, app._barged = False, False
    app._start_barge_in = lambda stop: None
    app._status = app._beep = lambda *a: None
    order = []

    class Speaker:
        def speak_interruptible(self, text, stop, on_piece=None):
            for piece in ("First.", "Second."):
                order.append(("before-audio", capsys.readouterr().out))
                on_piece(piece)
            return True
    app.speaker = Speaker()
    with patch("time.sleep"):
        app._say("First. Second.")
    assert order[0][1] == ""                               # nothing printed before her first sentence
    assert order[1][1] == "VERONICA: First."               # each sentence shows as she says it
    assert capsys.readouterr().out == " Second.\n\n"


def test_long_first_sentence_is_cut_for_the_local_voice():
    from veronica.speech.tts import _short_first
    out = _short_first(["It's 11:50 AM on Saturday, 17 October, and in Bengaluru it's 26 degrees.", "Next."])
    assert out == ["It's 11:50 AM on Saturday,", "17 October, and in Bengaluru it's 26 degrees.", "Next."]
    assert _short_first(["Good morning.", "x"]) == ["Good morning.", "x"]


def test_full_precision_model_is_preferred(tmp_path):
    from veronica.speech import kokoro_voice
    for name in ("kokoro-v1.0.int8.onnx", "kokoro-v1.0.onnx"):
        (tmp_path / name).write_bytes(b"x")
    assert kokoro_voice._model(tmp_path).name == "kokoro-v1.0.onnx"
