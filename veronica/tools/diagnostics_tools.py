"""
"Veronica, run diagnostics" — a spoken health report of this PC: CPU, memory, disk,
battery, uptime, internet, and what is using the most resources. The same readings feed
the proactive monitor (core/proactive.py), which speaks up on its own when something is off.

Only what Windows exposes honestly: CPU temperature isn't readable on most laptops without
vendor drivers, so it's reported as unavailable instead of invented.
"""
from __future__ import annotations

import datetime as dt
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

logger = get_logger("tools.diagnostics")

# Ignore the idle pseudo-process and VERONICA's own interpreter when naming culprits.
_IGNORED_PROCS = {"system idle process", "idle", "system", "registry", "memcompression"}


@dataclass
class Reading:
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    memory_used_gb: float = 0.0
    memory_total_gb: float = 0.0
    disk_free_gb: float = 0.0
    disk_percent: float = 0.0
    battery_percent: Optional[float] = None
    plugged_in: Optional[bool] = None
    battery_minutes_left: Optional[int] = None
    uptime_hours: float = 0.0
    online: Optional[bool] = None
    top_cpu: list[tuple[str, float]] = field(default_factory=list)
    top_memory: list[tuple[str, float]] = field(default_factory=list)


def _friendly(name: str) -> str:
    return re.sub(r"\.exe$", "", name or "", flags=re.I)


def collect(sample_s: float = 0.6, check_online: bool = True) -> Reading:
    import os
    import psutil
    r = Reading()
    procs = []
    me = os.getpid()
    for p in psutil.process_iter(["name"]):
        if p.pid == me:
            continue  # VERONICA measuring herself is not "what's slowing the PC"
        try:
            p.cpu_percent(None)  # prime per-process counters
            procs.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    r.cpu_percent = psutil.cpu_percent(interval=sample_s)
    cores = psutil.cpu_count() or 1
    by_cpu: dict[str, float] = {}
    by_mem: dict[str, float] = {}
    for p in procs:
        try:
            name = (p.info.get("name") or "").strip()
            if not name or name.lower() in _IGNORED_PROCS:
                continue
            by_cpu[name] = by_cpu.get(name, 0.0) + p.cpu_percent(None) / cores
            by_mem[name] = by_mem.get(name, 0.0) + p.memory_info().rss / 1e9
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    r.top_cpu = sorted(((_friendly(n), round(v, 1)) for n, v in by_cpu.items()), key=lambda x: -x[1])[:3]
    r.top_memory = sorted(((_friendly(n), round(v, 2)) for n, v in by_mem.items()), key=lambda x: -x[1])[:3]

    vm = psutil.virtual_memory()
    r.memory_percent, r.memory_used_gb, r.memory_total_gb = vm.percent, round(vm.used / 1e9, 1), round(vm.total / 1e9, 1)
    try:
        du = psutil.disk_usage("C:\\" if psutil.WINDOWS else "/")
        r.disk_free_gb, r.disk_percent = round(du.free / 1e9, 1), du.percent
    except Exception:
        pass
    b = psutil.sensors_battery()
    if b is not None:
        r.battery_percent, r.plugged_in = round(b.percent), bool(b.power_plugged)
        if not b.power_plugged and b.secsleft not in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN):
            r.battery_minutes_left = int(b.secsleft // 60)
    r.uptime_hours = round((time.time() - psutil.boot_time()) / 3600, 1)
    if check_online:
        try:
            import socket
            socket.create_connection(("1.1.1.1", 53), timeout=2).close()
            r.online = True
        except OSError:
            r.online = False
    return r


def assess(r: Reading) -> list[str]:
    """Problems worth mentioning, most important first (empty = all normal)."""
    issues = []
    if r.battery_percent is not None and not r.plugged_in and r.battery_percent <= 20:
        issues.append(f"battery is at {r.battery_percent} percent and not charging")
    if r.cpu_percent >= 85:
        culprit = f", mostly {r.top_cpu[0][0]}" if r.top_cpu else ""
        issues.append(f"the processor is at {round(r.cpu_percent)} percent{culprit}")
    if r.memory_percent >= 90:
        culprit = f", mostly {r.top_memory[0][0]}" if r.top_memory else ""
        issues.append(f"memory is {round(r.memory_percent)} percent full{culprit}")
    if r.disk_free_gb and r.disk_free_gb < 10:
        issues.append(f"only {r.disk_free_gb} gigabytes free on the C drive")
    if r.online is False:
        issues.append("there's no internet connection")
    if r.uptime_hours >= 72:
        issues.append(f"it hasn't been restarted in {round(r.uptime_hours / 24)} days")
    return issues


def report(r: Reading) -> str:
    issues = assess(r)
    head = "All systems normal." if not issues else \
        "I found " + ("one thing" if len(issues) == 1 else f"{len(issues)} things") + " worth a look: " + \
        "; ".join(issues) + "."
    battery = ""
    if r.battery_percent is not None:
        battery = f" Battery {r.battery_percent} percent, {'plugged in' if r.plugged_in else 'on battery'}"
        battery += f", about {r.battery_minutes_left // 60} hours {r.battery_minutes_left % 60} minutes left." \
            if r.battery_minutes_left else "."
    busiest = f" Busiest right now: {', '.join(n for n, _ in r.top_cpu[:2])}." if r.top_cpu else ""
    return (f"{head} Processor at {round(r.cpu_percent)} percent, memory {round(r.memory_percent)} percent "
            f"({r.memory_used_gb} of {r.memory_total_gb} gigabytes), {r.disk_free_gb} gigabytes free on C."
            f"{battery}{busiest} Up for {_uptime(r.uptime_hours)}."
            f"{'' if r.online is not False else ' No internet.'}")


def _uptime(hours: float) -> str:
    if hours < 1:
        return f"{round(hours * 60)} minutes"
    if hours < 48:
        return f"{round(hours)} hours"
    return f"{round(hours / 24)} days"


_DIAG = re.compile(
    r"\b(?:run\s+(?:a\s+|the\s+)?(?:full\s+)?diagnostics?|(?:system|pc|computer|laptop)\s+(?:check|diagnostics?|health|report)|"
    r"(?:how(?:'s|\s+is)|check)\s+my\s+(?:pc|computer|laptop|system)(?:\s+doing)?|"
    r"what(?:'s|\s+is)\s+slowing\s+(?:down\s+)?my\s+(?:pc|computer|laptop|system)|why\s+is\s+my\s+(?:pc|computer|laptop)\s+(?:so\s+)?slow)\b",
    re.I)


class RunDiagnosticsTool(Tool):
    name = "run_diagnostics"
    description = ("Full health check of this PC: CPU, memory, disk, battery, uptime, internet, and which "
                   "programs use the most resources. Use for 'run diagnostics' or 'why is my PC slow'.")
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, collector=collect) -> None:
        self._collect = collector

    def matches(self, text: str) -> bool:
        return bool(_DIAG.search(text))

    def run(self, **kwargs: Any) -> ToolResult:
        try:
            r = self._collect()
        except Exception as e:  # noqa: BLE001
            logger.warning("Diagnostics failed: %s", e)
            return ToolResult.fail("I couldn't read the system sensors.")
        return ToolResult.ok(report(r), issues=assess(r), cpu=r.cpu_percent, memory=r.memory_percent,
                             disk_free_gb=r.disk_free_gb, battery=r.battery_percent,
                             top_cpu=r.top_cpu, top_memory=r.top_memory)
