"""
Phase 11F - staged voice diagnostic for VERONICA.

Run from the project root (C:\\Users\\HomePC\\Desktop\\Veronica) with the SAME python
you used for the microphone tests:

    python voice_check.py              # all stages
    python voice_check.py --skip-stt   # mic + wake word only
    python voice_check.py --skip-wake  # mic + STT only

Each stage prints PASS / FAIL / SKIP with real measured values. Nothing is faked.
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys
import time
import traceback
import wave

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def hdr(title: str) -> None:
    print("\n" + "=" * 64)
    print("  " + title)
    print("=" * 64)


def stage_env() -> None:
    hdr("STAGE 0  Python + packages")
    print(f"python     : {sys.executable}")
    print(f"version    : {sys.version.split()[0]}")
    for mod in ("numpy", "sounddevice", "speech_recognition", "onnxruntime",
                "openwakeword", "faster_whisper", "pyaudio"):
        try:
            m = importlib.import_module(mod)
            print(f"  [ok]      {mod:<20} {getattr(m, '__version__', '')}")
        except Exception as e:
            print(f"  [missing] {mod:<20} ({type(e).__name__})")
    print("(pyaudio missing is EXPECTED and fine now.)")


def stage_config():
    hdr("STAGE 1  Config")
    from veronica.config import load_config, DEFAULT_CONFIG_PATH, DEFAULT_ENV_PATH
    cfg = load_config(DEFAULT_CONFIG_PATH, DEFAULT_ENV_PATH)
    print(f"stt.backend          = {cfg.stt.backend}")
    print(f"stt.mic_backend      = {cfg.stt.mic_backend}")
    print(f"stt.mic_device_name  = {getattr(cfg.stt, 'mic_device_name', None)}")
    print(f"stt.mic_device_index = {cfg.stt.mic_device_index}  (fallback)")
    print(f"stt.min_speech_rms   = {cfg.stt.min_speech_rms}")
    print(f"stt.language         = {cfg.stt.language}")
    print(f"voice.wake_model_path= {cfg.voice.wake_model_path}")
    return cfg


def stage_mic(cfg) -> bool:
    hdr("STAGE 2  Microphone levels (VERONICA MicrophoneInput)")
    import numpy as np
    from veronica.voice.microphone import MicrophoneInput
    from veronica.voice.microphone import resolve_input_device
    dev = resolve_input_device(getattr(cfg.stt, "mic_device_name", None), cfg.stt.mic_device_index)
    mic = MicrophoneInput(device_index=dev)
    if not mic.open():
        print("FAIL  could not open microphone")
        return False
    print(f"opened device={mic.device_index} backend={mic._backend}")

    def level(seconds: float) -> tuple[float, int]:
        n = int(seconds * mic.rate / mic.chunk_size)
        rms_vals, peak = [], 0
        for _ in range(n):
            a = np.frombuffer(mic.read(), dtype=np.int16)
            if a.size:
                rms_vals.append(float(np.sqrt(np.mean(a.astype(np.float64) ** 2))))
                peak = max(peak, int(np.max(np.abs(a))))
        return (max(rms_vals) if rms_vals else 0.0), peak

    try:
        print("Stay QUIET for 2 seconds...")
        q_rms, q_peak = level(2.0)
        print(f"  quiet : max RMS={q_rms:7.1f}  peak={q_peak}")
        print("Now SPEAK normally for 3 seconds (e.g. count 'one two three')...")
        s_rms, s_peak = level(3.0)
        print(f"  speech: max RMS={s_rms:7.1f}  peak={s_peak}")
    finally:
        mic.close()

    floor = cfg.stt.min_speech_rms
    needed = max(q_rms * 2.5, floor)
    if s_rms >= needed:
        print(f"PASS  speech level {s_rms:.0f} clears the start threshold (~{needed:.0f}).")
        return True
    print(f"WARN  speech level {s_rms:.0f} is below the start threshold (~{needed:.0f}).")
    print(f"      Fix: raise mic gain in Windows Sound settings, or set "
          f"stt.min_speech_rms to about {max(30, int(s_rms * 0.6))} in config/config.yaml.")
    return False


def stage_stt(cfg) -> bool:
    hdr(f"STAGE 3  Speech-to-text (backend={cfg.stt.backend})")
    from veronica.speech.stt import SpeechToText
    stt = SpeechToText(cfg.stt)
    print("Say a short sentence now, e.g. 'what time is it' (waiting up to 8 s)...")
    t0 = time.time()
    res = stt.listen_once(timeout_s=8)
    dt = time.time() - t0
    print(f"capture stats: {stt.last_capture_stats}")
    if res.success:
        print(f"PASS  heard: \"{res.text}\"  ({dt:.1f}s)")
        return True
    print(f"FAIL  {res.error}  ({dt:.1f}s)")
    return False


def stage_wake(cfg, live: bool) -> bool:
    hdr("STAGE 4  Wake-word model (openWakeWord, ONNX)")
    path = str(cfg.resolve_path(cfg.voice.wake_model_path))
    print(f"model path : {path}")
    if not os.path.isfile(path):
        print("FAIL  model file not found at that path")
        return False
    data = path + ".data"
    print(f"external weights (.onnx.data): {'present' if os.path.isfile(data) else 'not present'}")

    try:
        import numpy as np
        from openwakeword.model import Model
    except Exception as e:
        print(f"FAIL  openwakeword import: {e}")
        return False

    try:
        model = Model(wakeword_models=[path], inference_framework="onnx")
    except Exception as e:
        print(f"FAIL  model load: {type(e).__name__}: {e}")
        msg = str(e).lower()
        if "melspectrogram" in msg or "embedding" in msg or "no such file" in msg:
            print("      openWakeWord's shared feature models may be missing. Run once:")
            print("      python -c \"import openwakeword.utils as u; u.download_models()\"")
        return False
    keys = list(model.models.keys())
    print(f"PASS  model loaded; prediction keys = {keys}")
    key = keys[0]

    def score_pcm(pcm) -> float:
        model.reset()
        best = 0.0
        for i in range(0, len(pcm) - 1280 + 1, 1280):
            p = model.predict(pcm[i:i + 1280])
            best = max(best, float(p.get(key, 0.0)))
        return best

    sample = os.path.join(ROOT, "veronica", "veronica_real_voice.wav")
    if os.path.isfile(sample):
        with wave.open(sample, "rb") as w:
            sr_, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if sr_ == 16000 and ch == 1 and sw == 2:
            pcm = np.frombuffer(raw, dtype=np.int16)
            print(f"recorded sample score : {score_pcm(pcm):.4f}  ({os.path.basename(sample)})")
        else:
            print(f"skip sample: wav is {sr_} Hz / {ch} ch / {sw * 8}-bit (needs 16k mono 16-bit)")

    # A model that fires on pure silence fires on everything, so a high live
    # score proves nothing. Never report that as a PASS.
    silence = score_pcm(np.zeros(16000 * 3, dtype=np.int16))
    print(f"silence score         : {silence:.4f}")
    if silence >= 0.5:
        print("FAIL  model triggers on SILENCE - it would wake on any sound.")
        print("      The .onnx and .onnx.data are probably from different training runs,")
        print("      or the training itself is broken. Re-export one self-contained .onnx.")
        return False

    if live:
        from veronica.voice.microphone import MicrophoneInput
        from veronica.voice.microphone import resolve_input_device
        dev = resolve_input_device(getattr(cfg.stt, "mic_device_name", None), cfg.stt.mic_device_index)
        mic = MicrophoneInput(device_index=dev, chunk_size=1280)
        if mic.open():
            seconds = 10
            try:
                print("LIVE: get ready to say \"Veronica\" (pause ~1 s between each)...")
                for n in (3, 2, 1):
                    print(f"  {n}...", flush=True)
                    for _ in range(int(16000 / 1280)):   # keep the stream drained while counting
                        mic.read()
                print(f"  SPEAK NOW - say \"Veronica\" 3 times ({seconds} s)", flush=True)
                frames = [np.frombuffer(mic.read(), dtype=np.int16) for _ in range(int(seconds * 16000 / 1280))]
            finally:
                mic.close()
            pcm = np.concatenate(frames)

            # Save what the mic heard so a miss can be checked by ear / re-scored offline.
            out = os.path.join(ROOT, "veronica", "live_wake_test.wav")
            with wave.open(out, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
                w.writeframes(pcm.tobytes())

            # Per-second max score shows *when* (if ever) the model reacted.
            model.reset()
            per_sec = [0.0] * seconds
            for i in range(0, len(pcm) - 1280 + 1, 1280):
                s = float(model.predict(pcm[i:i + 1280]).get(key, 0.0))
                sec = min(i // 16000, seconds - 1)
                per_sec[sec] = max(per_sec[sec], s)
            live_score = max(per_sec)
            rms = float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))
            print("  per-second score: " + " ".join(f"{s:.2f}" for s in per_sec))
            print(f"live mic score        : {live_score:.4f}  (detector threshold 0.5, recording RMS {rms:.0f})")
            print(f"saved recording       : {out}")
            if live_score >= 0.5:
                print("PASS  live wake detected")
            else:
                print("WARN  live score under 0.5 - play the saved recording to check it contains \"Veronica\"")
                return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-stt", action="store_true")
    ap.add_argument("--skip-wake", action="store_true")
    ap.add_argument("--no-live-wake", action="store_true")
    a = ap.parse_args()

    stage_env()
    try:
        cfg = stage_config()
    except Exception:
        traceback.print_exc()
        return 1
    results = {}
    for name, fn in (("mic", lambda: stage_mic(cfg)),
                     ("stt", None if a.skip_stt else (lambda: stage_stt(cfg))),
                     ("wake", None if a.skip_wake else (lambda: stage_wake(cfg, not a.no_live_wake)))):
        if fn is None:
            results[name] = "SKIP"
            continue
        try:
            results[name] = "PASS" if fn() else "FAIL/WARN"
        except Exception:
            traceback.print_exc()
            results[name] = "ERROR"
    hdr("SUMMARY")
    for k, v in results.items():
        print(f"  {k:<5} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
