"""
Capability registry: a semantic layer over the flat Tool registry.

The LLM sees a list of 19 raw tool schemas, which is a lot of flat surface for
a small model to reason about. ULTRON's capability_registry.py solved this by
grouping raw tools into semantic categories (filesystem / browser / memory / …)
with a risk level, a default tool and alternates, then letting the prompt show
the categories instead of the raw list. This module adapts that pattern to
VERONICA: a ``Capability`` declares a name, a user-facing description, a
permission level, and the Tool names that back it. The orchestrator injects the
capability list into the system prompt so a local 8B model can pick the right
action without drowning in JSON schemas — the full schemas are still sent to the
provider when it asks for tool-calling.

Not for redistribution outside this project (the grouping pattern is from the
user's private ULTRON project).
"""
from __future__ import annotations

from dataclasses import dataclass

from veronica.safety.permissions import PermissionLevel


@dataclass(frozen=True)
class Capability:
    """A semantic capability the assistant can offer, backed by one or more Tools."""

    name: str
    description: str
    tools: tuple[str, ...]
    risk_level: PermissionLevel = PermissionLevel.SAFE


def build_default_capabilities() -> "CapabilityRegistry":
    """The capability map for VERONICA's built-in tool set. Tool names must match
    ``tools/registry.build_default_registry``. New tools get grouped here."""
    caps = [
        Capability(
            name="tell_time",
            description="Read the current date and time",
            tools=("get_current_time",),
        ),
        Capability(
            name="calculate",
            description="Evaluate arithmetic and math expressions",
            tools=("calculate",),
        ),
        Capability(
            name="notes",
            description="Create, recall, and manage short personal notes",
            tools=("take_note",),
        ),
        Capability(
            name="launch_apps",
            description="Open applications on this computer",
            tools=("open_application",),
            risk_level=PermissionLevel.MODERATE,
        ),
        Capability(
            name="control_audio",
            description="Adjust system volume",
            tools=("set_volume",),
            risk_level=PermissionLevel.MODERATE,
        ),
        Capability(
            name="capture_screen",
            description="Take a screenshot of the display",
            tools=("take_screenshot",),
        ),
        Capability(
            name="system_health",
            description="Report CPU / RAM / OS status of this machine",
            tools=("system_status",),
        ),
        Capability(
            name="find_files",
            description="Find and list files and directories on disk",
            tools=("find_file", "list_directory"),
        ),
        Capability(
            name="browse_web",
            description="Open websites and fetch webpage content",
            tools=("open_website", "fetch_webpage"),
        ),
        Capability(
            name="search_web",
            description="Search the web and YouTube",
            tools=("web_search", "play_media"),
        ),
        Capability(
            name="weather",
            description="Get current weather for a location",
            tools=("get_weather",),
        ),
        Capability(
            name="read_write_files",
            description="Read, write, and delete files; run shell commands",
            tools=("read_file", "write_file", "delete_file", "run_shell_command"),
            risk_level=PermissionLevel.SENSITIVE,
        ),
    ]
    return CapabilityRegistry(caps)


class CapabilityRegistry:
    """Maps capability names to tools and renders a compact prompt block."""

    def __init__(self, capabilities: list[Capability]) -> None:
        self._capabilities: dict[str, Capability] = {}
        self._order: list[str] = []
        for cap in capabilities:
            self._capabilities[cap.name] = cap
            self._order.append(cap.name)

    def get(self, name: str) -> Capability | None:
        return self._capabilities.get(name)

    def all(self) -> list[Capability]:
        return [self._capabilities[n] for n in self._order]

    def tools_for(self, name: str) -> tuple[str, ...]:
        cap = self._capabilities.get(name)
        return cap.tools if cap else ()

    def describe_for_prompt(self) -> str:
        """A compact, human-readable inventory for the system prompt — categories,
        not raw JSON schemas."""
        lines = ["Available capabilities:"]
        for cap in self.all():
            risk = cap.risk_level.name.title()
            lines.append(f"  - {cap.name}: {cap.description} (tools: {', '.join(cap.tools)}; gate: {risk})")
        return "\n".join(lines)


__all__ = ["Capability", "CapabilityRegistry", "build_default_capabilities"]