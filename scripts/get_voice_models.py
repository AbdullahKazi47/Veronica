"""Download the Kokoro voice model (VERONICA's default voice) into data/models/kokoro/.

    python scripts/get_voice_models.py

The model is too big for a GitHub repository (325 MB; GitHub refuses files over 100 MB),
so it comes from the official kokoro-onnx release instead and is checked against the
SHA-256 of the files VERONICA was tested with. Already-downloaded files are skipped.
Kokoro is Apache-2.0 (github.com/hexgrad/kokoro).
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import requests

RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
FILES = {
    "kokoro-v1.0.onnx": (325532387, "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"),
    "voices-v1.0.bin": (28214398, "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"),
}
TARGET = Path(__file__).resolve().parent.parent / "data" / "models" / "kokoro"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch(name: str, size: int, digest: str) -> bool:
    out = TARGET / name
    if out.exists() and out.stat().st_size == size and sha256(out) == digest:
        print(f"{name}: already here")
        return True
    part = out.with_suffix(out.suffix + ".part")
    print(f"{name}: downloading {size / 1e6:.0f} MB ...")
    with requests.get(RELEASE + name, stream=True, timeout=60) as r:
        r.raise_for_status()
        done = 0
        with open(part, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                done += len(chunk)
                print(f"\r  {done * 100 // size}%", end="", flush=True)
    print()
    if part.stat().st_size != size or sha256(part) != digest:
        part.unlink()
        print(f"{name}: download didn't match the expected file; removed it.")
        return False
    part.replace(out)
    print(f"{name}: ok")
    return True


def main() -> int:
    TARGET.mkdir(parents=True, exist_ok=True)
    ok = all(fetch(name, size, digest) for name, (size, digest) in FILES.items())
    print("Kokoro voice ready." if ok else "Some files failed; run this again.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
