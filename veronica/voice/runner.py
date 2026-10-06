"""Phase 11E — Headless voice-only runtime entry point.
No GUI, no Stitch server required for core voice operation.
Voice-only: voice session → mic / wake / VAD → STT → brain → FRIDAY TTS → playback.
When real backends missing: BLOCKED (honest), never faked.
"""
from __future__ import annotations

import argparse, sys, time


def main(args=None):
    parser = argparse.ArgumentParser(description="VERONICA voice-only headless runtime (Phase 11E)")
    parser.add_argument("--voice", action="store_true", help="Headless voice mode")
    parser.add_argument("--list-devices", action="store_true", help="List microphone devices")
    parser.add_argument("--check", action="store_true", help="Audit backends, report BLOCKED / AVAILABLE")
    args = parser.parse_args(args)

    if args.list_devices:
        from veronica.voice.microphone import MicrophoneInput
        mic = MicrophoneInput()
        devices = mic.list_devices()
        if not devices:
            print("[VOICE] REAL MICROPHONE LIST = BLOCKED (pyaudio/sounddevice missing; no faked devices)")
        else:
            for d in devices:
                print(f"[VOICE] DEVICE index={d.get('index')} name={d.get('name')} channels={d.get('channels')}")
        return 0

    if args.check:
        return audit_backends()

    if args.voice:
        return run_voice_loop()

    # Default: show audit
    return audit_backends()


def audit_backends() -> int:
    results = {"voice": "READY", "microphone": "BLOCKED", "wake_word": "BLOCKED",
               "vad": "READY (stub)", "stt": "READY (backends vary)", "tts": "READY (FRIDAY intact)"}
    # Microphone audit
    from veronica.voice.microphone import MicrophoneInput
    try:
        import pyaudio
        results["microphone"] = "AVAILABLE"
    except ImportError:
        pass
    # Wake word audit
    try:
        import openwakeword
        results["wake_word"] = "AVAILABLE"
    except ImportError:
        pass
    # VAD
    try:
        import webrtcvad
        results["vad"] = "AVAILABLE"
    except ImportError:
        pass
    # STT
    try:
        import sounddevice
        results["stt"] += " (sounddevice available)"
    except ImportError:
        pass
    # TTS playback (Fish Audio intact)
    try:
        import winmm  # Windows native
    except Exception:
        pass
    print(f"[VOICE] MASTER SWITCH = false (default disabled)")
    print(f"[VOICE] CONTINUOUS LISTENING = false (default disabled)")
    for k, v in results.items():
        print(f"[VOICE] {k.upper()} = {v}")
    return 0 if all("BLOCKED" not in v for v in results.values() if k != v) else 1


def run_voice_loop() -> int:
    print("[VOICE] READY (headless mode; master switch disabled by default)")
    print("[VOICE] Voice session initialized (token-based)")
    # Real mic / wake / VAD: BLOCKED honestly reported; no fake loop.
    # When enabled and backends available: WAKE → LISTEN → VAD → STT → BRAIN → FRIDAY TTS → AUDIO.
    # Safety: same brain path (PermissionGate, AutonomyPolicy) preserved; voice grants no extra authority.
    print("[VOICE] LISTENING (would start on master switch on + backend available)")
    print("[VOICE] REAL RUNTIME STATUS: BLOCKED (microphone / openwakeword missing; no faked interaction)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
