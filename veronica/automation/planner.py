"""
LLM-based planner: converts a natural-language goal into a concrete ordered list of
steps, each mapped to a tool when possible. Adapted from ULTRON planner patterns.

Adapted from the user's private ULTRON project (project_ultron/core/planner.py).
Not for redistribution.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from veronica.llm.base import ChatMessage, LLMProvider
from veronica.logging_setup import get_logger

logger = get_logger("automation.planner")


class TaskPlanStep:
    """A single planned step produced by the planner."""

    def __init__(
        self,
        step_number: int,
        description: str,
        tool_name: str = "",
        args: Dict[str, Any] | None = None,
    ) -> None:
        self.step_number = step_number
        self.description = description
        self.tool_name = tool_name
        self.args = args or {}
        self.completed = False
        self.result: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step_number,
            "description": self.description,
            "tool": self.tool_name,
            "args": self.args,
            "completed": self.completed,
        }


class MultiStepPlanner:
    """Break a goal into concrete `{description, tool, args}` steps.

    Only complex goals (multi-clause, multi-verb, compound) receive a plan; simple
    single-purpose commands fall back to a single-step plan so the caller can always
    treat ``plan`` uniformly.
    """

    PLANNING_PROMPT = (
        "You are a task planner for a personal AI assistant on Windows. "
        "Given a user's request, break it into concrete ordered steps.\n\n"
        "Available tools and their purposes:\n{tools}\n\n"
        "User request: {query}\n\n"
        "Return a JSON array of steps. Each step is an object with exactly: "
        '"description" (what to do), "tool" (the tool name, or empty if none), '
        '"args" (an object, or {{}} if none).  Return ONLY the JSON array, no other text.'
    )

    # Indicators that a request is compound / multi-step rather than a single command.
    COMPLEX_INDICATORS = [
        "and then", "after that", "also", "open.*and", "search.*then",
        "first.*then", "multi", "sequence", "step by step",
        "compare.*and", "setup.*workspace", "prepare.*for",
    ]

    def __init__(self, llm: Optional[LLMProvider] = None) -> None:
        self.llm = llm

    def is_complex(self, query: str) -> bool:
        q = query.lower()
        words = ["and", "then", "also", "while", "before", "after", "both", "compare"]
        count = sum(1 for w in words if w in q)
        if count >= 2:
            return True
        return any(re.search(indicator, q) for indicator in self.COMPLEX_INDICATORS)

    def create_plan(
        self,
        user_query: str,
        tools_schema: Optional[List[Dict[str, Any]]] = None,
        llm: Optional[LLMProvider] = None,
        learning_context: Optional[str] = None,
    ) -> List[TaskPlanStep]:
        """Plan a goal. Returns at least one step always (the fallback).

        ``learning_context`` is an optional advisory text block from the learning
        subsystem (relevant past experiences and lessons).  When provided it is
        included in the planner prompt so the LLM can use it as evidence when
        choosing a strategy — but it is advisory only; the LLM still generates a
        fresh plan and may ignore the context when the current situation differs.
        """
        provider = llm or self.llm
        if not self.is_complex(user_query) or provider is None:
            return [TaskPlanStep(1, f"Process request: '{user_query}'")]

        plan = self._llm_plan(user_query, tools_schema, provider,
                              learning_context=learning_context)
        if not plan:
            plan = self._fallback_plan(user_query)
        return plan

    def _llm_plan(
        self,
        query: str,
        tools_schema: Optional[List[Dict[str, Any]]] = None,
        llm: Optional[LLMProvider] = None,
        learning_context: Optional[str] = None,
    ) -> Optional[List[TaskPlanStep]]:
        provider = llm or self.llm
        if provider is None:
            return None
        tools_text = ""
        if tools_schema:
            for s in tools_schema:
                func = s.get("function", {})
                tools_text += f"- {func.get('name')}: {func.get('description')}\n"
        else:
            tools_text = "(tool list not available)"

        prompt = self.PLANNING_PROMPT.format(tools=tools_text, query=query)
        if learning_context:
            prompt += "\n\n" + learning_context
        try:
            response = provider.chat(
                messages=[
                    ChatMessage(role="system", content="You are a task planner."),
                    ChatMessage(role="user", content=prompt),
                ],
                temperature=0.2,
            )
            text = response.content or ""
            start = text.find("[")
            end = text.rfind("]") + 1
            if start == -1 or end <= start:
                return None
            raw_steps = json.loads(text[start:end])
            steps = []
            for i, raw in enumerate(raw_steps):
                steps.append(TaskPlanStep(
                    step_number=i + 1,
                    description=raw.get("description", f"Step {i + 1}"),
                    tool_name=raw.get("tool", ""),
                    args=raw.get("args", {}),
                ))
            return steps if steps else None
        except Exception as e:  # noqa: BLE001 — planning is advisory; never crash
            logger.warning("LLM planning failed: %s", e)
            return None

    def _fallback_plan(self, query: str) -> List[TaskPlanStep]:
        q = query.lower()
        steps = []
        # Simple compound patterns (open X and open Y, find X then open it)
        if "open" in q and "and" in q:
            parts = q.split("and")
            for i, part in enumerate(parts):
                part = part.strip()
                target = part.replace("open ", "").replace("launch ", "").strip()
                if target:
                    steps.append(TaskPlanStep(i + 1, f"Open: {target}", tool_name="open_app", args={"app": target}))
            if steps:
                return steps
        if ("find" in q or "search" in q) and "compare" in q:
            term = q
            for prefix in ["find ", "search for ", "locate "]:
                if prefix in q:
                    term = q.split(prefix)[-1].split("compare")[0].strip()
                    break
            return [
                TaskPlanStep(1, f"Search: {term}", tool_name="web_search", args={"query": term}),
                TaskPlanStep(2, f"Compare results for: {term}", tool_name="web_search", args={"query": f"compare {term}"}),
            ]
        return [TaskPlanStep(1, f"Process request: '{query}'")]

    def format_plan(self, steps: List[TaskPlanStep]) -> str:
        lines = ["EXECUTION PLAN:"]
        for s in steps:
            status = "DONE" if s.completed else "PENDING"
            tag = f" [tool: {s.tool_name}]" if s.tool_name else ""
            lines.append(f"  {s.step_number}. [{status}] {s.description}{tag}")
        return "\n".join(lines)
