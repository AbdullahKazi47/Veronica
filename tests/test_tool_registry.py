from veronica.safety.permissions import PermissionGate, PermissionLevel
from veronica.tools.base import Tool, ToolResult
from veronica.tools.registry import ToolRegistry


class DummyTool(Tool):
    name = "dummy"
    description = "A tool for testing."
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": []}

    def matches(self, text: str) -> bool:
        return "dummy" in text.lower()

    def extract_arguments(self, text: str) -> dict:
        return {"x": 1}

    def run(self, x: int = 0, **kwargs) -> ToolResult:
        return ToolResult.ok(f"dummy ran with x={x}", x=x)


class RiskyTool(Tool):
    name = "risky"
    description = "A tool that needs permission."
    permission = PermissionLevel.SENSITIVE

    def run(self, **kwargs) -> ToolResult:
        return ToolResult.ok("did the risky thing")


def make_registry(confirm_callback=None, threshold=PermissionLevel.MODERATE) -> ToolRegistry:
    gate = PermissionGate(threshold=threshold, confirm_callback=confirm_callback)
    registry = ToolRegistry(gate)
    registry.register(DummyTool())
    registry.register(RiskyTool())
    return registry


def test_register_and_get():
    registry = make_registry()
    assert registry.get("dummy") is not None
    assert registry.get("nonexistent") is None


def test_duplicate_registration_raises():
    registry = make_registry()
    try:
        registry.register(DummyTool())
        assert False, "should have raised"
    except ValueError:
        pass


def test_fast_match_finds_claiming_tool():
    registry = make_registry()
    tool = registry.find_fast_match("please run the dummy thing")
    assert tool is not None
    assert tool.name == "dummy"


def test_fast_match_returns_none_when_nothing_claims():
    registry = make_registry()
    assert registry.find_fast_match("completely unrelated text") is None


def test_dispatch_safe_tool_runs_without_confirmation():
    registry = make_registry(confirm_callback=None)
    result = registry.dispatch("dummy", x=5)
    assert result.success is True
    assert "x=5" in result.message


def test_dispatch_sensitive_tool_blocked_without_confirmation():
    registry = make_registry(confirm_callback=None)
    result = registry.dispatch("risky")
    assert result.success is False


def test_dispatch_sensitive_tool_allowed_with_confirmation():
    registry = make_registry(confirm_callback=lambda desc, lvl: True)
    result = registry.dispatch("risky")
    assert result.success is True


def test_dispatch_unknown_tool():
    registry = make_registry()
    result = registry.dispatch("does_not_exist")
    assert result.success is False
    assert "No such tool" in result.message


def test_schemas_shape():
    registry = make_registry()
    schemas = registry.schemas()
    names = {s["function"]["name"] for s in schemas}
    assert names == {"dummy", "risky"}
