"""
Routines by voice:
  "create a routine called good night: set an alarm for 7 am, then lock my PC"
  "good night"  /  "run work mode"  /  "start my leaving routine"
  "add mute the volume to my good night routine"   "remove step 2 from good night"
  "what routines do I have?"  "what does good night do?"  "delete the work mode routine"

Each step is run as if you had said it, through the normal command path and the
permission gate — a routine never skips a question.
"""
from __future__ import annotations

import re
import threading
from typing import Any, Callable, Optional

from veronica.core.routines import MAX_STEPS, RoutineStore, parse_steps, routine_key
from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

logger = get_logger("tools.routines")
_local = threading.local()


def _store(config) -> RoutineStore:
    path = config.resolve_path("data/routines.json") if config is not None else "data/routines.json"
    return RoutineStore(path)


_CREATE = re.compile(
    r"^\s*(?:please\s+)?(?:create|make|set\s+up|add|save|build|new)\s+(?:a\s+|my\s+|the\s+|new\s+)*routine\s+"
    r"(?:called|named|for)\s+[\"']?(?P<name>[\w' ]{1,40}?)[\"']?\s*(?::|,|-|\s+(?:that|which|to|where\s+you|where\s+i\s+want\s+you\s+to|"
    r"with\s+steps?|and\s+it\s+should|it\s+should)\b)\s*(?P<steps>.+?)\s*[.!]?\s*$", re.I)
_LIST = re.compile(r"\b(?:what|which|list|show|tell\s+me)\b.*\broutines?\b(?!\s+called)|\bmy\s+routines\b", re.I)
_DESCRIBE = re.compile(r"^\s*what\s+(?:does|is\s+in)\s+(?:my\s+|the\s+)?(?P<name>[\w' ]{1,40}?)(?:\s+routine)?\s+(?:do|have|contain)?\s*\??\s*$", re.I)
_DELETE = re.compile(r"^\s*(?:please\s+)?(?:delete|remove|forget|get\s+rid\s+of)\s+(?:my\s+|the\s+)?(?P<name>[\w' ]{1,40}?)\s+routine\s*[.!]?\s*$", re.I)
_ADD_STEP = re.compile(r"^\s*(?:please\s+)?add\s+(?P<step>.+?)\s+to\s+(?:my\s+|the\s+)?(?P<name>[\w' ]{1,40}?)\s+routine\s*[.!]?\s*$", re.I)
_DROP_STEP = re.compile(r"^\s*(?:please\s+)?(?:remove|delete|drop)\s+step\s+(?P<n>\d+|one|two|three|four|five|six)\s+"
                        r"(?:from|of|in)\s+(?:my\s+|the\s+)?(?P<name>[\w' ]{1,40}?)(?:\s+routine)?\s*[.!]?\s*$", re.I)
_RUN_VERB = re.compile(r"^\s*(?:please\s+)?(?:run|start|do|begin|activate|launch|execute|trigger|play|go)\s+"
                       r"(?:my\s+|the\s+|into\s+)?(?P<name>.+?)(?:\s+routine)?\s*[.!]?\s*$", re.I)
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


class CreateRoutineTool(Tool):
    name = "create_routine"
    description = ("Create or replace a routine: a name and a list of steps, each an ordinary spoken command "
                   "(e.g. name 'good night', steps ['set an alarm for 7 am', 'lock my PC']). Saying the name "
                   "later runs the steps. Steps still ask permission when they normally would.")
    permission = PermissionLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string"},
                       "steps": {"type": "array", "items": {"type": "string"}, "description": "Spoken commands, in order."}},
        "required": ["name", "steps"],
    }

    def __init__(self, config=None, store: Optional[RoutineStore] = None) -> None:
        self.store = store or _store(config)

    def matches(self, text: str) -> bool:
        return bool(_CREATE.match(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _CREATE.match(text)
        return {"name": m.group("name").strip(), "steps": parse_steps(m.group("steps"))} if m else {}

    def run(self, name: str = "", steps=None, **kwargs: Any) -> ToolResult:
        if isinstance(steps, str):
            steps = parse_steps(steps)
        steps = list(steps or [])
        if len(steps) > MAX_STEPS:
            return ToolResult.fail(f"That's a lot of steps; a routine can have up to {MAX_STEPS}.")
        existed = self.store.get(name) is not None
        try:
            r = self.store.save(name, steps)
        except ValueError as e:
            return ToolResult.fail(f"I couldn't save that routine: {e}")
        listing = "; ".join(f"{i}. {s}" for i, s in enumerate(r.steps, 1))
        verb = "Updated" if existed else "Saved"
        return ToolResult.ok(f"{verb} your {r.name} routine with {len(r.steps)} step{'s' if len(r.steps) != 1 else ''}: "
                             f"{listing}. Say \"{r.name}\" to run it.", name=r.name, steps=r.steps)


class RunRoutineTool(Tool):
    name = "run_routine"
    description = ("Run one of the user's saved routines by name. Each step runs as a normal command and still "
                   "asks permission where it normally would.")
    permission = PermissionLevel.SAFE         # the routine itself; every step is gated on its own
    parameters = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}

    def __init__(self, config=None, store: Optional[RoutineStore] = None,
                 runner: Optional[Callable[[str], str]] = None) -> None:
        self.store = store or _store(config)
        self.runner = runner                  # set by main: runs one spoken command, returns the reply

    def _named(self, text: str) -> Optional[str]:
        from veronica.tools.registry import normalize_command
        tidy = routine_key(normalize_command(text))
        if not tidy:
            return None
        routines = {routine_key(r.name): r.name for r in self.store.all()}
        if tidy in routines:
            return routines[tidy]
        if tidy.endswith(" routine") and tidy[: -len(" routine")] in routines:
            return routines[tidy[: -len(" routine")]]
        m = _RUN_VERB.match(normalize_command(text))
        if m:
            name = routine_key(m.group("name"))
            name = name[: -len(" routine")] if name.endswith(" routine") else name
            if name in routines:
                return routines[name]
        return None

    def matches(self, text: str) -> bool:
        try:
            return self._named(text) is not None
        except Exception:  # noqa: BLE001 - a broken routines file must never block other commands
            return False

    def extract_arguments(self, text: str) -> dict[str, Any]:
        name = self._named(text)
        return {"name": name} if name else {}

    def run(self, name: str = "", **kwargs: Any) -> ToolResult:
        r = self.store.get(name)
        if r is None:
            known = ", ".join(x.name for x in self.store.all())
            return ToolResult.fail(f"I don't have a routine called {name}." + (f" I have: {known}." if known else ""))
        if self.runner is None:
            return ToolResult.fail("Routines can't run right now (no command runner is connected).")
        if getattr(_local, "depth", 0):
            return ToolResult.fail("A routine can't start another routine.")
        _local.depth = 1
        replies, failed = [], 0
        try:
            for step in r.steps:
                try:
                    reply = (self.runner(step) or "").strip()
                except Exception as e:  # noqa: BLE001 - one bad step must not stop the rest
                    logger.exception("Routine %s step failed: %s", r.name, step)
                    reply, failed = f"{step} failed: {e}", failed + 1
                logger.info("Routine %s step %r -> %r", r.name, step, reply[:120])
                if reply:
                    replies.append(reply.rstrip(".") + ".")
        finally:
            _local.depth = 0
        self.store.mark_run(r.name)
        body = " ".join(replies) if replies else "All done."
        return ToolResult.ok(f"{r.name.capitalize()} routine: {body}", steps=len(r.steps), failed=failed)


class RoutineInfoTool(Tool):
    name = "routine_info"
    description = "List the user's routines, or say what one routine does."
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {"name": {"type": "string"}}, "required": []}

    def __init__(self, config=None, store: Optional[RoutineStore] = None) -> None:
        self.store = store or _store(config)

    def matches(self, text: str) -> bool:
        if _LIST.search(text):
            return True
        m = _DESCRIBE.match(text)
        return bool(m) and self.store.get(m.group("name")) is not None

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _DESCRIBE.match(text)
        if m and self.store.get(m.group("name")) is not None:
            return {"name": m.group("name").strip()}
        return {}

    def run(self, name: str = "", **kwargs: Any) -> ToolResult:
        if name:
            r = self.store.get(name)
            if r is None:
                return ToolResult.fail(f"I don't have a routine called {name}.")
            steps = "; ".join(f"{i}. {s}" for i, s in enumerate(r.steps, 1))
            return ToolResult.ok(f"Your {r.name} routine: {steps}.", steps=r.steps)
        items = self.store.all()
        if not items:
            return ToolResult.ok("You don't have any routines yet. Try: \"create a routine called good night: "
                                 "set an alarm for 7 am, then lock my PC\".")
        names = [f"{r.name} ({len(r.steps)} steps)" for r in items]
        return ToolResult.ok("Your routines: " + ", ".join(names) + ".", names=[r.name for r in items])


class EditRoutineTool(Tool):
    name = "edit_routine"
    description = ("Change a routine: action 'add_step' (step), 'remove_step' (number, from 1), or 'delete' "
                   "the whole routine.")
    permission = PermissionLevel.MODERATE     # like forgetting one remembered fact: no question, but deliberate
    parameters = {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": ["add_step", "remove_step", "delete"]},
                       "name": {"type": "string"}, "step": {"type": "string"}, "number": {"type": "integer"}},
        "required": ["action", "name"],
    }

    def __init__(self, config=None, store: Optional[RoutineStore] = None) -> None:
        self.store = store or _store(config)

    def matches(self, text: str) -> bool:
        for rx in (_ADD_STEP, _DROP_STEP, _DELETE):
            m = rx.match(text)
            if m and self.store.get(m.group("name")) is not None:
                return True
        return False

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _ADD_STEP.match(text)
        if m and self.store.get(m.group("name")):
            return {"action": "add_step", "name": m.group("name").strip(), "step": m.group("step").strip()}
        m = _DROP_STEP.match(text)
        if m and self.store.get(m.group("name")):
            n = m.group("n").lower()
            return {"action": "remove_step", "name": m.group("name").strip(), "number": int(_WORDS.get(n, n))}
        m = _DELETE.match(text)
        if m:
            return {"action": "delete", "name": m.group("name").strip()}
        return {}

    def run(self, action: str = "", name: str = "", step: str = "", number: int = 0, **kwargs: Any) -> ToolResult:
        if action == "delete":
            gone = self.store.delete(name)
            return ToolResult.ok(f"Deleted your {gone} routine.") if gone else ToolResult.fail(f"No routine called {name}.")
        if action == "add_step":
            r = self.store.add_step(name, step)
            if r is None:
                return ToolResult.fail(f"No routine called {name}.")
            return ToolResult.ok(f"Added \"{step}\" as step {len(r.steps)} of {r.name}.")
        if action == "remove_step":
            res = self.store.remove_step(name, int(number or 0))
            if res is None:
                return ToolResult.fail(f"{name} has no step {number}.")
            r, removed = res
            tail = f" That was its last step, so the routine is gone." if not r.steps else ""
            return ToolResult.ok(f"Removed \"{removed}\" from {r.name}.{tail}")
        return ToolResult.fail("I can add a step, remove a step, or delete a routine.")
