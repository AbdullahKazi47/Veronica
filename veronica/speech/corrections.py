"""
Personal speech corrections: "when I say attacks, I mean tabs".

Speech recognition mishears some words the same way every time for a given voice and
accent. A correction is a phrase -> replacement applied to every transcript (whole words,
any case) before VERONICA acts on it. Taught by voice, stored in data/stt_corrections.json.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Optional

from veronica.core import jsonsafe

_lock = threading.Lock()
_MAX_PHRASE = 60


class Corrections:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._mtime: Optional[float] = None
        self._map: dict[str, str] = {}

    def _refresh(self) -> None:
        try:
            mtime = self.path.stat().st_mtime if self.path.exists() else None
        except OSError:
            return
        if mtime != self._mtime:
            data, _ = jsonsafe.read_json(self.path, default={})
            self._map = {str(k).lower(): str(v) for k, v in (data or {}).items() if k and v}
            self._mtime = mtime

    def all(self) -> dict[str, str]:
        self._refresh()
        return dict(self._map)

    def add(self, heard: str, meant: str) -> bool:
        heard, meant = _clean(heard), _clean(meant)
        if not heard or not meant or heard == meant.lower() or len(heard) > _MAX_PHRASE:
            return False
        with _lock:
            self._refresh()
            self._map[heard] = meant
            jsonsafe.write_json(self.path, self._map)
            self._mtime = self.path.stat().st_mtime
        return True

    def remove(self, heard: str) -> bool:
        heard = _clean(heard)
        with _lock:
            self._refresh()
            if heard not in self._map:
                return False
            self._map.pop(heard)
            jsonsafe.write_json(self.path, self._map)
            self._mtime = self.path.stat().st_mtime
        return True

    def apply(self, text: str) -> str:
        mapping = self.all()
        if not text or not mapping:
            return text
        # Longest phrases first, so "minimise all attacks" beats "attacks".
        for heard in sorted(mapping, key=len, reverse=True):
            text = re.sub(rf"(?<!\w){re.escape(heard)}(?!\w)", mapping[heard], text, flags=re.I)
        return text


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip(" .,!?\"'").lower())
