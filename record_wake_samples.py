"""
Record your own voice for wake-word training (upload the zip to the Colab notebook).

The synthetic training voices are American English; a model trained only on them can
miss other accents. Real clips of *you* saying "Veronica" (positives) and saying other
things (negatives) fix that.

Run from the project root:

    python record_wake_samples.py                 # 40 "Veronica" + 25 other phrases
    python record_wake_samples.py --positives 20 --negatives 10

Output: veronica/training_voice/positive/*.wav, negative/*.wav and my_voice.zip
(16 kHz mono 16-bit, the format openWakeWord training expects).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import wave
import zipfile

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

RATE = 16000
CHUNK = 1280  # 80 ms

# Varying how you say it teaches the model your real range, not one perfect take.
POSITIVE_STYLES = [
    "normal voice", "normal voice", "a bit quieter", "a bit louder",
    "slowly", "quickly", "like calling from across the room", "turned slightly away from the laptop",
]

# Your normal speech, plus near-misses: the model must NOT wake on these.
NEGATIVE_PHRASES = [
    "What time is it", "Open Chrome", "Play some music", "Very nice", "Monica",
    "Verona", "Veronique", "Harmonica", "America", "Erica", "Ronnie come here",
    "Remind me tomorrow", "What's the weather today", "Hello how are you",
    "Turn up the volume", "Electronica", "Veranda", "Can you hear me",
    "Send a message", "Okay thank you", "Victoria", "Very good", "Rebecca",
    "Open the file", "Search for something",
]


def rms(a: np.ndarray) -> float:
    return float(np.sqrt(np.mean(a.astype(np.float64) ** 2))) if a.size else 0.0


def record(mic, seconds: float) -> np.ndarray:
    frames = [np.frombuffer(mic.read(), dtype=np.int16) for _ in range(int(seconds * RATE / CHUNK))]
    return np.concatenate(frames) if frames else np.zeros(0, np.int16)


def drain(mic, seconds: float) -> None:
    for _ in range(int(seconds * RATE / CHUNK)):
        mic.read()


def trim(pcm: np.ndarray, noise: float, pad_s: float = 0.25) -> np.ndarray | None:
    """Keep the spoken part plus a little padding; None if nothing was said."""
    f = 320  # 20 ms frames
    e = np.array([rms(pcm[i:i + f]) for i in range(0, len(pcm) - f + 1, f)])
    voiced = np.where(e > max(noise * 3.0, 300.0))[0]
    if voiced.size < 5:  # under ~100 ms of speech
        return None
    pad = int(pad_s * RATE)
    start = max(0, voiced[0] * f - pad)
    end = min(len(pcm), (voiced[-1] + 1) * f + pad)
    return pcm[start:end]


def save(path: str, pcm: np.ndarray) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.astype(np.int16).tobytes())


def take(mic, prompt: str, seconds: float, noise: float, max_len_s: float) -> np.ndarray:
    while True:
        print(f"\n  >> {prompt}")
        print("     3... ", end="", flush=True); drain(mic, 0.6)
        print("2... ", end="", flush=True); drain(mic, 0.6)
        print("1... ", end="", flush=True); drain(mic, 0.6)
        print("SPEAK", flush=True)
        clip = trim(record(mic, seconds), noise)
        if clip is None:
            print("     (didn't hear anything - let's redo that one)")
            continue
        if len(clip) > max_len_s * RATE:
            print("     (too long - say just the phrase once; redo)")
            continue
        print(f"     ok  ({len(clip) / RATE:.2f} s, level {rms(clip):.0f})")
        return clip


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--positives", type=int, default=40)
    ap.add_argument("--negatives", type=int, default=25)
    args = ap.parse_args()

    from veronica.config import load_config, DEFAULT_CONFIG_PATH, DEFAULT_ENV_PATH
    from veronica.voice.microphone import MicrophoneInput

    cfg = load_config(DEFAULT_CONFIG_PATH, DEFAULT_ENV_PATH)
    dev = cfg.stt.mic_device_index
    mic = MicrophoneInput(device_index=dev, chunk_size=CHUNK, rate=RATE) if dev is not None \
        else MicrophoneInput(chunk_size=CHUNK, rate=RATE)
    if not mic.open():
        print("FAIL  could not open microphone (check stt.mic_device_index in config/config.yaml)")
        return 1

    out = os.path.join(ROOT, "veronica", "training_voice")
    pos_dir, neg_dir = os.path.join(out, "positive"), os.path.join(out, "negative")
    os.makedirs(pos_dir, exist_ok=True)
    os.makedirs(neg_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    try:
        print("Stay quiet for 2 seconds (measuring room noise)...")
        noise = float(np.percentile([rms(record(mic, 0.1)) for _ in range(20)], 50))
        print(f"room noise level: {noise:.0f}")

        print(f"\nPART 1 of 2: say \"Veronica\" ONCE each time ({args.positives} takes).")
        for i in range(args.positives):
            style = POSITIVE_STYLES[i % len(POSITIVE_STYLES)]
            clip = take(mic, f"[{i + 1}/{args.positives}] Say \"Veronica\" - {style}", 2.0, noise, 1.8)
            save(os.path.join(pos_dir, f"me_{stamp}_{i:03d}.wav"), clip)

        print(f"\nPART 2 of 2: read each phrase ONCE ({args.negatives} takes). Do NOT say \"Veronica\".")
        for i in range(args.negatives):
            phrase = NEGATIVE_PHRASES[i % len(NEGATIVE_PHRASES)]
            clip = take(mic, f"[{i + 1}/{args.negatives}] Say: \"{phrase}\"", 3.0, noise, 2.9)
            save(os.path.join(neg_dir, f"me_{stamp}_{i:03d}.wav"), clip)
    except KeyboardInterrupt:
        print("\nStopped early - the clips recorded so far are kept.")
    finally:
        mic.close()

    zpath = os.path.join(out, "my_voice.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for sub in ("positive", "negative"):
            for name in sorted(os.listdir(os.path.join(out, sub))):
                if name.endswith(".wav"):
                    z.write(os.path.join(out, sub, name), f"{sub}/{name}")
    n_pos = len([n for n in os.listdir(pos_dir) if n.endswith(".wav")])
    n_neg = len([n for n in os.listdir(neg_dir) if n.endswith(".wav")])
    print(f"\nDONE  {n_pos} \"Veronica\" clips, {n_neg} other clips")
    print(f"Upload this file in the Colab notebook (cell 5b): {zpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
