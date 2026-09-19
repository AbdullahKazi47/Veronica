"""
VisionImagePrivacy: image preprocessing before vision provider analysis.

PRIVACY LIMITATION (documented per revised architecture):
- No true pixel-level redaction is implemented in V1.
- We rely on active-window crop, downscaling, optional grayscale,
  and avoiding full-screen transmission (unless explicitly configured).
- Raw screenshot bytes are never persisted or logged.
"""
from __future__ import annotations

from typing import Optional

from PIL import Image
import io
from veronica.vision.screen_state import ScreenState
from veronica.config import Config, VisionConfig
from veronica.logging_setup import get_logger

logger = get_logger("vision.image_privacy")


class VisionImagePrivacy:
    """
    Image privacy boundary: preprocesses screenshots before sending to
    VisionProvider.

    Design (revised architecture):
    - Active-window crop by default (when ScreenState.active_window available)
    - Configurable downscaling
    - Optional grayscale
    - Full-screen fallback OFF by default (explicit opt-in only)
    - No pixel-level redaction in V1 (documented limitation)
    - No image bytes logged or persisted
    """

    def __init__(self, config: Optional[Any] = None) -> None:
        self.config = config
        if isinstance(config, VisionConfig):
            cfg = config
        else:
            cfg = getattr(config, "vision", None) if config else None
        self.crop_active = getattr(cfg, "crop_to_active_window", True) if cfg else True
        self.downscale_w = getattr(cfg, "downscale_width", 640) if cfg else 640
        self.downscale_h = getattr(cfg, "downscale_height", 480) if cfg else 480
        self.grayscale = getattr(cfg, "to_grayscale", False) if cfg else False
        self.full_fallback = getattr(cfg, "full_screen_fallback", False) if cfg else False

    def process(
        self,
        screenshot_bytes: bytes,
        screen_state: ScreenState,
    ) -> bytes:
        """Process image bytes for privacy. Returns new bytes (never logs input)."""
        try:
            img = Image.open(io.BytesIO(screenshot_bytes))
        except Exception as exc:
            logger.debug("Image open failed: %s", exc)
            # Return original if we can't open; provider may still handle it
            return screenshot_bytes

        img = self._crop_active_if_available(img, screen_state)
        img = self._downscale(img)
        if self.grayscale:
            img = img.convert("L")
        output = io.BytesIO()
        img.save(output, format="PNG")
        processed = output.getvalue()
        # Do NOT log processed bytes; do NOT persist
        return processed

    def _crop_active_if_available(
        self, img: Image, screen_state: ScreenState
    ) -> Image:
        if not self.crop_active or not screen_state.active_window:
            return img
        bbox_data = screen_state.active_window.bounding_box
        if bbox_data is None:
            return img
        try:
            left = int(bbox_data.get("left", 0))
            top = int(bbox_data.get("top", 0))
            w = int(bbox_data.get("width", img.width))
            h = int(bbox_data.get("height", img.height))
            # Ensure crop fits within image
            left = max(0, min(left, img.width - 1))
            top = max(0, min(top, img.height - 1))
            right = min(left + w, img.width)
            bottom = min(top + h, img.height)
            if right > left and bottom > top:
                return img.crop((left, top, right, bottom))
        except Exception as exc:
            logger.debug("Crop failed: %s", exc)
        return img

    def _downscale(self, img: Image) -> Image:
        if self.downscale_w <= 0 or self.downscale_h <= 0:
            return img
        return img.resize((self.downscale_w, self.downscale_h), Image.Resampling.LANCZOS)
