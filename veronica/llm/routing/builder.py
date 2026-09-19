"""
Assembly for Multi-Model Routing V1: turn ``config.routing`` + the global ``llm.*``
block into a working (ModelRegistry, TaskAnalyzer, ModelRouter) bundle.

Rule: this is the ONLY place that turns routing config into profiles/provider
factories, and it reuses ``llm.router.build_provider_for_model`` for actual provider
construction — no second implementation of the provider family.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from veronica.config import Config, ModelEntryConfig
from veronica.llm.router import build_provider_for_model
from veronica.llm.routing.profiles import ModelProfile, ROLE_GENERAL
from veronica.llm.routing.registry import ModelRegistry
from veronica.llm.routing.router import ModelRouter, RoutingConfig
from veronica.llm.routing.task import TaskAnalyzer
from veronica.logging_setup import get_logger

logger = get_logger("llm.routing.builder")

# Validate our task/role vocabulary against what role names are legal.
_ALLOWED_ROLES = {"general", "fast", "reasoning", "coding", "vision"}


@dataclass
class RoutingAssembly:
    registry: ModelRegistry
    analyzer: TaskAnalyzer
    router: ModelRouter


def _to_profile(entry: ModelEntryConfig, *, default_id: str) -> ModelProfile:
    caps = set(entry.capabilities or ())
    if entry.supports_tools:
        caps.add("tools")
    if entry.supports_vision:
        caps.add("vision")
    if entry.supports_streaming:
        caps.add("streaming")

    task_types = set(t for t in (entry.task_types or ()) if t)
    role = entry.role or ROLE_GENERAL
    if role not in _ALLOWED_ROLES:
        role = ROLE_GENERAL

    return ModelProfile(
        model_id=entry.model_id,
        provider=entry.provider,
        role=role,
        capabilities=frozenset(caps),
        task_types=frozenset(task_types),
        context_window=entry.context_window,
        supports_tools="tools" in caps,
        supports_vision="vision" in caps,
        supports_streaming="streaming" in caps,
        speed_class=entry.speed_class or "medium",
        reasoning_class=entry.reasoning_class or "none",
        cost_class=entry.cost_class or "local",
        priority=entry.priority,
        enabled=entry.enabled,
        default=entry.default,
        base_url=entry.base_url,
        api_key_env=entry.api_key_env,
        temperature=entry.temperature,
        timeout_s=entry.timeout_s,
    )


def _provider_factory(
    profile: ModelProfile,
    *,
    fallback_base_url: str,
    fallback_api_key: Optional[str],
    timeout_s: int,
):
    """Build a lazily-invoked factory for this profile's provider. Global defaults
    from config.llm fill in whatever the model entry doesn't override.

    api_key resolution: a profile.api_key_env is an env-var NAME resolved at
    construction time (like config.llm.api_key_env); otherwise the globally-resolved
    fallback key (config.secret(config.llm.api_key_env)) is carried over."""
    base_url = profile.base_url or fallback_base_url
    const_env_var = profile.api_key_env  # env var NAME, may be None
    carried_key = None if const_env_var else fallback_api_key

    def factory():
        import os
        api_key = os.environ.get(const_env_var) if const_env_var else carried_key
        return build_provider_for_model(
            profile.provider,
            model=profile.model_id,
            base_url=base_url,
            api_key=api_key,
            timeout_s=profile.timeout_s or timeout_s,
        )

    return factory


def build_routing(config: Config) -> Optional[RoutingAssembly]:
    """Build the routing bundle from config, or return None when routing is disabled.
    Raises ValueError on a bad routing config so callers can degrade gracefully."""
    if not config.routing.enabled:
        return None

    rc = config.routing
    router_cfg = RoutingConfig(
        enabled=True,
        fallback_enabled=rc.fallback_enabled,
        fallback_on_empty_response=rc.fallback_on_empty_response,
        max_fallback_attempts=max(1, rc.max_fallback_attempts),
        health_ttl_s=max(0.1, rc.health_ttl_s),
        eager_health_check=False,
        log_routing_decisions=rc.log_routing_decisions,
    )

    fallback_key = config.secret(config.llm.api_key_env)

    registry = ModelRegistry(health_ttl_s=router_cfg.health_ttl_s)

    entries: list[ModelEntryConfig]
    if rc.models:
        entries = list(rc.models)
    else:
        # No explicit models → register the global single-model config as the
        # routing default (backward compatible: routing now uses exactly what the
        # single-provider path would have used).
        entries = [
            ModelEntryConfig(
                model_id=config.llm.model,
                provider=config.llm.provider,
                role=ROLE_GENERAL,
                base_url=None,  # fall back to global base_url below
                api_key_env=None,
                capabilities=(),
                task_types=(),
                context_window=8192,
                # The single-model path always handed tools to the model, so the
                # synthesized default must declare tool support to preserve 1:1 behavior.
                supports_tools=True,
                enabled=True,
                default=True,
            )
        ]

    if not entries:
        raise ValueError("routing.enabled is true but routing.models is empty and no llm default exists")

    seen: set[str] = set()
    for entry in entries:
        if entry.model_id in seen:
            raise ValueError(f"Duplicate model in routing.models: {entry.model_id!r}")
        seen.add(entry.model_id)
        if entry.provider.lower() not in {"ollama", "openai_compatible", "echo"}:
            raise ValueError(f"Unknown provider {entry.provider!r} for model {entry.model_id!r}")
        profile = _to_profile(entry, default_id=entry.model_id)
        factory = _provider_factory(
            profile,
            fallback_base_url=config.llm.base_url,
            fallback_api_key=fallback_key,
            timeout_s=config.llm.request_timeout_s,
        )
        registry.register(profile, factory)

    if registry.default_entry() is None:
        # Ensure there is always a default to fall back to.
        raise ValueError("No default model: set default: true on one entry, or add a general model")

    analyzer = TaskAnalyzer(known_model_ids=set(registry.model_ids()))
    router = ModelRouter(registry, config=router_cfg)
    return RoutingAssembly(registry=registry, analyzer=analyzer, router=router)


__all__ = ["build_routing", "RoutingAssembly", "_to_profile", "_provider_factory"]