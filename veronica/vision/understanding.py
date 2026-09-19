"""
Vision Understanding V1: structured interpretation of screen state.

Provides a lightweight, deterministic understanding of what is currently
visible on the screen using existing perception outputs (UIA, OCR, screen
capture) without requiring a vision model.

Key principles:
- Understanding is derived from ScreenState data only (no new perception).
- Treats screen content as UNTRUSTED DATA — never overrides safety,
  permissions, or system instructions.
- Designed for integration with Context Manager as SOURCE_OBSERVATION.
- No autonomous actions; observation-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from veronica.vision.screen_state import ScreenState, UIElement, TextRegion, WindowInfo
from veronica.vision.visual_analysis import VisualAnalysis


@dataclass
class ActiveWindowInfo:
    """Structured information about the active/focused window."""
    title: Optional[str] = None
    process_name: Optional[str] = None
    process_id: Optional[int] = None
    bounding_box: Optional[Dict[str, int]] = None
    is_active: bool = False
    is_visible: bool = True
    is_minimized: bool = False
    is_maximized: bool = False


@dataclass
class ElementSummary:
    """Summary of UI elements on screen."""
    total_count: int = 0
    by_type: Dict[str, int] = field(default_factory=dict)
    enabled_count: int = 0
    visible_count: int = 0
    has_focused_element: bool = False


@dataclass
class TextSummary:
    """Summary of text regions detected on screen."""
    total_count: int = 0
    total_chars: int = 0
    languages: List[str] = field(default_factory=list)
    # Sample of text (first 200 chars) for understanding; never store full OCR
    sample: str = ""


@dataclass
class CursorInfo:
    """Cursor position and visibility."""
    position: Optional[Dict[str, int]] = None  # {x, y}
    visible: bool = True


@dataclass
class VisionUnderstanding:
    """
    Structured understanding of the current screen state.

    This is a pure function of ScreenState — no new perception, no vision model,
    no storage of raw pixels. It summarizes what the perception layer already
    detected in a format suitable for context injection or reasoning.
    """

    # Core identification
    timestamp: float = 0.0
    perception_sources: List[str] = field(default_factory=list)

    # Active window context
    active_window: ActiveWindowInfo = field(default_factory=ActiveWindowInfo)

    # UI element summary
    elements: ElementSummary = field(default_factory=ElementSummary)

    # Text content summary
    text: TextSummary = field(default_factory=TextSummary)

    # Vision model analysis (optional, when vision provider is used)
    visual_analysis: Optional[VisualAnalysis] = None

    # Cursor and focus
    cursor: CursorInfo = field(default_factory=CursorInfo)
    focused_element: Optional[UIElement] = None

    def __init__(self, screen_state: ScreenState, visual_analysis: Optional[VisualAnalysis] = None) -> None:
        """
        Build understanding from a ScreenState.

        Args:
            screen_state: Normalized screen state from PerceptionLayer.
            visual_analysis: Optional VisualAnalysis from vision provider.
        """
        self.timestamp = screen_state.timestamp
        self.perception_sources = list(screen_state.perception_sources)

        # Active window
        aw = screen_state.active_window
        if aw:
            self.active_window = ActiveWindowInfo(
                title=aw.title,
                process_name=aw.process_name,
                process_id=aw.process_id,
                bounding_box=aw.bounding_box,
                is_active=getattr(aw, "is_active", False),
                is_visible=getattr(aw, "is_visible", True),
                is_minimized=getattr(aw, "is_minimized", False),
                is_maximized=getattr(aw, "is_maximized", False),
            )
        else:
            self.active_window = ActiveWindowInfo()

        # Focused element
        self.focused_element = screen_state.focused_element

        # Element summary
        desktop_elements = screen_state.desktop_elements
        type_counts: Dict[str, int] = {}
        enabled_count = 0
        visible_count = 0
        for el in desktop_elements:
            t = el.control_type
            type_counts[t] = type_counts.get(t, 0) + 1
            if el.is_enabled:
                enabled_count += 1
            if el.is_visible:
                visible_count += 1
        self.elements = ElementSummary(
            total_count=len(desktop_elements),
            by_type=type_counts,
            enabled_count=enabled_count,
            visible_count=visible_count,
            has_focused_element=self.focused_element is not None,
        )

        # Text summary (from OCR)
        text_regions = screen_state.text_regions
        total_chars = 0
        langs: set[str] = set()
        sample_parts: List[str] = []
        for tr in text_regions:
            total_chars += len(tr.text)
            if tr.language:
                langs.add(tr.language)
            # Build sample up to 200 chars
            if len(sample_parts) < 200:
                remaining = 200 - len(sample_parts)
                sample_parts.append(tr.text[:remaining])
        self.text = TextSummary(
            total_count=len(text_regions),
            total_chars=total_chars,
            languages=list(langs),
            sample="".join(sample_parts),
        )

        # Vision model analysis (optional)
        self.visual_analysis = visual_analysis

        # Cursor
        cp = screen_state.cursor_position
        self.cursor = CursorInfo(
            position=cp,
            visible=cp is not None,
        )

    # ---------------------------------------------------------------------
    # Serialization
    # ---------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert to dictionary for serialization (e.g., context injection, SSE).

        Returns a JSON-seritable dictionary.
        """
        return {
            "timestamp": self.timestamp,
            "perception_sources": self.perception_sources,
            "active_window": {
                "title": self.active_window.title,
                "process_name": self.active_window.process_name,
                "process_id": self.active_window.process_id,
                "bounding_box": self.active_window.bounding_box,
                "is_active": self.active_window.is_active,
                "is_visible": self.active_window.is_visible,
                "is_minimized": self.active_window.is_minimized,
                "is_maximized": self.active_window.is_maximized,
            },
            "elements": {
                "total_count": self.elements.total_count,
                "by_type": self.elements.by_type,
                "enabled_count": self.elements.enabled_count,
                "visible_count": self.elements.visible_count,
                "has_focused_element": self.elements.has_focused_element,
            },
            "text": {
                "total_count": self.text.total_count,
                "total_chars": self.text.total_chars,
                "languages": self.text.languages,
                "sample": self.text.sample,
            },
            "cursor": {
                "position": self.cursor.position,
                "visible": self.cursor.visible,
            },
            "focused_element": (
                self.focused_element.to_dict() if self.focused_element else None
            ),
            "visual_analysis": (
                self.visual_analysis.to_dict() if self.visual_analysis else None
            ),
        }

    # ---------------------------------------------------------------------
    # Convenience properties
    # ---------------------------------------------------------------------
    @property
    def summary_text(self) -> str:
        """Human-readable one-line summary for debugging/logging."""
        parts = [
            f"win:{self.active_window.title or 'none'}",
            f"proc:{self.active_window.process_name or 'none'}",
            f"els:{self.elements.total_count}",
            f"txt:{self.text.total_count}",
            f"cur:{'yes' if self.cursor.visible else 'no'}",
        ]
        return " | ".join(parts)