# Phase 11C — Wake, VAD, Continuous Listening Foundation

Status: IMPLEMENTED, LIVE VALIDATION BLOCKED

IMPLEMENTED:
- VoiceConfig (enabled=false, continuous_listening=false master switches).
- VoiceState machine (IDLE→WAKE_DETECTED→LISTENING→TRANSCRIBING→THINKING→SPEAKING→STOPPING/ERROR/IDLE).
- VoiceSession with session token (stale callback rejection).
- WakeWordDetector abstraction + MockWakeDetector (configurable phrases; unavailable = BLOCKED, not faked).
- VADDetector (start/end/silence/max/min thresholds; no permanent storage; no single amplitude simplification claim).
- Config YAML `voice:` section added.
- Brain integration preserved: transcript → EXISTING input path; no VoiceBrain / VoicePlanner / VoiceMemory / VoicePermission.
- TTS integration: response → Fish Audio FRIDAY (ref 8ec...ae7) unchanged.
- Master switch fully disables mic capture + wake loop; text mode continues; TTS continues independently.

MOCK-TESTED (tests/test_phase_11c_voice.py — A-W + X): 24 passed. No microphony dependency for test suite.

LIVE-VERIFIED: NONE for continuous listening / wake / VAD. Wake-word backend unavailable (no openwakeword/Porcupine installed); VAD uses mock; microphone smoke test BLOCKED by unavailable real backend.

BLOCKED:
- Real wake-word detector (no installed backend; not faked).
- Real microphone continuous capture (hardware available but real detector unavailable → BLOCKED, not simulated).
- Actual STT over voice audio (mock only; real test requires functioning VAD + wake).

SAFETY / ARCHITECTURE PRESERVED:
- Voice is input/output transport only.
- Existing PermissionGate, AutonomyPolicy, ExecutionAuthorization, VisualGrounding, TargetStateVerifier untouched.
- Voice transcript enters same brain/input path as typed text — no extra authority.
- Fish Audio key never logged (Config.secret repr=False; .env untracked).
- Phase 10 behavior unchanged (test suite intact).

CONFIG:
```yaml
voice:
  enabled: false
  continuous_listening: false
  wake_words: ["Veronica", "Vero"]
  listen_timeout_s: 10.0
  max_utterance_s: 8.0
  silence_duration_s: 1.0
  return_to_listen: true
  suppress_playback_during_capture: true
```

FILE CHANGES:
NEW: veronica/voice/state_machine.py, veronica/voice/wake_detector.py, veronica/voice/vad.py, veronica/voice/__init__.py (intentional minimal), tests/test_phase_11c_voice.py, docs/PHASE_11C_WAKE_VAD.md
MODIFIED: veronica/config.py (VoiceConfig + Config.voice), config/config.yaml (voice: section)
UNMODIFIED (intentional): veronica/safety/permissions.py, autonomy_policy.py, computer_use.py, visual_grounding.py, target verification, brain, LLM routing, Phase 10 components.

NOT STARTED (Phase 11D): interruption/barge-in advanced system.
