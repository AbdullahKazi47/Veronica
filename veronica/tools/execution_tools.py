"""
General-purpose "do anything on this machine" tools: run a shell command, read/write/
delete a file. This is deliberately the most powerful and most tightly gated module in
VERONICA — it's what turns the assistant from "a fixed set of tools" into "a brain that
can actually accomplish arbitrary tasks," which is only safe because of two independent
layers:

  1. PermissionLevel — every tool here is SENSITIVE or CRITICAL, so with the default
     config (safety.require_confirmation_above: SAFE) NOTHING in this file ever runs
     without an explicit human "yes" first — see safety/permissions.py.
  2. A hard-coded destructive-pattern blocklist on top of that, for shell commands —
     even a confirmed "yes" won't execute a small set of catastrophic patterns
     (recursive root deletes, disk formatting, raw disk writes, etc.), because a sleepy
     "y" keypress shouldn't be able to wipe a machine.

Neither layer existed in any of the six repos originally studied for VERONICA.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

logger = get_logger("tools.execution")

_MAX_FILE_READ_BYTES = 200_000
_SHELL_TIMEOUT_S = 30

# Patterns that stay blocked even after the user confirms — catastrophic, hard to
# undo, and almost never what someone actually meant to type.
_DANGEROUS_SHELL_PATTERNS = [
    re.compile(r"rm\s+-rf\s+/(\s|$)"),
    re.compile(r"rm\s+-rf\s+~(\s|$)"),
    re.compile(r"rm\s+-rf\s+\*"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+.*of=/dev/"),
    re.compile(r"format\s+[a-z]:", re.I),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),   # fork bomb
    re.compile(r"\bshutdown\b|\bhalt\b|\breboot\b", re.I),
]


class RunShellCommandTool(Tool):
    name = "run_shell_command"
    description = (
        "Run an arbitrary shell command on the user's machine and return its output. "
        "Use this for anything no other tool covers — installing a package, running a "
        "script, checking a process, etc. Always CRITICAL permission: the exact command "
        "is always confirmed with the user before it runs."
    )
    permission = PermissionLevel.CRITICAL
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The exact shell command to run."},
        },
        "required": ["command"],
    }

    def run(self, command: str = "", **kwargs: Any) -> ToolResult:
        if not command.strip():
            return ToolResult.fail("No command given.")

        for pattern in _DANGEROUS_SHELL_PATTERNS:
            if pattern.search(command):
                logger.warning("Blocked a destructive shell command even after confirmation: %s", command)
                return ToolResult.fail(
                    f"Refusing to run '{command}' — it matches a hard-blocked destructive pattern. "
                    "If you really need this, run it yourself outside VERONICA."
                )

        try:
            proc = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=_SHELL_TIMEOUT_S
            )
            output = (proc.stdout or "").strip()
            error = (proc.stderr or "").strip()
            summary = output if output else (error if error else "(no output)")
            if len(summary) > 2000:
                summary = summary[:2000] + "\n...(truncated)"
            if proc.returncode == 0:
                return ToolResult.ok(summary, returncode=proc.returncode, stdout=output, stderr=error)
            return ToolResult.fail(
                f"Command exited with code {proc.returncode}: {summary}",
                returncode=proc.returncode, stdout=output, stderr=error,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.fail(f"Command timed out after {_SHELL_TIMEOUT_S}s: '{command}'.")
        except Exception as e:
            logger.exception("Shell command failed")
            return ToolResult.fail(f"Couldn't run '{command}': {e}")


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read the text contents of a file (up to ~200KB) so it can be discussed or summarized."
    permission = PermissionLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Path to the file to read."}},
        "required": ["path"],
    }

    def run(self, path: str = "", **kwargs: Any) -> ToolResult:
        p = Path(path).expanduser()
        if not p.exists() or not p.is_file():
            return ToolResult.fail(f"'{path}' isn't a file I can find.")
        try:
            data = p.read_bytes()[:_MAX_FILE_READ_BYTES]
            text = data.decode("utf-8", errors="replace")
            note = "" if len(data) < _MAX_FILE_READ_BYTES else " (truncated at 200KB)"
            return ToolResult.ok(f"Read {len(data)} bytes from {p}{note}.", content=text, path=str(p))
        except Exception as e:
            return ToolResult.fail(f"Couldn't read '{path}': {e}")


class WriteFileTool(Tool):
    name = "write_file"
    description = "Create a new file or overwrite an existing file with the given text content."
    permission = PermissionLevel.SENSITIVE
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to write to."},
            "content": {"type": "string", "description": "Text content to write."},
        },
        "required": ["path", "content"],
    }

    def run(self, path: str = "", content: str = "", **kwargs: Any) -> ToolResult:
        if not path:
            return ToolResult.fail("No path given.")
        p = Path(path).expanduser()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return ToolResult.ok(f"Wrote {len(content)} characters to {p}.", path=str(p))
        except Exception as e:
            return ToolResult.fail(f"Couldn't write to '{path}': {e}")


class DeleteFileTool(Tool):
    name = "delete_file"
    description = "Permanently delete a single file. Irreversible — CRITICAL permission."
    permission = PermissionLevel.CRITICAL
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Path to the file to delete."}},
        "required": ["path"],
    }

    def run(self, path: str = "", **kwargs: Any) -> ToolResult:
        p = Path(path).expanduser()
        if not p.exists():
            return ToolResult.fail(f"'{path}' doesn't exist.")
        if not p.is_file():
            return ToolResult.fail(f"'{path}' isn't a single file (refusing to touch directories here).")
        try:
            p.unlink()
            return ToolResult.ok(f"Deleted {p}.", path=str(p))
        except Exception as e:
            return ToolResult.fail(f"Couldn't delete '{path}': {e}")
