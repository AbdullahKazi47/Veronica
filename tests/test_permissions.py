from veronica.safety.permissions import PermissionGate, PermissionLevel


def test_safe_action_never_needs_confirmation():
    gate = PermissionGate(threshold=PermissionLevel.MODERATE, confirm_callback=None)
    decision = gate.check("get_time", PermissionLevel.SAFE, "just checking the time")
    assert decision.allowed is True


def test_moderate_action_refused_without_confirm_channel():
    gate = PermissionGate(threshold=PermissionLevel.MODERATE, confirm_callback=None)
    decision = gate.check("open_app", PermissionLevel.MODERATE, "open chrome")
    assert decision.allowed is False
    assert "confirm" in decision.reason.lower() or "MODERATE" in decision.reason


def test_moderate_action_allowed_when_user_approves():
    gate = PermissionGate(threshold=PermissionLevel.MODERATE, confirm_callback=lambda desc, lvl: True)
    decision = gate.check("open_app", PermissionLevel.MODERATE, "open chrome")
    assert decision.allowed is True


def test_moderate_action_blocked_when_user_declines():
    gate = PermissionGate(threshold=PermissionLevel.MODERATE, confirm_callback=lambda desc, lvl: False)
    decision = gate.check("delete_file", PermissionLevel.SENSITIVE, "delete report.docx")
    assert decision.allowed is False


def test_dry_run_blocks_side_effects_even_with_approval():
    gate = PermissionGate(threshold=PermissionLevel.MODERATE, confirm_callback=lambda d, l: True, dry_run=True)
    decision = gate.check("shutdown", PermissionLevel.CRITICAL, "shut down the PC")
    assert decision.allowed is False


def test_level_from_name():
    assert PermissionGate.level_from_name("sensitive") == PermissionLevel.SENSITIVE
