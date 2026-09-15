"""
Stitch Bridge: the thin HTTP/SSE transport that connects the Stitch GUI (HTML/JS, in
this same directory) to a running VERONICA Orchestrator.

Design (mirrors the "UI is a swappable thin client" rule):
  - The page is a static file served by this same server (no CORS, no separate
    front-end build step). It talks to the backend over two stdlib-only channels:
      * POST /api  {method, args}  — client -> server requests.
      * GET  /events (Server-Sent Events) — server -> client push of signals.
    The JS in index.html adapts exactly the signal vocabulary the original ULTRON
    Stitch page used (show_ultron_message, set_core_state, show_tool_activity, ...)
    onto those two channels, so the GUI itself — 3D core, HUD, command console — is
    ported unchanged and only the transport differs.
  - The handler runs each command on a worker thread (so the SSE feed stays live),
    executes `Orchestrator.handle()`, and pushes the reply back as a signal.
  - Optional live tool activity: the bridge installs `tool_event_callback` on the
    orchestrator, so each fast-path or LLM-requested tool dispatch becomes a
    `show_tool_activity` line in the console — the same effect the original
    PyQt/QWebChannel app achieved, without PyQt.

No third-party dependencies. Requires only the standard library plus whatever the
VERONICA orchestrator already needs. Voice (mic button) is wired to the configured
STT/TTS backends and degrades to a clear message when STT is disabled.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from veronica.config import Config
from veronica.core.orchestrator import Orchestrator
from veronica.logging_setup import get_logger

logger = get_logger("ui.stitch.bridge")


def _index_file() -> str:
    import os
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")


class EventBroadcaster:
    """Fan a signal out to every connected SSE client. Thread-safe."""

    def __init__(self) -> None:
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, signal: str, args: Optional[list] = None) -> None:
        payload = {"signal": signal, "args": args or []}
        line = f"data: {json.dumps(payload)}\n\n"
        with self._lock:
            dead: list[queue.Queue] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(line)
                except queue.Full:
                    dead.append(q)  # slow consumer; drop it rather than block the UI
            for q in dead:
                self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


class StitchCommandHandler:
    """Backend methods the page can call. Testable without HTTP: instantiate with a
    fake orchestrator and call `handle(method, args)` directly."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        config: Config,
        broadcast: EventBroadcaster,
        task_executor=None,
        planner=None,
        closed_loop_controller=None,
        computer_use_loop=None,
    ) -> None:
        self.orchestrator = orchestrator
        self.config = config
        self.broadcast = broadcast
        self.session = orchestrator.new_session()
        self._mic_active = False
        # Share the app's TaskExecutor when it injected one into the orchestrator
        # (same history, same tasks); otherwise give the bridge its own built on the
        # registry. The planner defaults to a fresh MultiStepPlanner.
        if task_executor is None:
            task_executor = getattr(orchestrator, "task_executor", None)
        self.task_executor = task_executor or self._default_executor()
        if planner is None:
            from veronica.automation import MultiStepPlanner
            planner = MultiStepPlanner()
        self.planner = planner

        # Phase 6: closed-loop controller for observe→diagnose→replan cycle
        if closed_loop_controller is None:
            closed_loop_controller = getattr(orchestrator, "closed_loop_controller", None)
        self.closed_loop_controller = closed_loop_controller

        # Phase 7: computer perception & control loop
        self.computer_use_loop = computer_use_loop
        if self.computer_use_loop is None:
            from veronica.vision.computer_use import create_computer_use_loop
            self.computer_use_loop = create_computer_use_loop(
                event_callback=self._on_computer_use_event
            )

        if orchestrator.tool_event_callback is None:
            orchestrator.tool_event_callback = self._on_tool_event

    def _default_executor(self):
        from veronica.automation.task_executor import TaskExecutor
        executor = TaskExecutor(self.orchestrator.tools, tool_event_callback=self._on_tool_event)
        return executor

    # -- live tool activity -------------------------------------------------------
    def _on_tool_event(self, text: str) -> None:
        self.broadcast.publish("show_tool_activity", [text])

    # -- method surface -------------------------------------------------------------
    def handle(self, method: str, args: Optional[list] = None) -> dict[str, Any]:
        args = args or []
        try:
            if method == "sendCommand":
                return self._send_command(str(args[0]) if args else "")
            if method == "requestSystemStatus":
                return {"ok": True, **self._system_stats()}
            if method == "requestBackendStatus":
                return {"ok": True, "status": "ready"}
            if method == "requestMicStatus":
                return {"ok": True, "active": self._mic_active}
            if method == "startListening":
                return self._start_listening()
            if method == "stopListening":
                self._mic_active = False
                return {"ok": True}
            if method == "requestMemory":
                return self._memory_snapshot()
            if method == "startTask":
                return self._start_task(str(args[0]) if args else "")
            if method == "getTaskStatus":
                return self._task_status(str(args[0]) if args else "")
            if method == "listTasks":
                return {"ok": True, "tasks": self.task_executor.list_tasks()}
            if method == "cancelTask":
                cancelled = self.task_executor.cancel_task(str(args[0]) if args else "")
                return {"ok": cancelled, "error": None if cancelled else "Task not found or not running."}
            if method == "requestScreenState":
                return self._screen_state()
            if method == "executeComputerAction":
                return self._execute_computer_action(dict(args[0]) if args else {})
            if method == "cancelComputerAction":
                return self._cancel_computer_action()
            return {"ok": False, "error": f"Unknown method: {method}"}
        except Exception as e:  # noqa: BLE001 — the page must never see a 500 for a command
            logger.exception("Stitch method '%s' failed", method)
            self.broadcast.publish("show_error", [f"{method} failed: {e}"])
            return {"ok": False, "error": str(e)}

    # -- command -------------------------------------------------------------------
    def _send_command(self, text: str) -> dict[str, Any]:
        if not text.strip():
            return {"ok": True}
        thread = threading.Thread(target=self._run_command, args=(text,), daemon=True)
        thread.start()
        return {"ok": True}

    def _run_command(self, text: str) -> None:
        self.broadcast.publish("set_core_state", ["thinking"])
        try:
            response = self.orchestrator.handle(text, self.session)
        except Exception as e:  # noqa: BLE001
            logger.exception("Orchestrator.handle raised")
            self.broadcast.publish("set_core_state", ["error"])
            self.broadcast.publish("show_error", [f"Command failed: {e}"])
            return
        self.broadcast.publish("show_ultron_message", [response.text, "success"])
        if response.tool_used:
            self.broadcast.publish("set_system_status", [f"LAST TOOL: {response.tool_used}"])
        self.broadcast.publish("set_core_state", ["idle"])

    # -- multi-step tasks -------------------------------------------------------------
    def _start_task(self, goal: str) -> dict[str, Any]:
        """Begin a planned multi-step task on a worker thread. Planning, execution and
        each step's progress stream out as SSE events so the GUI can paint a live
        progress bar. Returns immediately — the task id arrives in `task_started`."""
        if not goal.strip():
            return {"ok": False, "error": "Empty task goal."}
        threading.Thread(target=self._run_task, args=(goal.strip(),), daemon=True).start()
        return {"ok": True}

    def _task_status(self, task_id: str) -> dict[str, Any]:
        detail = self.task_executor.get_task(task_id)
        if detail is None:
            return {"ok": False, "error": "No such task."}
        return {"ok": True, "task": detail}

    def _run_task(self, goal: str) -> None:
        self.broadcast.publish("set_core_state", ["thinking"])
        try:
            plan = self.planner.create_plan(
                goal,
                tools_schema=self.orchestrator.tools.schemas(),
                llm=self.orchestrator.llm,
            )
            if len(plan) <= 1:
                # A goal that didn't plan into steps is just a normal command.
                self.broadcast.publish("show_activity", [
                    f"'{goal}' doesn't need multiple steps — running as a normal command."
                ])
                self._run_command(goal)
                return
            steps = [{"description": s.description, "tool": s.tool_name, "args": s.args} for s in plan]
            task = self.task_executor.create_task(goal=goal, steps=steps)
            self.broadcast.publish("task_started", [task.task_id, task.goal, len(task.steps)])

            def _on_progress(task_id: str, current: int, total: int, desc: str) -> None:
                self.broadcast.publish("task_progress", [task_id, current, total, desc])

            # Phase 6: Use closed-loop controller if available
            if self.closed_loop_controller is not None:
                # Wire the closed-loop controller to broadcast SSE events
                self.closed_loop_controller.event_callback = self._on_closed_loop_event
                outcome = self.closed_loop_controller.execute(task, progress_callback=_on_progress)
            else:
                outcome = self.task_executor.execute_task(task, progress_callback=_on_progress)
            task_dict = task.to_dict()
            if task.status == "cancelled":
                self.broadcast.publish("task_cancelled", [task.task_id, task_dict])
                self.broadcast.publish("show_ultron_message", [self._task_summary(task), "error"])
            elif outcome["success"]:
                self.broadcast.publish("task_completed", [task.task_id, task_dict])
                self.broadcast.publish("show_ultron_message", [self._task_summary(task), "success"])
                self.broadcast.publish("set_system_status", [f"TASK {task.task_id} COMPLETE"])
            else:
                self.broadcast.publish("task_failed", [task.task_id, task_dict, outcome.get("error", "")])
                self.broadcast.publish("show_ultron_message", [self._task_summary(task), "error"])
        except Exception as e:  # noqa: BLE001
            logger.exception("Task '%s' raised", goal)
            self.broadcast.publish("show_error", [f"Task failed: {e}"])
            self.broadcast.publish("set_core_state", ["error"])
            return
        self.broadcast.publish("set_core_state", ["idle"])

    @staticmethod
    def _task_summary(task) -> str:
        lines = [f"Task {task.task_id}: {task.goal}"]
        for s in task.steps:
            mark = "✓" if s.status == "completed" else "✗"
            status_tag = f" ({s.status})" if s.status != "completed" else ""
            lines.append(f"  {mark} {s.description}{status_tag}")
        if task.status in ("completed", "failed", "cancelled"):
            lines.append(task.result_summary)
        return "\n".join(lines)

    # -- Phase 6: closed-loop SSE events ------------------------------------------
    def _on_closed_loop_event(self, signal: str, args: dict) -> None:
        """Bridge closed-loop controller events to SSE for the Stitch GUI.

        Maps the closed-loop lifecycle signals to SSE events the GUI understands:
          task_recovery_started  → task_recovery_started  (SSE)
          task_observing         → task_observing         (SSE)
          task_diagnosing        → task_diagnosing        (SSE)
          task_diagnosis_complete → task_diagnosis_complete (SSE)
          task_replanning        → task_replanning        (SSE)
          task_replanned         → task_replanned         (SSE)
          task_recovery_succeeded → task_recovery_succeeded (SSE)
          task_blocked           → task_blocked           (SSE)
          task_same_failure_detected → task_same_failure_detected (SSE)
          task_needs_confirmation → task_needs_confirmation (SSE)
        """
        task_id = args.get("task_id", "")
        try:
            if signal == "task_recovery_started":
                self.broadcast.publish(signal, [task_id, args.get("attempt", 1), args.get("max_attempts", 1)])
                self.broadcast.publish("show_activity", [f"Recovery attempt {args.get('attempt', 1)}/{args.get('max_attempts', 1)} for task {task_id}"])
            elif signal == "task_observing":
                status = args.get("overall_status", "unknown")
                completed = args.get("completed_steps", 0)
                total = args.get("total_steps", 0)
                self.broadcast.publish(signal, [task_id, status, completed, total])
                self.broadcast.publish("show_activity", [f"Observing task {task_id}: {status} ({completed}/{total} steps)"])
            elif signal == "task_diagnosing":
                failed = args.get("failed_steps", [])
                self.broadcast.publish(signal, [task_id, failed])
                self.broadcast.publish("show_activity", [f"Diagnosing task {task_id} — failed at step(s): {failed}"])
            elif signal == "task_diagnosis_complete":
                approach = args.get("recommended_approach", "unknown")
                confidence = args.get("confidence", "unknown")
                self.broadcast.publish(signal, [task_id, approach, confidence])
                self.broadcast.publish("show_activity", [f"Diagnosis: {approach} (confidence: {confidence})"])
            elif signal == "task_replanning":
                attempt = args.get("attempt", 1)
                self.broadcast.publish(signal, [task_id, attempt])
                self.broadcast.publish("show_activity", [f"Replanning task {task_id} (attempt {attempt})"])
            elif signal == "task_replanned":
                success = args.get("success", False)
                new_steps = args.get("new_steps", 0)
                preserved = args.get("preserved_steps", 0)
                self.broadcast.publish(signal, [task_id, success, new_steps, preserved])
                self.broadcast.publish("show_activity", [f"Replan: {new_steps} new steps, {preserved} preserved"])
            elif signal == "task_recovery_succeeded":
                attempts = args.get("attempts", 1)
                self.broadcast.publish(signal, [task_id, attempts])
                self.broadcast.publish("show_activity", [f"Task {task_id} recovered after {attempts} attempt(s)"])
            elif signal == "task_blocked":
                reason = args.get("reason", "unknown")
                self.broadcast.publish(signal, [task_id, reason])
                self.broadcast.publish("show_activity", [f"Task {task_id} blocked: {reason}"])
            elif signal == "task_same_failure_detected":
                self.broadcast.publish(signal, [task_id])
                self.broadcast.publish("show_activity", [f"Same failure detected for task {task_id} — stopping"])
            elif signal == "task_needs_confirmation":
                self.broadcast.publish(signal, [task_id, args.get("diagnosis", {})])
                self.broadcast.publish("show_activity", [f"Task {task_id} needs human review"])
            else:
                # Unknown signal — pass through as-is
                self.broadcast.publish(signal, [task_id, args])
        except Exception as e:
            logger.warning("Failed to broadcast closed-loop event %s: %s", signal, e)

    # -- Phase 7: computer perception & control ------------------------------------
    def _screen_state(self) -> dict[str, Any]:
        """Observe the current screen state and return it to the page.

        Returns a normalized, privacy-conscious snapshot (no raw pixels,
        no persisted screenshot).
        """
        try:
            state = self.computer_use_loop._observe()  # safe-filtered state
            payload = {
                "timestamp": state.timestamp,
                "active_window": state.active_window.to_dict() if state.active_window else None,
                "visible_windows_count": len(state.visible_windows),
                "desktop_elements_count": len(state.desktop_elements),
                "text_regions_count": len(state.text_regions),
                "focused_element": state.focused_element.to_dict() if state.focused_element else None,
                "cursor_position": state.cursor_position,
                "perception_sources": state.perception_sources,
                "confidence_overall": state.confidence_overall,
                "fingerprint": state.get_fingerprint(),
            }
            return {"ok": True, "state": payload}
        except Exception as e:
            logger.warning("Screen-state observation failed: %s", e)
            return {"ok": False, "error": str(e)}

    def _execute_computer_action(self, action_dict: dict[str, Any]) -> dict[str, Any]:
        """Execute a single computer action through the computer-use loop.

        Args come from the page as JSON: {action_type, target_name, coordinates,
        text, key, button, clicks, description}.
        """
        from veronica.vision.computer_use import ComputerAction
        from veronica.safety.permissions import PermissionLevel

        action_type = action_dict.get("action_type", "")
        if not action_type:
            return {"ok": False, "error": "Missing action_type."}

        # Map permission level to enum safely
        permission_name = action_dict.get("permission_level", "MODERATE")
        try:
            permission_level = PermissionLevel[permission_name]
        except KeyError:
            permission_level = PermissionLevel.MODERATE

        action = ComputerAction(
            action_type=action_type,
            target_name=action_dict.get("target_name"),
            coordinates=tuple(action_dict["coordinates"]) if action_dict.get("coordinates") else None,
            text=action_dict.get("text"),
            key=action_dict.get("key"),
            button=action_dict.get("button", "left"),
            clicks=int(action_dict.get("clicks", 1)),
            description=action_dict.get("description", f"{action_type} action"),
            permission_level=permission_level,
        )
        result = self.computer_use_loop.execute_action(action)
        return {"ok": result.success, "result": result.to_dict()}

    def _cancel_computer_action(self) -> dict[str, Any]:
        """Cancel any in-flight computer action."""
        self.computer_use_loop.cancel()
        return {"ok": True}

    def _on_computer_use_event(self, signal: str, args: dict) -> None:
        """Bridge computer-use loop events to SSE for the Stitch GUI.

        Maps the Phase 7 loop lifecycle signals to SSE events:
          action_observing          → action_observing          (SSE)
          action_understanding      → action_understanding      (SSE)
          action_selecting          → action_selecting          (SSE)
          action_permission_check   → action_permission_check   (SSE)
          action_executing          → action_executing          (SSE)
          action_observing_after    → action_observing_after    (SSE)
          action_verifying          → action_verifying          (SSE)
          action_completed          → action_completed          (SSE)
        """
        try:
            action = args.get("action", {})
            action_type = action.get("action_type", "unknown") if isinstance(action, dict) else "unknown"
            target = action.get("target_name") or action.get("key") or action.get("coordinates") or ""
            if signal == "action_observing":
                self.broadcast.publish(signal, [action_type, target])
                self.broadcast.publish("show_activity", [f"Computer action: observing screen to {action_type}"])
            elif signal == "action_understanding":
                self.broadcast.publish(signal, [action_type, target])
                self.broadcast.publish("show_activity", [f"Computer action: understanding context for {action_type} {target}"])
            elif signal == "action_selecting":
                self.broadcast.publish(signal, [action_type, target])
                self.broadcast.publish("show_activity", [f"Computer action: selected {action_type} -> {target}"])
            elif signal == "action_permission_check":
                self.broadcast.publish(signal, [action_type, target])
                self.broadcast.publish("show_activity", [f"Computer action: checking permission for {action_type}"])
            elif signal == "action_executing":
                self.broadcast.publish(signal, [action_type, target])
                self.broadcast.publish("show_activity", [f"Computer action: executing {action_type} on {target}"])
            elif signal == "action_observing_after":
                self.broadcast.publish(signal, [action_type, target])
            elif signal == "action_verifying":
                self.broadcast.publish(signal, [action_type, target])
                self.broadcast.publish("show_activity", [f"Computer action: verifying effect of {action_type}"])
            elif signal.startswith("action_verification_"):
                # P1: target-specific verification outcome (verified / unknown / failed)
                status = signal.replace("action_verification_", "")
                self.broadcast.publish("action_verification", [action_type, target, status])
            elif signal == "action_completed":
                verification_status = args.get("verification_status")
                self.broadcast.publish(signal, [action_type, target])
                if verification_status:
                    self.broadcast.publish("action_verification", [action_type, target, verification_status])
                self.broadcast.publish("show_activity", [f"Computer action completed: {action_type} {target}"])
            else:
                self.broadcast.publish(signal, [action_type, target])
        except Exception as e:
            logger.warning("Failed to broadcast computer-use event %s: %s", signal, e)

    # -- voice -----------------------------------------------------------------------
    def _start_listening(self) -> dict[str, Any]:
        if self.config.stt.backend == "none":
            self.broadcast.publish("show_activity", ["Voice disabled (stt.backend = 'none'). Enable a backend in config.yaml to use the mic."])
            return {"ok": False, "reason": "stt disabled"}
        if self._mic_active:
            return {"ok": True}
        self._mic_active = True
        threading.Thread(target=self._listen_and_handle, daemon=True).start()
        return {"ok": True}

    def _listen_and_handle(self) -> None:
        from veronica.speech.stt import SpeechToText
        from veronica.speech.tts import TextToSpeech

        stt = SpeechToText(self.config.stt)
        tts = TextToSpeech(self.config.tts, cache_dir=self.config.resolve_path(self.config.tts.cache_dir))
        self.broadcast.publish("set_voice_state", ["listening"])
        self.broadcast.publish("set_core_state", ["listening"])
        try:
            result = stt.listen_once()
        finally:
            self._mic_active = False
            self.broadcast.publish("set_voice_state", ["stopping"])

        if not result.success:
            self.broadcast.publish("show_activity", [f"STT: {result.error}"])
            self.broadcast.publish("set_core_state", ["idle"])
            return

        self.broadcast.publish("show_user_message", [result.text])
        self.broadcast.publish("set_core_state", ["thinking"])
        try:
            response = self.orchestrator.handle(result.text, self.session)
        except Exception as e:  # noqa: BLE001
            logger.exception("Orchestrator.handle raised (voice)")
            self.broadcast.publish("show_error", [f"Failed: {e}"])
            self.broadcast.publish("set_core_state", ["error"])
            return
        self.broadcast.publish("show_ultron_message", [response.text, "success"])
        self.broadcast.publish("set_core_state", ["speaking"])
        tts.speak(response.text)
        self.broadcast.publish("set_core_state", ["idle"])

    # -- status / memory -----------------------------------------------------------------
    def _system_stats(self) -> dict[str, Any]:
        stats: dict[str, Any] = {"cpu": "--", "ram": "--", "net": "Offline"}
        try:
            import psutil
            stats["cpu"] = round(psutil.cpu_percent(interval=None))
            stats["ram"] = round(psutil.virtual_memory().percent)
        except ImportError:
            stats["cpu"] = "--"
            stats["ram"] = "--"  # psutil optional; honest placeholder when absent
        try:
            import socket
            socket.create_connection(("1.1.1.1", 53), timeout=1.5).close()
            stats["net"] = "Online"
        except OSError:
            stats["net"] = "Offline"
        return stats

    def _memory_snapshot(self) -> dict[str, Any]:
        try:
            from veronica.memory.long_term import LongTermMemory
            lm = LongTermMemory(self.config.resolve_path(self.config.memory.long_term_path))
            items = lm.all() if hasattr(lm, "all") else []
        except Exception:
            items = []
        return {"ok": True, "count": len(items), "note": "Long-term memory entries count."}


class _SSEHandler(BaseHTTPRequestHandler):
    server_version = "VERONICA-Stitch/0.1"

    @property
    def bridge(self) -> "StitchHttpServer":
        return self.server  # type: ignore[return-value]

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep the console quiet; bridge logs are enough.
        pass

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path == "/" or self.path == "/index.html":
            self._serve_index()
            return
        if self.path == "/events":
            if self.bridge.broadcast.subscriber_count >= 8:
                # Rainy-day guard; a single user rarely opens more than a few tabs.
                self.send_error(503, "Too many event clients")
                return
            self._serve_sse()
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length else "{}"
        try:
            payload = json.loads(body)
            method = payload.get("method", "")
            args = payload.get("args") or []
        except (ValueError, AttributeError):
            self._send_json({"ok": False, "error": "Bad JSON payload."})
            return
        reply = self.bridge.handler.handle(method, args)
        self._send_json(reply)

    # -- internals ----------------------------------------------------------------------
    def _serve_index(self) -> None:
        try:
            with open(_index_file(), "rb") as f:
                html = f.read()
        except FileNotFoundError:
            self.send_error(500, "index.html missing next to bridge.py")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def _serve_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = self.bridge.broadcast.subscribe()
        try:
            # Handshake: tell the freshly connected page the backend is ready.
            self.wfile.write(b"data: {\"signal\": \"backend_ready\", \"args\": [\"ready\"]}\n\n")
            self.wfile.flush()
            while True:
                try:
                    line = q.get(timeout=15)
                    self.wfile.write(line.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    # Heartbeat keeps the connection alive through idle stretches.
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.bridge.broadcast.unsubscribe(q)

    def _send_json(self, obj: dict[str, Any]) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class StitchHttpServer:
    """Owns the HTTP server + broadcaster + command handler for one assembled app."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        config: Config,
        host: str = "127.0.0.1",
        port: int = 7171,
    ) -> None:
        self.host = host
        self.port = port
        self.broadcast = EventBroadcaster()
        self.handler = StitchCommandHandler(orchestrator, config, self.broadcast)
        self._server = ThreadingHTTPServer((host, port), _SSEHandler)
        self._server.broadcast = self.broadcast  # type: ignore[attr-defined]
        self._server.handler = self.handler  # type: ignore[attr-defined]

    def serve_forever(self) -> None:
        logger.info("Stitch GUI on http://%s:%s (Ctrl-C to stop)", self.host, self.port)
        try:
            self._server.serve_forever()
        finally:
            self._server.server_close()

    def shutdown(self) -> None:
        self._server.shutdown()

    def serve_in_background(self) -> None:
        """For tests / embedding: run the server on a thread."""
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        return None


def run_bridge(
    orchestrator: Orchestrator,
    config: Config,
    host: str = "127.0.0.1",
    port: int = 7171,
    open_browser: bool = True,
) -> None:
    server = StitchHttpServer(orchestrator, config, host=host, port=port)
    if open_browser:
        webbrowser.open(f"http://{host}:{port}")
    server.serve_forever()