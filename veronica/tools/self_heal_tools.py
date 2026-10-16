"""
"Veronica, run a self-check" / "heal yourself" / "are you okay?" -> run the health
checks, fix what's safely fixable (restart dead workers, restore corrupt memory files),
and report. No permission needed: restarting and restoring are non-destructive.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Optional

from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

# CLIApp points this at the live HealthMonitor so "self check" uses the running watchdog
# (with its thread watches). Left None in plain text mode -> a one-shot file+model check.
_live_monitor: Optional[Callable[[], object]] = None


def set_live_monitor(provider: Optional[Callable[[], object]]) -> None:
    global _live_monitor
    _live_monitor = provider


_SELF_HEAL = re.compile(
    r"\b(?:heal|fix|repair|check|diagnose)\s+your\s?self\b|"
    r"\b(?:run|do)\s+(?:a\s+)?(?:self[\s-]?(?:check|test|diagnostic|heal)|health\s+check|system\s+check)\b|"
    r"\bself[\s-]?(?:check|heal|repair|diagnostic)\b|"
    r"\bare\s+you\s+(?:ok|okay|alright|healthy|working\s+(?:ok|okay|properly))\b|"
    r"\bis\s+everything\s+(?:ok|okay|working)\s+(?:with\s+you)?\b", re.I)


class SelfHealTool(Tool):
    name = "self_heal"
    description = ("Run VERONICA's own health check and fix what's safely fixable: restart a stopped "
                   "background worker, restore a corrupted memory file from backup. Reports what it found "
                   "and did. Use for 'run a self check', 'heal yourself', 'are you okay'. Never edits code "
                   "or settings.")
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, config=None, monitor_provider: Optional[Callable[[], object]] = None) -> None:
        self.config = config
        self._provider = monitor_provider

    def matches(self, text: str) -> bool:
        return bool(_SELF_HEAL.search(text))

    def _monitor(self):
        provider = self._provider or _live_monitor
        if provider is not None:
            mon = provider()
            if mon is not None:
                return mon
        return self._one_shot()

    def _one_shot(self):
        """No live watchdog (text mode / tests): check this config's data files and model."""
        from veronica.core.self_heal import HealthMonitor
        files, llm = [], None
        if self.config is not None:
            files = _data_files(self.config)
            try:
                from veronica.llm.router import build_provider
                llm = build_provider(self.config)
            except Exception:  # noqa: BLE001
                llm = None
        return HealthMonitor(data_files=files, llm=llm)

    def run(self, **kwargs: Any) -> ToolResult:
        mon = self._monitor()
        try:
            results = mon.check(repair=True)
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"My self-check itself ran into trouble: {e}")
        healthy = all(r.ok for r in results)
        return ToolResult.ok(mon.summary(results), healthy=healthy,
                             issues=[r.detail for r in results if not r.ok])


def _data_files(config) -> list[Path]:
    """The JSON memory/state files worth checking and repairing."""
    rels = []
    mem = getattr(config, "memory", None)
    if mem is not None:
        rels += [getattr(mem, "long_term_path", ""), getattr(mem, "confidence_path", "")]
    coding = getattr(config, "coding", None)
    if coding is not None:
        rels.append(getattr(coding, "knowledge_path", ""))
    rels += ["data/profile.json", "data/reminders.json", "data/todos.json", "data/app_index.json"]
    out, seen = [], set()
    for rel in rels:
        if not rel:
            continue
        p = config.resolve_path(rel)
        if str(p) not in seen:
            seen.add(str(p))
            out.append(p)
    return out
