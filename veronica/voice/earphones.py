"""Earphones mode: in public, with earphones in, a quiet "Veronica" is enough.

An earphone mic sits close to your mouth, so even a soft voice is far louder there than
the people around you. At start-up this finds that mic (a Bluetooth headset, or a headset
in the jack) and listens through it instead of the laptop's own mic, boosts quiet speech
(voice/microphone.py `boost`) and lowers the wake-word and "loud enough" bars to match.
Settings: voice.earphones in config/config.yaml ("auto" | "on" | "off").
"""
from __future__ import annotations

from typing import Optional

from veronica.logging_setup import get_logger

logger = get_logger("voice.earphones")

_JACK = "jack mic"


def find_mic(names: list[str], devices=None, hostapis=None, default_input: Optional[int] = None) -> Optional[str]:
    """Name of the connected earphone mic, or None.

    Windows' default input wins when it is one (Windows picks the headset you just
    connected). A jack mic counts only then: the jack's input is listed even when the
    earphones plugged into it have no mic."""
    try:
        if devices is None or hostapis is None:
            import sounddevice as sd
            devices, hostapis = sd.query_devices(), sd.query_hostapis()
            default_input = sd.default.device[0]
    except Exception:  # noqa: BLE001
        return None
    wanted = [n.lower() for n in names if n]
    inputs = [(i, d.get("name", "")) for i, d in enumerate(devices) if d.get("max_input_channels", 0) > 0]

    def is_earphone(name: str) -> bool:
        return any(w in name.lower() for w in wanted)

    default_name = next((n for i, n in inputs if i == default_input), "")
    if default_name and is_earphone(default_name):
        return default_name.strip()
    hit = next((n for _, n in inputs if is_earphone(n) and _JACK not in n.lower()), None)
    return hit.strip() if hit else None


def apply(config, finder=find_mic) -> Optional[str]:
    """Switch to earphones mode if earphones are in (or mode is "on"). Returns what to
    tell the user, or None when it stays off."""
    ear = getattr(config.voice, "earphones", None)
    mode = (getattr(ear, "mode", "off") or "off").lower()
    if ear is None or mode == "off":
        return None
    mic = finder(list(ear.mic_names))
    if mic is None and mode != "on":
        return None
    if mic:
        config.stt.mic_device_name = mic
    from veronica.voice.microphone import set_input_gain
    set_input_gain(ear.gain)
    config.voice.wake_threshold = min(config.voice.wake_threshold, ear.wake_threshold)
    config.stt.min_speech_rms = min(config.stt.min_speech_rms, ear.min_speech_rms)
    config.stt.speech_over_ambient = min(config.stt.speech_over_ambient, ear.speech_ratio)
    config.voice.conversation_speech_ratio = min(config.voice.conversation_speech_ratio, ear.speech_ratio)
    logger.info("Earphones mode on (mic=%s, gain=%s, wake=%s).", mic or "default", ear.gain,
                config.voice.wake_threshold)
    where = f"your earphone mic ({mic})" if mic else "the microphone"
    return f"Earphones mode: I'm listening through {where}, so you can say my name quietly."
