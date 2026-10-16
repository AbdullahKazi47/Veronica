"""
Background tasks: "research X in the background", "do Y while I play my game".

A task runs through the same Orchestrator.handle() as anything spoken (same tools, same
PermissionGate, same routing) but in its own private session on a worker thread, so the
conversation carries on. When it finishes VERONICA announces the result; every result is
also kept in data/background_tasks.json for "what did you do while I was away?".

Safety: a background task can never ask for permission — the user may be away or in a
game, and a spoken "yes" meant for the foreground must not approve it. Anything that would
need confirmation is refused inside the task and reported when it finishes.
"""
from __future__ import annotations

import datetime as dt
import json
import queue
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from veronica.logging_setup import get_logger

logger = get_logger("core.background")

# Thread-local marker read by the permission prompt (ui/cli.py) to refuse instead of asking.
_local = threading.local()


def in_background() -> bool:
    return bool(getattr(_local, "active", False))


def note_permission_refused(description: str) -> None:
    refused = getattr(_local, "refused", None)
    if refused is not None:
        refused.append(description)


# "... in the background" / "... while I play" / "... and let me know when it's done"
_CUES = re.compile(
    r"(?:[,\s]+(?:and\s+)?(?:do\s+(?:it|that|this)\s+)?in\s+the\s+background\b.*$)|"
    r"(?:[,\s]+(?:while|when)\s+i(?:'m|\s+am)?\s+(?:play|playing|gaming|away|out|gone|busy|working|cooking|"
    r"sleeping|in\s+a\s+(?:game|meeting)|at\s+\w+|go(?:ing)?\b)\b.*$)|"
    r"(?:[,\s]+(?:and\s+)?(?:then\s+)?i(?:'ll|\s+will)\s+(?:go|be|play)\b.*$)|"
    r"(?:[,\s]+(?:and\s+)?(?:let|tell)\s+me\s+(?:know\s+)?when\s+(?:you(?:'re|\s+are)|it(?:'s|\s+is))\s+(?:done|finished|ready)\b.*$)",
    re.I)
_STATUS = re.compile(
    r"\b(?:what\s+(?:are\s+you|have\s+you\s+been)\s+(?:working\s+on|doing)|status\s+of\s+(?:my\s+)?(?:background\s+)?tasks?|"
    r"(?:are|is)\s+(?:you|it|my\s+task)\s+(?:done|finished)\s*(?:yet)?|what\s+did\s+you\s+do\s+while\s+i\s+was\s+(?:away|gone|out)|"
    r"any\s+(?:background\s+)?tasks?\s+(?:running|done|finished)|(?:background|my)\s+tasks?\s+status)\b", re.I)
_LEAD = re.compile(r"^\s*(?:(?:hey\s+)?veronica[,\s]+)?(?:please\s+|can\s+you\s+|could\s+you\s+)?", re.I)


def split_background_request(text: str) -> Optional[str]:
    """'research X in the background' -> 'research X'; None if it isn't a background ask."""
    m = _CUES.search(text or "")
    if not m:
        return None
    task = _LEAD.sub("", text[: m.start()]).strip(" ,.")
    return task or None


def is_status_request(text: str) -> bool:
    return bool(_STATUS.search(text or ""))


@dataclass
class BackgroundTask:
    id: str
    request: str
    status: str = "queued"            # queued | running | done | failed
    result: str = ""
    refused: list[str] = field(default_factory=list)
    created: str = field(default_factory=lambda: dt.datetime.now().isoformat(timespec="seconds"))
    finished: str = ""
    announced: bool = False


class BackgroundTasks:
    def __init__(self, orchestrator, announce: Callable[[BackgroundTask], None],
                 store_path: Optional[Path] = None, workers: int = 1,
                 session_factory: Optional[Callable[[], object]] = None) -> None:
        self.orchestrator = orchestrator
        self.announce = announce
        self.store_path = store_path
        self._session_factory = session_factory or self._private_session
        self._lock = threading.Lock()
        self._tasks: list[BackgroundTask] = self._load()
        self._queue: "queue.Queue[BackgroundTask]" = queue.Queue()
        self._workers = max(1, workers)
        self._worker_seq = 0
        self._worker_threads: list[threading.Thread] = []
        self.ensure_workers()

    def ensure_workers(self) -> int:
        """Start as many of THIS pool's worker threads as have died (or not started yet).
        Called at startup and by self-heal when a worker stops. Counts only its own
        threads so separate pools (e.g. in tests) never starve each other. Returns how
        many it started."""
        with self._lock:
            self._worker_threads = [t for t in self._worker_threads if t.is_alive()]
            started = 0
            while len(self._worker_threads) < self._workers:
                self._worker_seq += 1
                t = threading.Thread(target=self._work, name=f"background-{id(self)}-{self._worker_seq}",
                                     daemon=True)
                t.start()
                self._worker_threads.append(t)
                started += 1
            return started

    # -- persistence --------------------------------------------------------------------
    def _load(self) -> list[BackgroundTask]:
        if not self.store_path or not self.store_path.exists():
            return []
        try:
            tasks = [BackgroundTask(**t) for t in json.loads(self.store_path.read_text(encoding="utf-8"))]
        except Exception:
            return []
        for t in tasks:  # anything mid-flight when VERONICA stopped didn't finish
            if t.status in ("queued", "running"):
                t.status, t.result = "failed", "VERONICA was closed before this finished."
        return tasks[-50:]

    def _save(self) -> None:
        if not self.store_path:
            return
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store_path.with_suffix(".tmp")
            tmp.write_text(json.dumps([asdict(t) for t in self._tasks[-50:]], indent=1, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self.store_path)
        except OSError:
            pass

    # -- running ------------------------------------------------------------------------
    def _private_session(self):
        # Own conversation, never persisted: background work must not leak into (or be
        # confused by) the spoken conversation, and must not overwrite its saved file.
        from veronica.core.context import SessionContext
        from veronica.memory.short_term import ShortTermMemory
        return SessionContext(short_term=ShortTermMemory(max_messages=20))

    def submit(self, request: str) -> BackgroundTask:
        task = BackgroundTask(id=uuid.uuid4().hex[:6], request=request)
        with self._lock:
            self._tasks.append(task)
            self._save()
        self._queue.put(task)
        return task

    def _work(self) -> None:
        while True:
            task = self._queue.get()
            _local.active, _local.refused = True, []
            task.status = "running"
            try:
                response = self.orchestrator.handle(task.request, self._session_factory())
                task.result = (getattr(response, "text", "") or "").strip() or "Done."
                task.status = "done"
            except Exception as e:  # noqa: BLE001 — a task failing must not kill the worker
                logger.exception("Background task failed: %s", task.request)
                task.result, task.status = f"It failed: {e}", "failed"
            finally:
                task.refused = list(_local.refused)
                _local.active, _local.refused = False, None
                task.finished = dt.datetime.now().isoformat(timespec="seconds")
                with self._lock:
                    self._save()
            try:
                self.announce(task)
                task.announced = True
                with self._lock:
                    self._save()
            except Exception:
                logger.exception("Announcing background task failed")

    # -- status -------------------------------------------------------------------------
    def tasks(self) -> list[BackgroundTask]:
        with self._lock:
            return list(self._tasks)

    def status_report(self) -> str:
        tasks = self.tasks()
        running = [t for t in tasks if t.status in ("queued", "running")]
        recent = [t for t in tasks if t.status in ("done", "failed")][-3:]
        if not running and not recent:
            return "I'm not working on anything in the background."
        parts = []
        if running:
            parts.append("I'm still working on " + "; ".join(t.request for t in running) + ".")
        for t in reversed(recent):
            parts.append(f"{'Finished' if t.status == 'done' else 'Could not finish'}: {t.request}. {t.result}")
        return " ".join(parts)
