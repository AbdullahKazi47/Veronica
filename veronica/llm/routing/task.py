"""
Task analysis: what kind of request is this? Classified with plain, deterministic
heuristics — NO LLM is asked to decide which LLM should answer (spec hard rule).
The analyzer produces a TaskProfile the ModelRouter turns into a ModelSelection.

Pure functions, fully inspectable: given the same text it always returns the same
profile. Keyword lists live here so future task types/words are a one-file change.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from veronica.llm.routing.profiles import (
    TASK_ANALYSIS, TASK_CHAT, TASK_CODING, TASK_GENERAL, TASK_PLANNING,
    TASK_REASONING, TASK_SUMMARIZATION, TASK_TOOL_USE, TASK_VISION,
)

COMPLEXITY_LOW = "low"
COMPLEXITY_MEDIUM = "medium"
COMPLEXITY_HIGH = "high"

# Deterministic vocabulary. Words are matched as whole tokens against the lowercased
# text so "person" never triggers vision ("son in law", "er" substrings, ...).
_REASONING_WORDS = frozenset({
    "why", "explain", "reason", "because", "justify", "derive", "prove",
    "deduce", "infer", "logic", "implication", "counterfactual", "what if",
    "hypothesis", "evaluate", "assess", "compare", "contrast", "synthesize",
    "argue", "analysis", "root cause", "trade-off", "tradeoff",
})
_REASONING_PHRASES = (
    "think step by step", "step by step", "critical thinking", "cause and effect",
    "means-end", "work through", "deeper", "underlying reason", "implications",
)
_CODE_WORDS = frozenset({
    "code", "function", "script", "python", "javascript", "typescript", "java",
    "rust", "golang", "sql", "regex", "debug", "bug", "exception", "traceback",
    "syntax", "compile", "refactor", "implement", "algorithm", "function signature",
    "class", "variable", "api", "library", "package", "query", "cli", "pytest",
})
_CODE_PHRASES = (
    "write a function", "write a script", "write a program", "write code",
    "fix this", "what's wrong with this", "what is wrong with this",
    "make it work", "unit test", "test case", "code review", "error handling",
    "proof of concept", "boilerplate",
)
_PLANNING_WORDS = frozenset({
    "plan", "roadmap", "strategy", "schedule", "milestone", "blueprint",
    "steps", "approach", "checklist", "timeline", "organize", "prepare for",
    "decompose", "next steps",
})
_SUMMARIZE_WORDS = frozenset({
    "summarize", "summari", "summarise", "condense", "digest", "tl;dr",
    "synopsis", "abstract", "key points", "key takeaways", "in short",
    "brief me", "recap",
})
_TOOL_WORDS = frozenset({
    "open", "close", "launch", "search", "find", "play", "weather", "volume",
    "instal", "create file", "write file", "read file", "delete file", "run command",
    "scroll", "click", "screenshot", "note", "remind", "timer", "set up", "shut down",
    "restart", "install", "uninstall", "download", "browse", "navigate", "paint",
})
_CHAT_WORDS = frozenset({
    "hello", "hi", "hey", "good morning", "good evening", "good afternoon",
    "how are you", "nice to meet", "thank", "thanks", "you're welcome", "ok",
    "okay", "goodbye", "bye", "see you", "joke", "jokes", "fun fact", "hrllo",
})
_VISION_WORDS = frozenset({
    "screenshot", "image", "photo", "picture", "visual", "ocr", "look at",
    "describe this", "what's on my screen", "what is on my screen", "see my",
    "icon", "ui element", "screen", "capture",
})

# Streaming LLM V1 (Phase 4): a request that EXPLICITLY asks for output to arrive
# progressively ("stream the answer", "as it arrives", ...). Conservative on purpose:
# ordinary chat never sets this — only an explicit ask for live/incremental output.
_STREAM_WORDS = frozenset({"stream", "streaming", "progressively"})
_STREAM_PHRASES = (
    "as it arrives", "live response", "live output", "in real time",
    "type it out", "as they come", "word by word",
)

_FORBIDDEN_GENERAL = frozenset({"known", "detail"})


@dataclass(frozen=True)
class TaskProfile:
    """The result of analyzing one request: what kind of task, at what size/cost.

    ``explicit_model_request`` is the ONLY routing input that comes from the user
    directly. It is populated only when the current text names a model that exists
    in the registry — never from memory, from preferences, or from past turns.
    """
    task_type: str = TASK_GENERAL
    complexity: str = COMPLEXITY_MEDIUM
    requires_reasoning: bool = False
    requires_code: bool = False
    requires_vision: bool = False
    requires_tools: bool = False
    requires_streaming: bool = False   # user explicitly asked for output as it arrives
    latency_sensitive: bool = False
    estimated_context_size: int = 0
    priority: int = 50
    explicit_model_request: Optional[str] = None
    classification_notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "task_type": self.task_type,
            "complexity": self.complexity,
            "requires_reasoning": self.requires_reasoning,
            "requires_code": self.requires_code,
            "requires_vision": self.requires_vision,
            "requires_tools": self.requires_tools,
            "requires_streaming": self.requires_streaming,
            "latency_sensitive": self.latency_sensitive,
            "estimated_context_size": self.estimated_context_size,
            "priority": self.priority,
            "explicit_model_request": self.explicit_model_request,
        }


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) >= 2}


def _contains_any(text: str, words: frozenset[str], phrases: tuple[str, ...]) -> bool:
    if any(p in text for p in phrases):
        return True
    return bool(_tokens(text) & words)


class TaskAnalyzer:
    """Deterministic, cheap classification. Give it the current user text (plus a
    small context dict) and it returns a TaskProfile. Never calls a model."""

    def __init__(self, known_model_ids: Optional[set[str]] = None) -> None:
        # Used only to validate an explicit "use model X" request. Empty set means
        # no explicit model hint is ever honored (safe default).
        self._known_models = frozenset(known_model_ids or set())

    # -- public ---------------------------------------------------------------
    def analyze(self, text: str, context: Optional[dict] = None) -> TaskProfile:
        ctx = context or {}
        text = (text or "").strip()
        if not text:
            return TaskProfile(
                task_type=TASK_GENERAL,
                complexity=COMPLEXITY_LOW,
                latency_sensitive=True,
                estimated_context_size=256,
            )
        lower = text.lower()

        explicit = self._detect_explicit_model(lower)

        requires_vision = _contains_any(lower, _VISION_WORDS, ())
        requires_code = _contains_any(lower, _CODE_WORDS, _CODE_PHRASES)
        requires_reasoning = _contains_any(lower, _REASONING_WORDS, _REASONING_PHRASES)
        requires_tools = _contains_any(lower, _TOOL_WORDS, ())
        requires_streaming = _contains_any(lower, _STREAM_WORDS, _STREAM_PHRASES)
        planning = _contains_any(lower, _PLANNING_WORDS, ())
        summarizing = _contains_any(lower, _SUMMARIZE_WORDS, ())
        chatting = _contains_any(lower, _CHAT_WORDS, ())

        n_words = len(lower.split())

        task_type = self._choose_type(
            requires_vision=requires_vision,
            requires_code=requires_code,
            requires_reasoning=requires_reasoning,
            planning=planning,
            summarizing=summarizing,
            requires_tools=requires_tools,
            chatting=chatting,
            n_words=n_words,
            text=lower,
        )

        complexity = self._complexity(n_words, requires_reasoning, requires_code, planning)
        latency_sensitive = (n_words <= 14 and not requires_reasoning and not requires_code)
        estimated = self._estimate_context(text, ctx)

        priority = 50
        if explicit:
            priority = 90
        elif task_type in (TASK_REASONING, TASK_CODING, TASK_PLANNING, TASK_ANALYSIS):
            priority = 65
        elif task_type in (TASK_CHAT,) and complexity == COMPLEXITY_LOW:
            priority = 30

        notes = []
        if explicit:
            notes.append("explicit_model_request")
        if requires_vision:
            notes.append("required_capability=vision")
        if requires_tools:
            notes.append("required_capability=tools")
        if requires_streaming:
            notes.append("required_capability=streaming")
        if latency_sensitive:
            notes.append("latency_sensitive")

        return TaskProfile(
            task_type=task_type,
            complexity=complexity,
            requires_reasoning=requires_reasoning,
            requires_code=requires_code,
            requires_vision=requires_vision,
            requires_tools=requires_tools,
            requires_streaming=requires_streaming,
            latency_sensitive=latency_sensitive,
            estimated_context_size=estimated,
            priority=priority,
            explicit_model_request=explicit,
            classification_notes=tuple(notes),
        )

    # -- internal -------------------------------------------------------------
    def _detect_explicit_model(self, lower: str) -> Optional[str]:
        """Honor 'use <model>' / 'switch to <model>' / 'with <model>' ONLY when the
        named model is actually registered. An unknown name is a normal request, not
        a routing instruction."""
        if not self._known_models:
            return None
        # Longest first so "llama3.1:8b" is matched before any prefix model.
        for model_id in sorted(self._known_models, key=len, reverse=True):
            esc = re.escape(model_id)
            pattern = rf"\b(?:use|switch to|switch|with|try|prefer|using)\s+(?:model\s+)?{esc}\b"
            if re.search(pattern, lower):
                return model_id
        return None

    def _choose_type(self, **kw) -> str:
        # Hard-capability tasks win first; then increasing "cognitive weight".
        if kw["requires_vision"]:
            return TASK_VISION
        if kw["requires_code"]:
            return TASK_CODING
        if kw["requires_reasoning"]:
            if kw["n_words"] >= 12 or _contains_any(kw["text"], frozenset({"evaluate", "assess", "compare", "contrast", "synthesize", "analyze"}), ()):
                return TASK_ANALYSIS
            return TASK_REASONING
        if kw["planning"]:
            return TASK_PLANNING
        if kw["summarizing"]:
            return TASK_SUMMARIZATION
        if kw["requires_tools"] and kw["n_words"] <= 20:
            return TASK_TOOL_USE
        if kw["chatting"] and kw["n_words"] <= 12:
            return TASK_CHAT
        return TASK_GENERAL

    def _complexity(self, n_words: int, reasoning: bool, code: bool, planning: bool) -> str:
        if n_words >= 30 or reasoning or code or planning:
            return COMPLEXITY_HIGH
        if n_words >= 8:
            return COMPLEXITY_MEDIUM
        return COMPLEXITY_LOW

    def _estimate_context(self, text: str, ctx: dict) -> int:
        # Rough token estimate: words*1.3 + system/persona base, plus prior history
        # the orchestrator says is in the conversation.
        base = 512
        history = int(ctx.get("history_tokens", 0))
        return int(base + len(text.split()) * 1.3 + history)


__all__ = [
    "TaskProfile", "TaskAnalyzer",
    "COMPLEXITY_LOW", "COMPLEXITY_MEDIUM", "COMPLEXITY_HIGH",
]