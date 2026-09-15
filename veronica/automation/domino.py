"""
Domino chains: small, declarative tool pipelines where one step's output feeds the next.

Adapted from JARVIS OS's "Domino chains" concept (a way to orchestrate a sequence of
tools as one unit) and landed in a form that fits VERONICA's architecture: a chain is
just a list of steps, each naming an already-registered tool, and execution runs
*through* the existing ToolRegistry, so the permission gate still applies step by step.
No new interpreter, no sub-conversation — the orchestrator can run a chain the same way
it runs any single tool.

The feed-forward step is the point: a step's kwargs may reference ``{previous}``
(the previous step's message text) or ``{text}`` (the original request) and those are
resolved before dispatch. That is what turns "web_search" then "fetch_webpage" into
"search, then open the first result" instead of two unrelated calls.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from veronica.logging_setup import get_logger
from veronica.tools.base import ToolResult
from veronica.tools.registry import ToolRegistry

logger = get_logger("automation.domino")

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)*)\}")


def _lookup(context: dict[str, Any], dotted: str) -> str:
    """Resolve 'previous', 'text', or 'outputs.greeting'-style keys against the
    context dict, walking nested dicts on dots. Raises KeyError if missing."""
    node: Any = context
    for part in dotted.split("."):
        node = node[part]
    return str(node)

# Callable form: given the running context, return the full kwargs dict for this step.
Resolver = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class DominoStep:
    """One stage in a chain. ``tool`` names a registered Tool; ``kwargs`` is its
    argument dict, which may contain ``{previous}``, ``{text}``, or ``{outputs.<name>}``
    placeholders. Alternatively ``resolve`` is a callable that returns the full kwargs
    dict from the context — for steps whose arguments can't be templated."""

    tool: str
    name: str = ""
    kwargs: dict[str, Any] = field(default_factory=dict)
    resolve: Optional[Resolver] = None

    def resolved_name(self) -> str:
        return self.name or self.tool

    def resolve_args(self, context: dict[str, Any]) -> dict[str, Any]:
        if self.resolve is not None:
            args = self.resolve(context) or {}
            if not isinstance(args, dict):
                raise TypeError(f"DominoStep '{self.resolved_name()}'.resolve must return a dict, got {type(args)}")
            return args
        resolved: dict[str, Any] = {}
        for key, value in self.kwargs.items():
            if isinstance(value, str):
                resolved[key] = _PLACEHOLDER.sub(lambda m: _lookup(context, m.group(1)), value)
            else:
                resolved[key] = value
        return resolved


@dataclass
class StepOutcome:
    tool: str
    name: str
    success: bool
    message: str
    skipped_steps_after: int = 0


@dataclass
class DominoResult:
    name: str
    ok: bool
    results: list[StepOutcome]
    summary: str = ""

    @property
    def failed_step(self) -> Optional[StepOutcome]:
        for r in self.results:
            if not r.success:
                return r
        return None


class DominoChain:
    def __init__(
        self,
        name: str,
        description: str,
        steps: list[DominoStep],
        stops_on_failure: bool = True,
    ) -> None:
        self.name = name
        self.description = description
        self.steps = steps
        self.stops_on_failure = stops_on_failure

    def execute(self, registry: ToolRegistry, text: str = "") -> DominoResult:
        context: dict[str, Any] = {"text": text, "previous": "", "outputs": {}}
        outcomes: list[StepOutcome] = []
        skipped = 0

        for step in self.steps:
            try:
                args = step.resolve_args(context)
            except Exception as e:
                logger.exception("Domino step '%s' argument resolution failed.", step.resolved_name())
                outcomes.append(StepOutcome(step.tool, step.resolved_name(), False, f"Step configuration error: {e}"))
                skipped = len(self.steps) - len(outcomes)
                break

            result: ToolResult = registry.dispatch(step.tool, **args)
            if not result.success and self.stops_on_failure:
                outcomes.append(StepOutcome(step.tool, step.resolved_name(), False, result.message))
                skipped = len(self.steps) - len(outcomes)
                break

            outcomes.append(StepOutcome(step.tool, step.resolved_name(), result.success, result.message))
            context["previous"] = result.message
            context["outputs"][step.resolved_name()] = result.message

        if skipped and outcomes:
            outcomes[-1].skipped_steps_after = skipped

        ok = all(o.success for o in outcomes)
        summary = " ; ".join(f"{o.name}: {o.message}" for o in outcomes)
        return DominoResult(name=self.name, ok=ok, results=outcomes, summary=summary)


class DominoRegistry:
    """Named chains, executed through an existing ToolRegistry (gate included)."""

    def __init__(self, tool_registry: ToolRegistry) -> None:
        self.tool_registry = tool_registry
        self._chains: dict[str, DominoChain] = {}

    def register(self, chain: DominoChain) -> None:
        if chain.name in self._chains:
            raise ValueError(f"Domino chain '{chain.name}' is already registered.")
        self._chains[chain.name] = chain
        logger.info("Registered domino chain '%s' (%d step(s))", chain.name, len(chain.steps))

    def get(self, name: str) -> Optional[DominoChain]:
        return self._chains.get(name)

    def names(self) -> list[str]:
        return sorted(self._chains)

    def describe(self) -> str:
        if not self._chains:
            return ""
        lines = ["Available domino chains (run one by name):"]
        for name in self.names():
            lines.append(f"  - {name}: {self._chains[name].description}")
        return "\n".join(lines)

    def execute(self, name: str, text: str = "") -> DominoResult:
        chain = self.get(name)
        if chain is None:
            ChainNotFound = DominoResult(name=name, ok=False, results=[])
            ChainNotFound.summary = f"No domino chain named '{name}'. Known: {', '.join(self.names()) or 'none'}."
            return ChainNotFound
        return chain.execute(self.tool_registry, text=text)


def build_default_domino_registry(tool_registry: ToolRegistry) -> DominoRegistry:
    """The built-in chains. Kept deliberately small and SAFE/MODERATE-gated; every
    step still goes through the permission gate via ``ToolRegistry.dispatch``."""

    def _weather_for(context):
        city = (context.get("text") or "").strip() or "London"
        return {"city": city}

    daily_briefing = DominoChain(
        name="daily_briefing",
        description="System status, then weather for the given place, then a web search using the results.",
        steps=[
            DominoStep("system_status", name="status"),
            DominoStep("get_weather", name="weather", resolve=_weather_for),
            DominoStep("web_search", name="search", kwargs={"query": "daily briefing {text} {previous}"}),
        ],
    )

    research = DominoChain(
        name="quick_research",
        description="Search the web for a topic, fetch the top result's page, and save a summary note.",
        steps=[
            DominoStep("web_search", name="search", kwargs={"query": "{text}"}),
            DominoStep("fetch_webpage", name="fetch", kwargs={"url": "{text}"}),
            DominoStep("take_note", name="note", kwargs={"text": "Research notes on {text}: {previous}"}),
        ],
    )

    registry = DominoRegistry(tool_registry)
    registry.register(daily_briefing)
    registry.register(research)
    return registry