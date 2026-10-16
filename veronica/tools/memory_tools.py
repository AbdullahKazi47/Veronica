"""
Voice-managed long-term memory: "remember that ...", "what do you remember about me",
"forget ...". Facts persist in data/memory.json (LongTermMemory) and are injected into
every LLM turn by the orchestrator as labeled, informational context — never as
instructions and never as permission.

Persisting facts across restarts is the idea from Prodigy-AI's JSON memory; the code is
VERONICA's own. Secrets are refused before they are stored.
"""
from __future__ import annotations

import difflib
import re
from typing import Any, Optional

from veronica.memory.long_term import LongTermMemory
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

# Sentences ABOUT secrets ("my password is sunflower") don't look like a token, so the
# token check in learning.experience misses them; refuse these by meaning as well.
_SECRET_WORDS = re.compile(
    r"\b(pass\s?words?|passcodes?|pins?|otps?|cvv|cvc|card\s+numbers?|credit\s+cards?|debit\s+cards?|"
    r"bank\s+account|account\s+numbers?|ifsc|api\s+keys?|tokens?|secret\s+keys?|private\s+keys?|"
    r"seed\s+phrases?|recovery\s+codes?|security\s+codes?|aadhaar|pan\s+number|ssn|social\s+security)\b", re.I)
_MAX_FACT_CHARS = 300


def is_secret(text: str) -> bool:
    from veronica.learning.experience import _looks_like_secret_value
    return bool(_SECRET_WORDS.search(text or "")) or any(
        _looks_like_secret_value(w) for w in (text or "").split())


def fact_key(text: str) -> str:
    """'my sister's birthday is 12 March' -> "my sister's birthday" (so saying a new
    birthday replaces the old one instead of piling up contradictions)."""
    t = re.sub(r"\s+", " ", (text or "").strip().rstrip(".!")).lower()
    t = re.sub(r"^(?:that\s+)", "", t)
    m = re.match(r"^(.{2,60}?)\s+(?:is|are|was|were|=)\s+.+$", t)
    if m and len(m.group(1).split()) <= 6:
        return m.group(1)
    return t[:80]


def _first_person_to_second(text: str) -> str:
    """For speaking a fact back: "my sister's birthday is ..." -> "your sister's birthday is ..."."""
    swaps = {"my": "your", "i": "you", "i'm": "you're", "am": "are", "me": "you", "mine": "yours", "i've": "you've"}
    return re.sub(r"\b(my|i'm|i've|i|am|me|mine)\b", lambda m: swaps[m.group(1).lower()], text, flags=re.I)


class _MemoryToolBase(Tool):
    def __init__(self, config=None, memory: Optional[LongTermMemory] = None) -> None:
        if memory is None:
            path = config.resolve_path(config.memory.long_term_path) if config else None
            from pathlib import Path
            memory = LongTermMemory(path or Path("data/memory.json"))
        self.memory = memory


# Not "remember to ..." (a task) or "remember when/what ..." (a question).
_REMEMBER = re.compile(r"^\s*(?:please\s+)?(?:remember|don'?t\s+forget|keep\s+in\s+mind)\s+(?:that\s+)?"
                       r"(?!(?:to|when|what|who|how|where|why|if|me|us|the\s+time)\b)(.+?)\s*[.!]?\s*$", re.I)


class RememberFactTool(_MemoryToolBase):
    name = "remember_fact"
    description = ("Save a lasting fact or preference about the user to long-term memory, in the user's "
                   "own first-person words (e.g. 'my sister's birthday is 12 March', 'I am vegetarian'). "
                   "Use when the user asks you to remember something, or shares a durable personal fact. "
                   "Never store passwords, PINs, card or account numbers, or keys.")
    permission = PermissionLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {"fact": {"type": "string", "description": "The fact, in first person."}},
        "required": ["fact"],
    }

    def matches(self, text: str) -> bool:
        return bool(_REMEMBER.match(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _REMEMBER.match(text)
        return {"fact": m.group(1)} if m else {}

    def run(self, fact: str = "", **kwargs: Any) -> ToolResult:
        fact = re.sub(r"\s+", " ", (fact or "").strip()).rstrip(".!")
        if not fact:
            return ToolResult.fail("What should I remember?")
        if is_secret(fact):
            return ToolResult.fail("I don't store passwords, PINs, card numbers or keys. Please keep that somewhere safe instead.")
        if len(fact) > _MAX_FACT_CHARS:
            return ToolResult.fail("That's too long to remember as one fact. Try a shorter sentence.")
        key = fact_key(fact)
        replaced = self.memory.recall(key) is not None
        self.memory.remember(key, fact)
        spoken = _first_person_to_second(fact)
        return ToolResult.ok(("Updated. " if replaced else "Got it, I'll remember that ") + spoken + ".",
                             key=key, replaced=replaced)


_RECALL = re.compile(r"\bwhat\s+(?:do|else\s+do)\s+you\s+(?:know|remember)\s+about\s+me\b|"
                     r"\bwhat\s+(?:have\s+you|did\s+you)\s+(?:remembered|remember|saved)\b|"
                     r"\b(?:list|show|read)\s+(?:me\s+)?(?:my|your)\s+memor(?:y|ies)\b", re.I)


class RecallMemoryTool(_MemoryToolBase):
    name = "recall_memory"
    description = "List what VERONICA remembers about the user, optionally only facts matching a topic."
    permission = PermissionLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {"topic": {"type": "string", "description": "Optional word to filter by."}},
        "required": [],
    }

    def matches(self, text: str) -> bool:
        return bool(_RECALL.search(text))

    def run(self, topic: str = "", **kwargs: Any) -> ToolResult:
        facts = self.memory.search(topic) if topic else self.memory.all_facts()
        if not facts:
            return ToolResult.ok(f"I don't have anything saved{' about ' + topic if topic else ''} yet. "
                                 "Say 'remember that ...' to teach me.", count=0)
        spoken = [_first_person_to_second(str(f.value)) for f in sorted(facts, key=lambda f: f.created)]
        shown = spoken[:8]
        more = f" And {len(spoken) - 8} more." if len(spoken) > 8 else ""
        return ToolResult.ok("Here's what I remember: " + "; ".join(shown) + "." + more, count=len(spoken))


_FORGET_ALL = re.compile(r"^\s*(?:please\s+)?forget\s+(?:everything|all\s+(?:of\s+)?(?:it|that|my\s+memories|memories|"
                         r"about\s+me))(?:\s+about\s+me)?\s*[.!]?\s*$", re.I)
# "never mind, forget it" is not a memory request.
_FORGET = re.compile(r"^\s*(?:please\s+)?forget\s+(?:that\s+|about\s+)?(?!(?:it|that|this|about\s+it)\s*[.!]?\s*$)(.+?)\s*[.!]?\s*$",
                     re.I)


class ForgetFactTool(_MemoryToolBase):
    name = "forget_fact"
    description = "Delete one remembered fact. `which` is words from the fact, e.g. 'my sister's birthday'."
    permission = PermissionLevel.MODERATE
    parameters = {
        "type": "object",
        "properties": {"which": {"type": "string", "description": "Words identifying the fact."}},
        "required": ["which"],
    }

    def matches(self, text: str) -> bool:
        return bool(_FORGET.match(text)) and not _FORGET_ALL.match(text)

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _FORGET.match(text)
        return {"which": m.group(1)} if m else {}

    def _find(self, which: str):
        q = fact_key(which)
        facts = self.memory.all_facts()
        hit = next((f for f in facts if f.key == q), None) or \
            next((f for f in facts if q in f.key or q in str(f.value).lower()), None)
        if hit is None:
            close = difflib.get_close_matches(q, [f.key for f in facts], n=1, cutoff=0.6)
            hit = next((f for f in facts if close and f.key == close[0]), None)
        return hit

    def run(self, which: str = "", **kwargs: Any) -> ToolResult:
        if not (which or "").strip():
            return ToolResult.fail("What should I forget?")
        hit = self._find(which)
        if hit is None:
            return ToolResult.fail(f"I don't have anything saved about {which}.")
        self.memory.forget(hit.key)
        return ToolResult.ok(f"Forgotten: {_first_person_to_second(str(hit.value))}.", key=hit.key)


class ForgetAllMemoriesTool(_MemoryToolBase):
    name = "forget_all_memories"
    description = "Delete EVERYTHING VERONICA remembers about the user. Only when explicitly asked."
    permission = PermissionLevel.SENSITIVE  # destructive: always confirmed
    parameters = {"type": "object", "properties": {}, "required": []}

    def matches(self, text: str) -> bool:
        return bool(_FORGET_ALL.match(text))

    def run(self, **kwargs: Any) -> ToolResult:
        facts = self.memory.all_facts()
        for f in facts:
            self.memory.forget(f.key)
        return ToolResult.ok(f"Done. I've forgotten all {len(facts)} thing{'s' if len(facts) != 1 else ''} I knew about you.",
                             count=len(facts))
