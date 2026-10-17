from __future__ import annotations

import ast
import datetime as dt
import json
import operator
import re
from pathlib import Path
from typing import Any

from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

_TIME_PATTERN = re.compile(r"\b(what.?s the time|what time is it|current time|today.?s date|what.?s the date)\b", re.I)
_CALC_PATTERN = re.compile(r"\b(calculate|compute|what(?:'s| is))\b.*[\d]|"
                           r"\b(calculate|compute|what(?:'s| is))\b.*\b(?:plus|minus|times|divided|squared|"
                           r"cubed|square\s+root)\b", re.I)

# Spoken maths -> symbols: "12 times 7", "100 divided by 4", "5 squared", "two plus two".
_WORD_NUMBERS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "twenty": 20, "thirty": 30,
                 "forty": 40, "fifty": 50, "hundred": 100, "thousand": 1000}
_SPOKEN_OPS = [
    (r"\bto\s+the\s+power\s+of\b|\braised\s+to\b", "**"),
    (r"\bmultiplied\s+by\b|\btimes\b|(?<=\d)\s*x\s*(?=\d)", "*"),
    (r"\bdivided\s+by\b|\bover\b", "/"),
    (r"\bplus\b|\badded\s+to\b", "+"),
    (r"\bminus\b|\btake\s+away\b", "-"),
    (r"\bpercent\b|\bper\s+cent\b", "%"),
]


def spoken_to_expression(text: str) -> str:
    """'twelve times seven' -> '12 * 7' (only arithmetic words; anything else stays and is refused)."""
    t = text.lower().strip().rstrip("?.!")
    t = re.sub(r"^(?:the\s+)", "", t)
    t = re.sub(r"\b(" + "|".join(_WORD_NUMBERS) + r")\b", lambda m: str(_WORD_NUMBERS[m.group(1)]), t)
    t = re.sub(r"\bsquare\s+root\s+of\s+(\d+(?:\.\d+)?)", r"(\1**0.5)", t)
    t = re.sub(r"(\d+(?:\.\d+)?)\s+squared\b", r"(\1**2)", t)
    t = re.sub(r"(\d+(?:\.\d+)?)\s+cubed\b", r"(\1**3)", t)
    for pattern, sym in _SPOKEN_OPS:
        t = re.sub(pattern, f" {sym} ", t)
    return re.sub(r"\s+", " ", t).strip()
_MATH_EXPR_PATTERN = re.compile(r"^[\d\s\.\+\-\*\/\(\)%]+$")

_ALLOWED_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_OPS:
        return _ALLOWED_OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_OPS:
        return _ALLOWED_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("Unsupported expression.")


class CurrentTimeTool(Tool):
    name = "get_current_time"
    description = "Get the current local date and time."
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {}, "required": []}

    def matches(self, text: str) -> bool:
        return bool(_TIME_PATTERN.search(text))

    def run(self, **kwargs: Any) -> ToolResult:
        from veronica.core.flair import spoken_time
        now = dt.datetime.now()
        return ToolResult.ok(spoken_time(now), iso=now.isoformat())


def _number(value) -> str:
    """84.0 -> '84', 0.333333333 -> '0.333333', 1e21 stays readable."""
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return f"{value:.6g}"
    return str(value)


def _speakable(expr: str) -> str:
    """'(5**2)' -> '5 squared', '12 * 7' -> '12 times 7' — so she doesn't read out symbols."""
    t = re.sub(r"\((\d+(?:\.\d+)?)\*\*2\)", r"\1 squared", expr)
    t = re.sub(r"\((\d+(?:\.\d+)?)\*\*3\)", r"\1 cubed", t)
    t = re.sub(r"\((\d+(?:\.\d+)?)\*\*0\.5\)", r"the square root of \1", t)
    t = re.sub(r"\((\d+(?:\.\d+)?)/100\)\*(\d+(?:\.\d+)?)", r"\1 percent of \2", t)
    for sym, word in (("**", " to the power of "), ("*", " times "), ("/", " divided by "),
                      ("+", " plus "), (" - ", " minus "), ("%", " percent ")):
        t = t.replace(sym, word)
    return re.sub(r"\s+", " ", t).strip()


class CalculatorTool(Tool):
    name = "calculate"
    description = "Evaluate an arithmetic expression, e.g. '25% of 800' or '(4+5)*3'."
    permission = PermissionLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {"expression": {"type": "string", "description": "The arithmetic expression."}},
        "required": ["expression"],
    }

    def matches(self, text: str) -> bool:
        # Only claim questions that really are arithmetic: "what is the capital of 7 seas"
        # has a digit but belongs to the model.
        if not _CALC_PATTERN.search(text):
            return False
        expr = self.extract_arguments(text)["expression"].replace("^", "**")
        return bool(_MATH_EXPR_PATTERN.match(expr.replace("**", ""))) and bool(re.search(r"\d", expr))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        expr = re.sub(r"^(calculate|compute|what(?:'s| is))\s*", "", text.strip(), flags=re.I)
        expr = expr.rstrip("?").strip()
        if not re.search(r"%\s*of", expr, re.I):
            expr = spoken_to_expression(expr)
        # handle "X% of Y" -> (X/100)*Y
        pct = re.match(r"([\d\.]+)\s*%\s*of\s*([\d\.]+)", expr, re.I)
        if pct:
            expr = f"({pct.group(1)}/100)*{pct.group(2)}"
        return {"expression": expr}

    def run(self, expression: str = "", **kwargs: Any) -> ToolResult:
        cleaned = expression.replace("^", "**")
        if not _MATH_EXPR_PATTERN.match(cleaned.replace("**", "")):
            return ToolResult.fail(f"'{expression}' doesn't look like a safe arithmetic expression.")
        try:
            tree = ast.parse(cleaned, mode="eval")
            result = _safe_eval(tree.body)
        except Exception:
            return ToolResult.fail(f"Couldn't evaluate '{expression}'.")
        return ToolResult.ok(f"{_speakable(expression)} is {_number(result)}", result=result)


class NoteTool(Tool):
    name = "take_note"
    description = "Save a short note/reminder text for later retrieval."
    permission = PermissionLevel.MODERATE
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "The note content."}},
        "required": ["text"],
    }

    def __init__(self, config=None) -> None:
        self.config = config
        default_path = config.resolve_path("data/notes.json") if config else Path("data/notes.json")
        self.path = default_path

    def run(self, text: str = "", **kwargs: Any) -> ToolResult:
        if not text:
            return ToolResult.fail("No note text provided.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        notes = []
        if self.path.exists():
            try:
                notes = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                notes = []
        notes.append({"text": text, "created": dt.datetime.now().isoformat()})
        self.path.write_text(json.dumps(notes, indent=2), encoding="utf-8")
        return ToolResult.ok(f"Noted: \"{text}\"", count=len(notes))
