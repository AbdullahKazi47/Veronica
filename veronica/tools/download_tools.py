"""
Download a file from a direct link the user gives (an installer, a PDF, a dataset, a
document, any file they're entitled to). Asks first, saves to the Downloads folder, and
never runs what it fetches.

What it deliberately does NOT do: it makes a plain web request with no stored logins or
cookies, so it cannot sign in, get past a paywall, defeat DRM, or solve a CAPTCHA — it
can only fetch something already reachable at the link. That also means it is not a way
to pull copyrighted video from streaming or torrent sites.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

logger = get_logger("tools.download")

# Saved, never executed — matches OpenPathTool, which refuses to *run* these.
_EXT_BY_TYPE = {
    "application/pdf": ".pdf", "application/zip": ".zip", "application/json": ".json",
    "text/plain": ".txt", "text/csv": ".csv", "image/jpeg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/webp": ".webp", "video/mp4": ".mp4", "audio/mpeg": ".mp3",
    "application/msword": ".doc", "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls", "application/x-msdownload": ".exe",
}
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DOWNLOAD = re.compile(
    r"\b(?:download|grab|fetch|save|get)\b.*?(https?://\S+|www\.\S+\.\S+)", re.I)


def _downloads_dir(config) -> Path:
    custom = getattr(getattr(config, "download", None), "dir", "") or ""
    if custom:
        return Path(os.path.expandvars(os.path.expanduser(custom)))
    return Path.home() / "Downloads"


def _clean_name(name: str) -> str:
    name = _BAD_NAME.sub("_", unquote(name or "")).strip(" .")
    return name[:150] or "download"


def _human(n: Optional[int]) -> str:
    if not n:
        return "unknown size"
    size = float(n)
    for unit in ("bytes", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GB"


class DownloadFileTool(Tool):
    name = "download_file"
    description = ("Download a file from a direct http(s) link the user provides, into the Downloads folder. "
                   "Asks the user first. `url` is the link; optional `save_as` names the file. Cannot sign in, "
                   "bypass a paywall/DRM, or solve a CAPTCHA, and never runs what it downloads. Not for "
                   "copyrighted video from streaming/torrent sites.")
    permission = PermissionLevel.SENSITIVE   # downloading always asks (yes/no)
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Direct http(s) link to the file."},
            "save_as": {"type": "string", "description": "Optional filename to save it as."},
        },
        "required": ["url"],
    }

    def __init__(self, config=None, opener=None) -> None:
        self.config = config
        self._open = opener                    # optional: reveal the file afterwards (tests inject)

    def matches(self, text: str) -> bool:
        return bool(_DOWNLOAD.search(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _DOWNLOAD.search(text)
        if not m:
            return {}
        url = m.group(1).rstrip(".,)!")
        return {"url": url if url.startswith("http") else f"https://{url}"}

    def _max_bytes(self) -> int:
        mb = float(getattr(getattr(self.config, "download", None), "max_mb", 2048) or 0)
        return int(mb * 1024 * 1024) if mb > 0 else 0

    def run(self, url: str = "", save_as: str = "", **kwargs: Any) -> ToolResult:
        url = (url or "").strip().strip('"')
        if not url:
            return ToolResult.fail("What's the link? Give me a direct web address to the file.")
        scheme = urlparse(url).scheme.lower()
        if scheme and scheme not in ("http", "https"):
            # magnet:, ftp:, file: ... — not a plain web download (and not a torrent fetcher).
            return ToolResult.fail("I can only download from an http or https web link.")
        if not scheme:
            url = f"https://{url}"
        parsed = urlparse(url)
        if not parsed.netloc:
            return ToolResult.fail("That doesn't look like a web address.")
        try:
            import requests
        except ImportError:
            return ToolResult.fail("The `requests` library isn't installed.")

        cap = self._max_bytes()
        try:
            # No cookies, no auth: this can only fetch what's openly reachable at the link.
            with requests.get(url, stream=True, timeout=30, allow_redirects=True,
                              headers={"User-Agent": "Mozilla/5.0 VERONICA"}) as resp:
                resp.raise_for_status()
                declared = int(resp.headers.get("Content-Length") or 0)
                if cap and declared and declared > cap:
                    return ToolResult.fail(f"That file is {_human(declared)}, over the {cap // (1024*1024)} "
                                           "megabyte limit I'm set to download. Raise download.max_mb if you want it.")
                name = _clean_name(save_as or self._name_from(resp, parsed))
                dest = _downloads_dir(self.config)
                dest.mkdir(parents=True, exist_ok=True)
                target = self._unique(dest / name)
                part = target.with_suffix(target.suffix + ".part")
                written = 0
                with open(part, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if cap and written > cap:
                            f.close()
                            part.unlink(missing_ok=True)
                            return ToolResult.fail(f"Stopped — the download went over the {cap//(1024*1024)} "
                                                   "megabyte limit. Nothing was kept.")
                        f.write(chunk)
                part.replace(target)
        except requests.RequestException as e:
            return ToolResult.fail(f"The download failed: {e}")
        except OSError as e:
            return ToolResult.fail(f"I couldn't save the file: {e}")
        if self._open:
            try:
                self._open(str(target))
            except Exception:  # noqa: BLE001
                pass
        return ToolResult.ok(f"Downloaded {target.name} ({_human(written)}) to your Downloads folder.",
                             path=str(target), bytes=written)

    @staticmethod
    def _name_from(resp, parsed) -> str:
        disp = resp.headers.get("Content-Disposition", "")
        m = re.search(r'filename\*?=(?:[^\']*\'\')?["\']?([^"\';\r\n]+)', disp, re.I)
        if m:
            return m.group(1)
        base = os.path.basename(parsed.path)
        if base and "." in base:
            return base
        ext = _EXT_BY_TYPE.get((resp.headers.get("Content-Type", "").split(";")[0].strip().lower()), "")
        return (base or parsed.netloc.split(":")[0] or "download") + ext

    @staticmethod
    def _unique(path: Path) -> Path:
        if not path.exists():
            return path
        stem, suffix, n = path.stem, path.suffix, 2
        while True:
            cand = path.with_name(f"{stem} ({n}){suffix}")
            if not cand.exists():
                return cand
            n += 1
