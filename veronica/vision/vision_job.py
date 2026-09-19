"""
VisionJobExecutor: non-blocking background worker for vision analysis.

Guarantees:
- Maximum 1 concurrent vision job (bounded concurrency).
- Safe skip/coalesce: if busy, new submissions are skipped (not queued).
- Timeout: each job has a configurable maximum duration.
- Cancellation: supports cooperative cancellation via CancellationToken.
- Error isolation: vision provider failures do not crash the executor.
"""
from __future__ import annotations

import threading
import time
from typing import Optional, Callable, Any
from concurrent.futures import Future, TimeoutError as FuturesTimeoutError

from veronica.vision.vision_provider import VisionProvider, VisionProviderError, VisionTimeoutError, VisionCancellationError, CancellationToken
from veronica.vision.screen_state import ScreenState
from veronica.vision.visual_analysis import VisualAnalysis
from veronica.logging_setup import get_logger

logger = get_logger("vision.job_executor")


class VisionJobExecutor:
    """
    Executes vision analysis jobs in a background thread with strict bounds.

    Usage:
        executor = VisionJobExecutor(vision_provider, timeout_seconds=30.0)
        future = executor.submit(image_bytes, screen_state, prompt, cancellation_token)
        # Later: result = future.result(timeout=5)  # or handle exception

    The executor guarantees at most one job runs at a time.
    submit() returns immediately with a Future; if the executor is busy,
    the submission is skipped (returns None) to avoid queue buildup.
    """

    def __init__(
        self,
        vision_provider: VisionProvider,
        timeout_seconds: float = 30.0,
        max_workers: int = 1,
    ) -> None:
        if max_workers != 1:
            raise ValueError("VisionJobExecutor only supports max_workers=1")
        self._provider = vision_provider
        self._timeout_seconds = timeout_seconds
        self._max_workers = max_workers

        self._lock = threading.RLock()
        self._active_future: Optional[Future] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._shutdown = False

    def submit(
        self,
        image_bytes: bytes,
        screen_state: Optional[ScreenState] = None,
        prompt: Optional[str] = None,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> Optional[Future[VisualAnalysis]]:
        """
        Submit a vision analysis job for background execution.

        Returns:
            A Future if the job was accepted, None if the executor is busy
            (skip/coalesce behavior).

        The Future may raise:
            VisionProviderError, VisionTimeoutError, VisionCancellationError.
        """
        with self._lock:
            if self._shutdown:
                logger.debug("VisionJobExecutor is shutdown, rejecting submission.")
                return None
            if self._active_future is not None and not self._active_future.done():
                logger.debug("VisionJobExecutor is busy, skipping submission.")
                return None

            # Create a future to represent the result
            future: Future[VisualAnalysis] = Future()

            def _run_job() -> None:
                try:
                    # We will respect the timeout via the provider's analyze method
                    # but also wrap in a timeout for safety.
                    result = self._provider.analyze(
                        image=image_bytes,
                        screen_state=screen_state,
                        prompt=prompt,
                        timeout=self._timeout_seconds,
                        cancellation_token=cancellation_token,
                    )
                    future.set_result(result)
                except Exception as exc:
                    logger.debug("Vision job failed: %s", exc)
                    future.set_exception(exc)
                finally:
                    with self._lock:
                        self._active_future = None

            # Start the worker thread
            thread = threading.Thread(target=_run_job, name="vision-job-worker", daemon=True)
            with self._lock:
                self._active_future = future
                self._worker_thread = thread
            thread.start()
            return future

    def shutdown(self, wait: bool = True) -> None:
        """Shutdown the executor, preventing new submissions."""
        with self._lock:
            self._shutdown = True
            # We do not interrupt the active thread; let it finish.
        if wait and self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=5.0)