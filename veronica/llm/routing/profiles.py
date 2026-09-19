"""
ModelProfile: the metadata VERONICA's router reasons about. A profile says what a
model CAN do (capabilities), what TASKS it is FOR (task_types), how it performs
(speed / reasoning / cost classes), and how much the config author prefers it
(priority, enabled, default). It carries NO provider implementation — the actual
LLMProvider is bound later by the ModelRegistry. This is what keeps "which model
do I use" a routing concern instead of a code concern.

Multi-Model Routing V1.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# Task types VERONICA can classify a request as (see task.py). Model profiles may
# declare which of these they are suited for.
TASK_GENERAL = "general"
TASK_CHAT = "chat"
TASK_REASONING = "reasoning"
TASK_CODING = "coding"
TASK_PLANNING = "planning"
TASK_TOOL_USE = "tool_use"
TASK_VISION = "vision"
TASK_ANALYSIS = "analysis"
TASK_SUMMARIZATION = "summarization"

ALL_TASK_TYPES = frozenset({
    TASK_GENERAL, TASK_CHAT, TASK_REASONING, TASK_CODING, TASK_PLANNING,
    TASK_TOOL_USE, TASK_VISION, TASK_ANALYSIS, TASK_SUMMARIZATION,
})

# Extensible capability vocabulary (capabilities=... on a profile). V1 has the three
# that gate routing; future ones (vision models, longer context, ...) slot in here.
CAP_TOOLS = "tools"
CAP_VISION = "vision"
CAP_STREAMING = "streaming"

ALL_CAPABILITIES = frozenset({CAP_TOOLS, CAP_VISION, CAP_STREAMING})

# Role is the coarse "job posting" a model fills. The router maps task types to
# roles (see router.ROLE_PREFERENCES) — V1 ships fast / reasoning / coding / general.
ROLE_GENERAL = "general"
ROLE_FAST = "fast"
ROLE_REASONING = "reasoning"
ROLE_CODING = "coding"
ROLE_VISION = "vision"

ALL_ROLES = frozenset({
    ROLE_GENERAL, ROLE_FAST, ROLE_REASONING, ROLE_CODING, ROLE_VISION,
})

# Qualitative classes used for deterministic ranking (never a probe).
SPEED_FAST = "fast"
SPEED_MEDIUM = "medium"
SPEED_SLOW = "slow"

REASONING_NONE = "none"
REASONING_LIGHT = "light"
REASONING_HEAVY = "heavy"

COST_LOCAL = "local"     # runs on this machine
COST_FREE = "free"       # no billing, hosted
COST_LOW = "low"
COST_MEDIUM = "medium"
COST_HIGH = "high"

COST_ORDER = {COST_LOCAL: 0, COST_FREE: 1, COST_LOW: 2, COST_MEDIUM: 3, COST_HIGH: 4}
SPEED_ORDER = {SPEED_FAST: 0, SPEED_MEDIUM: 1, SPEED_SLOW: 2}
REASONING_ORDER = {REASONING_NONE: 0, REASONING_LIGHT: 1, REASONING_HEAVY: 2}


@dataclass(frozen=True)
class ModelProfile:
    """Read-only metadata about one routable model.

    All fields are config data, never runtime-measured values — availability lives
    in the registry's health layer, not here.
    """
    model_id: str
    provider: str                          # "ollama" | "openai_compatible" | "echo"
    role: str = ROLE_GENERAL
    capabilities: frozenset[str] = field(default_factory=frozenset)
    task_types: frozenset[str] = field(default_factory=frozenset)
    context_window: int = 8192
    supports_tools: bool = False
    supports_vision: bool = False
    supports_streaming: bool = False
    speed_class: str = SPEED_MEDIUM
    reasoning_class: str = REASONING_NONE
    cost_class: str = COST_LOCAL
    priority: int = 100                    # lower wins (config preference)
    enabled: bool = True
    default: bool = False                  # the routing fallback / primary general model
    # Optional per-model construction overrides (else the global llm.* values).
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    temperature: Optional[float] = None
    timeout_s: int = 60

    def __post_init__(self) -> None:
        # Keep capability booleans and the capabilities set in sync (source of
        # truth: the explicit booleans, which the spec names; the set is the
        # extensible surface).
        caps = set(self.capabilities)
        if self.supports_tools:
            caps.add(CAP_TOOLS)
        if self.supports_vision:
            caps.add(CAP_VISION)
        if self.supports_streaming:
            caps.add(CAP_STREAMING)
        object.__setattr__(self, "capabilities", frozenset(caps))

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities

    def handles_task(self, task_type: str) -> bool:
        """A profile is "for" a task when it declares it, or when it declares nothing
        (an untyped model is general-purpose)."""
        if not self.task_types:
            return True
        return task_type in self.task_types

    def summary(self) -> dict:
        """Observability: what gets logged / surfaced about this model. Deliberately
        no base_url / api_key / secrets."""
        return {
            "model_id": self.model_id,
            "provider": self.provider,
            "role": self.role,
            "capabilities": sorted(self.capabilities),
            "task_types": sorted(self.task_types),
            "context_window": self.context_window,
            "supports_tools": self.supports_tools,
            "supports_vision": self.supports_vision,
            "speed": self.speed_class,
            "reasoning": self.reasoning_class,
            "cost": self.cost_class,
            "priority": self.priority,
            "enabled": self.enabled,
            "default": self.default,
        }


__all__ = [
    "ModelProfile",
    "TASK_GENERAL", "TASK_CHAT", "TASK_REASONING", "TASK_CODING", "TASK_PLANNING",
    "TASK_TOOL_USE", "TASK_VISION", "TASK_ANALYSIS", "TASK_SUMMARIZATION",
    "ALL_TASK_TYPES",
    "CAP_TOOLS", "CAP_VISION", "CAP_STREAMING", "ALL_CAPABILITIES",
    "ROLE_GENERAL", "ROLE_FAST", "ROLE_REASONING", "ROLE_CODING", "ROLE_VISION",
    "ALL_ROLES",
    "SPEED_FAST", "SPEED_MEDIUM", "SPEED_SLOW",
    "REASONING_NONE", "REASONING_LIGHT", "REASONING_HEAVY",
    "COST_LOCAL", "COST_FREE", "COST_LOW", "COST_MEDIUM", "COST_HIGH",
    "COST_ORDER", "SPEED_ORDER", "REASONING_ORDER",
]