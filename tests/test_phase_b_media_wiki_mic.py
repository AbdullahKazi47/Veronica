"""Phase B: media keys, Wikipedia quick facts, microphone chosen by name."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from veronica.safety.permissions import PermissionGate
from veronica.tools.media_tools import MediaControlTool, WikipediaTool
from veronica.tools.registry import build_default_registry
from veronica.voice.microphone import resolve_input_device


@pytest.mark.parametrize("text,action", [
    ("pause", "play_pause"), ("pause the music", "play_pause"), ("resume", "play_pause"),
    ("play", "play_pause"), ("next song", "next"), ("skip", "next"), ("skip this track", "next"),
    ("play the next video", "next"), ("previous track", "previous"), ("go back song", "previous"),
    ("stop the music", "stop"),
])
def test_media_phrases_press_the_right_key(text, action):
    pressed = []
    tool = MediaControlTool(presser=pressed.append)
    assert tool.matches(text), text
    assert tool.extract_arguments(text) == {"action": action}
    assert tool.run(**tool.extract_arguments(text)).success
    assert len(pressed) == 1


@pytest.mark.parametrize("text", ["play lofi beats", "stop", "next week is busy", "pause for thought about life"])
def test_media_does_not_steal_other_requests(text):
    assert not MediaControlTool(presser=lambda vk: None).matches(text)


class FakeHttp:
    def __init__(self, title, page):
        self.calls, self.title, self.page = [], title, page

    def get(self, url, **kw):
        self.calls.append(url)
        if "opensearch" in str(kw.get("params", {}).get("action", "")):
            return SimpleNamespace(json=lambda: ["q", [self.title] if self.title else []])
        return SimpleNamespace(json=lambda: self.page)


def test_wikipedia_who_is_reads_two_sentences():
    page = {"type": "standard", "title": "A. P. J. Abdul Kalam",
            "extract": "Kalam was an Indian aerospace scientist. He was the 11th president of India. He was born in 1931."}
    http = FakeHttp("A. P. J. Abdul Kalam", page)
    tool = WikipediaTool(http=http)
    assert tool.matches("who was abdul kalam")
    result = tool.run(**tool.extract_arguments("who was abdul kalam"))
    assert result.message == "Kalam was an Indian aerospace scientist. He was the 11th president of India."
    assert http.calls[-1].endswith("/page/summary/A._P._J._Abdul_Kalam")


def test_wikipedia_disambiguation_and_offline():
    tool = WikipediaTool(http=FakeHttp("Mercury", {"type": "disambiguation", "extract": "Mercury may refer to"}))
    assert "several things" in tool.run(topic="mercury").message

    class Down:
        def get(self, *a, **k):
            raise OSError("offline")
    assert WikipediaTool(http=Down()).run(topic="x").success is False


@pytest.mark.parametrize("text", [
    "who are you", "who is my sister", "who is the prime minister of india", "who is there",
    "what is the time", "who is the best batsman in the world right now honestly",
])
def test_wikipedia_leaves_personal_and_role_questions_to_the_llm(text):
    assert not WikipediaTool(http=FakeHttp("", {})).matches(text)


DEVICES = [
    {"name": "Microsoft Sound Mapper - Input", "max_input_channels": 2, "hostapi": 0},
    {"name": "Speakers (Realtek)", "max_input_channels": 0, "hostapi": 0},
    {"name": "Microphone Array (Realtek) WASAPI", "max_input_channels": 2, "hostapi": 2},
    {"name": "Microphone Array (3- Realtek(R)", "max_input_channels": 4, "hostapi": 0},
]
HOSTAPIS = [{"name": "MME"}, {"name": "Windows DirectSound"}, {"name": "Windows WASAPI"}]


def test_mic_found_by_name_regardless_of_number():
    assert resolve_input_device("microphone array", 1, DEVICES, HOSTAPIS) == 3   # MME entry preferred


def test_mic_falls_back_to_valid_index_then_default():
    assert resolve_input_device("Nope", 0, DEVICES, HOSTAPIS) == 0
    assert resolve_input_device("Nope", 1, DEVICES, HOSTAPIS) is None   # index 1 is an output now
    assert resolve_input_device(None, None, DEVICES, HOSTAPIS) is None


@pytest.mark.parametrize("text,tool", [
    ("pause", "media_control"), ("next song", "media_control"),
    ("play lofi beats", "play_media"), ("who was abdul kalam", "wikipedia_summary"),
    ("remember that my dog is Bruno", "remember_fact"), ("what do you remember about me", "recall_memory"),
    ("forget my dog", "forget_fact"), ("forget everything", "forget_all_memories"),
    ("turn the volume up", "set_volume"), ("unmute", "set_volume"),
])
def test_fast_path_routing(text, tool, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert build_default_registry(PermissionGate()).find_fast_match(text).name == tool
