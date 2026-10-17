"""
Automatic long-term memory with Mem0 (github.com/mem0ai/mem0).

"Remember that ..." (memory_tools.py) only keeps what the user asks for. Mem0 also picks
facts out of ordinary conversation: say "I've got my networking exam on Friday" or "I
prefer tea" and a later turn can use it. After each conversational turn a background
worker sends the exchange to Mem0, whose model (Gemini Flash-Lite) writes short facts and
updates or drops ones that changed; each new request looks up the few that relate to it.

Where things go: the facts and their search index stay on this PC (data/mem0/, a local
Qdrant store + SQLite history). Making facts and searching uses Gemini over the internet,
like her replies do; the text is passed through the privacy guard first, and a turn that
talks about passwords, cards, PINs or ID numbers is never stored. Mem0's usage telemetry
is off. Remembered facts are data for answers, never instructions or permission.
"""
from __future__ import annotations

import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Callable, Optional

from veronica.logging_setup import get_logger

logger = get_logger("memory.mem0")

os.environ.setdefault("MEM0_TELEMETRY", "False")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

USER_ID = "owner"
_MIN_WORDS = 4                 # "thanks", "what time is it" carry no facts worth a model call


def available() -> bool:
    try:
        import mem0  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _build(config):
    from mem0 import Memory
    mc = getattr(config, "mem0", None)
    key = config.secret("GEMINI_API_KEY") or config.secret(config.llm.api_key_env)
    base = config.resolve_path("data/mem0")
    base.mkdir(parents=True, exist_ok=True)
    return Memory.from_config({
        "llm": {"provider": "gemini", "config": {"model": getattr(mc, "model", None) or config.llm.model,
                                                  "api_key": key, "temperature": 0.1}},
        "embedder": {"provider": "gemini", "config": {"model": "models/gemini-embedding-001",
                                                       "api_key": key, "embedding_dims": 768}},
        "vector_store": {"provider": "qdrant", "config": {"path": str(base / "qdrant"), "on_disk": True,
                                                          "collection_name": "veronica",
                                                          "embedding_model_dims": 768}},
        "history_db_path": str(base / "history.db"),
    })


class Mem0Memory:
    """Thread-safe wrapper: lazy start, background writes, time-limited reads."""

    def __init__(self, config=None, factory: Optional[Callable[[], object]] = None,
                 scrub: Optional[Callable[[str], str]] = None, is_secret: Optional[Callable[[str], bool]] = None,
                 recall_timeout_s: float = 1.5) -> None:
        self.config = config
        self._factory = factory or (lambda: _build(config))
        self._scrub = scrub or _privacy_scrub
        self._is_secret = is_secret or _secret_check
        self._timeout = recall_timeout_s
        self._mem = None
        self._failed = ""
        self._lock = threading.Lock()
        self._queue: "queue.Queue[tuple[str, str]]" = queue.Queue(maxsize=50)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mem0-read")
        self._worker: Optional[threading.Thread] = None

    def warm(self) -> None:
        """Start Mem0 now (loading takes a few seconds) so the first question isn't slowed."""
        threading.Thread(target=self._get, name="mem0-start", daemon=True).start()

    def _get(self):
        with self._lock:
            if self._mem is None and not self._failed:
                try:
                    self._mem = self._factory()
                except Exception as e:  # noqa: BLE001
                    self._failed = f"{type(e).__name__}: {e}"
                    logger.warning("Mem0 couldn't start: %s", self._failed)
            return self._mem

    # -- writing -----------------------------------------------------------------------
    def observe(self, user_text: str, reply: str) -> bool:
        """Queue one exchange to learn from. False when it is skipped."""
        user_text, reply = (user_text or "").strip(), (reply or "").strip()
        if len(user_text.split()) < _MIN_WORDS or self._is_secret(user_text):
            return False
        try:
            self._queue.put_nowait((user_text, reply))
        except queue.Full:
            return False
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._work, name="mem0-write", daemon=True)
            self._worker.start()
        return True

    def _work(self) -> None:
        while True:
            try:
                user_text, reply = self._queue.get(timeout=60)
            except queue.Empty:
                return
            mem = self._get()
            if mem is None:
                continue
            try:
                result = mem.add([{"role": "user", "content": self._scrub(user_text)},
                                  {"role": "assistant", "content": self._scrub(reply)[:600]}], user_id=USER_ID)
                changed = [r for r in (result or {}).get("results", []) if r.get("event") in ("ADD", "UPDATE")]
                if changed:
                    logger.info("Mem0 learned %d fact(s).", len(changed))
            except Exception as e:  # noqa: BLE001 - a lost fact must never break a turn
                logger.info("Mem0 couldn't save that exchange: %s", type(e).__name__)

    def flush(self, timeout_s: float = 30.0) -> None:
        """Wait for queued exchanges (tests, shutdown)."""
        import time
        end = time.time() + timeout_s
        while not self._queue.empty() and time.time() < end:
            time.sleep(0.1)

    # -- reading -----------------------------------------------------------------------
    def recall(self, text: str, k: int = 5, min_score: float = 0.2) -> list[str]:
        """Facts related to this request, best first. [] when slow, offline or off."""
        if not (text or "").strip() or self._failed:
            return []

        def look():
            mem = self._get()
            if mem is None:
                return []
            hits = (mem.search(self._scrub(text), filters={"user_id": USER_ID}, top_k=k) or {}).get("results", [])
            return [h["memory"] for h in hits if h.get("memory") and (h.get("score") or 0) >= min_score]
        try:
            return self._pool.submit(look).result(timeout=self._timeout)
        except FutureTimeout:
            return []
        except Exception:  # noqa: BLE001
            return []

    def all(self, limit: int = 100) -> list[dict]:
        mem = self._get()
        if mem is None:
            return []
        try:
            return (mem.get_all(filters={"user_id": USER_ID}, top_k=limit) or {}).get("results", [])
        except Exception:  # noqa: BLE001
            return []

    # -- forgetting --------------------------------------------------------------------
    def forget_matching(self, which: str, min_score: float = 0.35) -> list[str]:
        """Delete the facts closest to `which`; returns what was forgotten."""
        mem = self._get()
        if mem is None or not (which or "").strip():
            return []
        try:
            hits = (mem.search(which, filters={"user_id": USER_ID}, top_k=3) or {}).get("results", [])
        except Exception:  # noqa: BLE001
            return []
        gone = []
        for h in hits:
            if (h.get("score") or 0) >= min_score:
                try:
                    mem.delete(h["id"])
                    gone.append(h.get("memory", ""))
                except Exception:  # noqa: BLE001
                    pass
        return gone

    def forget_all(self) -> int:
        mem = self._get()
        if mem is None:
            return 0
        n = len(self.all(limit=1000))
        try:
            mem.delete_all(user_id=USER_ID)
        except Exception:  # noqa: BLE001
            return 0
        return n


def _privacy_scrub(text: str) -> str:
    from veronica.safety import privacy
    return privacy.get_guard().scrub_text(text)


def _secret_check(text: str) -> bool:
    from veronica.tools.memory_tools import is_secret
    return is_secret(text)


_SHARED: Optional[Mem0Memory] = None
_SHARED_LOCK = threading.Lock()


def get_mem0(config) -> Optional[Mem0Memory]:
    """The one Mem0Memory for this run, or None when it is off or not installed."""
    global _SHARED
    mc = getattr(config, "mem0", None)
    if config is None or mc is None or not getattr(mc, "enabled", False) or not available():
        return None
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = Mem0Memory(config)
        return _SHARED
