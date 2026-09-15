from veronica.core.loop_guard import LoopGuard, LoopGuardConfig


def test_identical_calls_blocked():
    guard = LoopGuard(LoopGuardConfig(max_identical_calls=3))
    verdicts = [
        guard.check_call("read_file", {"path": "/a"}),
        guard.check_call("read_file", {"path": "/a"}),
        guard.check_call("read_file", {"path": "/a"}),
    ]
    # With max_identical_calls=3, the 3rd identical call is the first one blocked.
    assert verdicts[0].blocked is False
    assert verdicts[1].blocked is False
    assert verdicts[2].blocked is True
    assert "Identical call" in verdicts[2].reason


def test_different_args_are_distinct():
    guard = LoopGuard(LoopGuardConfig(max_identical_calls=3, per_tool_budget=20, max_turns=20))
    for i in range(10):
        v = guard.check_call("read_file", {"path": f"/file_{i}"})
        assert not v.blocked  # never identical, never over budget


def test_per_tool_budget():
    guard = LoopGuard(LoopGuardConfig(per_tool_budget=3))
    verdicts = [
        guard.check_call("run_shell_command", {"cmd": f"echo {i}"}) for i in range(4)
    ]
    assert all(not v.blocked for v in verdicts[:3])
    assert verdicts[3].blocked
    assert "budget" in verdicts[3].reason


def test_ping_pong_warns_not_blocks():
    guard = LoopGuard()
    # A,B,A,B,A,B — alternating
    for i in range(6):
        name = "open_app" if i % 2 == 0 else "close_app"
        v = guard.check_call(name, {"name": "chrome"})
        if i < 5:
            assert not v.blocked
        else:
            assert v.warned
            assert not v.blocked


def test_hard_turn_cap():
    guard = LoopGuard(LoopGuardConfig(max_turns=4))
    verdicts = [
        guard.check_call("tool_a", {"i": i}) for i in range(5)
    ]
    assert not verdicts[3].blocked
    assert verdicts[4].blocked
    assert "Max dispatches" in verdicts[4].reason


def test_reset_clears_state():
    guard = LoopGuard(LoopGuardConfig(per_tool_budget=1, max_identical_calls=10))
    assert not guard.check_call("run_shell_command", {"cmd": "ls"}).blocked
    assert guard.check_call("run_shell_command", {"cmd": "ls"}).blocked  # budget hit
    guard.reset()
    assert guard.turns == 0
    assert not guard.check_call("run_shell_command", {"cmd": "ls"}).blocked


def test_hash_is_order_independent():
    h1 = LoopGuard._hash_call("t", {"b": 1, "a": 2})
    h2 = LoopGuard._hash_call("t", {"a": 2, "b": 1})
    assert h1 == h2