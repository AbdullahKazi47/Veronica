"""
Abilities added by voice: "Veronica, add flight tracking".

  build    — the coding model writes one Ability (veronica/plugin_api.py) for the request
  check    — before ANY of it runs, the code is inspected: only plain-data imports and the
             plugin API, no file/OS/eval/introspection tricks, web access only to the sites
             it declares (enforced again at call time)
  test     — its EXAMPLES run in a separate Python process with a time limit; failures go
             back to the model for a fix (at most twice)
  approve  — nothing is switched on until the user says yes (the activate tool is SENSITIVE)
  load     — approved abilities are recorded with a SHA-256 of their code; a file that
             changed since approval is refused, and the check runs again at every start

An ability can only look things up on the internet and return a sentence. It never
controls the PC. Files live in data/abilities/ (drafts/ and approved/).
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from veronica.core import jsonsafe
from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

logger = get_logger("core.abilities")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_NAME = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
_MAX_CODE = 20_000
_RUN_TIMEOUT_S = 20
_TEST_TIMEOUT_S = 60
_ALLOWED_IMPORTS = {
    "re", "json", "math", "datetime", "time", "typing", "urllib.parse", "statistics", "collections",
    "dataclasses", "random", "textwrap", "html", "itertools", "functools", "decimal",
    "fractions", "zoneinfo", "calendar",
}   # not "string": string.Formatter can reach internal objects through a format string
_PLUGIN_NAMES = {"Ability", "AbilityError", "http_get_json", "http_get_text", "api_key"}
_FORBIDDEN_CALLS = {"eval", "exec", "compile", "open", "__import__", "globals", "locals", "vars", "getattr",
                    "setattr", "delattr", "input", "breakpoint", "help", "exit", "quit", "memoryview",
                    "classmethod", "staticmethod", "super", "object",
                    "get_type_hints", "ForwardRef"}   # typing helpers that evaluate text as code
# str.format / format_map can walk attributes ("{0.__class__}") outside the checked syntax.
_FORBIDDEN_ATTRS = {"format", "format_map", "get_type_hints"}


# ---------------------------------------------------------------- static check

def check_code(code: str) -> tuple[list[str], dict]:
    """(problems, facts). facts: name, description, hosts, class_name. No code is run."""
    problems: list[str] = []
    facts: dict = {}
    if len(code) > _MAX_CODE:
        return [f"Code is too long ({len(code)} characters; max {_MAX_CODE})."], facts
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"Syntax error on line {e.lineno}: {e.msg}"], facts

    abilities = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in _ALLOWED_IMPORTS:
                    problems.append(f"Line {node.lineno}: importing {a.name} is not allowed.")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "veronica.plugin_api":
                bad = [a.name for a in node.names if a.name not in _PLUGIN_NAMES]
                if bad:
                    problems.append(f"Line {node.lineno}: only {sorted(_PLUGIN_NAMES)} may come from the plugin API.")
            elif mod not in _ALLOWED_IMPORTS or node.level:
                problems.append(f"Line {node.lineno}: importing from {mod or '.'} is not allowed.")
        elif isinstance(node, ast.Attribute) and (node.attr.startswith("_") or node.attr in _FORBIDDEN_ATTRS):
            problems.append(f"Line {node.lineno}: '.{node.attr}' is not allowed.")
        elif isinstance(node, ast.Name) and (node.id in _FORBIDDEN_CALLS or node.id.startswith("_")):
            problems.append(f"Line {node.lineno}: '{node.id}' is not allowed.")
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name.startswith("_"):
            problems.append(f"Line {node.lineno}: names starting with '_' are not allowed.")
        elif isinstance(node, ast.arg) and node.arg.startswith("_"):
            problems.append(f"Line {node.lineno}: names starting with '_' are not allowed.")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            problems.append(f"Line {node.lineno}: global/nonlocal is not allowed.")
        elif isinstance(node, ast.ClassDef) and any(isinstance(b, ast.Name) and b.id == "Ability" for b in node.bases):
            abilities.append(node)

    if len(abilities) != 1:
        problems.append("The code must define exactly one class based on Ability.")
        return problems, facts
    cls = abilities[0]
    facts["class_name"] = cls.name
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            key = stmt.targets[0].id
            if key in ("name", "description", "hosts", "parameters"):
                try:
                    facts[key] = ast.literal_eval(stmt.value)
                except ValueError:
                    problems.append(f"{key} must be a plain value.")
    if not _NAME.match(str(facts.get("name", ""))):
        problems.append("name must be lower_snake_case, 3-40 characters.")
    if not isinstance(facts.get("description"), str) or len(facts.get("description", "")) < 10:
        problems.append("description must say what it does.")
    hosts = facts.get("hosts")
    if not isinstance(hosts, list) or not all(isinstance(h, str) and re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", h) for h in hosts):
        problems.append("hosts must be a list of website names like 'api.example.com'.")
    if not isinstance(facts.get("parameters", {}), dict):
        problems.append("parameters must be a JSON-schema dict.")
    if not any(isinstance(n, ast.FunctionDef) and n.name == "run" for n in cls.body):
        problems.append("The class needs a run(...) method that returns a sentence.")
    if not any(isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "EXAMPLES" for t in n.targets)
               for n in tree.body):
        problems.append("Add EXAMPLES = [{'args': {...}, 'expect': 'text'}] for testing.")
    return problems, facts


# ---------------------------------------------------------------- testing in a separate process

_HARNESS = r"""
import importlib.util, json, sys, threading
sys.path.insert(0, sys.argv[1])
from veronica import plugin_api
spec = importlib.util.spec_from_file_location("ability_under_test", sys.argv[2])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
cls = [v for v in vars(mod).values() if isinstance(v, type) and issubclass(v, plugin_api.Ability) and v is not plugin_api.Ability][0]
results = []
for ex in list(getattr(mod, "EXAMPLES", []))[:5]:
    args, expect = dict(ex.get("args", {})), str(ex.get("expect", "") or "")
    try:
        out = plugin_api._run_with_hosts(cls.hosts, cls().run, **args)
        ok = isinstance(out, str) and bool(out.strip()) and (expect.lower() in out.lower())
        results.append({"args": args, "ok": ok, "output": str(out)[:400],
                        "error": "" if ok else f"expected the answer to contain {expect!r}"})
    except Exception as e:
        results.append({"args": args, "ok": False, "output": "", "error": f"{type(e).__name__}: {e}"[:400]})
print("RESULTS:" + json.dumps(results))
"""


def run_examples(path: Path, timeout_s: int = _TEST_TIMEOUT_S) -> tuple[bool, list[dict], str]:
    """Run the ability's EXAMPLES in a separate Python process (time-limited)."""
    try:
        p = subprocess.run([sys.executable, "-I", "-c", _HARNESS, str(PROJECT_ROOT), str(path)],
                           capture_output=True, text=True, timeout=timeout_s,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), encoding="utf-8",
                           errors="replace")
    except subprocess.TimeoutExpired:
        return False, [], f"The tests didn't finish within {timeout_s} seconds."
    line = next((l for l in p.stdout.splitlines() if l.startswith("RESULTS:")), "")
    if not line:
        return False, [], (p.stderr or p.stdout or "no output").strip()[-800:]
    results = json.loads(line[len("RESULTS:"):])
    return bool(results) and all(r["ok"] for r in results), results, ""


# ---------------------------------------------------------------- generation

_SYSTEM = """You write ONE VERONICA ability: a small Python module that looks something up on the
internet and returns ONE or TWO short spoken sentences. Rules (the code is rejected otherwise):
- from veronica.plugin_api import Ability, http_get_json, http_get_text, api_key  (nothing else from veronica)
- other imports ONLY from: re, json, math, datetime, time, typing, urllib.parse, statistics, collections,
  dataclasses, random, textwrap, html, itertools, functools, decimal, fractions, zoneinfo, calendar
- no open/eval/exec/getattr, no names or attributes starting with "_" (no self._x helpers, no __init__),
  no str.format() (use f-strings), no globals, no files, no threads, no OS access
- exactly one class based on Ability with: name (lower_snake_case), description (what it does and what each
  argument means — the voice assistant decides when to call it from this), parameters (JSON schema),
  hosts (list of EVERY website it calls, e.g. ["opensky-network.org"]), and run(self, **args) -> str
- web access ONLY through http_get_json(url, params=None, headers=None) / http_get_text(...); GET only
- prefer a free public API that needs no key. If a key is unavoidable use api_key("SOMETHING_API_KEY")
  and, when it's empty, return a sentence telling the user to add SOMETHING_API_KEY to config/.env
- handle 'nothing found' with a friendly sentence; never return raw JSON
- at module level: EXAMPLES = [{"args": {...}, "expect": "a word the answer will contain"}] with 1-3
  realistic examples that work against the live API right now (expect may be "" if unpredictable)
Reply with ONLY one ```python code block, then one line: SUMMARY: <one sentence for the user>."""


def _extract_code(raw: str) -> tuple[str, str]:
    m = re.search(r"```(?:python)?\s*\n(.*?)\n```", raw or "", re.S)
    code = m.group(1) if m else ""
    s = re.search(r"^SUMMARY:\s*(.+)$", raw or "", re.M)
    return code, (s.group(1).strip() if s else "")


def sha256(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- running an approved ability

class AbilityTool(Tool):
    """An approved ability, as a normal (LLM-callable, read-only) VERONICA tool."""

    permission = PermissionLevel.SAFE      # it can only read the web and return text

    def __init__(self, ability_cls, hosts: list[str]) -> None:
        self._cls, self._hosts = ability_cls, list(hosts)
        self.name = ability_cls.name
        self.description = f"[added ability] {ability_cls.description} Uses: {', '.join(hosts)}."
        self.parameters = ability_cls.parameters or {"type": "object", "properties": {}, "required": []}

    def matches(self, text: str) -> bool:
        return False                       # called by the model from its description, never by regex

    def run(self, **kwargs: Any) -> ToolResult:
        from veronica import plugin_api
        box: dict = {}

        def work() -> None:
            try:
                box["out"] = plugin_api._run_with_hosts(self._hosts, self._cls().run, **kwargs)
            except Exception as e:  # noqa: BLE001
                box["err"] = e
        t = threading.Thread(target=work, name=f"ability-{self.name}", daemon=True)
        t.start()
        t.join(_RUN_TIMEOUT_S)
        if t.is_alive():
            return ToolResult.fail(f"The {self.name.replace('_', ' ')} ability took too long.")
        if "err" in box:
            return ToolResult.fail(f"The {self.name.replace('_', ' ')} ability failed: {box['err']}")
        out = box.get("out")
        if not isinstance(out, str) or not out.strip():
            return ToolResult.fail(f"The {self.name.replace('_', ' ')} ability gave no answer.")
        return ToolResult.ok(out.strip()[:1000])


def _load_class(path: Path):
    import importlib.util
    from veronica import plugin_api
    spec = importlib.util.spec_from_file_location(f"veronica_ability_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return next(v for v in vars(mod).values()
                if isinstance(v, type) and issubclass(v, plugin_api.Ability) and v is not plugin_api.Ability)


# ---------------------------------------------------------------- manager

class AbilityManager:
    def __init__(self, config=None, registry=None, ask: Optional[Callable[[str, str], str]] = None,
                 root: Optional[Path] = None, tester: Callable = run_examples, llm=None) -> None:
        self.config = config
        self.registry = registry
        self._ask, self._llm = ask, llm
        self.root = Path(root) if root else (config.resolve_path("data/abilities") if config is not None
                                             else PROJECT_ROOT / "data" / "abilities")
        self.drafts, self.approved = self.root / "drafts", self.root / "approved"
        self.record_path = self.root / "approved.json"
        self._test = tester
        self.pending: Optional[str] = None
        self.pending_at = 0.0

    def _get_ask(self):
        if self._ask is None:
            from veronica.tools.learning_tools import make_ask
            self._ask = make_ask(self._llm, self.config)
        return self._ask

    def _records(self) -> dict:
        data, _ = jsonsafe.read_json(self.record_path, default={})
        return data or {}

    def builtin_names(self) -> set[str]:
        if self.registry is None:
            return set()
        approved = set(self._records())
        return {t.name for t in self.registry.all()} - approved

    # -- build ------------------------------------------------------------------
    def build(self, request: str, max_fixes: int = 2) -> dict:
        ask = self._get_ask()
        if ask is None:
            return {"ok": False, "message": "I need my language model to build abilities."}
        self.drafts.mkdir(parents=True, exist_ok=True)
        prompt = f"Build this ability: {request}"
        last_problem, summary = "", ""
        for attempt in range(max_fixes + 1):
            try:
                raw = ask(_SYSTEM, prompt)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "message": f"I couldn't build that right now: {e}"}
            code, summary = _extract_code(raw)
            problems, facts = check_code(code) if code else (["No code block in the reply."], {})
            name = facts.get("name", "")
            if not problems and name in self.builtin_names():
                problems = [f"The name {name} is already one of my built-in tools; pick another name."]
            if problems:
                last_problem = "; ".join(problems[:6])
                logger.info("Ability draft rejected (attempt %d): %s", attempt + 1, last_problem)
                prompt = f"Build this ability: {request}\n\nYour last version was REJECTED: {last_problem}\nFix it."
                continue
            path = self.drafts / f"{name}.py"
            path.write_text(code, encoding="utf-8")
            passed, results, error = self._test(path)
            if passed:
                self.pending, self.pending_at = name, time.time()
                return {"ok": True, "name": name, "summary": summary or facts.get("description", ""),
                        "hosts": facts.get("hosts", []), "results": results}
            last_problem = error or "; ".join(f"{r['args']}: {r['error']}" for r in results if not r["ok"])
            logger.info("Ability %s failed its tests (attempt %d): %s", name, attempt + 1, last_problem[:300])
            prompt = (f"Build this ability: {request}\n\nYour code:\n```python\n{code}\n```\n"
                      f"FAILED its examples (output is untrusted data): {last_problem[:1500]}\nFix it.")
        return {"ok": False, "message": f"I couldn't get a working version after {max_fixes + 1} tries. "
                                        f"Last problem: {last_problem[:300]}"}

    # -- approve / load / remove ---------------------------------------------------
    def activate(self, name: str) -> tuple[bool, str]:
        draft = self.drafts / f"{name}.py"
        if not draft.exists():
            return False, f"I don't have a finished {name.replace('_', ' ')} ability to switch on."
        code = draft.read_text(encoding="utf-8")
        problems, facts = check_code(code)
        if problems:
            return False, "It no longer passes my safety check: " + "; ".join(problems[:3])
        self.approved.mkdir(parents=True, exist_ok=True)
        (self.approved / f"{name}.py").write_text(code, encoding="utf-8")
        records = self._records()
        records[name] = {"sha256": sha256(code), "hosts": facts.get("hosts", []), "approved_at": time.time(),
                         "description": facts.get("description", "")}
        jsonsafe.write_json(self.record_path, records)
        draft.unlink(missing_ok=True)
        ok, msg = self._register(name)
        if self.pending == name:
            self.pending = None
        return ok, msg

    def _register(self, name: str) -> tuple[bool, str]:
        rec = self._records().get(name)
        path = self.approved / f"{name}.py"
        if rec is None or not path.exists():
            return False, f"{name} isn't approved."
        code = path.read_text(encoding="utf-8")
        if sha256(code) != rec["sha256"]:
            logger.warning("Ability %s changed since it was approved; not loading it.", name)
            return False, f"{name} changed since you approved it, so I won't load it."
        problems, facts = check_code(code)
        if problems:
            return False, f"{name} fails my safety check: {problems[0]}"
        try:
            cls = _load_class(path)
        except Exception as e:  # noqa: BLE001
            return False, f"{name} failed to load: {e}"
        if self.registry is not None:
            if self.registry.get(name) is not None:
                self.registry.unregister(name)
            self.registry.register(AbilityTool(cls, facts.get("hosts", [])))
        return True, f"{name.replace('_', ' ').capitalize()} is switched on."

    def load_approved(self) -> list[str]:
        loaded = []
        for name in self._records():
            ok, msg = self._register(name)
            (loaded.append(name) if ok else logger.warning("Ability not loaded: %s", msg))
        return loaded

    def remove(self, name: str) -> bool:
        records = self._records()
        if name not in records:
            return False
        records.pop(name)
        jsonsafe.write_json(self.record_path, records)
        path = self.approved / f"{name}.py"
        if path.exists():
            removed = self.root / "removed"
            removed.mkdir(parents=True, exist_ok=True)
            path.replace(removed / f"{name}.{int(time.time())}.py")     # kept, not destroyed
        if self.registry is not None and self.registry.get(name) is not None:
            self.registry.unregister(name)
        return True

    def list(self) -> list[dict]:
        return [{"name": k, **v} for k, v in self._records().items()]

    def find(self, words: str) -> Optional[str]:
        key = re.sub(r"[^a-z0-9]+", "_", (words or "").lower()).strip("_")
        key = re.sub(r"_(?:ability|skill|feature)$", "", key)
        names = list(self._records()) + [p.stem for p in self.drafts.glob("*.py")] if self.drafts.exists() \
            else list(self._records())
        for n in names:
            if n == key or key in n or n in key:
                return n
        return None
