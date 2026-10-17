"""Speaking while the reply is still being written, extra backup brains, and auto-start /
one-copy-only."""
from __future__ import annotations

import platform
import threading
import uuid
from types import SimpleNamespace

import pytest

from veronica.voice.live_speech import LiveSpeech


def _feed(live, *deltas):
    for d in deltas:
        live.on_event("response_delta", {"delta": d})


def test_sentences_come_out_as_soon_as_they_end():
    live, started = LiveSpeech(), []
    live.on_first_piece = lambda: started.append(True)
    _feed(live, "Good morn", "ing. It's sunny")
    assert live.sent == ["Good morning."] and started == [True]      # first sentence: straight away
    _feed(live, " today. ", "Ok. And it will rain later.")
    assert live.sent == ["Good morning.", "It's sunny today."]
    live.close()
    assert live.sent[-1] == "Ok. And it will rain later."             # tiny "Ok." rides with the next one
    assert list(live.pieces()) == live.sent


def test_long_first_sentence_is_cut_at_a_comma():
    live = LiveSpeech()
    _feed(live, "They have three hearts and blue blood, and two-thirds of their neurons are in their arms. ")
    assert live.sent[0] == "They have three hearts and blue blood,"


def test_markdown_is_not_read_aloud():
    live = LiveSpeech()
    _feed(live, "**Sure thing.** Here's `the` plan:\n- step one\n")
    live.close()
    assert all("*" not in p and "`" not in p for p in live.sent)


def test_covers_tells_whether_the_reply_was_already_said():
    live = LiveSpeech()
    _feed(live, "It's 26 degrees in Bengaluru. Light drizzle.")
    live.close()
    assert live.covers("It's 26 degrees in Bengaluru. Light drizzle.")
    assert not live.covers("Google's AI service is overloaded right now.")
    assert not LiveSpeech().covers("anything")


def test_nothing_after_close():
    live = LiveSpeech()
    live.close()
    _feed(live, "Late text.")
    assert live.sent == [] and list(live.pieces()) == []


# ---------------------------------------------------------------- the CLI path

class _Orch:
    def __init__(self, deltas, reply):
        self.deltas, self.reply, self.stream_event_callback = deltas, reply, None

    def handle_stream(self, text, session):
        for d in self.deltas:
            self.stream_event_callback("response_delta", {"delta": d})
        return SimpleNamespace(text=self.reply)

    def handle(self, text, session):
        return SimpleNamespace(text=self.reply)


def _app(orch):
    from veronica.ui.cli import CLIApp
    app = CLIApp.__new__(CLIApp)
    app.config = SimpleNamespace(streaming=SimpleNamespace(enabled=True, speak_while_writing=True),
                                 assistant=SimpleNamespace(name="VERONICA"))
    app.voice_mode, app.orchestrator, app.session = True, orch, None
    spoken = []
    app.speaker = SimpleNamespace(speak_stream=lambda *a, **k: None)
    app._say = lambda text, pieces=None: spoken.append(list(pieces) if pieces is not None else text)
    return app, spoken


def test_cli_speaks_the_stream_and_not_again():
    app, spoken = _app(_Orch(["The sky is blue. ", "Light scatters."], "The sky is blue. Light scatters."))
    reply, live = app._answer("why is the sky blue")
    assert spoken == [["The sky is blue.", "Light scatters."]]
    assert live is not None and live.covers(reply)       # the main loop won't say it a second time
    assert app.orchestrator.stream_event_callback is None


def test_cli_without_stream_text_says_the_reply_normally():
    app, spoken = _app(_Orch([], "Opened Spotify."))
    reply, live = app._answer("open spotify")
    assert reply == "Opened Spotify." and live is None and spoken == []


def test_cli_off_when_streaming_is_off():
    app, spoken = _app(_Orch(["x. "], "plain"))
    app.config.streaming.enabled = False
    assert app._answer("hi") == ("plain", None)


# ---------------------------------------------------------------- backup brains

def test_models_without_a_key_are_skipped(monkeypatch):
    from veronica.config import Config, ModelEntryConfig
    from veronica.llm.routing.builder import build_routing
    monkeypatch.delenv("NO_SUCH_KEY_FOR_TEST", raising=False)
    cfg = Config()
    cfg.routing.enabled = True
    cfg.routing.models = [
        ModelEntryConfig(model_id="main", provider="echo", role="fast", default=True, supports_tools=True),
        ModelEntryConfig(model_id="needs-key", provider="openai_compatible", role="backup",
                         api_key_env="NO_SUCH_KEY_FOR_TEST", supports_tools=True),
    ]
    assert build_routing(cfg).registry.model_ids() == ["main"]
    monkeypatch.setenv("NO_SUCH_KEY_FOR_TEST", "k")
    assert "needs-key" in build_routing(cfg).registry.model_ids()


def test_config_has_the_extra_google_models_and_optional_backups():
    from veronica.config import load_config
    ids = {m.model_id: m for m in load_config().routing.models}
    assert {"gemini-3.1-flash-lite", "gemini-3.7-flash"} <= set(ids)
    assert ids["gpt-oss-120b"].api_key_env == "CEREBRAS_API_KEY"
    assert ids["nvidia/nemotron-3-super-120b-a12b:free"].api_key_env == "OPENROUTER_API_KEY"


# ---------------------------------------------------------------- one copy only, auto-start

@pytest.mark.skipif(platform.system() != "Windows", reason="Windows named mutex")
def test_second_copy_is_refused():
    import veronica.main as m
    saved = m._INSTANCE_LOCK
    name = f"VERONICA_test_{uuid.uuid4().hex}"
    try:
        assert m.already_running(name) is False
        result = []
        t = threading.Thread(target=lambda: result.append(m.already_running(name)))
        t.start()
        t.join()
        assert result == [True]
    finally:
        m._INSTANCE_LOCK = saved


def test_autostart_script_only_uses_the_startup_folder():
    from pathlib import Path
    script = (Path(__file__).resolve().parent.parent / "scripts" / "autostart.ps1").read_text(encoding="utf-8")
    assert 'GetFolderPath("Startup")' in script and "-Remove" in script
    assert "HKLM" not in script and "Register-ScheduledTask" not in script
