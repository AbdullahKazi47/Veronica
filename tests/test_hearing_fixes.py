"""Seen live 2026-10-17 evening: 'Veronica' alone ignored four times, 'Open Doraemon' heard
as 'Open Door,' and dropped, then heard right and ignored as background speech (2.5x the
room vs a 3x bar), 'Veronica, open.' answered as two requests, and library log lines in
the terminal."""
from __future__ import annotations

import logging
from types import SimpleNamespace

from veronica.speech.stt import STTResult


def _app():
    from veronica.ui.cli import CLIApp
    app = CLIApp.__new__(CLIApp)
    app.config = SimpleNamespace(assistant=SimpleNamespace(name="VERONICA"),
                                 voice=SimpleNamespace(wake_words=["Veronica", "Vero"], conversation_speech_ratio=3.0,
                                                       conversation_min_confidence=0),
                                 stt=SimpleNamespace(confirm_below_confidence=0))
    app.orchestrator = SimpleNamespace(tools=SimpleNamespace(
        find_fast_match=lambda t: object() if t.lower().startswith("open ") else None))
    app._ends_conversation = lambda t: False
    return app


def _heard(text, speech, ambient=380.0):
    return STTResult(success=True, text=text, stats={"ambient_rms": ambient, "speech_rms": speech, "voiced_s": 1.0})


def test_a_command_at_2_5x_the_room_is_accepted():
    assert _app()._accept(_heard("Open Doraemon.", 967.6, 379.1), open_conversation=True) == "Open Doraemon."


def test_talk_aimed_at_her_name_is_accepted():
    assert _app()._accept(_heard("Veronica what are you doing", 900), open_conversation=True)


def test_background_chatter_still_needs_the_full_bar():
    assert _app()._accept(_heard("and then the minister said", 900), open_conversation=True) is None
    assert _app()._accept(_heard("Open Doraemon.", 600), open_conversation=True) is None   # under 2x: the TV


def test_her_name_alone_counts_as_being_called():
    app = _app()
    called = STTResult(success=False, error="Not speech (only a hint word; heard 'VERONICA').",
                       stats={"ambient_rms": 380.0, "speech_rms": 1100.0})
    assert app._just_my_name(called)
    quiet = STTResult(success=False, error="Not speech (only a hint word; heard 'Veronica,').",
                      stats={"ambient_rms": 380.0, "speech_rms": 500.0})
    assert not app._just_my_name(quiet)                                   # noise, not the user
    other = STTResult(success=False, error="Not speech (low confidence; heard 'Microsoft Ede').",
                      stats={"ambient_rms": 380.0, "speech_rms": 1100.0})
    assert not app._just_my_name(other)


def test_her_name_in_front_is_not_a_separate_request():
    from veronica.core.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    orch.config = SimpleNamespace(assistant=SimpleNamespace(name="VERONICA"),
                                  voice=SimpleNamespace(wake_words=["Veronica", "Vero"]))
    assert orch._without_name("Veronica, open.") == "open."
    assert orch._without_name("hey Veronica open Spotify") == "open Spotify"
    assert orch._without_name("Veronica") == "Veronica"                   # just the name stays
    assert orch._without_name("Veronica's voice is nice") == "Veronica's voice is nice"


def _stt(gemini_text="Open Doraemon.", escalate=-0.55):
    from veronica.speech.stt import SpeechToText
    stt = SpeechToText.__new__(SpeechToText)
    stt.config = SimpleNamespace(escalate_below_logprob=escalate)
    stt._gemini_key = lambda: "k"
    stt._recognize_gemini = lambda pcm: STTResult(success=True, text=gemini_text)
    return stt


def test_a_clearly_spoken_unsure_read_is_rescued_by_the_cloud():
    unsure = STTResult(success=False, error="Not speech (low confidence; heard 'Open Door,').")
    loud = {"ambient_rms": 380.0, "speech_rms": 1000.0, "voiced_s": 0.9}
    got = _stt()._rescue("offline", unsure, b"pcm", loud)
    assert got is not None and got.text == "Open Doraemon."


def test_rescue_never_for_noise_or_when_kept_offline():
    unsure = STTResult(success=False, error="Not speech (low confidence; heard 'Microsoft Ede').")
    assert _stt()._rescue("offline", unsure, b"", {"ambient_rms": 380.0, "speech_rms": 500.0, "voiced_s": 0.9}) is None
    assert _stt()._rescue("offline", unsure, b"", {"ambient_rms": 380.0, "speech_rms": 1000.0, "voiced_s": 0.2}) is None
    loud = {"ambient_rms": 380.0, "speech_rms": 1000.0, "voiced_s": 0.9}
    assert _stt(escalate=0)._rescue("offline", unsure, b"", loud) is None            # user chose fully offline
    no_speech = STTResult(success=False, error="Not speech (no-speech score; heard 'Word, Word').")
    assert _stt()._rescue("offline", no_speech, b"", loud) is None


def test_library_console_handlers_are_removed(monkeypatch):
    import veronica.logging_setup as ls
    lib = logging.getLogger("phonemizer_test_lib")
    lib.addHandler(logging.StreamHandler())
    monkeypatch.setattr(ls, "_QUIET", True)
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        root.addHandler(logging.StreamHandler())
        ls.quiet_third_party()
        assert not any(type(h) is logging.StreamHandler for h in lib.handlers)
        assert not any(type(h) is logging.StreamHandler for h in root.handlers)
    finally:
        root.handlers = saved


def test_browser_use_does_not_set_up_logging():
    import os
    import veronica.integrations.browser_agent  # noqa: F401
    assert os.environ.get("BROWSER_USE_SETUP_LOGGING") == "false"


def test_repeated_word_noise_is_junk():
    from veronica.speech.stt import whisper_junk
    assert whisper_junk("Mora, Mora, Mora, Mora, Mora, Mora.", "", -0.2)
    assert whisper_junk("Microsoft Teams, Microsoft Teams, Microsoft Teams, Microsoft Teams.", "", -0.2)
    assert whisper_junk("no no no no", "", -0.1)
    assert whisper_junk("open Spotify and play my liked songs", "", -0.2) == ""
    assert whisper_junk("very very good", "", -0.2) == ""


def test_an_empty_stream_falls_back_to_the_normal_path(tmp_path):
    from veronica.config import Config
    from veronica.core.orchestrator import Orchestrator
    from veronica.llm.base import LLMProvider, LLMResponse, StreamChunk
    from veronica.llm.streaming import StreamController
    from veronica.memory.long_term import LongTermMemory
    from veronica.safety.permissions import PermissionGate, PermissionLevel
    from veronica.tools.registry import ToolRegistry

    class Quiet(LLMProvider):
        name = "q"
        supports_streaming = True

        def chat(self, messages, tools=None, temperature=0.4, voice_mode=False):
            return LLMResponse(content="Muted the group, boss.")

        def chat_stream(self, messages, tools=None, temperature=0.4, voice_mode=False):
            yield StreamChunk(text="", sequence=0)
            yield StreamChunk(text="", sequence=1, done=True, finish_reason="stop")
    cfg = Config()
    cfg.routing.enabled = False
    cfg.streaming.enabled = cfg.streaming.preferred = True
    orch = Orchestrator(config=cfg, llm=Quiet(), tools=ToolRegistry(PermissionGate(threshold=PermissionLevel.SENSITIVE)),
                        long_term_memory=LongTermMemory(tmp_path / "m.json"), stream_controller=StreamController(coalesce_ms=0))
    assert orch.handle_stream("mute the batch one group in WhatsApp", orch.new_session()).text == "Muted the group, boss."


def test_ctrl_c_mid_sentence_is_a_goodbye_not_a_traceback(monkeypatch, capsys):
    """Seen live: Ctrl+C while she was speaking printed a KeyboardInterrupt traceback."""
    import sys
    import veronica.main as m

    class CLI:
        def __init__(self, **kw):
            pass

        def run(self):
            raise KeyboardInterrupt
    monkeypatch.setattr(sys, "argv", ["veronica"])
    monkeypatch.setattr(m, "CLIApp", CLI)
    monkeypatch.setattr(m, "build_app", lambda config, interactive=True: SimpleNamespace(orchestrator=None))
    m.main()
    out = capsys.readouterr().out
    assert "Goodbye" in out and "Traceback" not in out


# 2026-10-18: noise read as lists by Whisper, then guessed at by the cloud and answered.
def test_noise_lists_from_the_log_are_never_sent_to_the_cloud():
    from veronica.speech.stt import noise_list
    for junk in ("Microsoft Teams, Microsoft Teams,", "Server, Server.", "Go, Go, Go, Go", "Web, Web, Web.",
                 "JavaScript, Word, Knowledge, Blockchain, F frustrated,", "Microsoft, Cllular, Crop, Mamos, Crab."):
        assert noise_list(junk), junk
    for real in ("Open Door,", "open Chrome, Spotify and Word", "What say you?", "Yes, please.",
                 "I can't get on a server like this."):
        assert not noise_list(real), real
    unsure = STTResult(success=False, error="Not speech (low confidence; heard 'Microsoft Teams, Microsoft Teams,').")
    loud = {"ambient_rms": 380.0, "speech_rms": 1000.0, "voiced_s": 0.9}
    assert _stt(gemini_text="sabhi ka naya desh")._rescue("offline", unsure, b"pcm", loud) is None


def test_a_cloud_read_that_shares_nothing_with_the_offline_one_is_dropped():
    loud = {"ambient_rms": 380.0, "speech_rms": 1000.0, "voiced_s": 0.9}
    unsure = STTResult(success=False, error="Not speech (low confidence; heard 'Go to the cup').")
    assert _stt(gemini_text="conveniencia")._rescue("offline", unsure, b"pcm", loud) is None
    unsure = STTResult(success=False, error='Not speech (low confidence; heard "What say you? How do you comfort?").')
    assert _stt(gemini_text="What say you?")._rescue("offline", unsure, b"pcm", loud).text == "What say you?"



# ---------------------------------------------------------------- 2026-10-18 evening: "she's listening but not responding"

def test_after_her_name_an_unclear_read_gets_the_cloud():
    """'Cicc', 'Sf3', 'Bionic, C' right after the wake word were binned in silence."""
    unsure = STTResult(success=False, error="Not speech (low confidence; heard 'Cicc.').")
    quiet = {"ambient_rms": 380.0, "speech_rms": 500.0, "voiced_s": 0.8}      # under 2x the room
    stt = _stt(gemini_text="Open YouTube.")
    assert stt._rescue("offline", unsure, b"", quiet) is None                # not addressed: binned
    stt.addressed = True
    got = stt._rescue("offline", unsure, b"", quiet)
    assert got is not None and got.text == "Open YouTube."


def test_she_says_when_she_did_not_catch_it():
    import threading
    from veronica.ui.cli import CLIApp
    app = _app()
    app.voice_mode, app.wake_mode, app.stt = True, True, SimpleNamespace(addressed=False, known_ambient_rms=None)
    app.wake_gate = SimpleNamespace(wait=lambda on_near_miss=None: True, ambient_rms=None, reason="")
    app.config.voice.wake_threshold = 0.5
    app._beep = app._status = lambda *a: None
    app._barged, app._follow_up_until = False, 0.0
    said, heard = [], [STTResult(success=False, error="Not speech (low confidence; heard 'Cicc.')."),
                       STTResult(success=True, text="open YouTube", stats={})]
    app._say = said.append
    app._listen = lambda timeout_s=6: heard.pop(0)
    app._conversation_open = lambda: False
    got = CLIApp._get_input(app)
    assert said == ["Sorry, I didn't catch that. Say it again?"] and got == "open YouTube"


def test_supertonic_text_is_made_speakable():
    from veronica.speech.supertonic_voice import speakable
    assert speakable("Yes \u2014 it\u2019s done\u2026") == "Yes ,  it's done..."
    garbled = speakable("Yes \xe2\x80\x94 ok")                     # the mojibake that crashed her voice
    assert "\x80" not in garbled and "\x94" not in garbled and garbled.startswith("Yes")


def test_streamed_replies_are_decoded_as_utf8(monkeypatch):
    """requests guessed ISO-8859-1 for text/event-stream: 'Yes \u2014' arrived as mojibake."""
    import json
    import requests
    from veronica.llm.openai_compatible_provider import OpenAICompatibleProvider
    body = ("data: " + json.dumps({"choices": [{"delta": {"content": "Yes \u2014 caf\u00e9"}, "index": 0}]},
                                  ensure_ascii=False) + "\n\ndata: [DONE]\n\n").encode("utf-8")

    class Resp:
        encoding = "ISO-8859-1"
        def raise_for_status(self):
            pass
        def iter_lines(self, decode_unicode=False):
            for line in body.split(b"\n"):
                yield line.decode(self.encoding) if decode_unicode else line
    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp())
    p = OpenAICompatibleProvider(base_url="https://x/v1", api_key="k", model="m")
    text = "".join(c.text for c in p.chat_stream([]))
    assert "Yes \u2014 caf\u00e9" in text


def test_mic_meter_dots_when_quiet_bars_when_speaking():
    from veronica.voice.meter import render
    line = render([50, 60, 2400, 900, 55], 380, fancy=True)
    cells = line.split("listening")[1].split()
    assert cells[0] == cells[1] == cells[4] == "\u00b7" and cells[2] not in ("\u00b7",) and cells[2] > cells[3]
    assert "*" in render([2400], 380, fancy=False)
