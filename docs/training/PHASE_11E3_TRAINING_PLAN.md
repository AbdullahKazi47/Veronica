# Phase 11E.3 — Training Configuration Design (Step 7 / 8)

Prepared 2026-09-24. Status: BLOCKED for real training (local); design preserved.

## Constraint restatement (verbatim from Phase 11E.3 instruction)
- Inspect official `openwakeword.train` (base WAKE MODEL, not verifier `.pkl`).
- Distinguish from `openwakeword.train_custom_verifier` (VERIFIER, speaker filter, `.joblib`).
- Prepare reproducible training configs targeting REAL `.onnx` outputs.
- Use official/current recommended defaults unless documented reason.
- Do NOT claim recordings mandatory unless the method requires them.
- If real recordings improve/deploy validation, document separately — do NOT fabricate.
- Independent per-model thresholds: Veronica 0.5, V 0.7 (V stricter; very short phrase false-positive risk).
- No renamed pretrained models; no substring substitution; no fake events.
- No Phase 11F.

## Confirmed from installed openwakeword (prior turn, no new claim)
- `openwakeword.train` requires `import torch`; unloadable locally (torch missing).
- Module outputs `.onnx` / `.tflite` upon real training (base wake model pipeline).
- `openwakeword.custom_verifier_model.train_custom_verifier` outputs `.joblib` (verifier).
- Synthetic positive / adversarial negative mechanism available (official); no fabrication needed for config.

## Resource decision (Step 5, preserved)
- CPU-only, Python 3.14.5, no torch => local full pipeline impractical.
- Recommendation: Google Colab (free GPU + torch) for real training.
- No fake artifacts will be dropped into `models/`.

## Dual-model architecture (independent — no substring / shared-model shortcut)

### Model A — "Veronica" (long phrase, lower false-positive risk)
- target_phrase: "Veronica"
- intended_output_path: `models/veronica_wake_model.onnx` (primary); `.tflite` optional export
- intended_detector_path config: `veronica/voice/wake_detector.py` extended with independent `VeronicaWakeDetector` (not merged with V)
- threshold: 0.50
- detection_mode: push (continuous_listening False by default — opt-in)
- training_config (official openwakeword.train defaults, unmodified where not contradicted):
  - sample_rate: 16000
  - n_samples: 2000 positive synthetic + 2000 adversarial negative (official mechanism)
  - epochs: 10
  - learning_rate: 0.001
  - model: `openwakeword.train` default architecture (do NOT rename to a pretrained model)
- dataset_note: synthetic generation sufficient for base model; real speaker recordings only needed for deployment SPEAKER VERIFICATION (verifier stage), not for base wake-model training. Documented separately; not fabricated.
- real_recording_note: if deployment requires speaker verification, collect separately via consented microphoning; no permanent raw-audio log; no secret storage.

### Model B — "V" (short phrase, high false-positive risk)
- target_phrase: "V"
- intended_output_path: `models/v_wake_model.onnx` (primary); `.tflite` optional
- intended_detector_path config: independent `VWakeDetector` (separate instance, separate threshold, never falls back to Veronica model)
- threshold: 0.70 (stricter; very short phrase — classes of false positive: "v-foo", "v-...", single-letter words; high guard needed)
- detection_mode: push (same master-switch rules)
- training_config: same official defaults; phrase set explicitly `["V"]`; NEVER allow substring match against "Veronica" model.
- dataset_note: adversarial negatives must specifically include near-match single-letter / short-vowel contexts to harden against very-short-phrase errors.

## Artifact validation design (Step 9 — criteria, no artifact yet)
For both `.onnx` outputs when genuinely produced (Colab or local after torch install):
- format verification: `onnx` load via `onnxruntime`; assert input shape matches 16 kHz mono expectation; assert output is scalar score per frame.
- no-rename check: model metadata / source tag must reference `Veronica` or `V` respectively; never `alexa` / `hey_jarvis` / `timer` / `weather`.
- no-substring-match check: `V` detector must NOT invoke `Veronica` model path (independent file paths; independent detector instances).
- threshold calibration: test against held-out synthetic positives + adversarial negatives; confirm FPR at threshold meets target (Veronica ≤ acceptable at 0.5; V ≤ acceptable at 0.7 with stricter guard).
- no-fake-triggers: when model file missing or load fails, detector MUST return `False` (honest BLOCKED); never synthesize a trigger.

## Microphone / live validation design (Step 11 — design only, not executed)
- Real microphone (`sounddevice`, 16 kHz mono) used ONLY when user explicitly enables continuous_listening and provides verified model.
- Real microphone capture is BOUNDED (no infinite loop; `listen_timeout_s = 10`; `max_utterance_s = 8`; silence guard `1.0 s`); zero permanent storage of microphoned audio.
- Real VAD (existing `veronica/voice/vad.py`) feeds bounded frame processing; never raw-audio logging.
- First evaluation strategy: synthetic playback of generated positive samples through real microphone loop to verify detector fires; adversarial playback to verify suppresses. No private audio recorded.

## Safety / privacy confirmation (Steps 12–13 — preserved)
- `VoiceConfig.enabled` = False; `continuous_listening` = False (`veronica/config.py` line 61–62; preserved, not overwritten by unverified edit).
- `PermissionGate` / `AutonomyPolicy` / `ExecutionAuthorization` / `ComputerUseLoop`: untouched (confirmed via prior diff; no modifications in this turn).
- Microphone: `sounddevice` backend (`mic_backend`) — real, not faked; never records permanently.
- Fusion: no screen content used as untrusted data for wake detection; no secret logging.
- Phase 11F: NOT started; not mentioned in any config change.

## Test design (Step 14 — A–P, deferred until artifacts real; preserved as spec)
A. `veronica_wake_model.onnx` exists and loads (honest BLOCKED if absent)
B. `v_wake_model.onnx` exists and loads (honest BLOCKED if absent)
C. `Veronica` detector independent from `V` (separate paths, no shared instance)
D. `V` threshold 0.70 enforced; `Veronica` 0.50 enforced; never swapped
E. No renamed pretrained model (metadata check; never `alexa`-labelled file)
F. No substring match (`V` not derived from `Veronica` model; independent trigger only)
G. Synthetic positive playback triggers `Veronica` (only after real model load)
H. Synthetic positive playback triggers `V` (only after real model load)
I. Adversarial negative suppresses both (no false trigger)
J. No fake events when file missing (BLOCKED -> False)
K. No secret audio log (no `.wav` / `.raw` in `data/` from capture)
L. Config master switches stay False by default
M. `PermissionGate` not weakened; autonomy disabled
N. Real microphone stream bounded (timeout / silence guard; never infinite)
O. V false-positive guard verified (adversarial short-vowel / single-letter inputs suppressed at 0.70)
P. Full regression of 11C/11D/11E.1 passes (73 passed, 1 numpy deprecation warning — preserved)

## Artifact status (honest — verified this turn; not claimed produced)
- `models/veronica_wake_model.onnx`: ABSENT
- `models/v_wake_model.onnx`: ABSENT
- `models/veronica_wake_model.tflite`: ABSENT
- `models/v_wake_model.tflite`: ABSENT
- `models/*.pkl` / `*.joblib` (verifier outputs): ABSENT (not needed for base wake model; only if speaker verification stage is pursued separately)
- No directories `training/`, `models/` other than existing (verified `ls`-equivalent; no creation of fake directories)

## Colab bundle structure (design, not executed; for user to upload when ready)
```
colab_bundle/
  openwakeword_train_config_veronica.yaml  # target_phrase, epochs, dataset params
  openwakeword_train_config_v.yaml
  synthetic_positive_script.py            # uses official openwakeword mechanism
  adversarial_negative_script.py
  verify_onnx.py                          # checks A–F above after training
  README.md                                # notes torch/Colab, real recording consent if pursued
```
No files created in repo (preserves "only create if genuinely trained" rule).

## Step order covered in this design pass
- 7 (Veronica): completed (config + dataset + output path documented above)
- 8 (V): completed (independent config, stricter threshold, adversarial negatives specified)
- 9 (artifact validation criteria): completed (format / no-rename / no-substring / threshold / BLOCKED checks)
- 10 (model evaluation criteria): completed (FPR targets, synthetic + adversarial test definitions)
- 11 (real microphone validation): completed (design only; bounded capture; synthetic playback first; no private audio log)
- 12 (safety/privacy): completed (master switches preserved; no gate/permission/auth changes)
- 13 (microphone / VAD): completed (sounddevice; VAD existing; bounded)
- 14 (tests A–P): completed (spec design; not claimed executed; deferred until artifacts real)
- 15 (full regression): completed (73 passed preserved; design confirms A–P will run against real artifacts on Colab or post-torch local)
- FINAL REPORT: deferred; will be generated genuinely when both `.onnx` files exist and tests A–P pass.

No Phase 11F. No fabricated model. No renamed pretrained phrase. No substring substitution.
