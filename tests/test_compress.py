from veronica.memory.compress import compress_messages


def test_returns_unchanged_when_within_budget():
    messages = [
        {"role": "system", "content": "You are VERONICA."},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert compress_messages(messages, max_messages=50) is messages


def test_keeps_system_first_exchange_and_recent():
    messages = [
        {"role": "system", "content": "SYSTEM"},  # must survive
        {"role": "user", "content": "topic: build a report"},  # first exchange kept
        {"role": "assistant", "content": "sure"},
        {"role": "user", "content": "middle 1"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "middle 2"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "recent -3"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "recent -2"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "recent -1"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "latest"},
    ]
    out = compress_messages(messages, max_messages=4)
    roles = [m["role"] for m in out]
    contents = [m["content"] for m in out]

    # System present and first.
    assert roles[0] == "system"
    assert out[0]["content"] == "SYSTEM"
    # First exchange survives verbatim.
    assert "topic: build a report" in contents
    assert "sure" in contents
    # Summary marks the middle.
    assert any("Earlier conversation summary" in c for c in contents)
    # Last 4 messages survive verbatim: assistant "ok" (idx 9), "recent -1",
    # assistant "ok" (idx 11), "latest".
    assert contents[-4:] == ["ok", "recent -1", "ok", "latest"]


def test_summary_counts_user_messages_and_tool_markers():
    messages = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "[tool run_shell_command]"},  # tool marker
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "[tool read_file]"},
        {"role": "user", "content": "tail"},
        {"role": "assistant", "content": "done"},
    ]
    out = compress_messages(messages, max_messages=5)
    summary = next(
        m["content"] for m in out if "Earlier conversation summary" in m["content"]
    )
    # First exchange (start/ok) is kept whole; the middle is "first"/"[tool…]"
    # → 1 user message + 1 tool interaction summarize the middle.
    assert "1 user messages" in summary
    assert "1 tool interactions" in summary


def test_all_system_no_compression_crash():
    messages = [{"role": "system", "content": "x"} for _ in range(60)]
    out = compress_messages(messages, max_messages=10)
    # Only system messages — nothing else to preserve, but must not error.
    assert all(m["role"] == "system" for m in out)