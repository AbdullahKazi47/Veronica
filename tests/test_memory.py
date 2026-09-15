from pathlib import Path

from veronica.memory.long_term import LongTermMemory
from veronica.memory.short_term import ShortTermMemory


def test_short_term_rolls_over(tmp_path):
    mem = ShortTermMemory(max_messages=3)
    for i in range(5):
        mem.add("user", f"message {i}")
    messages = mem.as_messages()
    assert len(messages) == 3
    assert messages[-1]["content"] == "message 4"


def test_short_term_clear():
    mem = ShortTermMemory(max_messages=5)
    mem.add("user", "hi")
    mem.clear()
    assert len(mem) == 0


def test_long_term_remember_and_recall(tmp_path):
    path = tmp_path / "memory.json"
    mem = LongTermMemory(path)
    mem.remember("owner_name", "Abdul")
    assert mem.recall("owner_name") == "Abdul"


def test_long_term_persists_across_instances(tmp_path):
    path = tmp_path / "memory.json"
    mem1 = LongTermMemory(path)
    mem1.remember("favorite_editor", "VS Code")

    mem2 = LongTermMemory(path)
    assert mem2.recall("favorite_editor") == "VS Code"


def test_long_term_forget(tmp_path):
    path = tmp_path / "memory.json"
    mem = LongTermMemory(path)
    mem.remember("temp_fact", "x")
    assert mem.forget("temp_fact") is True
    assert mem.recall("temp_fact") is None
    assert mem.forget("temp_fact") is False


def test_long_term_search(tmp_path):
    path = tmp_path / "memory.json"
    mem = LongTermMemory(path)
    mem.remember("owner_name", "Abdul")
    mem.remember("owner_city", "Bhatkal")
    results = mem.search("owner")
    assert len(results) == 2
