"""
ModelRegistry: the single source of truth about which models exist, what they can
do, and (cached) whether they are healthy. It owns METADATA only — model selection
rules live in ModelRouter, provider I/O lives in the LLMProvider implementations.

Lazy by design: registering a model does not construct its provider or probe it.
Providers are cheap config+session objects, and we only build one when the router
actually routes to it (``entry.provider()``). ``check_health`` probes via the
provider's cheap ``is_available()`` and caches for ``health_ttl_s``.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from veronica.llm.base import LLMProvider
from veronica.llm.routing.health import HealthState, ModelHealth
from veronica.llm.routing.profiles import ModelProfile
from veronica.logging_setup import get_logger

logger = get_logger("llm.routing.registry")

# Sentinel for "provider construction failed" so we don't retry it every call.
_PROVIDER_FAILED = object()


@dataclass
class ModelEntry:
    """One registered model: profile (what it is) + lazy provider factory (how to
    talk to it) + health (how it's doing). No business logic for choosing."""
    profile: ModelProfile
    provider_factory: Callable[[], LLMProvider]
    health: ModelHealth = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "health", ModelHealth(model_id=self.profile.model_id))
        self._provider: Optional[LLMProvider] = None
        self._provider_error: Optional[str] = None
        self._lock = threading.Lock()

    @property
    def model_id(self) -> str:
        return self.profile.model_id

    def provider(self) -> LLMProvider:
        """Return THIS model's provider, constructing it once on first use. If
        construction fails, raise a clear error (the router falls back)."""
        if self._provider is not None:
            return self._provider
        with self._lock:
            if self._provider is not None:
                return self._provider
            try:
                self._provider = self.provider_factory()
            except Exception as e:  # noqa: BLE001 — construction must not crash routing
                self._provider_error = str(e)
                self.health.mark_unavailable(str(e))
                raise RuntimeError(
                    f"Provider for model '{self.model_id}' failed to construct: {e}"
                ) from e
            return self._provider


class ModelRegistry:
    """Metadata owner for every routable model. No routing decisions here."""

    def __init__(self, health_ttl_s: float = 30.0) -> None:
        self._entries: dict[str, ModelEntry] = {}
        self._health_ttl_s = max(0.1, health_ttl_s)
        self._lock = threading.RLock()
        self._store_path = None

    # -- registration ---------------------------------------------------------
    def register(self, profile: ModelProfile, provider_factory: Callable[[], LLMProvider]) -> ModelEntry:
        if not profile.model_id:
            raise ValueError("model_id is required to register a model")
        if profile.model_id in self._entries:
            raise ValueError(f"Duplicate model registration: {profile.model_id!r}")
        entry = ModelEntry(profile=profile, provider_factory=provider_factory)
        with self._lock:
            self._entries[profile.model_id] = entry
        logger.info("Registered routing model %s (provider=%s, role=%s)",
                    profile.model_id, profile.provider, profile.role)
        return entry

    def unregister(self, model_id: str) -> None:
        with self._lock:
            self._entries.pop(model_id, None)

    # -- lookup ---------------------------------------------------------------
    def get(self, model_id: str) -> Optional[ModelEntry]:
        return self._entries.get(model_id)

    def profile_of(self, model_id: str) -> Optional[ModelProfile]:
        entry = self._entries.get(model_id)
        return entry.profile if entry else None

    def list(self) -> list[ModelEntry]:
        return list(self._entries.values())

    def list_profiles(self) -> list[ModelProfile]:
        return [e.profile for e in self._entries.values()]

    def model_ids(self) -> list[str]:
        return sorted(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def is_enabled(self, model_id: str) -> bool:
        entry = self._entries.get(model_id)
        return bool(entry and entry.profile.enabled)

    # -- capability/type queries (pure, no probing) ---------------------------
    def find_capable(self, capability: Optional[str] = None, provider: Optional[str] = None) -> list[ModelEntry]:
        """Models that have the capability AND match the provider filter AND are enabled."""
        out = []
        for e in self._entries.values():
            p = e.profile
            if not p.enabled:
                continue
            if capability and not p.has_capability(capability):
                continue
            if provider and p.provider != provider:
                continue
            out.append(e)
        return out

    def find_by_role(self, role: str) -> list[ModelEntry]:
        return [e for e in self._entries.values() if e.profile.enabled and e.profile.role == role]

    def find_handles_task(self, task_type: str) -> list[ModelEntry]:
        return [e for e in self._entries.values() if e.profile.enabled and e.profile.handles_task(task_type)]

    def defaults(self) -> list[ModelEntry]:
        return [e for e in self._entries.values() if e.profile.enabled and e.profile.default]

    def default_entry(self) -> Optional[ModelEntry]:
        entries = self.defaults()
        if entries:
            return min(entries, key=lambda e: e.profile.priority)
        # No explicit default: fall back to the lowest-priority enabled general model.
        general = [e for e in self._entries.values() if e.profile.enabled and e.profile.role == "general"]
        if general:
            return min(general, key=lambda e: e.profile.priority)
        return None

    # -- health ---------------------------------------------------------------
    def health_of(self, model_id: str) -> Optional[ModelHealth]:
        entry = self._entries.get(model_id)
        return entry.health if entry else None

    def check_health(self, model_id: str, *, force: bool = False) -> Optional[ModelHealth]:
        """Cheap cached availability check. Probing uses the provider's is_available()
        (for Ollama: GET /api/tags — one HTTP call) and re-probes only after the TTL
        expires, so routing decisions never fan out probes."""
        entry = self._entries.get(model_id)
        if entry is None:
            return None
        h = entry.health
        probe = entry.provider
        if not force and h.checked_at is not None and (time.time() - h.checked_at) < self._health_ttl_s:
            return h
        try:
            p = probe()
        except Exception as e:  # noqa: BLE001
            h.mark_unavailable(str(e))
            return h
        try:
            available = bool(p.is_available())
        except Exception as e:  # noqa: BLE001 — is_available must not crash routing
            h.mark_unavailable(str(e))
            return h
        if available:
            h.mark_available()
        else:
            h.mark_unavailable(f"is_available() returned False for {model_id}")
        return h

    def health_summaries(self) -> dict[str, dict]:
        return {mid: entry.health.summary() for mid, entry in sorted(self._entries.items())}

    # -- health that survives a restart ---------------------------------------
    def attach_health_store(self, path) -> None:
        """Keep each model's set-aside on disk, so a restart doesn't re-hammer a model that
        is out of quota for the day. Only parked models are stored; a parked model whose
        cooldown has elapsed is tried again (dropped on load). Call once, after registering
        every model."""
        from pathlib import Path
        self._store_path = Path(path)
        saved: dict = {}
        try:
            if self._store_path.exists():
                import json
                saved = json.loads(self._store_path.read_text(encoding="utf-8")) or {}
        except Exception as e:  # noqa: BLE001
            logger.debug("Couldn't read the model-health store: %s", e)
        restored = 0
        with self._lock:
            for mid, entry in self._entries.items():
                data = saved.get(mid)
                if isinstance(data, dict) and entry.health.restore(data):
                    restored += 1
                entry.health.on_change = self._on_health_change
        if restored:
            logger.info("Restored %d model(s) still in their set-aside from last run.", restored)

    def _on_health_change(self, _health) -> None:
        if self._store_path is not None:
            self._save_health()

    def _save_health(self) -> None:
        """Write the currently-parked models (those with a set-aside still running)."""
        if self._store_path is None:
            return
        snapshot = {mid: entry.health.to_state() for mid, entry in self._entries.items()
                    if entry.health.set_aside_until() is not None}
        try:
            from veronica.core import jsonsafe
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            jsonsafe.write_json(self._store_path, snapshot)
        except Exception as e:  # noqa: BLE001 — persistence must never break routing
            logger.debug("Couldn't write the model-health store: %s", e)

    # -- registry metadata ----------------------------------------------------
    def summary(self) -> dict:
        return {
            "models": len(self._entries),
            "enabled": sum(1 for e in self._entries.values() if e.profile.enabled),
            "model_ids": self.model_ids(),
        }


__all__ = ["ModelRegistry", "ModelEntry"]