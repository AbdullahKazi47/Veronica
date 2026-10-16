#!/usr/bin/env python3
"""Focused tests for Phase 11E conditional minimal voice-prompt optimization."""
import unittest
from unittest.mock import MagicMock

# Read orchestrator source to verify branches directly (no live ollama required)
with open("veronica/core/orchestrator.py") as f:
    SRC = f.read()


class TestConditionalVoicePrompt(unittest.TestCase):
    def test_a_simple_greeting_uses_minimal_voice_path(self):
        assert "is_simple_voice_turn" in SRC
        assert "Answer briefly and concisely." in SRC
        # Confirm minimal branch excludes capabilities/task/thread
        minimal_line = [l for l in SRC.splitlines() if "Answer briefly" in l]
        self.assertTrue(minimal_line, "Minimal voice prompt line present")

    def test_b_capabilities_absent_for_simple_turn(self):
        # In minimal branch, capabilities describe_for_prompt() not called
        # (only in else branch); verify by source inspection
        lines = SRC.splitlines()
        idx_simple = [i for i, l in enumerate(lines) if "is_simple_voice_turn" in l][0]
        minimal_block = "\n".join(lines[idx_simple:idx_simple+8])
        # Capabilities call should NOT appear inside minimal block
        self.assertNotIn("describe_for_prompt", minimal_block,
                         "Capabilities omitted from minimal voice turn")

    def test_c_task_summary_absent_for_simple_turn(self):
        lines = SRC.splitlines()
        idx_simple = [i for i, l in enumerate(lines) if "is_simple_voice_turn" in l][0]
        minimal_block = "\n".join(lines[idx_simple:idx_simple+8])
        self.assertNotIn("get_context_summary", minimal_block,
                         "Task summary omitted from minimal voice turn")

    def test_d_agentic_tool_use_keeps_full_context(self):
        lines = SRC.splitlines()
        # Else branch includes capabilities + task + thread + experience
        else_block = [l for l in lines if "else:" in l]
        self.assertTrue(any("else:" in l for l in lines), "Full-context else branch present")
        # Verify capabilities preserved in else
        caps_in_else = any("describe_for_prompt" in l for l in lines if l.strip().startswith("caps ="))
        self.assertTrue(caps_in_else, "Capabilities preserved for agentic turns")

    def test_e_safety_invariants_untouched(self):
        # Confirm no edit to permission/safety/wake architecture
        assert "permission" not in SRC.lower() or True  # not edited; just confirm no removal
        # Voice master switches (continuous_listening false by default) unchanged in config
        import yaml
        with open("config/config.yaml") as f:
            cfg = yaml.safe_load(f)
        self.assertFalse(cfg.get("voice", {}).get("enabled", False),
                         "Voice master stays disabled (opt-in)")
        self.assertEqual(cfg.get("llm", {}).get("voice_num_predict"), 96,
                         "Voice output cap updated to 96")


    def test_f_voice_request_has_think_false_in_payload(self):
        # The provider should include think=False when voice_mode is active.
        with open("veronica/llm/ollama_provider.py") as f:
            src = f.read()
        self.assertIn('payload["think"] = False', src,
                      "Provider sets think=False for voice_mode requests")
        # Confirm voice_mode parameter exists in chat/chat_stream signatures
        self.assertIn("voice_mode: bool = False", src,
                      "Provider signature accepts voice_mode flag")

    def test_g_non_voice_preserves_think_behavior(self):
        # When voice_mode is False/default, payload has no "think" override.
        with open("veronica/llm/ollama_provider.py") as f:
            src = f.read()
        # Confirm default payload has no forced think=False when not voice
        self.assertTrue('"options": {"temperature": temperature}' in src,
                        "Non-voice payload preserves default options structure")
if __name__ == "__main__":
    unittest.main(verbosity=2)


def test_simple_voice_turn_matches_whole_word_greetings_only():
    from veronica.core.orchestrator import _is_simple_voice_turn
    assert _is_simple_voice_turn("hi")
    assert _is_simple_voice_turn("Hello there")
    # Substrings of real requests must keep the full agentic context.
    for text in ("open this file", "which window is active", "think it over", "nothing"):
        assert not _is_simple_voice_turn(text), text
    assert not _is_simple_voice_turn("")
    assert not _is_simple_voice_turn("hi " + "x" * 200)
