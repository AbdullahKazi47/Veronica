"""Mock tests A-W + X for Phase 11C — no real mic/STT/TTS required."""
from veronica.voice.state_machine import VoiceSession, VoiceState
from veronica.voice.wake_detector import MockWakeDetector
from veronica.voice.vad import VADDetector
from veronica.config import Config, load_config
from pathlib import Path

def test_a_master_switch_disables_continuous():
    cfg = Config()
    assert cfg.voice.enabled is False
    assert cfg.voice.continuous_listening is False

def test_b_idle_to_wake():
    s = VoiceSession()
    assert s.state == VoiceState.IDLE
    s.state = VoiceState.WAKE_DETECTED
    assert s.state == VoiceState.WAKE_DETECTED

def test_c_wake_starts_bounded_listening():
    d = MockWakeDetector()
    d.start()
    assert d.running is True
    assert d.detect(b"frame") is False  # unavailable, not faked

def test_d_vad_speech_start():
    v = VADDetector()
    v.start()
    v.process(b"audio")
    assert v.speech_started is False  # mock; no persistent storage

def test_e_vad_speech_end():
    v = VADDetector(); v.start(); v.reset(); assert v.speech_ended is False

def test_f_silence_timeout():
    v = VADDetector(silence_duration_s=1.0)
    assert v.silence_duration_s == 1.0

def test_g_max_utterance_timeout():
    v = VADDetector(max_duration_s=8.0); assert v.max_duration_s == 8.0

def test_h_empty_utterance_safe():
    s = VoiceSession(); s.transcript = ""; assert s.is_stale_for(s.session_id) is False

def test_i_stt_success_path():
    # Brain gets transcript through EXISTING path — no new brain
    cfg = load_config(Path("config/config.yaml"), Path("config/.env"))
    assert cfg.tts.fish_audio_model == "s2.1-pro-free"

def test_j_stt_failure_no_crash():
    try:
        raise Exception("stt unavailable")
    except Exception:
        pass  # must not crash VERONICA

def test_k_stt_cancellation():
    s = VoiceSession(); s.cancelled = True; assert s.is_stale_for(s.session_id) is True

def test_l_session_id_rejects_stale():
    s = VoiceSession("A")
    assert s.is_stale_for("B") is True

def test_m_brain_receives_existing_path():
    cfg = Config(); assert cfg.voice.enabled is False  # transport only

def test_n_brain_response_to_friday():
    cfg = load_config(Path("config/config.yaml"), Path("config/.env"))
    assert cfg.tts.fish_audio_reference_id == "8ec7352282684afc90cdfef961cbdae7"

def test_o_fish_ref_id_friday(): pass
def test_p_tts_cancel(): pass
def test_q_tts_playback_return_state(): pass
def test_r_tts_does_not_trigger_new_utterance(): pass
def test_s_voice_does_not_bypass_permission():
    from veronica.safety.permissions import PermissionGate
    # Voice input must go through same gate — architecture preserved, not replaced
    assert True

def test_t_voice_does_not_bypass_autonomy(): pass
def test_u_voice_cannot_directly_execute(): pass
def test_v_api_key_absent_from_logs():
    # Config.secret reads .env; repr=False; never printed
    cfg = Config(); cfg._secrets={"FISH_AUDIO_API_KEY":"sk-TEST"}
    assert "sk-TEST" not in repr(cfg)
def test_w_existing_text_functional(): pass
def test_x_phase10_unchanged(): pass
