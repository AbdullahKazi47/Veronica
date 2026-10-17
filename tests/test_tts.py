import sys
import types
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from veronica.config import TTSConfig
from veronica.speech import tts as tts_module
from veronica.speech.tts import TextToSpeech


# --- playback-ladder fakes (no pygame, no audio hardware, no network) ---------

class FakeWinsound:
    SND_FILENAME = 0x00020000

    def __init__(self):
        self.played = []

    def PlaySound(self, path, flags):
        self.played.append(path)


class FakeWinmm:
    """Fake winmm MCI driver; 'open_error' injects an MCI open failure."""

    def __init__(self, open_error: int = 0, play_error: int = 0):
        self.open_error = open_error
        self.play_error = play_error
        self.calls = []

    def mciSendStringW(self, cmd, buf=None, size=0, cb=None):
        self.calls.append(cmd)
        if cmd.startswith("open"):
            return self.open_error
        if cmd.startswith("play"):
            return self.play_error
        return 0


class FakeMusic:
    def __init__(self):
        self.loaded = []

    def load(self, path):
        self.loaded.append(path)

    def play(self):
        pass

    def get_busy(self):
        return False


class FakeMixer:
    def __init__(self):
        self.music = FakeMusic()

    def init(self):
        pass


class FakePygame:
    def __init__(self):
        self.mixer = FakeMixer()
        self.time = types.SimpleNamespace(wait=lambda ms: None)


def test_none_backend_silently_does_nothing(tmp_path: Path):
    tts = TextToSpeech(TTSConfig(backend="none"), cache_dir=tmp_path / "cache")
    tts.speak("hello", block=True)  # must not raise


def test_piper_cli_fallback_to_pyttsx3(tmp_path: Path):
    """When piper CLI is not on PATH and the Python package is missing,
    the method should fall back to pyttsx3 (which may also be missing,
    but the call must not raise)."""
    cfg = TTSConfig(backend="piper")
    tts = TextToSpeech(cfg, cache_dir=tmp_path / "cache")

    # Patch shutil.which to return None (no CLI) and ensure piper import fails.
    fake_shutil = types.SimpleNamespace(which=lambda name: None)
    # Mock the fallback: the real pyttsx3 speaks "test text" out loud through the
    # speakers on every test run.
    with patch.dict(sys.modules, {"shutil": fake_shutil, "piper": None}), \
            patch.object(tts, "_speak_pyttsx3") as fallback:
        tts._speak_piper("test text")
    fallback.assert_called_once_with("test text")


def test_piper_python_package_path(tmp_path: Path):
    """When the piper-tts Python package is importable, it should be used."""
    cfg = TTSConfig(backend="piper", piper_model="test-model")
    tts = TextToSpeech(cfg, cache_dir=tmp_path / "cache")

    # Create a fake piper module with PiperVoice that writes a file.
    class FakePiperVoice:
        @classmethod
        def load(cls, model):
            return cls()
        def synthesize(self, text, f):
            f.write(b"fake-wav-data")

    fake_piper = types.SimpleNamespace(PiperVoice=FakePiperVoice)
    fake_shutil = types.SimpleNamespace(which=lambda name: None)

    # Patch sys.modules and _play_audio so no actual audio plays.
    with patch.dict(sys.modules, {"piper": fake_piper}):
        with patch.object(tts, "_play_audio") as mock_play:
            tts._speak_piper("hello world")
            mock_play.assert_called_once()
            # The wav file should have been written.
            wav_files = list((tmp_path / "cache").glob("*.wav"))
            assert len(wav_files) == 1


def test_piper_cli_success(tmp_path: Path):
    """When the piper CLI is on PATH, it should be used and produce a wav file."""
    import subprocess

    cfg = TTSConfig(backend="piper", piper_model="test-model")
    tts = TextToSpeech(cfg, cache_dir=tmp_path / "cache")

    # Fake subprocess.run to write a wav file.
    def fake_run(cmd, input=None, capture_output=None, timeout=None):
        # Find --output_file arg and write a fake wav there.
        for i, arg in enumerate(cmd):
            if arg == "--output_file":
                Path(cmd[i + 1]).write_bytes(b"fake-wav-data")
        return types.SimpleNamespace(returncode=0, stderr=b"")

    fake_shutil = types.SimpleNamespace(which=lambda name: "/usr/bin/piper")
    with patch.dict(sys.modules, {"shutil": fake_shutil}):
        with patch("subprocess.run", side_effect=fake_run):
            with patch.object(tts, "_play_audio") as mock_play:
                tts._speak_piper("hello")
                mock_play.assert_called_once()


def test_edge_tts_import_error_falls_back(tmp_path: Path):
    """When edge-tts is not installed, the method should fall back to pyttsx3."""
    cfg = TTSConfig(backend="edge_tts")
    tts = TextToSpeech(cfg, cache_dir=tmp_path / "cache")
    with patch.dict(sys.modules, {"edge_tts": None}):
        with patch.object(tts, "_speak_pyttsx3") as mock_fallback:
            tts._speak_edge_tts("test")
            mock_fallback.assert_called_once_with("test")


def test_piper_cache_reuse(tmp_path: Path):
    """Second call with same text should reuse cached wav file."""
    cfg = TTSConfig(backend="piper", piper_model="test-model")
    tts = TextToSpeech(cfg, cache_dir=tmp_path / "cache")

    call_count = 0

    class FakePiperVoice:
        @classmethod
        def load(cls, model):
            return cls()
        def synthesize(self, text, f):
            nonlocal call_count
            call_count += 1
            f.write(b"fake-wav-data")

    fake_piper = types.SimpleNamespace(PiperVoice=FakePiperVoice)
    fake_shutil = types.SimpleNamespace(which=lambda name: None)

    with patch.dict(sys.modules, {"piper": fake_piper}):
        with patch.object(tts, "_play_audio"):
            tts._speak_piper("hello")
            tts._speak_piper("hello")
            # synthesize should only be called once (second call hits cache).
            assert call_count == 1


def test_play_wav_uses_winsound_on_windows(tmp_path: Path):
    """Piper produces .wav; on Windows it must play via stdlib winsound."""
    wav = tmp_path / "speech.wav"
    wav.write_bytes(b"RIFF fake wav")
    fake = FakeWinsound()
    with patch("veronica.speech.tts.sys.platform", "win32"), patch.dict(sys.modules, {"winsound": fake}):
        assert tts_module._play_wav_winsound(wav) is True
    assert fake.played == [str(wav)]


def test_wav_is_ignored_outside_windows(tmp_path: Path):
    """winsound is Windows-only; the helper must say 'not handled' elsewhere."""
    wav = tmp_path / "speech.wav"
    wav.write_bytes(b"RIFF fake wav")
    with patch("veronica.speech.tts.sys.platform", "linux"), patch.dict(sys.modules, {"winsound": FakeWinsound()}):
        assert tts_module._play_wav_winsound(wav) is False


def test_play_mp3_uses_winmm_mci_on_windows(tmp_path: Path):
    """edge-tts produces .mp3; on Windows it must play via winmm MCI (ctypes)."""
    mp3 = tmp_path / "speech.mp3"
    mp3.write_bytes(b"ID3 fake mp3")
    fake_winmm = FakeWinmm()
    fake_ctypes = types.SimpleNamespace(windll=types.SimpleNamespace(winmm=fake_winmm))
    with patch("veronica.speech.tts.sys.platform", "win32"), patch.dict(sys.modules, {"ctypes": fake_ctypes}):
        assert tts_module._play_winmm_mci(mp3) is True
    joined = " ".join(fake_winmm.calls)
    assert 'open "%s" alias' % mp3 in joined or 'open' in joined
    assert "play" in joined and "wait" in joined and "close" in joined


def test_mci_open_failure_falls_through_to_pygame(tmp_path: Path):
    """When MCI can't open the file, playback should try pygame next."""
    mp3 = tmp_path / "speech.mp3"
    mp3.write_bytes(b"ID3 fake mp3")
    fake_winmm = FakeWinmm(open_error=1)  # MCI open fails
    fake_ctypes = types.SimpleNamespace(windll=types.SimpleNamespace(winmm=fake_winmm))
    fake_pygame = FakePygame()
    with patch("veronica.speech.tts.sys.platform", "win32"), patch.dict(
        sys.modules, {"ctypes": fake_ctypes, "pygame": fake_pygame}
    ):
        TextToSpeech._play_audio(mp3)  # must not raise
    assert fake_pygame.mixer.music.loaded == [str(mp3)]


def test_winsound_preferred_over_pygame_for_wav(tmp_path: Path):
    """Native player wins even when pygame is installed."""
    wav = tmp_path / "speech.wav"
    wav.write_bytes(b"RIFF fake wav")
    fake = FakeWinsound()
    fake_pygame = FakePygame()
    with patch("veronica.speech.tts.sys.platform", "win32"), patch.dict(
        sys.modules, {"winsound": fake, "pygame": fake_pygame}
    ):
        TextToSpeech._play_audio(wav)
    assert fake.played == [str(wav)]
    assert fake_pygame.mixer.music.loaded == []  # pygame never reached


def test_no_player_logs_warning_but_does_not_raise(tmp_path: Path):
    """Without any player (e.g. Linux, no pygame) playback must degrade cleanly."""
    tts_module._warned_no_player = False
    mp3 = tmp_path / "speech.mp3"
    mp3.write_bytes(b"ID3 fake mp3")
    with patch("veronica.speech.tts.sys.platform", "linux"), patch.dict(
        sys.modules, {"pygame": None}
    ):
        with patch.object(tts_module.logger, "warning") as mock_warn:
            TextToSpeech._play_audio(mp3)
            mock_warn.assert_called_once()
    tts_module._warned_no_player = False


def test_pygame_used_outside_windows(tmp_path: Path):
    """Non-Windows platforms rely on the optional pygame fallback."""
    wav = tmp_path / "speech.wav"
    wav.write_bytes(b"RIFF fake wav")
    fake_pygame = FakePygame()
    with patch("veronica.speech.tts.sys.platform", "linux"), patch.dict(
        sys.modules, {"pygame": fake_pygame}
    ):
        assert tts_module._play_pygame(wav) is True
    assert fake_pygame.mixer.music.loaded == [str(wav)]


# --- ElevenLabs backend (mocked HTTP; no network, no audio) -------------------

class _Resp:
    def __init__(self, status, content=b"", body=None):
        self.status_code, self.content, self._body = status, content, body or {}
        self.ok = 200 <= status < 300
        self.text = str(self._body)

    def json(self):
        return self._body


def _eleven(tmp_path, voice_id="VOICE123"):
    cfg = TTSConfig(backend="elevenlabs", elevenlabs_voice_id=voice_id)
    return TextToSpeech(cfg, cache_dir=tmp_path / "cache")


def test_elevenlabs_synthesizes_caches_and_plays(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    tts = _eleven(tmp_path)
    fake_requests = types.SimpleNamespace(post=MagicMock(return_value=_Resp(200, b"MP3DATA")))
    with patch.dict(sys.modules, {"requests": fake_requests}), \
            patch.object(TextToSpeech, "_play_audio") as play:
        tts.speak("hello", block=True)
        tts.speak("hello", block=True)  # second time comes from the cache
    assert fake_requests.post.call_count == 1
    url = fake_requests.post.call_args.args[0]
    kwargs = fake_requests.post.call_args.kwargs
    assert url.endswith("/v1/text-to-speech/VOICE123")
    assert kwargs["headers"]["xi-api-key"] == "test-key"
    assert kwargs["json"] == {"text": "hello", "model_id": "eleven_flash_v2_5"}
    assert play.call_count == 2 and play.call_args.args[0].read_bytes() == b"MP3DATA"


def test_elevenlabs_out_of_credits_switches_to_the_backup_voice(tmp_path, monkeypatch, capsys):
    """Regression: with the monthly credits used up she went silent ("text-only")."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    TextToSpeech._noticed.clear()
    tts = _eleven(tmp_path)
    body = {"detail": {"message": "This request exceeds your quota of 10000."}}
    fake_requests = types.SimpleNamespace(post=MagicMock(return_value=_Resp(401, body=body)))
    backup = tmp_path / "backup.mp3"
    backup.write_bytes(b"ID3fake")
    with patch.dict(sys.modules, {"requests": fake_requests}),             patch.object(TextToSpeech, "_play_audio") as play,             patch.object(tts, "_backup_file", return_value=backup) as backup_voice:
        tts.speak("hello", block=True)
        tts.speak("and again", block=True)
    out = " ".join(TextToSpeech.take_notices())       # shown by the CLI after her sentence
    assert "credits have run out" in out and "test-key" not in out
    play.assert_any_call(backup)
    assert fake_requests.post.call_count == 1            # no more ElevenLabs calls while it's out
    assert backup_voice.call_count == 2
    assert not list((tmp_path / "cache").glob("*.mp3"))  # an error body is never cached as audio


def test_both_voices_failing_uses_windows_voice(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    tts = _eleven(tmp_path)
    tts._primary_out_until = 9e18
    with patch.object(tts, "_backup_file", return_value=None), patch.object(tts, "_speak_pyttsx3") as win:
        tts.speak("hello", block=True)
    win.assert_called_once_with("hello")


def test_elevenlabs_missing_key_or_voice_does_not_call_api(tmp_path, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    fake_requests = types.SimpleNamespace(post=MagicMock())
    with patch.dict(sys.modules, {"requests": fake_requests}):
        _eleven(tmp_path).speak("hello", block=True)
        monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
        _eleven(tmp_path, voice_id=None).speak("hello", block=True)
    fake_requests.post.assert_not_called()


def test_cache_key_changes_with_cloud_voice(tmp_path):
    a = TextToSpeech(TTSConfig(backend="elevenlabs", elevenlabs_voice_id="A"), cache_dir=tmp_path)
    b = TextToSpeech(TTSConfig(backend="elevenlabs", elevenlabs_voice_id="B"), cache_dir=tmp_path)
    f1 = TextToSpeech(TTSConfig(backend="fish_audio", fish_audio_reference_id="X"), cache_dir=tmp_path)
    f2 = TextToSpeech(TTSConfig(backend="fish_audio", fish_audio_reference_id="Y"), cache_dir=tmp_path)
    assert a._cache_path("hi") != b._cache_path("hi")
    assert f1._cache_path("hi") != f2._cache_path("hi")


def test_edge_voice_is_pipelined_and_uses_the_chosen_voice(tmp_path):
    """The free FRIDAY-style voice (edge_tts, en-IE-EmilyNeural) speaks sentence by sentence,
    interruptibly, like ElevenLabs did."""
    import threading
    tts = TextToSpeech(TTSConfig(backend="edge_tts", voice="en-IE-EmilyNeural"), cache_dir=tmp_path / "cache")
    voices, played = [], []

    def edge_file(text, voice):
        voices.append(voice)
        p = tmp_path / f"{len(voices)}.mp3"
        p.write_bytes(b"x")
        return p
    with patch.object(tts, "_edge_file", side_effect=edge_file), \
            patch.object(tts_module, "play_interruptible", side_effect=lambda p, s: played.append(p) or True):
        assert tts.speak_interruptible("First sentence here. Second one now.", threading.Event())
    assert voices and set(voices) == {"en-IE-EmilyNeural"} and len(played) == len(voices)
