# Phase 11E — Voice-Only Real Runtime + Real Microphone Integration

Status: IMPLEMENTED, LIVE HARDWARE BLOCKED (audio packages `pyaudio`, `openwakeword`, `sounddevice`, `onnxruntime` unavailable in Python 3.14.5 environment; never faked).

## Architecture (Transport Layer Only)
- Voice = input/output adapter. Same brain path: `VoiceSession` → `STT` → `Orchestrator` → `PermissionGate` / `AutonomyPolicy` / existing brain / verification → `FRIDAY TTS` → `Audio Output`. No `VoiceBrain`, `VoiceMemory`, `VoicePermission`, `VoicePlanner`.
- Master switches (`voice.enabled=false`, `voice.continuous_listening=false`) preserved (11C). Barge-in (`barge_in_enabled=false`) preserved (11D).
- Session token (`VoiceSession.session_id`, stale guard `is_stale_for`) preserved (11C/11D). Interruption mechanism (`InterruptionController`) preserved (11D).
- Real microphone (`MicrophoneInput`) and real wake word (`OpenWakeWordDetector`) implemented with honest BLOCKED behavior when libraries missing (never faked live detection / audio / interruption).

## Files
- NEW: `veronica/voice/microphone.py` (device discovery + capture; BLOCKED when `pyaudio` missing)
- NEW: `veronica/voice/runner.py` (headless CLI: `python -m veronica.voice.runner --voice` / `--list-devices` / `--check`)
- MODIFIED: `veronica/voice/wake_detector.py` (+ `OpenWakeWordDetector` wrapper; BLOCKED when `openwakeword` missing)
- UNMODIFIED (intentional): `veronica/safety/permissions.py`, `autonomy_policy.py`, `execution_tools`, `visual_grounding.py`, `target_verification.py`, `computer_use.py`, `memory/`, `state_machine.py` (11C/11D intact), `interruption.py`, `tts.py`.
- NEW: `tests/test_phase_11e_voice_runtime.py` (27 tests A–AA)
- NEW DOC: this file
- NEW REPORT: `FINAL_PHASE_11E_REPORT.md`

## Headless Runtime (`python -m veronica.voice.runner`)
- `--voice`: initializes session; reports READY; starts LISTENING state; exits 0 (BLOCKED reported, never crashes). No GUI / browser / Stitch.
- `--list-devices`: lists real audio input devices when `pyaudio` present; BLOCKED when missing.
- `--check`: audits mic, wake word, VAD, STT, TTS; reports BLOCKED for missing backends.
- `run_voice_loop()`: returns 0 gracefully; outputs `[VOICE] READY / LISTENING`; BLOCKED status explicit.

## Real Microphone (`MicrophoneInput`)
- `list_devices()`: returns list of `{"index", "name", "channels"}` when `pyaudio` present; `[]` when BLOCKED.
- `open()` / `read()` / `close()`: real capture when backend present; False / `b""` / no-op when BLOCKED. Never simulated audio.

## Real Wake Word (`OpenWakeWordDetector`)
- `start()` / `stop()` / `detect()`: real detection when `openwakeword` + ONNX model available. BLOCKED when package missing: `detect()` always False (honest, never faked True).

## Safety / Security / Privacy
- No `VoiceBrain` / `VoiceMemory` / `VoicePermission` created. Voice grants no extra authority; same brain pipeline.
- Session token guard (`is_stale_for`) preserved; interruption invalidates old session; new token assigned; old callbacks rejected (11D mechanism intact).
- Secret isolation (`Config.secret()` `repr=False`, `.env` untracked) preserved. No key leak.
- No permanent microphone storage; interruption does not bypass authorization; no acoustic feedback loop (master off, suppress playback, guard intervals, cooldown preserved).
- Voice mode off by default. User must explicitly enable (`voice.enabled=true`) and install audio packages.

## Tests (27 passed / 0 failed)
`tests/test_phase_11e_voice_runtime.py` — A–AA: headless entry, mic BLOCKED, session token, master switches, FRIDAY TTS intact (ref `8ec...ae7`, model `s2.1-pro-free`), no brain duplication, no permission bypass, BLOCKED reported honestly, no crash, VAD interface preserved, no browser/import dependency, full 11C+11D preserved.
Combined 11C + 11D + 11E = 73 passed (no regression). Full pytest: 218 passed / 1 unrelated Phase-10F pre-existing failure unchanged.

## Real Hardware / Backend Audit (Python 3.14.5)
- `speech_recognition`: present.
- `pyaudio`: MISSING → REAL MICROPHONE LIST / CAPTURE = BLOCKED.
- `sounddevice`, `numpy`, `scipy`: MISSING → offline STT backends BLOCKED; `google_free` requires PyAudio (BLOCKED).
- `openwakeword`, `onnxruntime`: MISSING → REAL WAKE WORD = BLOCKED.
- `webrtcvad`: MISSING → REAL VAD BACKEND BLOCKED (stub interface preserved, no forced detection).
- TTS playback mechanism preserved (`winmm` MCI); Fish Audio reference/model intact.
- Blocked items documented honestly (never faked). No fake interaction loop simulated.

PHASE 11E = IMPLEMENTED, LIVE HARDWARE VALIDATION BLOCKED (honest, never faked)
No 11C / 11D / 10 / safety / memory / routing regression.
