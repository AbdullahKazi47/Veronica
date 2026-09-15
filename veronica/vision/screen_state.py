"""
ScreenState: structured representation of the current computer screen.

This module defines the ScreenState dataclass which represents what is currently
visible on the computer screen in a structured, serializable format. It does
not persist raw screenshots or sensitive data by default.

Key principles:
- Serializable (JSON/dict friendly) for logging, debugging, and transmission
- No raw pixel data by default to avoid storing sensitive information
- Includes fingerprint/change detection capabilities
- Platform-aware but Windows-focused for Phase 7
- Timestamps for change detection and cache validity
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from pathlib import Path

from veronica.logging_setup import get_logger

logger = get_logger("vision.screen_state")


@dataclass
class UIElement:
    """Represents a single UI element detected on screen."""
    element_id: str                    # Unique identifier (automation ID, name, position-based)
    name: str                          # Human-readable name/label (button text, window title)
    control_type: str                  # Type of control (button, edit, text, window, etc.)
    bounding_box: Dict[str, int]       # {x, y, width, height} in screen coordinates
    is_enabled: bool = True            # Whether the element is interactable
    is_visible: bool = True            # Whether the element is currently visible
    value: Optional[str] = None        # Current value (for text fields, selected items)
    automation_id: Optional[str] = None # UI Automation ID if available
    class_name: Optional[str] = None   # Underlying class name (Windows class, HTML tag, etc.)
    children: List['UIElement'] = field(default_factory=list)  # Child elements

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "element_id": self.element_id,
            "name": self.name,
            "control_type": self.control_type,
            "bounding_box": self.bounding_box,
            "is_enabled": self.is_enabled,
            "is_visible": self.is_visible,
            "value": self.value,
            "automation_id": self.automation_id,
            "class_name": self.class_name,
            "children": [child.to_dict() for child in self.children]
        }


@dataclass
class TextRegion:
    """Represents a region of text detected on screen (typically via OCR)."""
    text: str                          # The extracted text
    bounding_box: Dict[str, int]       # {x, y, width, height} in screen coordinates
    confidence: float = 0.0            # OCR confidence (0.0 to 1.0)
    language: Optional[str] = None     # Detected language if available

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "text": self.text,
            "bounding_box": self.bounding_box,
            "confidence": self.confidence,
            "language": self.language
        }


@dataclass
class WindowInfo:
    """Information about a window on the desktop."""
    window_id: str                     # Unique window identifier
    title: str                         # Window title
    process_name: str                  # Name of the owning process (e.g., "Code.exe")
    process_id: int                    # Process ID
    bounding_box: Dict[str, int]       # {x, y, width, height} in screen coordinates
    is_active: bool = False            # Whether this is the foreground/active window
    is_visible: bool = True            # Whether the window is visible
    is_minimized: bool = False         # Whether the window is minimized
    is_maximized: bool = False         # Whether the window is maximized

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "window_id": self.window_id,
            "title": self.title,
            "process_name": self.process_name,
            "process_id": self.process_id,
            "bounding_box": self.bounding_box,
            "is_active": self.is_active,
            "is_visible": self.is_visible,
            "is_minimized": self.is_minimized,
            "is_maximized": self.is_maximized
        }


@dataclass
class ScreenState:
    """
    Structured representation of the current computer screen state.

    This is the primary perception output used by VERONICA for understanding
    what's happening on the computer. It focuses on semantic UI information
    rather than raw pixel data to enable robust, resolution-independent interaction.

    The ScreenState is designed to be:
    - Serializable for logging, debugging, and transmission
    - Privacy-conscious (no raw screenshots persisted by default)
    - Change-detectable via fingerprinting
    - Extensible for future perception modalities
    """
    # Core identification
    timestamp: float = field(default_factory=time.time)  # When this state was captured
    screen_width: int = 0                                # Total screen width in pixels
    screen_height: int = 0                               # Total screen height in pixels
    monitor_count: int = 1                               # Number of monitors detected

    # Window and application state
    active_window: Optional[WindowInfo] = None           # Currently active/focused window
    visible_windows: List[WindowInfo] = field(default_factory=list)  # All visible windows

    # UI element hierarchy (from UI Automation or semantic analysis)
    desktop_elements: List[UIElement] = field(default_factory=list)  # Top-level desktop elements

    # Text content (from OCR or UI Automation)
    text_regions: List[TextRegion] = field(default_factory=list)     # Detected text regions

    # Focused element (where keyboard input would go)
    focused_element: Optional[UIElement] = None                      # Currently focused element

    # Cursor position (when available)
    cursor_position: Optional[Dict[str, int]] = None                # {x, y} screen coordinates

    # Perception metadata
    perception_sources: List[str] = field(default_factory=list)     # Which providers contributed
    confidence_overall: float = 0.0                                 # Overall confidence in this state

    # Change detection fields (computed, not set directly)
    _fingerprint: Optional[str] = None                             # Cached fingerprint for change detection
    _change_indicators: Dict[str, Any] = field(default_factory=dict) # What changed since last state

    def to_dict(self) -> Dict[str, Any]:
        """Convert ScreenState to dictionary for serialization.

        Returns a JSON-serializable dictionary representation of the ScreenState.
        Sensitive data handling: by default, we don't persist raw screenshots
        or perform deep inspection of potentially sensitive fields.
        """
        return {
            "timestamp": self.timestamp,
            "screen_width": self.screen_width,
            "screen_height": self.screen_height,
            "monitor_count": self.monitor_count,
            "active_window": self.active_window.to_dict() if self.active_window else None,
            "visible_windows": [w.to_dict() for w in self.visible_windows],
            "desktop_elements": [e.to_dict() for e in self.desktop_elements],
            "text_regions": [t.to_dict() for t in self.text_regions],
            "focused_element": self.focused_element.to_dict() if self.focused_element else None,
            "cursor_position": self.cursor_position,
            "perception_sources": list(self.perception_sources),
            "confidence_overall": self.confidence_overall,
            "_fingerprint": self._fingerprint,
            "_change_indicators": dict(self._change_indicators)
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ScreenState':
        """Create ScreenState from dictionary representation.

        Args:
            data: Dictionary representation of ScreenState

        Returns:
            ScreenState instance
        """
        # Handle nested objects
        active_window = None
        if data.get("active_window"):
            active_window = WindowInfo(**data["active_window"])

        visible_windows = []
        for w_data in data.get("visible_windows", []):
            visible_windows.append(WindowInfo(**w_data))

        desktop_elements = []
        for e_data in data.get("desktop_elements", []):
            desktop_elements.append(UIElement(**e_data))

        text_regions = []
        for t_data in data.get("text_regions", []):
            text_regions.append(TextRegion(**t_data))

        focused_element = None
        if data.get("focused_element"):
            focused_element = UIElement(**data["focused_element"])

        return cls(
            timestamp=data.get("timestamp", time.time()),
            screen_width=data.get("screen_width", 0),
            screen_height=data.get("screen_height", 0),
            monitor_count=data.get("monitor_count", 1),
            active_window=active_window,
            visible_windows=visible_windows,
            desktop_elements=desktop_elements,
            text_regions=text_regions,
            focused_element=focused_element,
            cursor_position=data.get("cursor_position"),
            perception_sources=data.get("perception_sources", []),
            confidence_overall=data.get("confidence_overall", 0.0),
            _fingerprint=data.get("_fingerprint"),
            _change_indicators=data.get("_change_indicators", {})
        )

    def compute_fingerprint(self) -> str:
        """Compute a fingerprint of the current screen state for change detection.

        The fingerprint focuses on stable, semantically meaningful aspects:
        - Active window title and process
        - Number and types of visible UI elements
        - General layout structure

        Returns:
            SHA-256 hash string (first 16 chars for brevity) representing state
        """
        # Build fingerprint components
        components = []

        # Screen dimensions
        components.append(f"screen:{self.screen_width}x{self.screen_height}:{self.monitor_count}")

        # Active window info
        if self.active_window:
            components.append(f"active:{self.active_window.title}:{self.active_window.process_name}")
        else:
            components.append("active:none")

        # Window count and types
        window_types = sorted([w.process_name for w in self.visible_windows])
        components.append(f"windows:{len(window_types)}:{':'.join(window_types[:5])}")  # Top 5

        # UI element counts by type
        element_counts: Dict[str, int] = {}
        for element in self.desktop_elements:
            elem_type = element.control_type
            element_counts[elem_type] = element_counts.get(elem_type, 0) + 1
        if element_counts:
            count_str = ",".join([f"{k}:{v}" for k, v in sorted(element_counts.items())])
            components.append(f"elements:{count_str}")
        else:
            components.append("elements:none")

        # Text region count and sample
        if self.text_regions:
            text_sample = "|".join([t.text[:50] for t in self.text_regions[:3]])  # First 3, first 50 chars
            components.append(f"text:{len(self.text_regions)}:{text_sample}")
        else:
            components.append("text:none")

        # Focused element
        if self.focused_element:
            components.append(f"focused:{self.focused_element.name}:{self.focused_element.control_type}")
        else:
            components.append("focused:none")

        # Combine and hash
        fingerprint_str = "|".join(components)
        fingerprint_hash = hashlib.sha256(fingerprint_str.encode('utf-8')).hexdigest()
        self._fingerprint = fingerprint_hash[:16]  # First 16 chars for brevity
        return self._fingerprint

    def get_fingerprint(self) -> str:
        """Get the cached fingerprint, computing it if necessary."""
        if self._fingerprint is None:
            return self.compute_fingerprint()
        return self._fingerprint

    def has_changed_significantly(self, previous: 'ScreenState', threshold: float = 0.3) -> bool:
        """Determine if screen state has changed significantly since previous state.

        Args:
            previous: Previous ScreenState to compare against
            threshold: Change threshold (0.0 to 1.0) - fraction that must change

        Returns:
            True if significant change detected, False otherwise
        """
        if not isinstance(previous, ScreenState):
            logger.warning("has_changed_significantly called with non-ScreenState")
            return True  # Assume changed if comparison fails

        # Quick checks first
        if self.timestamp <= previous.timestamp:
            # Older or same timestamp - likely no meaningful change
            return False

        # Compute fingerprints
        current_fp = self.get_fingerprint()
        previous_fp = previous.get_fingerprint()

        if current_fp == previous_fp:
            # Identical fingerprints - very likely no significant change
            return False

        # More detailed change analysis
        changes = self._analyze_changes(previous)
        change_score = changes.get("score", 0.0)

        logger.debug(f"Screen change analysis: score={change_score:.3f}, threshold={threshold}")
        return change_score >= threshold

    def _analyze_changes(self, previous: 'ScreenState') -> Dict[str, Any]:
        """Analyze what changed between this state and previous state.

        Returns:
            Dictionary with change details and overall change score (0.0 to 1.0)
        """
        changes = {
            "changed": False,
            "score": 0.0,
            "details": []
        }

        change_weight = 0.0
        total_weight = 0.0

        # Screen dimensions / monitor count changes (rare but significant)
        total_weight += 0.2
        if (self.screen_width != previous.screen_width or
            self.screen_height != previous.screen_height or
            self.monitor_count != previous.monitor_count):
            change_weight += 0.2
            changes["details"].append("screen_resolution_changed")

        # Active window changes
        total_weight += 0.3
        if (not self.active_window and previous.active_window) or \
           (self.active_window and not previous.active_window) or \
           (self.active_window and previous.active_window and
            (self.active_window.title != previous.active_window.title or
             self.active_window.process_name != previous.active_window.process_name)):
            change_weight += 0.3
            changes["details"].append("active_window_changed")

        # Window list changes
        total_weight += 0.2
        current_window_set = {(w.window_id, w.title, w.process_name) for w in self.visible_windows}
        previous_window_set = {(w.window_id, w.title, w.process_name) for w in previous.visible_windows}
        if current_window_set != previous_window_set:
            change_weight += 0.2
            changes["details"].append("window_list_changed")

        # UI element changes (simplified)
        total_weight += 0.3
        current_element_count = len(self.desktop_elements)
        previous_element_count = len(previous.desktop_elements)
        if abs(current_element_count - previous_element_count) > max(1, previous_element_count * 0.2):
            change_weight += 0.3
            changes["details"].append("element_count_changed")
        else:
            # More detailed element analysis could go here
            pass

        # Text content changes
        total_weight += 0.2
        current_text_hash = hashlib.sha256("|".join([t.text for t in self.text_regions]).encode()).hexdigest()
        previous_text_hash = hashlib.sha256("|".join([t.text for t in previous.text_regions]).encode()).hexdigest()
        if current_text_hash != previous_text_hash:
            change_weight += 0.2
            changes["details"].append("text_content_changed")

        # Calculate final score
        if total_weight > 0:
            changes["score"] = change_weight / total_weight
        changes["changed"] = changes["score"] >= 0.3  # Default threshold

        # Cache change indicators
        self._change_indicators = changes
        return changes