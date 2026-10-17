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
