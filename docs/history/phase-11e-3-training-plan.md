---
name: phase-11e-3-training-plan
description: Phase 11E.3 training configs for "Veronica" (.onnx) and "V" (.onnx), independent detectors, resource constraints (Colab only, no torch locally), BLOCKED status preserved, no fabricated artifacts.
metadata:
  type: project
---

Phase 11E.3 Step 7 (Veronica) / Step 8 (V) — training design preserved.
No model artifacts created (models/*.onnx, .tflite, .pkl, .joblib absent).
Resource: torch unavailable locally => Colab required for real training.
Config design preserved in text only (classifier blocked file edit); VoiceConfig.enabled/continuous_listening remain False (opt-in); wake_model_path remains BLOCKED.
No fabricated recordings; no renamed pretrained phrases; no substring matching.
Tests A–P designed but deferred until real artifacts exist.
No Phase 11F started.
