# Phase 11E.1 — Unblocked Audio Runtime & Wake Word Infrastructure

## Overview
Phase 11E.1 unblocks real audio hardware input, VAD, and local wake-word infrastructure on Windows 11 with Python 3.14.5 without requiring PyAudio (which fails build on Python 3.14 on Windows).

## Components Unblocked

### 1. Real Microphone Input (`veronica/voice/microphone.py`)
- Backend: `sounddevice` (v0.5.6) + `numpy` (v2.5.3) as primary; optional `pyaudio` fallback.
- Format: 16 kHz, 1 channel (mono), `int16`, 1024 frames per chunk.
- Verified on local hardware: Realtek Audio capture interface discovered, opened, read, and closed cleanly.
- Honest degradation: returns `[]` or `b""` when backends/devices are unavailable. Never fakes audio capture.

### 2. Real VAD Integration (`veronica/voice/vad.py`)
- Real PCM frame processing via `VADDetector`.
- Bounded processing (no infinite capture loops; zero permanent storage of microphone streams).
- Configurable thresholding and silence duration guards.

### 3. Local Wake Engine Infrastructure (`veronica/voice/wake_detector.py`)
- Engine: `openwakeword` with `onnxruntime` local execution.
- Stock Pretrained Phrases:
  - `alexa`
  - `hey_jarvis`
  - `hey_mycroft`
  - `hey_rhasspy`
  - `timer`
  - `weather`
- **Veronica Model Status: BLOCKED (Honest Reporting)**:
  - "Veronica" is not among openWakeWord's included models.
  - Training Mechanism: `openwakeword.custom_verifier_model.train_custom_verifier`.
  - Target Path: `models/veronica_wake_model.tflite` (configured in `VoiceConfig.wake_model_path`).
  - Strict Rule: No renamed models or fake triggers. The engine reports `running = False` and `detect() = False` until a verified custom model is provided.

### 4. Safety and Architecture Invariants
- Voice remains strictly an I/O transport layer.
- `PermissionGate`, `AutonomyPolicy`, `ExecutionAuthorization`, and `ComputerUseLoop` are completely untouched.
- `VoiceConfig.enabled` and `VoiceConfig.continuous_listening` remain `False` by default (opt-in).
- No secrets logged; `.env` separation preserved.
- Phase 11F is not started.
