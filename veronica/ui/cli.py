"""
Reference text/voice front-end. Deliberately thin: it does nothing but collect input
(typed or spoken), hand it to `Orchestrator.handle()`, and present the output (printed
and/or spoken). A GUI, the 3D orb HUD, or a Slack bot would all look the same shape.
"""
from __future__ import annotations

from veronica.config import Config
from veronica.core.orchestrator import Orchestrator
from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel
from veronica.speech.stt import SpeechToText
from veronica.speech.tts import TextToSpeech, SynchronousSpeaker

logger = get_logger("ui.cli")

_EXIT_WORDS = {"exit", "quit", "bye", "goodbye", "shutdown"}


def _confirm_in_terminal(description: str, level: PermissionLevel) -> bool:
    answer = input(f"\n[VERONICA needs {level.name} permission] {description}\nProceed? [y/N] ").strip().lower()
    return answer in ("y", "yes")


class CLIApp:
    def __init__(self, config: Config, orchestrator: Orchestrator, voice_mode: bool = False) -> None:
        self.config = config
        self.orchestrator = orchestrator
        self.voice_mode = voice_mode
        self.session = orchestrator.new_session()
        self.tts = TextToSpeech(config.tts, cache_dir=config.resolve_path(config.tts.cache_dir))
        self.speaker = SynchronousSpeaker(self.tts)
        self.stt = SpeechToText(config.stt) if voice_mode else None
        self._tts_active = False  # prevents self-listening during playback

    def banner(self) -> None:
        print(f"\n{'=' * 60}")
        print(f"  {self.config.assistant.name} — online.")
        print(f"  Type your request, or 'exit' to quit. Voice mode: {'ON' if self.voice_mode else 'off (text only)'}")
        print(f"{'=' * 60}\n")

    def run(self) -> None:
        self.banner()
        while True:
            try:
                text = self._get_input()
            except (KeyboardInterrupt, EOFError):
                print("\nShutting down.")
                break

            if not text:
                continue
            if text.strip().lower() in _EXIT_WORDS:
                self._say("Goodbye.")
                break

            response = self.orchestrator.handle(text, self.session)
            self._say(response.text)

    def _get_input(self) -> str:
        if self.voice_mode and self.stt is not None:
            # Stage 1: LISTEN — microphone is active
            print("[listening...]")
            result = self.stt.listen_once()
            if not result.success:
                print(f"[STT] {result.error}")
                return ""
            print(f"You (voice): {result.text}")
            return result.text
        return input("You: ")

    def _say(self, text: str) -> None:
        print(f"{self.config.assistant.name}: {text}\n")
        if self.voice_mode:
            # Stage 2: Block microphone while TTS plays — prevents self-listening feedback
            self._tts_active = True
            try:
                self.speaker.speak(text)
            except Exception as e:
                logger.warning("TTS playback failed: %s", e)
            finally:
                self._tts_active = False
            # Stage 3: Short cooldown + buffer flush before re-listening
            import time
            time.sleep(0.2)
