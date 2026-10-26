"""
The tool/plugin registry: where tools are collected, looked up by name, fast-matched
against raw text, and dispatched through the permission gate.

This is intentionally simple (a dict + a list) rather than a clever plugin-loading
framework, because the actual extensibility need is "add a class, register it in
build_default_registry()" — anything fancier (entry-point discovery, hot-reloading)
can be layered on top later without changing this contract.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionGate
from veronica.tools.base import Tool, ToolResult

logger = get_logger("tools.registry")


_FILLER = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|alright|all\s+right|right|so|now|then|and|hey|hi|um+|uh+|hmm+|well|just|"
    r"please|veronica|can\s+you|could\s+you|would\s+you|will\s+you|i\s+want\s+you\s+to|"
    r"i\s+need\s+you\s+to)\b[\s,.!]*)+", re.I)
_TAIL = re.compile(r"[\s,]+(?:please|for\s+me|now|right\s+now|quickly)\s*[.!?]*\s*$", re.I)
_STUTTER = re.compile(r"\b(\w+)(?:\s+\1\b)+", re.I)


def normalize_command(text: str) -> str:
    """Tidy spoken words for command matching: drop leading filler and polite tails, and
    collapse stutters. Meaning is unchanged; the original text still goes to the model."""
    t = _STUTTER.sub(r"\1", text or "")
    for _ in range(3):
        t = _FILLER.sub("", t)
        t = _TAIL.sub("", t)
    return re.sub(r"\s+", " ", t).strip(" ,.")


class ToolRegistry:
    def __init__(self, gate: PermissionGate) -> None:
        self._tools: dict[str, Tool] = {}
        self.gate = gate

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool
        logger.info("Registered tool '%s' (permission=%s)", tool.name, tool.permission.name)

    def unregister(self, name: str) -> bool:
        """Remove a tool (used only for abilities the user added and later removed)."""
        return self._tools.pop(name, None) is not None

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    # Copies, not live views: integrations (God's Eye View) register tools from a
    # background thread while requests are being served.
    def all(self) -> Iterable[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict]:
        """All tool schemas, for handing to an LLM that supports function-calling."""
        return [t.schema() for t in list(self._tools.values())]

    def _first_match(self, text: str) -> Optional[Tool]:
        for tool in list(self._tools.values()):
            try:
                if tool.matches(text):
                    return tool
            except Exception:
                logger.exception("Tool '%s' raised while matching text; skipping.", tool.name)
        return None

    def fast_match(self, text: str) -> tuple[Optional[Tool], str]:
        """(tool, the text it matched). Tries the words as heard, then a tidied version
        without spoken filler ("ok now minimise all" -> "minimise all") or stutters
        ("restore restore all tabs") — so casual speech doesn't need the cloud model."""
        tool = self._first_match(text)
        if tool is not None:
            return tool, text
        tidy = normalize_command(text)
        if tidy and tidy != text:
            tool = self._first_match(tidy)
            if tool is not None:
                return tool, tidy
        return None, text

    def find_fast_match(self, text: str) -> Optional[Tool]:
        """Return the first tool willing to claim this text via its fast-path matcher."""
        return self.fast_match(text)[0]

    def dispatch(self, name: str, /, **kwargs) -> ToolResult:  # / : tools may take a "name" argument
        tool = self.get(name)
        if tool is None:
            return ToolResult.fail(f"No such tool: '{name}'.")

        # Every action is on record, whether a quick command or the model chose it: an
        # unlogged model tool call is how an unexpected sleep once went untraceable.
        logger.info("Running tool %s(%s) [permission %s]", tool.name, _brief(kwargs), tool.permission.name)
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


def _brief(kwargs: dict) -> str:
    """Tool arguments for the log, shortened and with secrets scrubbed."""
    try:
        from veronica.safety import privacy
        text = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
        return privacy.get_guard().scrub_text(text)[:200]
    except Exception:  # noqa: BLE001
        return "..."


def _summarizer(llm):
    if llm is None:
        return None
    from veronica.llm.base import ChatMessage

    def summarize(prompt: str) -> str:
        resp = llm.chat([ChatMessage(role="user", content=prompt)], temperature=0.3)
        return resp.content if getattr(resp, "ok", True) else ""
    return summarize


_CODING_TOOLS = {"learn_topic", "recall_learning", "forget_topic", "write_code", "update_code", "run_code"}
# (build_ability & co. are added after the main list, also only when coding is enabled)


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
        AboutMeTool,
        ForgetAllMemoriesTool,
        ForgetFactTool,
        RecallMemoryTool,
        RememberFactTool,
        ResetPersonalityTool,
    )
    from veronica.tools.media_tools import MediaControlTool, WikipediaTool
    from veronica.tools.camera_tools import LookThroughCameraTool
    from veronica.tools.diagnostics_tools import RunDiagnosticsTool
    from veronica.tools.news_tools import NewsTool, ShowNewsTool
    from veronica.tools.notification_tools import ReadNotificationsTool
    from veronica.tools.screen_tools import LookAtScreenTool
    from veronica.tools.understanding_tools import LookUpTool, ReviewFixesTool, TeachWordsTool
    from veronica.tools.voiceprint_tools import VoiceprintTool
    from veronica.tools.voice_tools import ChangeVoiceTool
    from veronica.tools.gps_tools import UsePhoneGpsTool, WhereAmITool
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
    from veronica.tools.self_heal_tools import SelfHealTool
    from veronica.tools.privacy_tools import PrivacyReportTool
    from veronica.tools.download_tools import DownloadFileTool
    from veronica.tools.speech_tools import SpeechCorrectionTool
    from veronica.tools.voice_test_tools import VoiceTestTool
    from veronica.integrations.earth_views import (
        EarthGlobeTool, LookFromSpaceTool, NasaWorldviewTool, SatelliteTracker3DTool,
    )
    from veronica.tools.calendar_tools import AddEventTool, CalendarTool, ConnectCalendarTool, RemoveEventTool
    from veronica.integrations.calendar import Calendar
    from veronica.tools.learning_tools import (
        ForgetTopicTool,
        LearnTopicTool,
        RecallLearningTool,
        RunCodeTool,
        StopCodeTool,
        UpdateCodeTool,
        WriteCodeTool,
    )
    from veronica.tools.explorer_tools import ExplorerNavigateTool
    from veronica.tools.fixit_tools import (
        ApplyFixTool, FixHistoryTool, FixProblemTool, FixSession, InstallProgramTool,
    )
    from veronica.tools.tab_tools import CloseTabTool, ListTabsTool, SwitchTabTool
    from veronica.tools.window_tools import (
        CancelShutdownTool,
        CloseWindowTool,
        LockPCTool,
        SleepPCTool,
        OpenPathTool,
        ShutdownTool,
        WindowControlTool,
    )
    from veronica.tools.youtube_tools import YouTubeSearchTool

    profile, reminders, todos = Profile(config), ReminderStore(config), TodoStore(config)
    calendar = Calendar(config)
    # Installed apps/browsers are discovered, not listed; both 'open' tools share one parse.
    from veronica.tools.app_index import shared_index
    from veronica.tools.system_tools import _OpenRouting
    apps = shared_index(config)
    fix_session = FixSession(config)
    open_routing = _OpenRouting(config, apps)
    weather = WeatherTool(config=config, profile=profile)
    news = NewsTool(config=config, summarizer=_summarizer(llm))
    registry = ToolRegistry(gate)
    coding_on = getattr(getattr(config, "coding", None), "enabled", True)
    # Jev (decision layer) early: "Jev, should I ..." must not be taken for another command.
    # It only advises; the router/analyzer for "how would you handle X" are attached in main.
    from veronica.core.jev import Jev
    from veronica.tools.jev_tools import JevDecideTool, JevRouteTool, JevWhyTool
    from veronica.tools.learning_tools import _facts_reader
    jev = Jev(config=config, llm=llm, facts=_facts_reader(config) if config is not None else None)
    for tool in (JevRouteTool(registry), JevWhyTool(jev), JevDecideTool(jev)):
        registry.register(tool)
    fix_session.dispatch = registry.dispatch   # each fix step goes through the PermissionGate
    # Routines first: their names are the user's own phrases ("good night", "work mode")
    # and must win over every built-in matcher. Each step is still gated on its own.
    from veronica.tools.routine_tools import CreateRoutineTool, EditRoutineTool, RoutineInfoTool, RunRoutineTool
    for tool in (CreateRoutineTool(config=config), EditRoutineTool(config=config),
                 RoutineInfoTool(config=config), RunRoutineTool(config=config)):
        registry.register(tool)
    # God's Eye View first: its 'open the globe' / 'show me that' must win over 'open <site>'.
    from veronica.integrations.gods_eye import register_gods_eye
    from veronica.integrations.world_monitor import register_world_monitor
    if config is not None:
        register_gods_eye(registry, config, profile)
        try:
            register_world_monitor(registry, config)      # tools appear once its Docker stack answers
        except Exception:  # noqa: BLE001 - never block start-up
            logger.exception("World Monitor registration failed.")
        from veronica.integrations import browser_agent
        if getattr(config.browser_agent, "enabled", True) and browser_agent.available():
            registry.register(browser_agent.BrowserTaskTool(config=config))
    for tool in (
        # Voice productivity first: their fast-path phrases ("remind me in 10 minutes to
        # check the weather") must win over the generic weather/time/calc matchers.
        # Calendar before the briefing, reminders, news and time: "what's on tomorrow" is a calendar question.
        ConnectCalendarTool(config=config, calendar=calendar),
        AddEventTool(config=config, calendar=calendar, reminders=reminders),
        RemoveEventTool(config=config, calendar=calendar, reminders=reminders),
        CalendarTool(config=config, calendar=calendar),
        DailyBriefingTool(config=config, weather_tool=weather, profile=profile,
                          reminders=reminders, todos=todos, calendar=calendar),
        SetReminderTool(config=config, store=reminders),
        ListRemindersTool(config=config, store=reminders),
        CancelReminderTool(config=config, store=reminders),
        TodoTool(config=config, store=todos),
        SetHomeCityTool(config=config, profile=profile),
        UsePhoneGpsTool(config=config),
        WhereAmITool(config=config, profile=profile),
        ChangeVoiceTool(config=config),         # 'change your voice to Emma', 'what voices do you have'
        TeachWordsTool(config=config),          # 'my notes means Obsidian' (before 'remember that ...')
        ReviewFixesTool(config=config),         # 'what did you get wrong this week?'
        LookUpTool(config=config),              # 'look up the Treaty of Versailles'
        SpeechCorrectionTool(config=config),    # 'when I say X I mean Y' (before forget tools)
        ResetPersonalityTool(config=config),    # before ForgetTopic: 'forget what you learned about me'
        AboutMeTool(config=config),             # 'what have you learned about me?'
        ForgetTopicTool(config=config),         # before ForgetFact: 'forget what you learned about X'
        ForgetAllMemoriesTool(config=config),   # before ForgetFact: 'forget everything'
        ForgetFactTool(config=config),
        RecallMemoryTool(config=config),
        RememberFactTool(config=config),
        # Views of the planet (KeepTrack, NASA Worldview, Earth) before 'open <site/app>'.
        SatelliteTracker3DTool(config=config, index=apps),
        LookFromSpaceTool(config=config, index=apps),
        NasaWorldviewTool(config=config, index=apps),
        EarthGlobeTool(config=config, index=apps),
        ShowNewsTool(config=config, news=news, index=apps),  # before news: 'show me the news'
        news,
        MediaControlTool(),                    # before YouTube: bare 'play'/'pause'
        # Fix-it before the plain screen/camera looks: "look at this error" is a problem to solve.
        ApplyFixTool(fix_session),             # "fix it" / "do it" — only with a fresh plan waiting
        FixHistoryTool(fix_session),           # "what did you change?" / "undo that"
        FixProblemTool(fix_session, config=config),
        InstallProgramTool(config=config),     # asks; only Microsoft-/publisher-signed installers
        LookAtScreenTool(config=config),
        LookThroughCameraTool(config=config),
        ReadNotificationsTool(config=config),
        VoiceprintTool(config=config),          # "learn my voice" / "only listen to me"
        VoiceTestTool(),                       # "run a voice test" (before self-heal: "check your hearing")
        SelfHealTool(config=config),           # "heal yourself" / "are you okay"
        PrivacyReportTool(config=config),      # "what do you send to the cloud"
        RunDiagnosticsTool(),                  # before system_status: the deeper check
        WikipediaTool(config=config),
        # Everyday PC control at the right permission level (only close/shutdown ask),
        # before the 'open' tools so 'open the projects folder' finds the folder.
        CancelShutdownTool(),
        ShutdownTool(),
        LockPCTool(),
        SleepPCTool(),                         # asks first (SENSITIVE)
        # Tabs and Explorer before the window tools: 'close all tabs' and 'switch to the docs tab' are not
        # about windows; 'open C drive' / 'go into projects' are not apps.
        ListTabsTool(),
        SwitchTabTool(),
        CloseTabTool(),                        # asks first (SENSITIVE)
        ExplorerNavigateTool(),
        WindowControlTool(),
        CloseWindowTool(),
        OpenPathTool(config=config, routing=open_routing),
        CurrentTimeTool(),
        CalculatorTool(),
        NoteTool(config=config),
        OpenApplicationTool(config=config, routing=open_routing, verify=True),
        SetVolumeTool(),                       # before update_code: 'make it louder'
        # Learning and coding: studying and writing never ask; running code always does.
        LearnTopicTool(config=config, llm=llm),
        RecallLearningTool(config=config),
        WriteCodeTool(config=config, llm=llm),
        UpdateCodeTool(config=config, llm=llm),
        StopCodeTool(config=config, llm=llm),    # "stop the server" (before close_window's "close ...")
        RunCodeTool(config=config, llm=llm),
        ScreenshotTool(config=config),
        SystemStatusTool(),
        FindFileTool(),
        ListDirectoryTool(),
        DownloadFileTool(config=config),       # "download <url>" before open/search
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
        if not coding_on and tool.name in _CODING_TOOLS:
            continue
        registry.register(tool)
    # Abilities added by voice: built/approved through these tools; approved ones (code
    # unchanged since approval, safety check passed again) are loaded last, read-only.
    if coding_on:
        from veronica.core.abilities import AbilityManager
        from veronica.tools.ability_tools import (
            ActivateAbilityTool, BuildAbilityTool, ListAbilitiesTool, RemoveAbilityTool,
        )
        manager = AbilityManager(config=config, registry=registry, llm=llm)
        for tool in (BuildAbilityTool(manager), ActivateAbilityTool(manager), ListAbilitiesTool(manager),
                     RemoveAbilityTool(manager)):
            registry.register(tool)
        try:
            loaded = manager.load_approved()
            if loaded:
                logger.info("Added abilities loaded: %s", ", ".join(loaded))
        except Exception:  # noqa: BLE001 - a broken ability must never stop VERONICA starting
            logger.exception("Couldn't load added abilities.")
    return registry
