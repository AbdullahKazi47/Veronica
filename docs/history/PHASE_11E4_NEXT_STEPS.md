# Phase 11E.4 — Next Steps (Step 6: Colab Execution)

Steps 1-5 are complete:
- Step 1: Inspected official `openwakeword.train` source (torch dependency, .onnx export, synthetic generation).
- Step 2: Designed Colab workflow (docs/training/COLAB_WORKFLOW.md).
- Step 3: Prepared genuine config (config/training/veronica_train_config.yaml).
- Step 4: Confirmed official synthetic mechanism (no fabricated recordings).
- Step 5: Documented custom negatives rationale (docs/training/PHASE_11E4_CUSTOM_NEGATIVES.md).

**Step 6: Execute Colab workflow to produce real `veronica_wake_model.onnx`**

Follow the exact steps below in a Google Colab notebook (GPU runtime recommended). No local execution is possible due to missing torch on Windows CPU-only environment.

```
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

After successful execution:
1. Download `veronica_wake_model.onnx` (and optionally `.tflite`) from Colab.
2. Place at `models/veronica_wake_model.onnx`.
3. Validate with `onnxruntime` (Step 7).
4. Proceed to offline evaluation (Step 8), real microphone evaluation (Step 9), false-positive measurement (Step 10), configuration (Step 11), headless validation (Step 12), test updates (Step 15), full regression (Step 16), and final report.

**Important**: Do not create placeholder or fake artifacts. Only proceed when the genuine ONNX file exists and loads correctly.