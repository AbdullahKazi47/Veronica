from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

_FIND_PATTERN = re.compile(r"\bfind (?:my |the )?(?:file )?(.+)$", re.I)
_LIST_PATTERN = re.compile(r"\blist files? in (.+)$", re.I)

_SEARCH_ROOTS = [Path.home() / "Documents", Path.home() / "Downloads", Path.home() / "Desktop"]


class FindFileTool(Tool):
    name = "find_file"
    description = "Search for a file by (partial) name under common user folders."
    permission = PermissionLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Partial filename to search for."}},
        "required": ["query"],
    }

    def matches(self, text: str) -> bool:
        return bool(_FIND_PATTERN.search(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _FIND_PATTERN.search(text)
        return {"query": m.group(1).strip()} if m else {}

    def run(self, query: str = "", max_results: int = 10, **kwargs: Any) -> ToolResult:
        if not query:
            return ToolResult.fail("No search term given.")
        query_lower = query.lower()
        matches: list[str] = []
        for root in _SEARCH_ROOTS:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if path.is_file() and query_lower in path.name.lower():
                    matches.append(str(path))
                    if len(matches) >= max_results:
                        break
            if len(matches) >= max_results:
                break

        if not matches:
            return ToolResult.fail(f"No files matching '{query}' found in Documents/Downloads/Desktop.")
        return ToolResult.ok(f"Found {len(matches)} file(s) matching '{query}'.", files=matches)


class ListDirectoryTool(Tool):
    name = "list_directory"
    description = "List files in a given folder (or a friendly name like 'downloads')."
    permission = PermissionLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {"folder": {"type": "string", "description": "Folder path or friendly name."}},
        "required": ["folder"],
    }

    _FRIENDLY = {
        "documents": Path.home() / "Documents",
        "downloads": Path.home() / "Downloads",
        "desktop": Path.home() / "Desktop",
    }

    def matches(self, text: str) -> bool:
        return bool(_LIST_PATTERN.search(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _LIST_PATTERN.search(text)
        return {"folder": m.group(1).strip()} if m else {}

    def run(self, folder: str = "", **kwargs: Any) -> ToolResult:
        target = self._FRIENDLY.get(folder.lower(), Path(folder))
        if not target.exists() or not target.is_dir():
            return ToolResult.fail(f"'{folder}' isn't a folder I can see.")
        entries = sorted(p.name for p in target.iterdir())[:50]
        return ToolResult.ok(f"{len(entries)} item(s) in {target}.", entries=entries, path=str(target))
