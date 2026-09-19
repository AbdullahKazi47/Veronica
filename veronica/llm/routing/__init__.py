"""
Multi-Model Routing V1 — public surface.

Usage (for VERONICA internals):
    from veronica.llm.routing import build_routing
    assembly = build_routing(config)   # returns None if routing.enabled is False
    router    = assembly.router        # ModelRouter — asks "which model?"
    analyzer  = assembly.analyzer      # TaskAnalyzer — classifies user text
    registry  = assembly.registry      # ModelRegistry — owns profiles + health
"""
from __future__ import annotations

from veronica.llm.routing.builder import RoutingAssembly, build_routing
from veronica.llm.routing.health import HealthState, ModelHealth
from veronica.llm.routing.profiles import ModelProfile, ALL_TASK_TYPES, ALL_CAPABILITIES
from veronica.llm.routing.registry import ModelEntry, ModelRegistry
from veronica.llm.routing.router import (
    ModelRouter, ModelSelection, RoutedResponse, RoutingConfig, ROLE_PREFERENCES,
)
from veronica.llm.routing.task import TaskAnalyzer, TaskProfile

__all__ = [
    "build_routing",
    "RoutingAssembly",
    "ModelProfile",
    "ModelEntry",
    "ModelRegistry",
    "ModelHealth",
    "HealthState",
    "ModelRouter",
    "ModelSelection",
    "RoutedResponse",
    "RoutingConfig",
    "TaskAnalyzer",
    "TaskProfile",
    "ROLE_PREFERENCES",
    "ALL_TASK_TYPES",
    "ALL_CAPABILITIES",
]
