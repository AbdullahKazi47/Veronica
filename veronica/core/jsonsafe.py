"""
Crash- and corruption-resistant JSON storage for VERONICA's memory files.

Every write goes through a temp file and an atomic replace, and the previous good copy
is kept as ``<name>.bak``. On read, if the main file is unreadable (a power cut mid-write,
a half-synced cloud file, a manual edit that broke the JSON), the backup is used instead,
and the broken file is set aside as ``<name>.corrupt`` rather than deleted — self-heal,
not data loss. This is what lets "fix yourself" restore a damaged memory file on its own.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Optional

from veronica.logging_setup import get_logger

logger = get_logger("core.jsonsafe")


def _try_load(path: Path) -> tuple[bool, Any]:
    try:
        return True, json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, None


def read_json(path: Path, default: Any = None) -> tuple[Any, Optional[str]]:
    """Return (data, repair_note). repair_note is None normally, or a short human line
    when the main file was broken and the backup (or the default) had to be used."""
    path = Path(path)
    if not path.exists():
        return (default, None)
    ok, data = _try_load(path)
    if ok:
        return (data, None)
    bak = path.with_suffix(path.suffix + ".bak")
    ok_bak, data_bak = _try_load(bak) if bak.exists() else (False, None)
    # Keep the broken file for inspection instead of silently overwriting it.
    try:
        corrupt = path.with_suffix(path.suffix + ".corrupt")
        shutil.copy2(path, corrupt)
    except OSError:
        corrupt = None
    if ok_bak:
        try:
            shutil.copy2(bak, path)       # restore so the next save builds on good data
        except OSError:
            pass
        logger.warning("Restored %s from backup after corruption.", path.name)
        return (data_bak, f"{path.name} was damaged; I restored it from the backup.")
    logger.error("Both %s and its backup are unreadable; starting fresh.", path.name)
    return (default, f"{path.name} was damaged and had no usable backup; I started it fresh "
                     f"(the old file is kept as {corrupt.name if corrupt else path.name + '.corrupt'}).")


def write_json(path: Path, data: Any, *, indent: int = 2) -> None:
    """Atomic write. The current good file (if any) is copied to ``.bak`` first, so a
    failure partway through can never leave zero good copies."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        ok, _ = _try_load(path)
        if ok:                             # only ever back up a file that already parses
            try:
                shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
            except OSError:
                pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=indent, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def check_file(path: Path, *, repair: bool = False) -> tuple[bool, Optional[str]]:
    """Is this JSON file healthy? With repair=True, restore it from backup if not.
    Returns (healthy_or_repaired, note)."""
    path = Path(path)
    if not path.exists():
        return (True, None)                # a store that hasn't been written yet is fine
    ok, _ = _try_load(path)
    if ok:
        return (True, None)
    if not repair:
        return (False, f"{path.name} is corrupt.")
    _, note = read_json(path)              # read_json does the restore/quarantine
    bak = path.with_suffix(path.suffix + ".bak")
    healthy, _ = _try_load(path) if path.exists() else (bak.exists() and _try_load(bak)[0], None)
    return (bool(healthy), note)
