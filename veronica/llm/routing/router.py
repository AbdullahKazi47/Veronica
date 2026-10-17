"""
ModelRouter: turns a TaskProfile into a selectable ModelSelection.

Selection priorities (spec): explicit user/model request > required capability >
task type > context requirements > availability > configured priority > latency >
cost > default fallback. Every decision is deterministic, inspectable (selection.reason
lists each applied rule), and logged — routing never silently picks an unexpected model,
and it never probes the network on the hot path.

Execution lives here too: ``selection.chat(...)`` runs the chosen provider and, on a
genuine failure (provider raised, returned ok=False, or produced an empty response),
falls back through the capability-preserving chain. If NOTHING succeeds the call
returns a controlled RoutedResponse(succeeded=False) — never a fabricated success.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from veronica.llm.base import ChatMessage, LLMResponse
from veronica.llm.routing.profiles import (
    CAP_STREAMING, CAP_TOOLS, CAP_VISION,
    COST_ORDER, REASONING_HEAVY, REASONING_LIGHT, REASONING_NONE,
    ROLE_BACKUP, ROLE_CODING, ROLE_FAST, ROLE_GENERAL, ROLE_REASONING, ROLE_VISION,
    SPEED_FAST, SPEED_MEDIUM, SPEED_SLOW,
    TASK_ANALYSIS, TASK_CHAT, TASK_CODING, TASK_GENERAL, TASK_PLANNING,
    TASK_REASONING, TASK_SUMMARIZATION, TASK_TOOL_USE, TASK_VISION,
)
from veronica.llm.routing.health import HealthState
from veronica.llm.routing.registry import ModelEntry, ModelRegistry
from veronica.logging_setup import get_logger

logger = get_logger("llm.routing.router")

# Task type -> preferred roles, most-preferred first. Config role tags beat nothing,
# but explicit task_types on a profile beat the role heuristic (they are more specific).
ROLE_PREFERENCES: dict[str, tuple[str, ...]] = {
    TASK_VISION: (ROLE_VISION, ROLE_GENERAL),
    TASK_CODING: (ROLE_CODING, ROLE_GENERAL),
    TASK_REASONING: (ROLE_REASONING, ROLE_GENERAL),
    TASK_ANALYSIS: (ROLE_REASONING, ROLE_GENERAL),
    TASK_PLANNING: (ROLE_REASONING, ROLE_GENERAL),
    TASK_SUMMARIZATION: (ROLE_FAST, ROLE_GENERAL),
    TASK_CHAT: (ROLE_FAST, ROLE_GENERAL),
    TASK_TOOL_USE: (ROLE_GENERAL, ROLE_FAST),
    TASK_GENERAL: (ROLE_GENERAL, ROLE_FAST),
}


@dataclass(frozen=True)
class RoutingConfig:
    """Config for the router itself (not models). Defaults keep routing safe & quiet."""
    enabled: bool = False
    fallback_enabled: bool = True
    fallback_on_empty_response: bool = True
    max_fallback_attempts: int = 2
    health_ttl_s: float = 30.0
    # Lazy by default: routing never probes a model just because it was registered.
    # The first real call (or an explicit check_health) establishes AVAILABLE/UNAVAILABLE.
    eager_health_check: bool = False
    log_routing_decisions: bool = True


@dataclass
class ModelSelection:
    """One routing decision. Carries the chosen model + its capability-preserving
    fallback chain. ``satisfied=False`` means NO configured model can honour the
    task (e.g. vision requested, no vision model) — callers must not pretend."""

    model_id: str
    provider_name: str
    role: str
    reason: list[str] = field(default_factory=list)
    decision: str = "routed"
    satisfied: bool = True
    capability_gaps: list[str] = field(default_factory=list)
    fallback_ids: list[str] = field(default_factory=list)
    priority: int = 100
    metadata: dict[str, Any] = field(default_factory=dict)
    # Internal execution handles (populated by ModelRouter.route).
    _registry: Optional[ModelRegistry] = field(default=None, repr=False)
    _config: Optional[RoutingConfig] = field(default=None, repr=False)

    def to_metadata(self) -> dict:
        return {
            "model_id": self.model_id,
            "provider": self.provider_name,
            "role": self.role,
            "decision": self.decision,
            "satisfied": self.satisfied,
            "capability_gaps": list(self.capability_gaps),
            "reason": list(self.reason),
            "fallback_ids": list(self.fallback_ids),
            "metadata": dict(self.metadata),
        }

    @property
    def context_window(self) -> Optional[int]:
        """Context Window Management V1: the chosen model's declared window read as
        DATA from the registry (no routing decision, no provider construction). This is
        what lets the ContextManager budget a request to an exact model limit without
        ever importing the router itself."""
        if self._registry is None or not self.satisfied:
            return None
        entry = self._registry.get(self.model_id)
        return entry.profile.context_window if entry is not None else None

    # -- execution (with capability-preserving fallback) ----------------------
    def chat(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.4,
        voice_mode: bool = False,
    ) -> "RoutedResponse":
        if not self.satisfied:
            return self._unsatisfied_response()
        if self._registry is None:
            return RoutedResponse(
                content="Routing is not wired (no registry).",
                ok=False, succeeded=False,
                routing=self.to_metadata(),
            )

        cfg = self._config if self._config is not None else RoutingConfig()
        chain_ids = [self.model_id] + ([f for f in self.fallback_ids if f != self.model_id]
                                       if cfg.fallback_enabled else [])
        chain_ids = chain_ids[: max(1, cfg.max_fallback_attempts + 1)]

        attempts: list[dict] = []
        last_failure_content: Optional[str] = None
        for chain_pos, mid in enumerate(chain_ids):
            entry = self._registry.get(mid)
            if entry is None:
                attempts.append({"model_id": mid, "skipped": "not_registered"})
                continue
            attempt = self._try_one(entry, messages, tools, temperature, cfg, chain_pos,
                                    voice_mode=voice_mode)
            attempts.append(
                {
                    "model_id": mid,
                    "position": chain_pos,
                    **attempt["meta"],
                }
            )
            if attempt["response"] is not None:
                resp = attempt["response"]
                routing = dict(self.to_metadata())
                routing.update(
                    {
                        "attempts": attempts,
                        "used_fallback": chain_pos > 0,
                        "fallback_used": chain_pos > 0,
                        "succeeded": True,
                        "latency_s": attempt["meta"].get("latency_s", 0.0),
                    }
                )
                return RoutedResponse(
                    content=resp.content,
                    tool_calls=list(resp.tool_calls),
                    raw=resp.raw,
                    ok=resp.ok,
                    routing=routing,
                    succeeded=True,
                )
            if attempt.get("failure_content"):
                last_failure_content = attempt["failure_content"]

        # Everything failed → controlled error. No fabricated success.
        if last_failure_content:
            content = last_failure_content
        else:
            tried = ", ".join(a.get("model_id", "?") for a in attempts) or "none"
            logger.warning("All routed models failed (tried: %s).", tried)
            from veronica.llm.openai_compatible_provider import BUSY_MESSAGE
            content = BUSY_MESSAGE
        routing = dict(self.to_metadata())
        routing.update({"attempts": attempts, "used_fallback": True,
                        "fallback_used": True, "succeeded": False})
        return RoutedResponse(
            content=content,
            ok=False,
            routing=routing,
            succeeded=False,
        )

    def _try_one(
        self,
        entry: ModelEntry,
        messages: list[ChatMessage],
        tools: Optional[list[dict]],
        temperature: float,
        cfg: RoutingConfig,
        chain_pos: int,
        voice_mode: bool = False,
    ) -> dict:
        """Attempt one model. Returns {'response' or None, 'meta', 'failure_content'}.
        Never raises — every failure mode is captured and recorded on health."""
        h = entry.health
        if h.state is HealthState.UNAVAILABLE and h.method != "assumed" and not h.retry_due():
            return {"response": None, "meta": {"skipped": "unavailable"}, "failure_content": None}

        try:
            provider = entry.provider()
        except Exception as e:  # noqa: BLE001
            h.record_failure(str(e))
            return {"response": None, "meta": {"skipped": "provider_construction_failed"},
                    "failure_content": str(e)}

        effective_temp = entry.profile.temperature if entry.profile.temperature is not None else temperature
        start = time.monotonic()
        try:
            extra = {"voice_mode": True} if voice_mode else {}
            response = provider.chat(messages, tools=tools, temperature=effective_temp, **extra)
        except Exception as e:  # noqa: BLE001 — providers shouldn't raise, but defend
            h.record_failure(str(e))
            latency = time.monotonic() - start
            return {"response": None,
                    "meta": {"failed": f"{type(e).__name__}: {_short(str(e))}", "latency_s": round(latency, 3)},
                    "failure_content": str(e)}
        latency = time.monotonic() - start

        if response is None:
            h.record_failure("provider returned None")
            return {"response": None, "meta": {"failed": "empty_response"}, "failure_content": None}

        if not response.ok:
            h.record_failure(getattr(response, "content", "") or "ok=False")
            return {"response": None,
                    "meta": {"failed": "ok_false", "latency_s": round(latency, 3)},
                    "failure_content": getattr(response, "content", None) or None}

        if cfg.fallback_on_empty_response and not response.content and not response.tool_calls:
            h.record_failure("empty response (no content, no tool calls)")
            return {"response": None, "meta": {"failed": "empty_response", "latency_s": round(latency, 3)},
                    "failure_content": None}

        h.record_success(latency_s=latency)
        return {"response": response, "meta": {"latency_s": round(latency, 3)}, "failure_content": None}

    def _unsatisfied_response(self) -> "RoutedResponse":
        return RoutedResponse(
            content=(
                f"I can't fully handle this request: {', '.join(self.capability_gaps) or 'unsupported'}. "
                "No configured model supports the required capability, so I won't pretend it succeeded."
            ),
            ok=False,
            succeeded=False,
            routing=self.to_metadata(),
        )

    def streaming_provider(self) -> tuple[Optional[LLMProvider], Optional[str], Optional[str]]:
        """Streaming LLM V1: the SINGLE provider that should stream this turn, resolved
        BEFORE any text flows (no mid-stream model switch). Returns
        ``(provider, model_id, provider_name)`` when the routed model truthfully
        advertises CAP_STREAMING; otherwise ``(None, None, None)`` so the orchestrator
        falls back to the ordinary non-streaming chat() path — never a fake stream.

        An unsatisfied selection (no configured model can honour the required
        capabilities, e.g. the user asked to stream but no model advertises it) also
        returns None; the orchestrator reads ``capability_gaps`` to decline honestly.
        """
        if not self.satisfied or self._registry is None:
            return None, None, None
        entry = self._registry.get(self.model_id)
        if entry is None or not entry.profile.supports_streaming:
            return None, None, None
        try:
            provider = entry.provider()          # lazily built once, reused on later hops
        except Exception:  # noqa: BLE001 — a broken provider must not fake a stream
            return None, None, None
        return provider, entry.profile.model_id, entry.profile.provider


@dataclass
class RoutedResponse(LLMResponse):
    """LLMResponse + routing observability + a hard truth flag for callers."""
    routing: Optional[dict] = field(default=None, repr=False)
    succeeded: bool = True

    def __post_init__(self) -> None:
        if self.routing is None:
            self.routing = {}


class ModelRouter:
    """Owns routing policy. ``registry`` owns metadata; this selects from it."""

    def __init__(
        self,
        registry: ModelRegistry,
        config: Optional[RoutingConfig] = None,
    ) -> None:
        self.registry = registry
        self.config = config if config is not None else RoutingConfig()
        if self.config.eager_health_check:
            for entry in list(self.registry.find_by_role(ROLE_GENERAL))[:3]:
                try:
                    self.registry.check_health(entry.model_id)
                except Exception:  # noqa: BLE001
                    pass

    # -- selection ------------------------------------------------------------
    def route(self, task_profile: Any, context: Optional[dict] = None) -> ModelSelection:
        """Pick the model for this task. Deterministic given the same inputs."""
        ctx = context or {}
        t = task_profile
        base_meta = {
            "task_type": t.task_type,
            "complexity": getattr(t, "complexity", "medium"),
            "estimated_context_size": getattr(t, "estimated_context_size", 0),
        }

        # (1) explicit user/model request
        explicit = getattr(t, "explicit_model_request", None)
        explicit_entry = self.registry.get(explicit) if explicit else None
        if explicit and explicit_entry is not None and explicit_entry.profile.enabled:
            # Streaming is the only capability that can be "required by words" and the
            # user explicitly named a model. Honour the user's instruction (it wins),
            # but record the streaming gap so nothing is silently downgraded — the
            # caller (orchestrator) reads capability_gaps and declines to fake a stream.
            caps_for_explicit = [CAP_STREAMING] if getattr(t, "requires_streaming", False) else []
            return self._build_selection(
                explicit_entry, t, ctx, base_meta,
                decision="explicit",
                reason=["explicit_user_instruction", f"model={explicit}"],
                hard_caps=caps_for_explicit,
            )
        explicit_unknown = explicit is not None and explicit_entry is None
        if explicit is not None and explicit_entry is not None and not explicit_entry.profile.enabled:
            explicit_unknown = True  # disabled treated like unknown — record, don't honour

        # (2) required capabilities (hard filter — never silently downgraded)
        hard_caps: list[str] = []
        if getattr(t, "requires_vision", False):
            hard_caps.append(CAP_VISION)
        if getattr(t, "requires_tools", False) and ctx.get("tools_available", False):
            hard_caps.append(CAP_TOOLS)
        if getattr(t, "requires_streaming", False):
            hard_caps.append(CAP_STREAMING)

        # (3) context requirements (context_window fit)
        need = getattr(t, "estimated_context_size", 0) or 0

        candidates = [e for e in self.registry.list() if e.profile.enabled]
        if hard_caps:
            compliant = [e for e in candidates if all(e.profile.has_capability(c) for c in hard_caps)]
            if not compliant:
                gaps = [c for c in hard_caps if not any(e.profile.has_capability(c) for e in candidates)]
                return ModelSelection(
                    model_id="", provider_name="", role="",
                    reason=[f"required_capability:{g}" for g in gaps],
                    decision=f"unsupported_capability:{','.join(gaps)}",
                    satisfied=False,
                    capability_gaps=gaps,
                    metadata=base_meta,
                    _registry=self.registry, _config=self.config,
                )
            candidates = compliant

        # context fit: prefer models whose window fits the estimate
        fitting = [e for e in candidates if e.profile.context_window >= need] if need else candidates
        context_fit = bool(fitting)
        if fitting:
            candidates = fitting

        ranked = self._rank(candidates, t, ctx)
        if not ranked:
            return ModelSelection(
                model_id="", provider_name="", role="",
                reason=["no_eligible_model"],
                decision="no_eligible_model", satisfied=False,
                metadata=base_meta,
                _registry=self.registry, _config=self.config,
            )

        primary, rest = self._with_backups(ranked, t)
        # fallback: capability-compliant next-best models (preserves hard caps)
        fallbacks = [e.model_id for e in rest[: self.config.max_fallback_attempts]]

        extras: list[str] = []
        if explicit_unknown:
            # user asked for a model we don't have — record it, keep routing normally.
            # Never silently pick something else AS IF the request was honoured.
            extras = ["explicit_user_instruction_unavailable", f"model={explicit}"]

        return self._build_selection(
            primary, t, ctx, base_meta,
            decision="routed_with_unknown_explicit" if explicit_unknown else "routed",
            reason=extras + primary_reasons(primary, t, ctx),
            fallback_ids=fallbacks,
            context_fit=context_fit,
            hard_caps=hard_caps,
        )

    # -- helpers --------------------------------------------------------------
    def _build_selection(
        self,
        entry: ModelEntry,
        t: Any,
        ctx: dict,
        base_meta: dict,
        *,
        decision: str,
        reason: list[str],
        fallback_ids: Optional[list[str]] = None,
        context_fit: bool = True,
        hard_caps: Optional[list[str]] = None,
    ) -> ModelSelection:
        p = entry.profile
        gaps: list[str] = []
        for cap in hard_caps or []:
            if not p.has_capability(cap):
                gaps.append(cap)
        meta = dict(base_meta)
        meta.update(
            {
                "fallback_ids": fallback_ids or [],
                "health_state": entry.health.state.value,
                "health_method": entry.health.method,
                "context_fit": context_fit,
                "capabilities": sorted(p.capabilities),
            }
        )
        sel = ModelSelection(
            model_id=p.model_id,
            provider_name=p.provider,
            role=p.role,
            reason=reason,
            decision=decision,
            satisfied=True,
            capability_gaps=gaps,
            fallback_ids=fallback_ids or [],
            priority=p.priority,
            metadata=meta,
            _registry=self.registry,
            _config=self.config,
        )
        if self.config.log_routing_decisions:
            logger.info(
                "Routing decision: task=%s -> model=%s (provider=%s, role=%s, caps=%s) reason=%s",
                getattr(t, "task_type", "?"), p.model_id, p.provider, p.role,
                ",".join(sorted(p.capabilities)) or "-",
                ";".join(reason),
            )
        return sel

    @staticmethod
    def _with_backups(ranked: list[ModelEntry], t: Any) -> tuple[ModelEntry, list[ModelEntry]]:
        """(primary, fallbacks in order). Backup-role models (another provider) come
        FIRST among the fallbacks: when one Gemini model is overloaded the other
        usually is too, so the next try should be a different service. While the main
        model is in a recent outage and a backup is healthy, the backup goes first, so
        each turn doesn't wait on a request that is about to fail."""
        backups = [e for e in ranked if e.profile.role == ROLE_BACKUP]
        mains = [e for e in ranked if e.profile.role != ROLE_BACKUP]
        if not backups or not mains:
            return ranked[0], ranked[1:]
        heavy = getattr(t, "task_type", "") in (TASK_REASONING, TASK_CODING, TASK_ANALYSIS, TASK_PLANNING)
        if heavy:   # thinking tasks: the stronger backup first
            backups.sort(key=lambda e: 0 if e.profile.reasoning_class == "heavy" else 1)
        if mains[0].health.recently_failed() and not backups[0].health.recently_failed():
            return backups[0], backups[1:] + mains
        return mains[0], backups + mains[1:]

    def _rank(self, entries: list[ModelEntry], t: Any, ctx: dict) -> list[ModelEntry]:
        """Deterministic ranking with the spec's priority order as explicit TIERS:
          1) task-type aff   (explicit task_types pin > preferred role > other role)
          2) availability    (AVAILABLE > UNKNOWN > DEGRADED > UNAVAILABLE)
          3) configured priority (lower wins)
          4) latency fit     (only for latency_sensitive tasks)
          5) default flag    (breaks ties toward the default model)
          6) cost class      (local > free > ... > high)
          7) model_id        (last-resort deterministic tiebreak)
        This is why a low-priority "fast" model can never out-rank a task-matched
        coding/reasoning model — task affinity dominates priority."""
        task_type = getattr(t, "task_type", TASK_GENERAL)
        prefs = ROLE_PREFERENCES.get(task_type, (ROLE_GENERAL, ROLE_FAST))
        latency = getattr(t, "latency_sensitive", False) or ctx.get("latency_sensitive", False)
        speed_key = {SPEED_FAST: 0, SPEED_MEDIUM: 1, SPEED_SLOW: 2}
        avail_key = {
            HealthState.AVAILABLE: 0,
            HealthState.UNKNOWN: 1,
            HealthState.DEGRADED: 2,
            HealthState.UNAVAILABLE: 3,
        }

        def task_tier(e: ModelEntry) -> int:
            p = e.profile
            if task_type in p.task_types:
                return 0                    # explicit pin in config
            if p.role == prefs[0]:
                return 1
            if p.role == prefs[1]:
                return 2
            return 3

        def key(e: ModelEntry) -> tuple:
            p = e.profile
            return (
                task_tier(e),
                avail_key.get(e.health.state, 1),
                p.priority,
                speed_key.get(p.speed_class, 1) if latency else 0,
                0 if p.default else 1,
                COST_ORDER.get(p.cost_class, 5),
                p.model_id,
            )

        return sorted(entries, key=key)


def primary_reasons(entry: ModelEntry, t: Any, ctx: dict) -> list[str]:
    reasons = [f"task_type={getattr(t, 'task_type', TASK_GENERAL)}"]
    if getattr(t, "requires_vision", False):
        reasons.append("required_capability=vision")
    if getattr(t, "requires_tools", False) and ctx.get("tools_available", False):
        reasons.append("required_capability=tools")
    if getattr(t, "requires_streaming", False):
        reasons.append("required_capability=streaming")
    reasons.append(f"role={entry.profile.role}")
    reasons.append(f"priority={entry.profile.priority}")
    need = getattr(t, "estimated_context_size", 0)
    if need and entry.profile.context_window >= need:
        reasons.append("context_fit")
    elif need:
        reasons.append("context_oversize")
    return reasons


def _short(message: str, limit: int = 120) -> str:
    message = (message or "").strip()
    return message if len(message) <= limit else message[:limit - 1] + "…"


__all__ = [
    "ModelRouter", "ModelSelection", "RoutedResponse", "RoutingConfig",
    "ROLE_PREFERENCES",
]