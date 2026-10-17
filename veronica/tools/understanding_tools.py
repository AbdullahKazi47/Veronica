"""Voice commands for core/understanding.py and core/grounding.py:

  "my notes means Obsidian", "by the lab I mean the computer lab", "what words have you learned?",
  "forget what my notes means"
  "what did you get wrong this week?", "forget that correction"
  "look up the Treaty of Versailles" (Wikipedia summary, read out with its source)
"""
from __future__ import annotations

import re
from typing import Any, Optional

from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

_TEACH = re.compile(
    r"^\s*(?:(?:learn|remember|note)\s+(?:that\s+)?)?(?:by\s+)?[\"']?(?P<a>[a-z][\w' ]{1,38}?)[\"']?\s+"
    r"(?:means|is\s+short\s+for|stands\s+for)\s+(?P<b>[\w'.\- ]{2,80}?)\s*[.!]?\s*$|"
    r"^\s*by\s+[\"']?(?P<c>[\w' ]{2,38}?)[\"']?,?\s+i\s+mean\s+(?P<d>[\w'.\- ]{2,80}?)\s*[.!]?\s*$", re.I)
_LIST_WORDS = re.compile(r"\bwhat\s+(?:words|nicknames|names|aliases)\s+(?:have\s+you\s+learned|do\s+you\s+know)\b|"
                         r"\b(?:list|show)\s+(?:my\s+)?(?:words|nicknames|aliases)\b", re.I)
_FORGET_WORD = re.compile(r"^\s*forget\s+(?:what\s+)?[\"']?(?P<a>[\w' ]{2,38}?)[\"']?\s+means\s*[.!]?\s*$|"
                          r"^\s*forget\s+the\s+(?:word|nickname|alias)\s+[\"']?(?P<b>[\w' ]{2,38}?)[\"']?\s*[.!]?\s*$", re.I)
# "x means y" sentences that are questions or facts, not teaching
_NOT_TEACHING = re.compile(r"^\s*(?:what|which|who|how|does|do|is|why)\b|\b(?:word|term)\s+\w+\s+means\b", re.I)


class TeachWordsTool(Tool):
    name = "teach_words"
    description = ("Learn, list or forget the user's own words for things ('my notes' means Obsidian), "
                   "applied to every later request.")
    permission = PermissionLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {"phrase": {"type": "string"}, "meaning": {"type": "string"},
                       "action": {"type": "string", "enum": ["learn", "list", "forget"]}},
        "required": [],
    }

    def __init__(self, config=None, understanding=None) -> None:
        from veronica.core.understanding import Understanding
        self.u = understanding or Understanding(config)

    def matches(self, text: str) -> bool:
        if _LIST_WORDS.search(text) or _FORGET_WORD.match(text):
            return True
        m = _TEACH.match(text)
        if not m or _NOT_TEACHING.search(text):
            return False
        # Only clear teaching: "learn that ...", "my X means ...", "by X I mean ..." (not "that means war").
        taught = re.match(r"^\s*(?:learn|remember|note)\b", text, re.I)
        return bool(taught or m.group("c") or (m.group("a") or "").lower().startswith("my "))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        if _LIST_WORDS.search(text):
            return {"action": "list"}
        m = _FORGET_WORD.match(text)
        if m:
            return {"action": "forget", "phrase": m.group("a") or m.group("b")}
        m = _TEACH.match(text)
        if m:
            return {"action": "learn", "phrase": m.group("a") or m.group("c"), "meaning": m.group("b") or m.group("d")}
        return {"action": "list"}

    def run(self, action: str = "learn", phrase: str = "", meaning: str = "", **kwargs: Any) -> ToolResult:
        if action == "list":
            words = self.u.aliases.all()
            if not words:
                return ToolResult.ok("I haven't learned any of your own words yet. Say something like "
                                     "'my notes means Obsidian'.")
            return ToolResult.ok("Your words: " + "; ".join(f"{k} means {v}" for k, v in words.items()) + ".")
        if action == "forget":
            return (ToolResult.ok(f"Okay, '{phrase}' is just '{phrase}' again.") if self.u.aliases.remove(phrase)
                    else ToolResult.fail(f"I didn't have a meaning for '{phrase}'."))
        if not self.u.aliases.add(phrase, meaning):
            return ToolResult.fail("I couldn't learn that one. Try 'my notes means Obsidian'.")
        return ToolResult.ok(f"Got it. When you say '{phrase.strip()}', I'll take it as {meaning.strip()}.")


# ("corrections" alone are the speech corrections in speech_tools.py: "forget the correction for attacks")
_REVIEW = re.compile(r"\bwhat\s+(?:did|have)\s+you\s+(?:get|got|gotten)\s+wrong\b|"
                     r"\bwhat\s+have\s+you\s+learned\s+from\s+(?:me|my\s+corrections)\b|\bmistakes?\s+(?:this|last)\s+week\b",
                     re.I)
_FORGET_FIX = re.compile(r"^\s*(?:forget|undo|drop)\s+(?:that|the\s+last|your\s+last)\s+(?:correction|fix|lesson)"
                         r"(?P<which>)\s*[.!]?\s*$", re.I)


class ReviewFixesTool(Tool):
    name = "review_corrections"
    description = ("Say what VERONICA got wrong and learned from the user's corrections ('No, I meant ...'), "
                   "or forget one of those lessons.")
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {"forget": {"type": "string"}}, "required": []}

    def __init__(self, config=None, understanding=None) -> None:
        from veronica.core.understanding import Understanding
        self.u = understanding or Understanding(config)

    def matches(self, text: str) -> bool:
        return bool(_REVIEW.search(text) or _FORGET_FIX.match(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _FORGET_FIX.match(text)
        if m:
            return {"forget": m.group("which") or "last"}
        return {}

    def run(self, forget: str = "", **kwargs: Any) -> ToolResult:
        if forget:
            gone = self.u.fixes.forget(None if forget == "last" else forget)
            return (ToolResult.ok(f"Forgotten: '{gone.said}' won't turn into '{gone.meant}' any more.") if gone
                    else ToolResult.fail("I don't have a correction like that."))
        week = self.u.fixes.since(7 * 86400)
        if not week:
            return ToolResult.ok("Nothing this week. You haven't had to correct me.")
        lines = [f"you said '{f.said}' and meant '{f.meant}'" + (f", used {f.uses} times since" if f.uses else "")
                 for f in week[-8:]]
        return ToolResult.ok(f"This week I got {len(week)} thing{'s' if len(week) != 1 else ''} wrong: "
                             + "; ".join(lines) + ". Say 'forget that correction' to drop the last one.")


_LOOKUP = re.compile(r"^\s*(?:(?:hey\s+)?veronica[,\s]+)?(?:please\s+)?(?:look\s+up|wiki(?:pedia)?)\s+(?P<q>.+?)\s*[.?!]?\s*$",
                     re.I)


class LookUpTool(Tool):
    name = "look_up"
    description = ("Look a topic up on Wikipedia and return its summary with the source. Use for facts about "
                   "people, places, events, science and history before answering from memory.")
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}

    def __init__(self, config=None, lookup=None) -> None:
        self._lookup = lookup

    def matches(self, text: str) -> bool:
        return bool(_LOOKUP.match(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _LOOKUP.match(text)
        return {"query": m.group("q")} if m else {}

    def run(self, query: str = "", **kwargs: Any) -> ToolResult:
        from veronica.core.grounding import wikipedia
        if not query.strip():
            return ToolResult.fail("What should I look up?")
        src = (self._lookup or wikipedia)(query)
        if src is None:
            return ToolResult.fail(f"I couldn't find {query} on Wikipedia.")
        sentences = re.split(r"(?<=[.!?])\s+", src.text)
        return ToolResult.ok(f"According to {src.name}: " + " ".join(sentences[:3]), source=src.name, url=src.url)
