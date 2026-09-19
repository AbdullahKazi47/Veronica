"""
VisualAnalysis: structured representation of vision model output.

Carries visual understanding produced by a VisionProvider. It is a PERCEPTION
DATA STRUCTURE, never an instruction or action. Output is always treated as
untrusted external screen data.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class DetectedElement:
    """A UI element visually identified by a vision model."""
    label: str
    bounding_box: Dict[str, int] = field(default_factory=dict)  # {left, top, width, height}
    confidence: float = 0.0
    attributes: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "bounding_box": dict(self.bounding_box),
            "confidence": round(self.confidence, 4),
            "attributes": dict(self.attributes),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DetectedElement:
        return cls(
            label=str(data.get("label", "")),
            bounding_box=dict(data.get("bounding_box", {})),
            confidence=float(data.get("confidence", 0.0)),
            attributes=dict(data.get("attributes", {})),
        )


@dataclass
class DetectedTextRegion:
    """A text region visually recognized by a vision model."""
    text: str
    bounding_box: Dict[str, int] = field(default_factory=dict)  # {left, top, width, height}
    confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "bounding_box": dict(self.bounding_box),
            "confidence": round(self.confidence, 4),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DetectedTextRegion:
        return cls(
            text=str(data.get("text", "")),
            bounding_box=dict(data.get("bounding_box", {})),
            confidence=float(data.get("confidence", 0.0)),
        )


@dataclass
class VisualAnalysis:
    """
    Structured result of vision model analysis.

    Purely informational perception data. Never contains executable commands,
    permissions, or raw image bytes.
    """
    summary: str = ""
    active_window_title: Optional[str] = None
    detected_elements: List[DetectedElement] = field(default_factory=list)
    detected_text: List[DetectedTextRegion] = field(default_factory=list)
    visual_state: str = ""
    confidence: float = 0.0
    timestamp: float = field(default_factory=time.time)
    provider: str = ""
    model: str = ""
    latency_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": self.summary,
            "active_window_title": self.active_window_title,
            "detected_elements": [e.to_dict() for e in self.detected_elements],
            "detected_text": [t.to_dict() for t in self.detected_text],
            "visual_state": self.visual_state,
            "confidence": round(self.confidence, 4),
            "timestamp": self.timestamp,
            "provider": self.provider,
            "model": self.model,
            "latency_ms": round(self.latency_ms, 2),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> VisualAnalysis:
        elements = [
            DetectedElement.from_dict(e) for e in data.get("detected_elements", [])
        ]
        texts = [
            DetectedTextRegion.from_dict(t) for t in data.get("detected_text", [])
        ]
        return cls(
            summary=str(data.get("summary", "")),
            active_window_title=data.get("active_window_title"),
            detected_elements=elements,
            detected_text=texts,
            visual_state=str(data.get("visual_state", "")),
            confidence=float(data.get("confidence", 0.0)),
            timestamp=float(data.get("timestamp", time.time())),
            provider=str(data.get("provider", "")),
            model=str(data.get("model", "")),
            latency_ms=float(data.get("latency_ms", 0.0)),
        )
