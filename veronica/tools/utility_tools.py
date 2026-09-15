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
_CALC_PATTERN = re.compile(r"\b(calculate|compute|what is)\b.*[\d]", re.I)
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
        now = dt.datetime.now()
        return ToolResult.ok(
            f"It's {now.strftime('%H:%M')} on {now.strftime('%A, %d %B %Y')}.",
            iso=now.isoformat(),
        )


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
        return bool(_CALC_PATTERN.search(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        expr = re.sub(r"^(calculate|compute|what is)\s*", "", text.strip(), flags=re.I)
        expr = expr.rstrip("?").strip()
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
        return ToolResult.ok(f"{expression} = {result}", result=result)


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
