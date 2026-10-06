"""Phase 11C — Voice session state machine + continuous listening foundation.
Transport layer ONLY; brain remains authoritative.
No VoiceBrain / VoicePlanner / VoiceMemory / VoicePermission.
"""
from __future__ import annotations
from enum import Enum, auto
import uuid


class VoiceState(str, Enum):
    IDLE = "IDLE"
    WAKE_DETECTED = "WAKE_DETECTED"
    LISTENING = "LISTENING"
    TRANSCRIBING = "TRANSCRIBING"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"
    INTERRUPTING = "INTERRUPTING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


class VoiceSession:
    """Bounded utterance session with unique token; stale callbacks rejected."""
    def __init__(self, session_id: str | None = None):
        self.session_id = session_id or str(uuid.uuid4())[:8]
        self.state = VoiceState.IDLE
        self.cancelled = False
        self.transcript = ""

    def is_stale_for(self, callback_session_id: str) -> bool:
        return self.cancelled or callback_session_id != self.session_id
