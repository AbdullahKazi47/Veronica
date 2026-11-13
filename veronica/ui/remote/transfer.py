"""
PC <-> phone, through the phone page (idea from Fenn's "Phone link" tile, fenn.click).

  * The island's Phone tile shows whether your phone page is open ("connected") and sends your
    clipboard (text or a picture) or a file to it. They appear in the phone page's chat as
    "From your PC" with Copy / Download.
  * The phone page's paperclip sends files the other way; they land in Downloads\\From phone.

Same safety as the rest of the phone page: it only exists on 127.0.0.1, Tailscale publishes it to
your own devices, and every request needs the page's token. A file you share is only offered for
an hour (then forgotten), only the exact files you shared can be downloaded, and nothing on the PC
is ever browsable. Uploads are size-capped and saved under a cleaned-up name, never overwriting.
"""
from __future__ import annotations

import mimetypes
import re
import threading
import time
import uuid
from pathlib import Path
from typing import BinaryIO, Optional

SHARE_FOR_S = 3600
MAX_UPLOAD = 200 * 1024 * 1024
MAX_SHARE = 2 * 1024 * 1024 * 1024
MAX_TEXT = 20_000
CONNECTED_WITHIN_S = 20.0


def safe_name(name: str) -> str:
    """A file name that can't climb out of the folder or upset Windows."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .")
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com\d|lpt\d)(\..*)?", name):
        name = "_" + name
    return name[:150] or "file"


def unique(folder: Path, name: str) -> Path:
    target = folder / name
    stem, suffix, n = target.stem, target.suffix, 2
    while target.exists():
        target = folder / f"{stem} ({n}){suffix}"
        n += 1
    return target


class PhoneLink:
    def __init__(self, outbox: Optional[Path] = None, inbox: Optional[Path] = None,
                 clock=time.time) -> None:
        self.clock = clock
        self.outbox = outbox or (Path.home() / "AppData" / "Local" / "Temp" / "veronica-phone")
        self.inbox = inbox or (Path.home() / "Downloads" / "From phone")
        self._items: list[dict] = []
        self._lock = threading.Lock()
        self._seen = 0.0
        self.last = ""                                   # what the island's Phone tile shows

    # -- is the phone there -----------------------------------------------------------------
    def seen(self) -> None:
        self._seen = self.clock()

    def connected(self) -> bool:
        return self.clock() - self._seen < CONNECTED_WITHIN_S

    # -- PC -> phone ------------------------------------------------------------------------
    def _add(self, item: dict) -> dict:
        item.update(id=uuid.uuid4().hex[:12], created=self.clock())
        with self._lock:
            self._prune()
            self._items = (self._items + [item])[-20:]
        return item

    def _prune(self) -> None:
        now = self.clock()
        self._items = [i for i in self._items if now - i["created"] < SHARE_FOR_S]

    def send_text(self, text: str) -> dict:
        text = (text or "").strip()[:MAX_TEXT]
        if not text:
            raise ValueError("Nothing to send.")
        self.last = "Sent your clipboard to your phone"
        return self._add({"kind": "text", "text": text, "name": "", "size": len(text)})

    def send_image(self, png: bytes) -> dict:
        if not png:
            raise ValueError("No picture to send.")
        self.outbox.mkdir(parents=True, exist_ok=True)
        path = self.outbox / f"clipboard-{uuid.uuid4().hex[:8]}.png"
        path.write_bytes(png)
        self.last = "Sent a picture to your phone"
        return self._add({"kind": "image", "name": "Picture.png", "size": len(png), "path": str(path)})

    def send_file(self, path: str) -> dict:
        p = Path(path)
        if not p.is_file():
            raise ValueError("That isn't a file.")
        size = p.stat().st_size
        if size > MAX_SHARE:
            raise ValueError("That file is too big to send (over 2 GB).")
        self.last = f"Sent {p.name} to your phone"
        kind = "image" if (mimetypes.guess_type(p.name)[0] or "").startswith("image/") else "file"
        return self._add({"kind": kind, "name": p.name, "size": size, "path": str(p)})

    def items_for_phone(self) -> list[dict]:
        """What the phone page shows (never a path on the PC)."""
        with self._lock:
            self._prune()
            return [{k: v for k, v in i.items() if k != "path"} for i in self._items]

    def file_for(self, item_id: str) -> Optional[tuple[Path, str, str]]:
        """(path, name, mime) for a shared item still on offer, else None."""
        with self._lock:
            self._prune()
            item = next((i for i in self._items if i["id"] == item_id and i.get("path")), None)
        if item is None or not Path(item["path"]).is_file():
            return None
        name = item["name"] or Path(item["path"]).name
        return Path(item["path"]), name, mimetypes.guess_type(name)[0] or "application/octet-stream"

    # -- phone -> PC ------------------------------------------------------------------------
    def receive(self, name: str, stream: BinaryIO, length: int) -> Path:
        if length <= 0:
            raise ValueError("The file was empty.")
        if length > MAX_UPLOAD:
            raise ValueError("That file is too big (over 200 MB).")
        self.inbox.mkdir(parents=True, exist_ok=True)
        target = unique(self.inbox, safe_name(name))
        part = target.with_name(target.name + ".part")
        left = length
        with part.open("wb") as f:
            while left > 0:
                chunk = stream.read(min(1024 * 1024, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left:
            part.unlink(missing_ok=True)
            raise ValueError("The upload stopped before the end.")
        part.replace(target)
        self.last = f"Got {target.name} from your phone"
        return target


_link: Optional[PhoneLink] = None


def get_phone_link() -> PhoneLink:
    global _link
    if _link is None:
        _link = PhoneLink()
    return _link
