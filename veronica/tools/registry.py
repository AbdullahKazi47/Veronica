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

    # Copies, not live views: integrations (God's Eye View) register tools from a
    # background thread while requests are being served.
    def all(self) -> Iterable[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict]:
        """All tool schemas, for handing to an LLM that supports function-calling."""
        return [t.schema() for t in list(self._tools.values())]

    def find_fast_match(self, text: str) -> Optional[Tool]:
        """Return the first tool willing to claim this text via its fast-path matcher."""
        for tool in list(self._tools.values()):
            try:
                if tool.matches(text):
                    return tool
            except Exception:
                logger.exception("Tool '%s' raised while matching text; skipping.", tool.name)
        return None

    def dispatch(self, name: str, /, **kwargs) -> ToolResult:  # / : tools may take a "name" argument
        tool = self.get(name)
        if tool is None:
            return ToolResult.fail(f"No such tool: '{name}'.")

        decision = self.gate.check(
            tool_name=tool.name,
            level=tool.permission,
            description=f"{tool.name}({kwargs})",
        )
        if not decision.allowed:
            # Spelled out for the model: it once said "All windows minimized" after a "no".
            return ToolResult.fail(f"NOT DONE — '{tool.name}' did not run ({decision.reason or 'not permitted'}). "
                                   "Nothing happened; tell the user it was not done.", refused=True)

        try:
            return tool.run(**kwargs)
        except TypeError as e:
            logger.warning("Bad arguments for tool '%s': %s", name, e)
            return ToolResult.fail(f"'{tool.name}' received invalid arguments: {e}")
        except Exception:
            logger.exception("Tool '%s' raised unexpectedly.", name)
            return ToolResult.fail(f"'{tool.name}' failed unexpectedly. Check the logs.")


def _summarizer(llm):
    if llm is None:
        return None
    from veronica.llm.base import ChatMessage

    def summarize(prompt: str) -> str:
        resp = llm.chat([ChatMessage(role="user", content=prompt)], temperature=0.3)
        return resp.content if getattr(resp, "ok", True) else ""
    return summarize


def build_default_registry(gate: PermissionGate, config=None, llm=None) -> ToolRegistry:
    """Construct a registry with every built-in tool. This is the single place that
    knows the full built-in tool list — new tools get one extra line here.

    `llm` (optional) lets tools that need one quick model call — the news summary —
    use the configured fast model directly instead of a second tool round-trip."""
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
    from veronica.tools.briefing_tools import DailyBriefingTool
    from veronica.tools.memory_tools import (
        ForgetAllMemoriesTool,
        ForgetFactTool,
        RecallMemoryTool,
        RememberFactTool,
    )
    from veronica.tools.media_tools import MediaControlTool, WikipediaTool
    from veronica.tools.camera_tools import LookThroughCameraTool
    from veronica.tools.diagnostics_tools import RunDiagnosticsTool
    from veronica.tools.news_tools import NewsTool, ShowNewsTool
    from veronica.tools.notification_tools import ReadNotificationsTool
    from veronica.tools.screen_tools import LookAtScreenTool
    from veronica.tools.productivity_tools import (
        CancelReminderTool,
        ListRemindersTool,
        Profile,
        ReminderStore,
        SetHomeCityTool,
        SetReminderTool,
        TodoStore,
        TodoTool,
    )
    from veronica.tools.utility_tools import CalculatorTool, CurrentTimeTool, NoteTool
    from veronica.tools.web_tools import FetchWebpageTool, OpenWebsiteTool, WebSearchTool
    from veronica.tools.weather_tools import WeatherTool
    from veronica.tools.window_tools import (
        CancelShutdownTool,
        CloseWindowTool,
        LockOrSleepTool,
        OpenPathTool,
        ShutdownTool,
        WindowControlTool,
    )
    from veronica.tools.youtube_tools import YouTubeSearchTool

    profile, reminders, todos = Profile(config), ReminderStore(config), TodoStore(config)
    # Installed apps/browsers are discovered, not listed; both 'open' tools share one parse.
    from veronica.tools.app_index import shared_index
    from veronica.tools.system_tools import _OpenRouting
    apps = shared_index(config)
    open_routing = _OpenRouting(config, apps)
    weather = WeatherTool(config=config, profile=profile)
    news = NewsTool(config=config, summarizer=_summarizer(llm))
    registry = ToolRegistry(gate)
    # God's Eye View first: its 'open the globe' / 'show me that' must win over 'open <site>'.
    from veronica.integrations.gods_eye import register_gods_eye
    if config is not None:
        register_gods_eye(registry, config, profile)
    for tool in (
        # Voice productivity first: their fast-path phrases ("remind me in 10 minutes to
        # check the weather") must win over the generic weather/time/calc matchers.
        DailyBriefingTool(config=config, weather_tool=weather, profile=profile,
                          reminders=reminders, todos=todos),
        SetReminderTool(config=config, store=reminders),
        ListRemindersTool(config=config, store=reminders),
        CancelReminderTool(config=config, store=reminders),
        TodoTool(config=config, store=todos),
        SetHomeCityTool(config=config, profile=profile),
        ForgetAllMemoriesTool(config=config),   # before ForgetFact: 'forget everything'
        ForgetFactTool(config=config),
        RecallMemoryTool(config=config),
        RememberFactTool(config=config),
        ShowNewsTool(config=config, news=news, index=apps),  # before news: 'show me the news'
        news,
        MediaControlTool(),                    # before YouTube: bare 'play'/'pause'
        LookAtScreenTool(config=config),
        LookThroughCameraTool(config=config),
        ReadNotificationsTool(config=config),
        RunDiagnosticsTool(),                  # before system_status: the deeper check
        WikipediaTool(config=config),
        # Everyday PC control at the right permission level (only close/shutdown ask),
        # before the 'open' tools so 'open the projects folder' finds the folder.
        CancelShutdownTool(),
        ShutdownTool(),
        LockOrSleepTool(),
        WindowControlTool(),
        CloseWindowTool(),
        OpenPathTool(),
        CurrentTimeTool(),
        CalculatorTool(),
        NoteTool(config=config),
        OpenApplicationTool(config=config, routing=open_routing),
        SetVolumeTool(),
        ScreenshotTool(config=config),
        SystemStatusTool(),
        FindFileTool(),
        ListDirectoryTool(),
        OpenWebsiteTool(config=config, routing=open_routing),
        WebSearchTool(config=config, index=apps),
        FetchWebpageTool(),
        YouTubeSearchTool(config=config, index=apps),
        weather,
        # -- general-purpose "do anything" layer: SENSITIVE/CRITICAL, always confirmed --
        ReadFileTool(),
        WriteFileTool(),
        DeleteFileTool(),
        RunShellCommandTool(),
    ):
        registry.register(tool)
    return registry
