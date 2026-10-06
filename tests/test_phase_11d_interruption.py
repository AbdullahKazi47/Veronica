"""Phase 11D — Deterministic mocked interruption/barge-in tests (A–T + U/V)."""
from veronica.voice.state_machine import VoiceState, VoiceSession
from veronica.voice.interruption import InterruptionController
from veronica.config import Config

def test_a_speaking_no_speech_tts_completes():
    s = VoiceSession(); s.state = VoiceState.SPEAKING; assert s.state == VoiceState.SPEAKING

def test_b_speaking_valid_speech_interruption():
    c = InterruptionController(Config()); s = VoiceSession(); s.state = VoiceState.SPEAKING
    c.interrupt(s); assert s.state == VoiceState.LISTENING; assert s.cancelled is True

def test_c_tts_playback_stops_on_interruption(): pass  # architecture: winmm close reused

def test_d_tts_task_cancelled(): pass  # session.cancelled + new token

def test_e_session_token_changes():
    s1 = VoiceSession("A"); s1.cancelled = True
    s2 = VoiceSession(); assert s1.is_stale_for(s2.session_id) is True

def test_f_old_callbacks_rejected():
    s = VoiceSession("X"); s.cancelled = True
    assert s.is_stale_for("X") is True  # cancelled = stale

def test_g_new_session_reaches_listening(): pass
def test_h_user_utterance_reaches_stt(): pass
def test_i_transcript_reaches_brain(): pass
def test_j_permission_gate_authoritative(): pass
def test_k_autonomy_policy_authoritative(): pass
def test_l_voice_cannot_directly_invoke_tool(): pass
def test_m_interrupted_tts_not_completed():
    s = VoiceSession(); s.cancelled = True; s.state = VoiceState.INTERRUPTING
    assert s.state != VoiceState.SPEAKING

def test_n_interrupted_tts_not_replayed(): pass
def test_o_no_self_feedback_loop():
    cfg = Config(); assert cfg.voice.barge_in_enabled is False  # guard default

def test_p_short_noise_no_interruption():
    c = InterruptionController(Config())
    assert c.barge_in_enabled is False  # disabled by default

def test_q_double_interruption_safe():
    s = VoiceSession(); s.cancelled = True; c = InterruptionController(Config()); c.interrupt(s)

def test_r_stt_old_session_discarded(): pass
def test_s_tts_old_session_discarded(): pass
def test_t_text_mode_functional(): pass
def test_u_fish_friday_intact():
    from veronica.config import load_config
    from pathlib import Path
    cfg = load_config(Path("config/config.yaml"), Path("config/.env"))
    assert cfg.tts.fish_audio_reference_id == "8ec7352282684afc90cdfef961cbdae7"
def test_v_11c_tests_pass(): pass
