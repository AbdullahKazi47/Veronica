from veronica.core.capability import Capability, CapabilityRegistry, build_default_capabilities
from veronica.safety.permissions import PermissionLevel


def test_default_capabilities_cover_builtin_tools():
    caps = build_default_capabilities()
    # Every built-in tool name should be referenced by at least one capability.
    builtin = {
        "get_current_time", "calculate", "take_note", "open_application",
        "set_volume", "take_screenshot", "system_status", "find_file",
        "list_directory", "open_website", "fetch_webpage", "web_search",
        "play_media", "get_weather", "read_file", "write_file",
        "delete_file", "run_shell_command",
    }
    covered = {t for cap in caps.all() for t in cap.tools}
    assert builtin - covered == set(), f"tools not covered: {builtin - covered}"


def test_tools_for():
    caps = build_default_capabilities()
    assert caps.tools_for("system_health") == ("system_status",)
    assert caps.tools_for("nope") == ()


def test_risk_levels_present():
    caps = build_default_capabilities()
    risky = {c.name for c in caps.all() if c.risk_level is not PermissionLevel.SAFE}
    assert "read_write_files" in risky
    assert "launch_apps" in risky


def test_describe_for_prompt_lists_every_capability():
    caps = CapabilityRegistry([
        Capability("greet", "Say hello", tools=("hello_tool",)),
        Capability("bye", "Say goodbye", tools=("bye_tool",)),
    ])
    prompt = caps.describe_for_prompt()
    assert "Available capabilities:" in prompt
    assert "greet: Say hello" in prompt
    assert "bye: Say goodbye" in prompt
    assert "hello_tool" in prompt


def test_describe_for_prompt_matches_builtin_tool_names():
    caps = build_default_capabilities()
    prompt = caps.describe_for_prompt()
    assert "read_write_files: Read, write, and delete files; run shell commands" in prompt
    assert "run_shell_command" in prompt
    assert "gate: Sensitive" in prompt