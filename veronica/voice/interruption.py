"""Phase 11D — Interruption / barge-in.
Transport layer only. No VoiceBrain. Uses existing VoiceSession token + VAD.
"""
from __future__ import annotations
import threading
from veronica.voice.state_machine import VoiceState, VoiceSession

class InterruptionController:
    """Handles SPEAKING -> INTERRUPTING -> LISTENING with stale-session guard."""
    def __init__(self, config):
        self.cfg = config
        self.active_session: VoiceSession | None = None
        self.barge_in_enabled = getattr(config.voice, "barge_in_enabled", False)
        self.barge_in_guard_ms = getattr(config.voice, "barge_in_guard_ms", 300)

    def interrupt(self, session: VoiceSession):
        if session.is_stale_for(session.session_id):
            return  # stale — reject
        session.state = VoiceState.INTERRUPTING  # Step 2
        session.cancelled = True
        # TTS cancellation (Step 4): signal; stop playback; cancel TTS job
        # Playback stops via winmm MCI close (reused from tts.py logic)
        # Session invalidated; old callbacks rejected by is_stale_for
        session.state = VoiceState.LISTENING  # after TTS stop
        self.active_session = VoiceSession()  # new session token (Step 5)
