"""
The tool/plugin registry: where tools are collected, looked up by name, fast-matched
against raw text, and dispatched through the permission gate.

This is intentionally simple (a dict + a list) rather than a clever plugin-loading
framework, because the actual extensibility need is "add a class, register it in
build_default_registry()" — anything fancier (entry-point discovery, hot-reloading)
can be layered on top later without changing this contract.
"""
from __future__ import annotations

from typing import Iterable, Optional

from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionGate
from veronica.tools.base import Tool, ToolResult

logger = get_logger("tools.registry")


class ToolRegistry:
    def __init__(self, gate: PermissionGate) -> None:
        self._tools: dict[str, Tool] = {}
        self.gate = gate

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool
        logger.info("Registered tool '%s' (permission=%s)", tool.name, tool.permission.name)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def all(self) -> Iterable[Tool]:
        return self._tools.values()

    def schemas(self) -> list[dict]:
        """All tool schemas, for handing to an LLM that supports function-calling."""
        return [t.schema() for t in self._tools.values()]

    def find_fast_match(self, text: str) -> Optional[Tool]:
        """Return the first tool willing to claim this text via its fast-path matcher."""
        for tool in self._tools.values():
            try:
                if tool.matches(text):
                    return tool
            except Exception:
                logger.exception("Tool '%s' raised while matching text; skipping.", tool.name)
        return None

    def dispatch(self, name: str, **kwargs) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult.fail(f"No such tool: '{name}'.")

        decision = self.gate.check(
            tool_name=tool.name,
            level=tool.permission,
            description=f"{tool.name}({kwargs})",
        )
        if not decision.allowed:
            return ToolResult.fail(decision.reason or f"'{tool.name}' was not permitted to run.")

        try:
            return tool.run(**kwargs)
        except TypeError as e:
            logger.warning("Bad arguments for tool '%s': %s", name, e)
            return ToolResult.fail(f"'{tool.name}' received invalid arguments: {e}")
        except Exception:
            logger.exception("Tool '%s' raised unexpectedly.", name)
            return ToolResult.fail(f"'{tool.name}' failed unexpectedly. Check the logs.")


def build_default_registry(gate: PermissionGate, config=None) -> ToolRegistry:
    """Construct a registry with every built-in tool. This is the single place that
    knows the full built-in tool list — new tools get one extra line here."""
    from veronica.tools.execution_tools import (
        DeleteFileTool,
        ReadFileTool,
        RunShellCommandTool,
        WriteFileTool,
    )
    from veronica.tools.file_tools import FindFileTool, ListDirectoryTool
    from veronica.tools.system_tools import (
        OpenApplicationTool,
        ScreenshotTool,
        SetVolumeTool,
        SystemStatusTool,
    )
    from veronica.tools.utility_tools import CalculatorTool, CurrentTimeTool, NoteTool
    from veronica.tools.web_tools import FetchWebpageTool, OpenWebsiteTool, WebSearchTool
    from veronica.tools.weather_tools import WeatherTool
    from veronica.tools.youtube_tools import YouTubeSearchTool

    registry = ToolRegistry(gate)
    for tool in (
        CurrentTimeTool(),
        CalculatorTool(),
        NoteTool(config=config),
        OpenApplicationTool(),
        SetVolumeTool(),
        ScreenshotTool(config=config),
        SystemStatusTool(),
        FindFileTool(),
        ListDirectoryTool(),
        OpenWebsiteTool(),
        WebSearchTool(),
        FetchWebpageTool(),
        YouTubeSearchTool(),
        WeatherTool(config=config),
        # -- general-purpose "do anything" layer: SENSITIVE/CRITICAL, always confirmed --
        ReadFileTool(),
        WriteFileTool(),
        DeleteFileTool(),
        RunShellCommandTool(),
    ):
        registry.register(tool)
    return registry
