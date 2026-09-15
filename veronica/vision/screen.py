"""
Vision entry point. Deliberately thin today (screenshot + OCR) — none of the six
studied repos have real vision beyond one (OCR.py in GauravSingh9356/J.A.R.V.I.S using
tesseract), so this is built from scratch as a clean extension point for future
capabilities: screen description via a vision-capable LLM, object detection, etc.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from veronica.logging_setup import get_logger

logger = get_logger("vision.screen")


@dataclass
class OCRResult:
    success: bool
    text: str = ""
    error: str = ""


def read_text_from_screen() -> OCRResult:
    """Take a screenshot and OCR it. Requires pyautogui, Pillow, pytesseract, and a
    Tesseract binary installed on the system (see docs/GETTING_STARTED.md)."""
    try:
        import pyautogui
        import pytesseract
    except ImportError:
        return OCRResult(success=False, error="Install `pyautogui pytesseract Pillow` and the Tesseract binary for OCR.")

    try:
        img = pyautogui.screenshot()
        text = pytesseract.image_to_string(img)
        return OCRResult(success=True, text=text.strip())
    except Exception as e:
        logger.exception("Screen OCR failed")
        return OCRResult(success=False, error=str(e))


def read_text_from_image(path: str | Path) -> OCRResult:
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return OCRResult(success=False, error="Install `pytesseract Pillow` and the Tesseract binary for OCR.")

    try:
        text = pytesseract.image_to_string(Image.open(path))
        return OCRResult(success=True, text=text.strip())
    except Exception as e:
        logger.exception("Image OCR failed")
        return OCRResult(success=False, error=str(e))
