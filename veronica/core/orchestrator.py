"""
The Orchestrator: VERONICA's brain. One method in, one method out — `handle(text)` ->
`OrchestratorResponse`. Everything else (CLI, voice loop, a future GUI, a scheduled
agent) is a thin driver around this class, per the "UI is a swappable client" principle.

Routing strategy (in order):
  1. Fast path: does any registered Tool's `matches()` claim this text outright? If so,
     run it directly — no LLM round trip. This is the pattern Prodigy-AI's
     intents.py/dispatcher.py got right, generalized onto the Tool registry.
  2. LLM path: hand the conversation (+ tool schemas) to the configured LLM provider.
     If the model responds with tool_calls, dispatch each through the registry (which
     enforces permissions) and feed the results back for a final natural-language reply.
  3. Every tool call — fast-path or LLM-requested — goes through the same
     PermissionGate, so the safety story doesn't depend on which path triggered it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from veronica.config import Config
from veronica.core.capability import CapabilityRegistry
from veronica.core.compound import split_compound_command
from veronica.core.context import SessionContext
from veronica.core.loop_guard import LoopGuard, LoopGuardConfig
from veronica.learning.lesson import format_lessons_for_prompt, get_relevant_lessons
from veronica.learning.policy import SAFETY_SENSITIVE_TOOLS
from veronica.llm.base import ChatMessage, LLMProvider
from veronica.logging_setup import get_logger
from veronica.memory.long_term import LongTermMemory
from veronica.memory.short_term import ShortTermMemory
from veronica.tools.registry import ToolRegistry

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from veronica.automation.closed_loop import ClosedLoopController
    from veronica.learning.adaptation import AdaptationResult, BehavioralAdapter
    from veronica.memory.thread import ThreadMemory

logger = get_logger("core.orchestrator")

_MAX_TOOL_HOPS = 8  # guard against runaway tool-call loops; raised from 4 so multi-step
                    # tasks (e.g. "read this file, then write a summary to a new one")
                    # have room to complete now that execution_tools.py exists


@dataclass
class OrchestratorResponse:
    text: str
    tool_used: Optional[str] = None
    tool_results: list[dict] = field(default_factory=list)
    # Agentic path: when a goal was planned and executed as a multi-step task, this
    # carries the full task state (steps, progress, status) for front-ends to render.
    task: Optional[dict] = None


class Orchestrator:
    def __init__(
        self,
        config: Config,
        llm: LLMProvider,
        tools: ToolRegistry,
        long_term_memory: LongTermMemory,
        capabilities: Optional[CapabilityRegistry] = None,
        tool_event_callback: Optional[Callable[[str], None]] = None,
        planner: Optional["object"] = None,        # automation.planner.MultiStepPlanner
        task_executor: Optional["object"] = None,  # automation.task_executor.TaskExecutor
        learning_store: Optional["object"] = None,  # learning.store.ExperienceStore
        learning_engine: Optional["object"] = None,  # learning.learner.LearningEngine
        behavioral_adapter: Optional[BehavioralAdapter] = None,  # learning.adaptation.BehavioralAdapter
        closed_loop_controller: Optional["ClosedLoopController"] = None,
        # Thread Memory (Phase 7): persistent episodic/semantic memory. OPTIONAL — when
        # absent (or disabled), the orchestrator behaves exactly as before. It can never
        # break the assistant: every access is wrapped and degrades to empty context.
        thread_memory: Optional["ThreadMemory"] = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.tools = tools
        self.long_term_memory = long_term_memory
        # Optional live instrumentation hook: called with a short human string each
        # time a tool is dispatched (fast-path or LLM-requested). Front-ends such as
        # the Stitch GUI use it to render a live "VERONICA ran get_weather" activity
        # line; when unset (None) dispatch is unchanged.
        self.tool_event_callback = tool_event_callback
        # Optional semantic overlay: when set, its compact capability inventory is
        # injected into the system prompt so small local models can orient without
        # drowning in every raw tool schema (the full schemas are still sent when the
        # provider requests tool-calling).
        self.capabilities = capabilities
        # Agentic path (injected in main.py): when BOTH a planner and an executor are
        # present, goals that look multi-step are planned and run as a verified
        # multi-step task instead of a single LLM tool-calling turn. When either is
        # absent the orchestrator behaves exactly as before.
        self.planner = planner
        self.task_executor = task_executor
        # Adaptive planning (Phase 3): when a learning store is present the agentic path
        # retrieves relevant past experiences BEFORE planning and passes them to the
        # planner as advisory context. This is strictly optional and fault-tolerant —
        # retrieval failures fall back to the existing planner behavior unchanged.
        self.learning_store = learning_store
        self.learning_engine = learning_engine
        # Behavioral adapter for Phase 4 controlled adaptation.
        self.behavioral_adapter = behavioral_adapter
        # Phase 6: closed-loop controller — wraps TaskExecutor with observe→diagnose→replan
        # cycle. When present, the agentic path uses it instead of direct executor calls.
        self.closed_loop_controller = closed_loop_controller
        # Thread Memory (Phase 7): optional persistent episodic/semantic memory. When set
        # and enabled, a small number of relevant memories are injected into the prompt —
        # clearly separated from the live conversation context. It grants NO permissions.
        self.thread_memory = thread_memory

    def new_session(self) -> SessionContext:
        return SessionContext(short_term=ShortTermMemory(max_messages=self.config.memory.short_term_max_messages))

    def handle(self, text: str, session: SessionContext) -> OrchestratorResponse:
        text = text.strip()
        if not text:
            return OrchestratorResponse(text="I didn't catch anything.")

        # ---- 0. agentic path (plan → execute → verify) -------------------------
        # Decided BEFORE the compound split on purpose: a goal like "plan and run a
        # research task then save the notes" is one integral goal for the planner, and
        # the conjunction splitter would wrongly chop it into ["plan", "run ..."].
        # Fast-path commands (single sentence, no agentic marker) never reach here.
        if self._is_agentic_request(text):
            return self._handle_agentic(text, session)

        # ---- 1. compound split -------------------------------------------------
        clauses = split_compound_command(text)
        if len(clauses) > 1:
            return self._run_compound(clauses, session)

        return self._handle_single(text, session)

    # -- agentic helpers -----------------------------------------------------------
    # Substrings that mark a request as "plan this whole thing for me", as opposed to
    # a single command. Matched case-insensitively on the whole goal.
    _AGENTIC_TRIGGERS: tuple[str, ...] = (
        "plan and run", "plan this", "make a plan", "step by step",
        "multi-step", "multistep", "in sequence", "as an agent", "set up a",
        "set up my", "organize my", "figure out how to",
    )

    def _is_agentic_request(self, text: str) -> bool:
        """Heuristic gate for the agentic path. Requires a configured pipeline, then
        fires on an explicit planning phrase or on a multi-sentence goal (a paragraph
        of intent the compound splitter won't touch because it splits on conjunctions,
        not periods)."""
        if self.planner is None or self.task_executor is None:
            return False
        lower = text.lower()
        if any(t in lower for t in self._AGENTIC_TRIGGERS):
            return True
        # >= 2 sentences signals a compound goal the splitter can't atomize.
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
        return len(sentences) >= 2

    def _get_learning_context(self, goal: str) -> str:
        """Retrieve relevant lessons from the experience store and format them as
        advisory planning context. Returns "" on any failure or when learning is disabled."""
        if self.learning_store is None:
            return ""
        try:
            confidence_fn = (
                self.learning_engine.confidence.confidence
                if self.learning_engine is not None
                else None
            )
            lessons = get_relevant_lessons(
                goal,
                self.learning_store,
                confidence_fn=confidence_fn,
                limit=5,
            )
            logger.info(
                "Adaptive planning: retrieved %d relevant lesson(s) for goal=%r",
                len(lessons), goal[:80],
            )
            return format_lessons_for_prompt(lessons)
        except Exception:  # noqa: BLE001 — learning must never break planning
            logger.exception("Learning context retrieval failed; planning without it. goal=%r",
                             goal[:80])
            return ""

    # Phase 4: controlled behavioral adaptation
    def _get_lessons(self, goal: str) -> list:
        """Retrieve ranked lessons for behavioral adaptation (returns Lesson objects)."""
        if self.learning_store is None or self.learning_engine is None:
            return []
        try:
            confidence_fn = self.learning_engine.confidence.confidence
            lessons = get_relevant_lessons(
                goal,
                self.learning_store,
                confidence_fn=confidence_fn,
                limit=10,  # get more for adaptation consideration
                min_score=0.35,  # MIN_RELEVANCE from lesson.py
            )
            logger.info(
                "Behavioral adaptation: retrieved %d lesson(s) for goal=%r",
                len(lessons), goal[:80],
            )
            return lessons
        except Exception:
            logger.exception("Lesson retrieval failed for adaptation; proceeding without lessons. goal=%r",
                             goal[:80])
            return []

    def _adaptation_context(self, goal: str) -> set[str]:
        """Detect explicit tool names mentioned in the user's goal. When the user names a
        tool directly (e.g. "use find_file"), that is an explicit instruction which must
        always override a learned preference."""
        explicit: set[str] = set()
        goal_lower = goal.lower()
        registered = set()
        for schema in self.tools.schemas():
            fn = schema.get("function", {})
            name = fn.get("name", "")
            if name:
                registered.add(name)
        registered.update(SAFETY_SENSITIVE_TOOLS)
        # Also the adapter's own substitutable groups so a goal naming any preferred tool
        # keeps the original tool in place.
        if self.behavioral_adapter is not None:
            for members in getattr(self.behavioral_adapter, "tool_groups", {}).values():
                registered.update(members)

        for tool in registered:
            # "use find_file", "with find_file", "run find_file"
            if tool in goal_lower:
                explicit.add(tool)
            # A goal writes tool names with spaces ("find file") but that collides with
            # natural language ("find the file") — only match the exact underscored form
            # plus common phrase anchors to avoid over-matching.
            for phrase in ("use ", "with ", "run ", "via ", "tool "):
                if f"{phrase}{tool.replace('_', ' ')}" in goal_lower or f"{phrase}{tool}" in goal_lower:
                    explicit.add(tool)
        return explicit

    def _adapt_plan(self, plan: list[dict], lessons: list, goal: str, context: dict) -> tuple[list[dict], "AdaptationResult"]:
        """Apply behavioral adaptation to a plan. Returns (adapted_plan, adaptation_result)."""
        if not self.behavioral_adapter or not lessons:
            # Return a result that indicates no adaptation was applied
            from veronica.learning.adaptation import AdaptationResult
            empty_result = AdaptationResult(
                original_plan=plan,
                adapted_plan=plan,
                applied=[],
                rejected=[],
                considered=[],
                decision_summary="No adaptation (no adapter or lessons)",
                adaptation_applied=False,
            )
            return plan, empty_result

        try:
            adaptation_result = self.behavioral_adapter.adapt(
                plan, lessons, goal=goal, context=context
            )
            adapted_plan = adaptation_result.adapted_plan
            if adaptation_result.adaptation_applied:
                logger.info("Behavioral adaptation applied: %s", adaptation_result.decision_summary)
            else:
                logger.info("Behavioral adaptation considered but not applied: %s", adaptation_result.decision_summary)
            return adapted_plan, adaptation_result
        except Exception:
            logger.exception("Behavioral adaptation failed; preserving original plan.")
            from veronica.learning.adaptation import AdaptationResult
            fallback_result = AdaptationResult(
                original_plan=plan,
                adapted_plan=plan,
                applied=[],
                rejected=[],
                considered=[],
                decision_summary="Adaptation error; original plan preserved",
                adaptation_applied=False,
            )
            return plan, fallback_result

    def _handle_agentic(self, text: str, session: SessionContext) -> OrchestratorResponse:
        """Plan the goal, then execute the plan step-by-step with verification. A goal
        that turns out not to need a plan (single step) falls through to the normal
        LLM path so the reply pipeline is unchanged.

        Phase 4: If behavioral adaptation is enabled, the plan may be transformed
        based on validated, high-confidence lessons (advisory and strong only).
        The original plan is preserved in the experience for audit.

        Phase 6: If a closed-loop controller is available, it wraps the executor
        with observe→diagnose→replan cycle for failure recovery.
        """
        session.turn_count += 1
        session.short_term.add("user", text)

        # Retrieve lessons once for both planner context and behavioral adaptation
        learning_context = self._get_learning_context(text)
        lessons = self._get_lessons(text) if self.behavioral_adapter else []

        plan = self.planner.create_plan(
            text,
            tools_schema=self.tools.schemas(),
            llm=self.llm,
            learning_context=learning_context,
        )
        if len(plan) <= 1:
            # Nothing worth planning — hand it to the normal single-turn path.
            return self._handle_via_llm(text, session)

        # Convert plan to dict format for adaptation
        plan_dicts = [{"description": s.description, "tool": s.tool_name, "args": s.args} for s in plan]
        original_plan_dicts = list(plan_dicts)  # preserve original for experience

        # Phase 4: apply behavioral adaptation if available and lessons exist
        adaptation_applied = False
        adaptation_result = None
        if lessons and self.behavioral_adapter:
            # Prepare context for adaptation: include explicit tool mentions from goal
            context = {
                "goal": text,
                "explicit_tools": self._adaptation_context(text),
            }
            adapted_plan, adaptation_result = self._adapt_plan(plan_dicts, lessons, text, context)
            adaptation_applied = adaptation_result.adaptation_applied
            if adaptation_applied:
                plan_dicts = adapted_plan

        logger.info("Agentic task '%s' planned as %d steps (adapted=%s)",
                    text, len(plan_dicts), adaptation_applied)

        # Execute the (possibly adapted) plan
        task = self.task_executor.create_task(
            goal=text,
            steps=plan_dicts,
            adaptation_applied=adaptation_applied,
            adaptation_result=adaptation_result,
        )

        last_tool: Optional[str] = None
        tool_results: list[dict] = []

        def _on_progress(task_id: str, current: int, total: int, desc: str) -> None:
            if self.tool_event_callback:
                self.tool_event_callback(f"task {current}/{total}: {desc}")

        # Phase 6: Use closed-loop controller if available, otherwise fall back to executor
        if self.closed_loop_controller is not None:
            outcome = self.closed_loop_controller.execute(task, progress_callback=_on_progress)
        else:
            outcome = self.task_executor.execute_task(task, progress_callback=_on_progress)

        for step in task.steps:
            if step.tool_name:
                last_tool = step.tool_name
            if getattr(step.result, "success", True):
                tool_results.append(
                    getattr(step.result, "__dict__", {"message": str(step.result)})
                )

        states = [s.status for s in task.steps]
        lines = [f"Executed plan ({len(task.steps)} steps):"]
        for s in task.steps:
            mark = "✓" if s.status == "completed" else "✗"
            note = f" — {s.verification_note}" if s.verification_note else ""
            lines.append(f"  {mark} {s.description}{note}")
        lines.append(outcome.get("result", "Task finished."))
        reply = "\n".join(lines)
        session.short_term.add("assistant", reply)
        return OrchestratorResponse(
            text=reply,
            tool_used=last_tool,
            tool_results=tool_results,
            task=task.to_dict(),
        )

    def _run_compound(self, clauses: list[str], session: SessionContext) -> OrchestratorResponse:
        """Run each clause through the normal single-turn pipeline, in order, and
        join the replies. Tool results are collected across all clauses so the
        caller sees the full trace; the conversation log gets one user + assistant
        turn per clause, which keeps the short-term trace honest."""
        replies: list[str] = []
        tool_results: list[dict] = []
        tool_used: Optional[str] = None
        for clause in clauses:
            sub = self._handle_single(clause, session)
            replies.append(sub.text)
            tool_results.extend(sub.tool_results)
            tool_used = tool_used or sub.tool_used
        return OrchestratorResponse(
            text=" ; ".join(replies),
            tool_used=tool_used,
            tool_results=tool_results,
        )

    def _handle_single(self, text: str, session: SessionContext) -> OrchestratorResponse:
        session.turn_count += 1
        session.short_term.add("user", text)

        # ---- 1. fast path -----------------------------------------------------
        tool = self.tools.find_fast_match(text)
        if tool is not None:
            args = tool.extract_arguments(text)
            if self.tool_event_callback:
                self.tool_event_callback(f"fast-path {tool.name} {args or ''}".strip())
            result = self.tools.dispatch(tool.name, **args)
            session.last_tool_used = tool.name
            reply = result.message
            session.short_term.add("assistant", reply)
            return OrchestratorResponse(text=reply, tool_used=tool.name, tool_results=[result.__dict__])

        # ---- 2. LLM path (with tool-calling) -----------------------------------
        return self._handle_via_llm(text, session)

    def _handle_via_llm(self, text: str, session: SessionContext) -> OrchestratorResponse:
        messages = self._build_messages(session, text=text)
        tool_schemas = self.tools.schemas()

        collected_results: list[dict] = []
        last_tool_name: Optional[str] = None
        guard = LoopGuard(LoopGuardConfig(max_turns=_MAX_TOOL_HOPS))

        for _hop in range(_MAX_TOOL_HOPS):
            response = self.llm.chat(messages, tools=tool_schemas, temperature=self.config.llm.temperature)

            if not response.tool_calls:
                reply = response.content or "I'm not sure how to respond to that."
                session.short_term.add("assistant", reply)
                return OrchestratorResponse(text=reply, tool_used=last_tool_name, tool_results=collected_results)

            # Model wants to call one or more tools.
            messages.append(ChatMessage(role="assistant", content=response.content or ""))
            blocked_any = False
            for call in response.tool_calls:
                verdict = guard.check_call(call.name, call.arguments)
                if verdict.blocked:
                    logger.warning("Loop guard blocked call to %s: %s", call.name, verdict.reason)
                    blocked_any = True
                    messages.append(
                        ChatMessage(
                            role="tool",
                            content=f"[blocked by loop guard: {verdict.reason}]",
                            tool_call_id=call.id,
                            name=call.name,
                        )
                    )
                    continue
                if verdict.warned:
                    logger.info("Loop guard warning: %s", verdict.reason)
                if self.tool_event_callback:
                    self.tool_event_callback(f"tool {call.name}")
                result = self.tools.dispatch(call.name, **call.arguments)
                last_tool_name = call.name
                collected_results.append(result.__dict__)
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=result.message,
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )
            if blocked_any and guard.turns >= guard.config.max_turns:
                # Hard cap reached because of the guard — bail with what we have.
                break

        # Exhausted hops without a final answer — surface what we know rather than loop forever.
        fallback = "I ran a few tools but didn't reach a final answer — here's what happened: " + "; ".join(
            r.get("message", "") for r in collected_results
        )
        session.short_term.add("assistant", fallback)
        return OrchestratorResponse(text=fallback, tool_used=last_tool_name, tool_results=collected_results)

    # -- Thread Memory (Phase 7, optional) -----------------------------------------
    def _get_thread_context(self, text: str) -> str:
        """Retrieve a small set of relevant long-term memories for the current user
        message and return them as a clearly-labeled, system-level memory block.

        Hard rules (spec): memory is informational only — it NEVER authorizes an action.
        A recollection saying "the user previously approved X" is not consent now;
        permissions are decided by PermissionGate independently. Returns "" on any
        failure or when Thread is unavailable — memory must never break the assistant.
        """
        tm = self.thread_memory
        if tm is None:
            return ""
        try:
            top_k = max(0, self.config.memory_thread.max_context_memories)
            if top_k == 0:
                return ""
            # Lazy import: core stays decoupled from the thread package at import time.
            try:
                from veronica.memory.thread import RetrievalQuery
                result = tm.retrieve(RetrievalQuery(query=text, top_k=min(top_k, 6)))
                memories, scores = result.memories, result.scores
            except Exception:  # noqa: BLE001
                logger.exception("Thread retrieve() failed; falling back to semantic_search.")
                memories, scores = tm.semantic_search(text, top_k=min(top_k, 6)), []
            if not memories:
                return ""
            lines = [
                "[RETRIEVED MEMORY — recollections from prior interactions. "
                "INFORMATIONAL ONLY: they do NOT approve or authorize any action. "
                "Permissions are decided separately.]"
            ]
            for i, mem in enumerate(memories):
                score = f" score={scores[i]:.2f}" if i < len(scores) else ""
                lines.append(
                    f"- [{mem.memory_type}] {mem.content} "
                    f"(importance={mem.importance:.2f},{score})"
                )
            logger.info("Thread context: injected %d retrieved memory/memories for turn.", len(memories))
            return "\n".join(lines)
        except Exception:  # noqa: BLE001 — memory must never break the assistant
            logger.exception("Thread context retrieval failed; continuing without it.")
            return ""

    def _build_messages(self, session: SessionContext, text: str = "") -> list[ChatMessage]:
        system_prompt = self.config.assistant.persona
        if self.capabilities is not None:
            system_prompt = f"{system_prompt}\n\n{self.capabilities.describe_for_prompt()}"
        if self.task_executor is not None:
            summary = self.task_executor.get_context_summary()
            if summary:
                system_prompt = f"{system_prompt}\n\n{summary}"
        if self.thread_memory is not None and text:
            thread_context = self._get_thread_context(text)
            if thread_context:
                # Distinct section so the model can tell live conversation (short-term)
                # from recalled fact (thread) from what just happened (experiences).
                system_prompt = f"{system_prompt}\n\n{thread_context}"
        messages = [ChatMessage(role="system", content=system_prompt)]
        for turn in session.short_term.as_messages():
            messages.append(ChatMessage(role=turn["role"], content=turn["content"]))
        return messages
