# Phase 11E.4 — Google Colab Training Workflow (Step 2 / Step 6)
# Status: DESIGN / NOT EXECUTED locally (torch unavailable; CPU-only Windows impractical)
# Required for REAL artifact: veronica_wake_model.onnx

> **Superseded (2026-10-15): use `VERONICA_WAKE_TRAINING_COLAB.ipynb` (repo root).**
> Models from `PHASE_11E4_COLAB_NOTEBOOK_CLEAN.ipynb` scored 0.999 on pure silence:
> that notebook set `feature_data_files: {}`, so training had no general negative audio.
> v2 adds the ACAV100M negative features, AudioSet/FMA background, MIT RIRs, pinned
> Python 3.11 deps, exports one self-contained `.onnx` (no `.onnx.data`), and fails its
> final cell if the model fires on silence/noise. `voice_check.py` applies the same silence gate.

## 1. Why Colab (honest resource assessment)
- Local environment: Windows 11, Python 3.14.5, Intel CPU, Intel Iris Xe, NO torch installed.
- `openwakeword.train.train.py` begins with `import torch`; module is unloadable locally.
- Training involves PyTorch model training (auto_train), feature extraction (AudioFeatures), and ONNX export (`torch.onnx.export`). GPU is strongly preferred for 50000 steps and feature computation.
- No fake/local placeholder will be used.

## 2. Colab notebook steps (reproducible)
```python
# Cell 1 — Environment
!pip install openwakeword torch onnx soundfile scipy tqdm pyyaml numpy
# Clone official openwakeword repo for generate_samples.py / generate_adversarial_texts / piper
!git clone https://github.com/dscripka/openwakeword.git
# Or use pip-installed package paths if already present; this ensures piper_sample_generator_path is available.

# Cell 2 — Upload config
# Upload: config/training/veronica_train_config.yaml (provided in repo; see Step 3)
# Adjust paths inside the YAML to reference Colab-mounted directories if needed.

# Cell 3 — Generate clips (official synthetic mechanism; NO fabricated recordings)
import subprocess, os
# Generate positive + adversarial negative clips using official openwakeword mechanism
subprocess.run(["python", "-m", "openwakeword.train",
                "--training_config", "veronica_train_config.yaml",
                "--generate_clips"], check=True)

# Cell 4 — Augment and compute features
subprocess.run(["python", "-m", "openwakeword.train",
                "--training_config", "veronica_train_config.yaml",
                "--augment_clips", "--overwrite"], check=True)

# Cell 5 — Train
subprocess.run(["python", "-m", "openwakeword.train",
                "--training_config", "veronica_train_config.yaml",
                "--train_model"], check=True)

# Cell 6 — Verify artifacts (Step 7 — only consider complete when BOTH exist and load)
import onnx, os
onnx_path = "veronica_wake_model/verification/veronica_wake_model.onnx"  # adjust per config output_dir
assert os.path.isfile(onnx_path), "ONNX artifact missing — NOT complete"
model = onnx.load(onnx_path)
onnx.checker.check_model(model)
print("Genuine ONNX verified:", onnx_path, "size:", os.path.getsize(onnx_path), "bytes")
# If .tflite also requested: verify separately; not required for wake-word operation.
```

## 3. After artifact produced (Step 6 / Step 7 — real validation required)
- Download `veronica_wake_model.onnx` (and optionally `.tflite`) from Colab.
- Place at `models/veronica_wake_model.onnx`.
- Load with `onnxruntime`; confirm scalar score output; confirm input dimension matches 16 kHz mono.
- Confirm no renamed pretrained model (metadata / source tag reference `veronica`; never `alexa` / `hey_jarvis` / `timer` / `weather`).
- Confirm no placeholder (non-empty file; bytes > 0; valid ONNX graph).
- Confirm file does NOT correspond to any stock pretrained phrase; only genuinely trained `Veronica`.
