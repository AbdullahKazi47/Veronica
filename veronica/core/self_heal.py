"""
Self-healing: VERONICA watches her own health and fixes what she safely can, on her own.

What she heals automatically:
  * a long-lived worker thread that died (proactive monitor, background workers) is restarted
  * a corrupt memory/knowledge file is restored from its backup (see core/jsonsafe.py)

What she only reports (can't safely auto-fix):
  * the language model being unreachable
  * the disk being critically full

Hard rule: self-heal never edits code, never changes settings, and never touches the
permission gate. It restarts and restores; it does not rewrite VERONICA. A monitor that
could disable its own safety checks is one bad instruction away from breaking the system,
so it cannot. Everything it does is reported.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from veronica.core import jsonsafe
from veronica.logging_setup import get_logger

logger = get_logger("core.self_heal")


@dataclass
class HealthResult:
    name: str
    ok: bool
    detail: str = ""
    repaired: bool = False          # was failing, and self-heal fixed it this round


@dataclass
class Watch:
    """A group of daemon threads that should be running, and how to respawn them."""
    name_prefix: str
    expected: int
    restart: Callable[[], None]
    label: str = ""

    def alive(self) -> int:
        return sum(1 for t in threading.enumerate()
                   if t.is_alive() and t.name.startswith(self.name_prefix))


class HealthMonitor:
    def __init__(self, *, speak: Optional[Callable[[str, bool], None]] = None,
                 data_files: Optional[list[Path]] = None, llm=None, disk_free_min_gb: float = 1.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._speak = speak                     # speak(text, urgent) for things worth saying aloud
        self._data_files = [Path(p) for p in (data_files or [])]
        self._llm = llm
        self._disk_min = disk_free_min_gb
        self._clock = clock
        self._watches: list[Watch] = []
        self._stop = threading.Event()
        self._said: dict[str, float] = {}       # throttle repeated spoken warnings
        self.last_results: list[HealthResult] = []

    def watch_threads(self, name_prefix: str, expected: int, restart: Callable[[], None],
                      label: str = "") -> None:
        self._watches.append(Watch(name_prefix, max(1, expected), restart, label or name_prefix))

    # -- running --------------------------------------------------------------------------
    def start(self, every_s: float = 20.0) -> None:
        def loop() -> None:
            while not self._stop.wait(every_s):
                try:
                    self.check(repair=True)
                except Exception:               # the healer must never be what crashes
                    logger.exception("Self-heal round failed; continuing.")
        threading.Thread(target=loop, name="self-heal", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    # -- one round ------------------------------------------------------------------------
    def check(self, *, repair: bool = False) -> list[HealthResult]:
        results: list[HealthResult] = []
        results += self._check_threads(repair)
        results += self._check_data(repair)
        results.append(self._check_llm())
        results.append(self._check_disk())
        self.last_results = results
        if repair:
            for r in results:
                if r.repaired:
                    self._announce(r.name, f"Self-repair: {r.detail}", urgent=False)
                elif not r.ok:
                    self._announce(r.name, r.detail, urgent=(r.name == "disk"))
        return results

    def _check_threads(self, repair: bool) -> list[HealthResult]:
        out = []
        for w in self._watches:
            alive = w.alive()
            if alive >= w.expected:
                out.append(HealthResult(w.label, True, f"{alive} running"))
                continue
            detail = f"{w.label} had {alive} of {w.expected} workers running"
            if not repair:
                out.append(HealthResult(w.label, False, detail))
                continue
            try:
                w.restart()
                time.sleep(0.05)
                now = w.alive()
                if now >= w.expected:
                    out.append(HealthResult(w.label, True, f"restarted {w.label}", repaired=True))
                else:
                    out.append(HealthResult(w.label, False, f"{detail}; restart didn't take"))
            except Exception as e:              # noqa: BLE001
                logger.exception("Restarting %s failed", w.label)
                out.append(HealthResult(w.label, False, f"couldn't restart {w.label}: {e}"))
        return out

    def _check_data(self, repair: bool) -> list[HealthResult]:
        out = []
        for path in self._data_files:
            healthy, note = jsonsafe.check_file(path, repair=repair)
            if note:
                out.append(HealthResult(path.name, healthy, note, repaired=healthy))
            elif not healthy:
                out.append(HealthResult(path.name, False, f"{path.name} is corrupt."))
            else:
                out.append(HealthResult(path.name, True, "ok"))
        return out

    def _check_llm(self) -> HealthResult:
        if self._llm is None:
            return HealthResult("model", True, "not checked")
        try:
            ok = self._llm.is_available()
        except Exception as e:                  # noqa: BLE001
            return HealthResult("model", False, f"model check failed: {e}")
        return HealthResult("model", bool(ok),
                            "reachable" if ok else "the language model isn't reachable right now")

    def _check_disk(self) -> HealthResult:
        try:
            import shutil as _sh
            free_gb = _sh.disk_usage(Path.home().anchor or "C:\\").free / 1e9
        except Exception as e:                  # noqa: BLE001
            return HealthResult("disk", True, f"not checked: {e}")
        if free_gb < self._disk_min:
            return HealthResult("disk", False,
                                f"the drive is critically full, only {free_gb:.1f} gigabytes left")
        return HealthResult("disk", True, f"{free_gb:.0f} GB free")

    # -- reporting ------------------------------------------------------------------------
    def _announce(self, key: str, text: str, *, urgent: bool, cooldown_s: float = 1800) -> None:
        if self._speak is None:
            return
        now = self._clock()
        if now - self._said.get(key, -1e12) < cooldown_s:
            return
        self._said[key] = now
        try:
            self._speak(text, urgent)
        except Exception:                       # noqa: BLE001
            logger.exception("Self-heal couldn't announce: %s", text)

    def summary(self, results: Optional[list[HealthResult]] = None) -> str:
        results = results if results is not None else (self.last_results or self.check())
        repaired = [r for r in results if r.repaired]
        broken = [r for r in results if not r.ok]
        if repaired and not broken:
            return "I found and fixed " + _join(r.detail for r in repaired) + ". Everything's healthy now."
        if not broken:
            return "All systems healthy. Nothing needed fixing."
        parts = []
        if repaired:
            parts.append("I fixed " + _join(r.detail for r in repaired))
        parts.append("still a problem: " + _join(r.detail for r in broken))
        return ". ".join(p[0].upper() + p[1:] for p in parts) + "."


def _join(items) -> str:
    items = [i.rstrip(".") for i in items if i]
    if not items:
        return "nothing"
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]
