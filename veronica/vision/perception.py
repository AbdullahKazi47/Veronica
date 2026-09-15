"""
Perception abstraction layer for VERONICA.

This module provides a clean abstraction for computer perception - understanding
what's happening on the computer screen. It normalizes data from multiple
providers (UI Automation, screen capture, OCR, vision models) into a common
ScreenState representation.

Architecture follows a hybrid approach:
1. Windows UI Automation / accessibility information (primary)
2. application/window metadata
3. OCR/text extraction
4. deterministic screen analysis
5. visual computer-vision models (only when necessary)

Key principles:
- Do NOT make an LLM the mandatory sensor for every screen frame
- Do NOT send screenshots continuously to a remote API
- Should remain useful locally
- Normalized output - not provider-specific
- Designed for future providers
- Windows is the primary target for this phase

Usage:
    perception = PerceptionLayer()

    # Get current screen state using available providers
    state = perception.get_current_state()

    # Or get state from specific provider
    state_uia = perception.get_state_from_uia()
    state_screen = perception.get_state_from_screen_capture()
    state_ocr = perception.get_state_from_ocr()
"""
from __future__ import annotations

import time
import platform
from typing import Dict, List, Optional

from veronica.logging_setup import get_logger
from veronica.vision.screen_state import ScreenState, UIElement, TextRegion, WindowInfo

logger = get_logger("vision.perception")


class PerceptionProvider:
    """Base interface for perception providers.

    All perception providers must implement this interface to contribute
    data to the unified ScreenState.
    """

    def is_available(self) -> bool:
        """Check if this provider is available on the current system.

        Returns:
            True if provider is ready to use, False otherwise
        """
        return False

    def get_perception_sources(self) -> List[str]:
        """Get list of perception source identifiers this provider contributes.

        Returns:
            List of source names (e.g., "windows_ui_automation", "screen_capture")
        """
        return []

    def get_current_state(self, previous_state: Optional[ScreenState] = None) -> Optional[ScreenState]:
        """Get current screen state from this provider.

        Args:
            previous_state: Optional previous state for change detection optimization

        Returns:
            ScreenState if provider successful, None if unavailable/failed
        """
        return None

    def to_dict(self) -> Dict:
        """Convert provider info to dictionary for diagnostics.

        Returns:
            Dictionary with provider metadata
        """
        return {
            "available": self.is_available(),
            "sources": self.get_perception_sources(),
            "type": type(self).__name__
        }


class WindowsUIAutomationProvider(PerceptionProvider):
    """Windows UI Automation perception provider.

    Uses Windows UI Automation framework to discover windows, UI elements,
    and their properties. This provides semantic understanding of the desktop
    without relying on visual analysis.
    """

    def __init__(self):
        """Initialize the Windows UI Automation provider."""
        self._adapter = None
        self._initialize_adapter()

    def _initialize_adapter(self) -> None:
        """Initialize the UI Automation adapter."""
        try:
            from veronica.vision.uia_adapter import UIAutomationAdapter
            self._adapter = UIAutomationAdapter()
            if self._adapter.is_available():
                logger.debug("Windows UI Automation provider initialized")
            else:
                logger.debug("UI Automation adapter initialized but not available")
        except ImportError as e:
            logger.debug(f"UI Automation adapter not available: {e}")
        except Exception as e:
            logger.warning(f"Failed to initialize UI Automation adapter: {e}")

    def is_available(self) -> bool:
        """Check if Windows UI Automation is available.

        Returns:
            True if UIA adapter is ready, False otherwise
        """
        return self._adapter is not None and self._adapter.is_available()

    def get_perception_sources(self) -> List[str]:
        """Get perception sources this provider contributes.

        Returns:
            List of source identifiers
        """
        if self.is_available():
            return ["windows_ui_automation"]
        return []

    def get_current_state(self, previous_state: Optional[ScreenState] = None) -> Optional[ScreenState]:
        """Get current screen state using Windows UI Automation.

        Args:
            previous_state: Optional previous state for optimization

        Returns:
            ScreenState with UIA data if available, None otherwise
        """
        if not self.is_available():
            return None

        try:
            # Get UIA data
            windows = self._adapter.get_windows()
            active_window = self._adapter.get_active_window()
            desktop_elements = self._adapter.get_desktop_elements()
            focused_element = self._adapter.get_focused_element()

            # Create ScreenState
            state = ScreenState(
                timestamp=time.time(),
                active_window=active_window,
                visible_windows=windows,
                desktop_elements=desktop_elements,
                focused_element=focused_element,
                perception_sources=self.get_perception_sources(),
                confidence_overall=0.9  # UIA is generally reliable
            )

            logger.debug(f"Got UIA state: {len(windows)} windows, "
                        f"{len(desktop_elements)} desktop elements, "
                        f"active='{active_window.title if active_window else None}'")

            return state

        except Exception as e:
            logger.warning(f"Failed to get UIA state: {e}")
            return None


class ScreenCaptureProvider(PerceptionProvider):
    """Screen capture perception provider.

    Uses pyautogui to capture screenshots and basic screen information.
    Provides visual perception foundation for OCR and vision models.
    """

    def __init__(self):
        """Initialize the screen capture provider."""
        self._pyautogui = None
        self._initialize_pyautogui()

    def _initialize_pyautogui(self) -> None:
        """Initialize pyautogui if available."""
        try:
            import pyautogui
            self._pyautogui = pyautogui
            logger.debug("Screen capture provider initialized")
        except ImportError:
            logger.debug("pyautogui not available for screen capture")
        except Exception as e:
            logger.warning(f"Failed to initialize pyautogui: {e}")

    def is_available(self) -> bool:
        """Check if screen capture is available.

        Returns:
            True if pyautogui is ready, False otherwise
        """
        return self._pyautogui is not None

    def get_perception_sources(self) -> List[str]:
        """Get perception sources this provider contributes.

        Returns:
            List of source identifiers
        """
        if self.is_available():
            return ["screen_capture"]
        return []

    def get_current_state(self, previous_state: Optional[ScreenState] = None) -> Optional[ScreenState]:
        """Get current screen state using screen capture.

        Note: This captures basic screen info, NOT the full visual analysis.
        Visual analysis (OCR, vision models) are separate providers.

        Args:
            previous_state: Optional previous state for optimization

        Returns:
            ScreenState with basic screen info if available, None otherwise
        """
        if not self.is_available():
            return None

        try:
            # Get screen dimensions
            screen_size = self._pyautogui.size()

            # Get cursor position (if visible)
            cursor_pos = None
            try:
                cursor_pos = self._pyautogui.position()
                if cursor_pos:
                    cursor_pos = {"x": cursor_pos.x, "y": cursor_pos.y}
            except:
                pass

            # Create minimal ScreenState with just screen info
            state = ScreenState(
                timestamp=time.time(),
                screen_width=screen_size.width,
                screen_height=screen_size.height,
                monitor_count=1,  # pyautogui doesn't easily provide multi-monitor info
                cursor_position=cursor_pos,
                perception_sources=self.get_perception_sources(),
                confidence_overall=0.95  # Screen capture itself is reliable
            )

            logger.debug(f"Got screen capture state: {screen_size.width}x{screen_size.height}, "
                        f"cursor={cursor_pos}")

            return state

        except Exception as e:
            logger.warning(f"Failed to get screen capture state: {e}")
            return None

    def take_screenshot(self) -> Optional[bytes]:
        """Capture screenshot as bytes for downstream processing.

        Returns:
            Screenshot as PNG bytes, or None if failed
        """
        if not self.is_available():
            return None

        try:
            screenshot = self._pyautogui.screenshot()
            import io
            buffer = io.BytesIO()
            screenshot.save(buffer, format='PNG')
            return buffer.getvalue()
        except Exception as e:
            logger.warning(f"Failed to capture screenshot: {e}")
            return None


class OCRProvider(PerceptionProvider):
    """OCR perception provider.

    Uses pytesseract to extract text from screen regions. Can perform
    targeted OCR on specific areas rather than full screen analysis.
    """

    def __init__(self):
        """Initialize the OCR provider."""
        self._pytesseract = None
        self._PIL_Image = None
        self._pyautogui = None
        self._initialize_ocr()

    def _initialize_ocr(self) -> None:
        """Initialize OCR libraries if available."""
        try:
            import pytesseract
            from PIL import Image
            import pyautogui

            self._pytesseract = pytesseract
            self._PIL_Image = Image
            self._pyautogui = pyautogui
            logger.debug("OCR provider initialized")
        except ImportError as e:
            logger.debug(f"OCR dependencies not fully available: {e}")
        except Exception as e:
            logger.warning(f"Failed to initialize OCR provider: {e}")

    def is_available(self) -> bool:
        """Check if OCR is available.

        Returns:
            True if pytesseract and PIL are ready, False otherwise
        """
        return (self._pytesseract is not None and
                self._PIL_Image is not None and
                self._pyautogui is not None)

    def get_perception_sources(self) -> List[str]:
        """Get perception sources this provider contributes.

        Returns:
            List of source identifiers
        """
        if self.is_available():
            return ["ocr"]
        return []

    def get_current_state(self, previous_state: Optional[ScreenState] = None) -> Optional[ScreenState]:
        """Get current screen state using OCR.

        Captures screen and extracts text regions with bounding boxes.
        Full OCR is expensive - prefer targeted extraction when possible.

        Args:
            previous_state: Optional previous state for optimization

        Returns:
            ScreenState with text regions if available, None otherwise
        """
        if not self.is_available():
            return None

        try:
            # Capture screenshot
            screenshot = self._pyautogui.screenshot()

            # Get text with bounding boxes
            text_regions = self._extract_text_regions(screenshot)

            # Create ScreenState with OCR data
            state = ScreenState(
                timestamp=time.time(),
                text_regions=text_regions,
                perception_sources=self.get_perception_sources(),
                confidence_overall=0.7  # OCR confidence varies
            )

            logger.debug(f"Got OCR state: {len(text_regions)} text regions")

            return state

        except Exception as e:
            logger.warning(f"Failed to get OCR state: {e}")
            return None

    def _extract_text_regions(self, screenshot) -> List[TextRegion]:
        """Extract text regions with bounding boxes from screenshot.

        Args:
            screenshot: PIL Image from pyautogui

        Returns:
            List of TextRegion objects
        """
        try:
            # Get detailed OCR data with bounding boxes
            data = self._pytesseract.image_to_data(
                screenshot,
                output_type=self._pytesseract.Output.DICT
            )

            text_regions = []
            n_boxes = len(data['text'])

            for i in range(n_boxes):
                text = data['text'][i].strip()
                confidence = float(data['conf'][i])

                # Filter out low-confidence and empty text
                if confidence < 0.5 or len(text) < 2:
                    continue

                # Create TextRegion with bounding box
                text_region = TextRegion(
                    text=text,
                    bounding_box={
                        "x": data['left'][i],
                        "y": data['top'][i],
                        "width": data['width'][i],
                        "height": data['height'][i]
                    },
                    confidence=confidence / 100.0,  # Normalize to 0-1
                    language=None  # Not easily detected per-word
                )
                text_regions.append(text_region)

            logger.debug(f"Extracted {len(text_regions)} text regions from OCR")
            return text_regions

        except Exception as e:
            logger.warning(f"Failed to extract text regions: {e}")
            return []

    def extract_text_from_region(self, screenshot, region: Dict[str, int]) -> Optional[str]:
        """Extract text from a specific screen region.

        More efficient than full-screen OCR when you know where to look.

        Args:
            screenshot: PIL Image from pyautogui
            region: {x, y, width, height} region to analyze

        Returns:
            Extracted text string, or None if failed
        """
        if not self.is_available():
            return None

        try:
            # Crop region
            left = region.get("x", 0)
            top = region.get("y", 0)
            width = region.get("width", 0)
            height = region.get("height", 0)

            if width <= 0 or height <= 0:
                return None

            cropped = screenshot.crop((left, top, left + width, top + height))

            # OCR the cropped region
            text = self._pytesseract.image_to_string(cropped)
            return text.strip() if text else None

        except Exception as e:
            logger.warning(f"Failed to extract text from region: {e}")
            return None


class PerceptionLayer:
    """
    Unified perception abstraction layer.

    Orchestrates multiple perception providers to create a comprehensive
    screen state representation. Uses the hybrid approach specified:
    1. Windows UI Automation (primary)
    2. Screen metadata
    3. OCR text extraction
    4. Vision models (when necessary and available)

    Providers contribute their data, which is merged into a single
    normalized ScreenState.
    """

    def __init__(self):
        """Initialize the perception layer with all available providers."""
        # Initialize providers in priority order
        self.providers = []

        # 1. Windows UI Automation (primary, highest priority)
        self.uia_provider = WindowsUIAutomationProvider()
        if self.uia_provider.is_available():
            self.providers.append(self.uia_provider)
            logger.info("Windows UI Automation provider active")

        # 2. Screen capture (basic screen info)
        self.screen_provider = ScreenCaptureProvider()
        if self.screen_provider.is_available():
            self.providers.append(self.screen_provider)
            logger.info("Screen capture provider active")

        # 3. OCR (text extraction)
        self.ocr_provider = OCRProvider()
        if self.ocr_provider.is_available():
            self.providers.append(self.ocr_provider)
            logger.info("OCR provider active")

        # Future: VisionProvider would be added here when implemented

        logger.info(f"Perception layer initialized with {len(self.providers)} providers")

    def get_current_state(self, previous_state: Optional[ScreenState] = None) -> ScreenState:
        """Get current screen state by orchestrating all available providers.

        This is the main perception method. It queries all providers,
        merges their data, and returns a normalized ScreenState.

        Args:
            previous_state: Optional previous state for optimization

        Returns:
            ScreenState with data from all available providers
        """
        start_time = time.time()
        all_sources = []
        confidence_scores = []

        # Start with empty state
        state = ScreenState(timestamp=time.time())

        # Merge data from each provider
        for provider in self.providers:
            try:
                provider_start = time.time()
                provider_state = provider.get_current_state(previous_state)
                provider_duration = time.time() - provider_start

                if provider_state is not None:
                    # Merge provider data into main state
                    state = self._merge_states(state, provider_state)
                    all_sources.extend(provider.get_perception_sources())
                    if provider_state.confidence_overall > 0:
                        confidence_scores.append(provider_state.confidence_overall)

                    logger.debug(f"Provider {type(provider).__name__} contributed data "
                               f"(duration: {provider_duration:.3f}s)")

            except Exception as e:
                logger.warning(f"Provider {type(provider).__name__} failed: {e}")
                continue

        # Set final metadata
        state.perception_sources = all_sources
        if confidence_scores:
            state.confidence_overall = sum(confidence_scores) / len(confidence_scores)

        total_duration = time.time() - start_time
        logger.debug(f"Perception completed in {total_duration:.3f}s with sources: {all_sources}")

        # Compute fingerprint for change detection
        state.compute_fingerprint()

        return state

    def _merge_states(self, base: ScreenState, addition: ScreenState) -> ScreenState:
        """Merge two ScreenState objects, preferring non-empty values.

        Args:
            base: Base state to merge into (modified in place)
            addition: Additional state data to merge

        Returns:
            Merged base state
        """
        # Screen dimensions - use largest if available
        if addition.screen_width > base.screen_width:
            base.screen_width = addition.screen_width
        if addition.screen_height > base.screen_height:
            base.screen_height = addition.screen_height
        if addition.monitor_count > base.monitor_count:
            base.monitor_count = addition.monitor_count

        # Windows - prefer active window info
        if addition.active_window and (not base.active_window or
            (addition.active_window.process_name != "unknown" and
             base.active_window.process_name == "unknown")):
            base.active_window = addition.active_window

        # Visible windows - merge, avoiding duplicates
        if addition.visible_windows:
            base_window_ids = {w.window_id for w in base.visible_windows}
            for window in addition.visible_windows:
                if window.window_id not in base_window_ids:
                    base.visible_windows.append(window)

        # Desktop elements - merge from UIA (more detailed)
        if addition.desktop_elements and not base.desktop_elements:
            base.desktop_elements = addition.desktop_elements

        # Text regions - merge from OCR
        if addition.text_regions:
            base.text_regions.extend(addition.text_regions)

        # Focused element - prefer UIA data
        if addition.focused_element and not base.focused_element:
            base.focused_element = addition.focused_element

        # Cursor position - prefer most recent
        if addition.cursor_position:
            base.cursor_position = addition.cursor_position

        return base

    def get_state_from_uia(self) -> Optional[ScreenState]:
        """Get state specifically from Windows UI Automation provider.

        Returns:
            ScreenState if UIA available and successful, None otherwise
        """
        if self.uia_provider.is_available():
            return self.uia_provider.get_current_state()
        return None

    def get_state_from_screen_capture(self) -> Optional[ScreenState]:
        """Get state specifically from screen capture provider.

        Returns:
            ScreenState if screen capture available, None otherwise
        """
        if self.screen_provider.is_available():
            return self.screen_provider.get_current_state()
        return None

    def get_state_from_ocr(self) -> Optional[ScreenState]:
        """Get state specifically from OCR provider.

        Returns:
            ScreenState if OCR available and successful, None otherwise
        """
        if self.ocr_provider.is_available():
            return self.ocr_provider.get_current_state()
        return None

    def get_perception_sources(self) -> List[str]:
        """Get list of all available perception sources.

        Returns:
            List of source identifiers
        """
        sources = []
        for provider in self.providers:
            sources.extend(provider.get_perception_sources())
        return sources

    def is_available(self) -> bool:
        """Check if any perception providers are available.

        Returns:
            True if at least one provider is available
        """
        return len(self.providers) > 0

    def to_dict(self) -> Dict:
        """Convert perception layer info to dictionary for diagnostics.

        Returns:
            Dictionary with provider information and availability
        """
        return {
            "providers": [p.to_dict() for p in self.providers],
            "total_providers": len(self.providers),
            "available_sources": self.get_perception_sources()
        }


def create_perception_layer() -> PerceptionLayer:
    """Create and initialize a PerceptionLayer with all available providers.

    Returns:
        PerceptionLayer instance (may have zero providers if nothing available)
    """
    return PerceptionLayer()