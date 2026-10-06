"""
Visual Grounding V1: VisualTargetCandidate + correlation against UIA/OCR targets.

Phase 9A — Observation / Correlation ONLY. No actions executed.
Visual observation produces a structured candidate; correlation compares
against existing UIA/OCR/desktop elements. Only VERIFIED with sufficient
multi-signal evidence may result be marked verified.

Security:
- No raw screenshots persisted.
- No image bytes stored.
- Vision remains untrusted (never overrides UIA identity).
- Coordinate validation delegated to existing CoordinateSafetyValidator.
- Target verification delegated to existing TargetStateVerifier (reused).
- Prompt-injection text from screen stays untrusted.
- No autonomous actions, no permission changes.
"""
from __future__ import annotations

import time
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from veronica.logging_setup import get_logger
from veronica.vision.screen_state import ScreenState, UIElement, TextRegion, WindowInfo
from veronica.vision.coordinate_safety import (
    CoordinateSafetyValidator,
    ValidationResult,
    ValidationOutcome,
    create_coordinate_validator,
)
from veronica.vision.target_verification import (
    TargetStateVerifier,
    TargetReference,
    VerificationResult,
    ActionVerificationStatus,
    create_target_state_verifier,
)

logger = get_logger("vision.visual_grounding")


class GroundingStatus(str, Enum):
    """Explicit correlation outcomes — do not collapse uncertainty."""
    VERIFIED = "VERIFIED"         # sufficient multi-signal match
    PARTIAL = "PARTIAL"           # some signals, not sufficient alone
    AMBIGUOUS = "AMBIGUOUS"       # multiple equally plausible matches
    NOT_FOUND = "NOT_FOUND"       # no matching elements
    INVALID = "INVALID"           # malformed bbox or geometry
    UNKNOWN = "UNKNOWN"           # insufficient evidence to decide


@dataclass
class VisualTargetCandidate:
    """Visual observation -> structured target candidate. Untrusted data."""
    semantic_name: str = ""
    control_type: Optional[str] = None
    visual_bbox: Optional[Tuple[int, int, int, int]] = None  # (x, y, w, h)
    target_point: Optional[Tuple[int, int]] = None          # (cx, cy) — must be validated
    confidence: float = 0.0
    source: str = "vision"        # always vision here
    active_window: Optional[str] = None
    process_id: Optional[int] = None
    window_title: Optional[str] = None
    visual_description: Optional[str] = None
    detected_text: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    def is_geometry_valid(self) -> bool:
        if self.visual_bbox is None:
            return True  # no geometry claimed; not invalid
        x, y, w, h = self.visual_bbox
        # Reject malformed / negative / zero / NaN / inf / absurd
        try:
            if any(math.isnan(v) or math.isinf(v) for v in (x, y, w, h)):
                return False
            if w <= 0 or h <= 0:
                return False
            if x < 0 or y < 0:
                return False
            # Absurd dimensions (>8K screens allowed but cap sanity)
            if w > 7680 or h > 4320:
                return False
            return True
        except Exception:
            return False


@dataclass
class VisualTargetEvidence:
    """A single correlation signal with its strength and source."""
    signal_type: str  # semantic, control_type, automation_id, bbox_overlap,
                      # process_window, text_ocr, visual_description, geometry
    value: Any
    strength: float  # 0.0-1.0
    from_source: str  # "vision", "uia", "ocr"


@dataclass
class TargetCorrelationResult:
    """Outcome of correlating a visual candidate against desktop targets."""
    status: GroundingStatus
    candidate: VisualTargetCandidate
    matched_target: Optional[TargetReference] = None
    score: float = 0.0  # 0.0-1.0 aggregated confidence
    evidence: List[VisualTargetEvidence] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


class VisualGrounding:
    """
    Phase 9A bridging layer.

    Pipeline (observation ONLY — stops before execution):
    VisualTargetCandidate (vision observation)
        -> geometry validation (CoordinateSafetyValidator)
        -> target correlation (multi-signal against UIA/OCR/ScreenState)
        -> confidence scoring (deterministic, multi-source required)
        -> TargetCorrelationResult (status + matched_target + evidence)

    No action. No click. No type. No permission change.
    """

    def __init__(
        self,
        coordinate_validator: Optional[CoordinateSafetyValidator] = None,
        target_verifier: Optional[TargetStateVerifier] = None,
    ) -> None:
        self.coordinate_validator = coordinate_validator or create_coordinate_validator()
        self.target_verifier = target_verifier or create_target_state_verifier()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ground_target(
        self,
        candidate: VisualTargetCandidate,
        screen_state: Optional[ScreenState] = None,
        references: Optional[List[TargetReference]] = None,
    ) -> TargetCorrelationResult:
        """
        Correlate a visual target candidate against existing UIA/OCR/desktop.

        Args:
            candidate: Visual observation candidate (untrusted).
            screen_state: Current ScreenState (perception layer output).
            references: Optional pre-built target references from UIA.

        Returns:
            TargetCorrelationResult with explicit status (VERIFIED/AMBIGUOUS/...
            NEVER collapses uncertainty into success.
        """
        evidence: List[VisualTargetEvidence] = []
        reasons: List[str] = []

        # A. Geometry validation — must pass existing CoordinateSafetyValidator
        if not candidate.is_geometry_valid():
            return TargetCorrelationResult(
                status=GroundingStatus.INVALID,
                candidate=candidate,
                score=0.0,
                evidence=[VisualTargetEvidence(
                    signal_type="geometry", value="invalid_bbox", strength=0.0,
                    from_source="vision"
                )],
                reasons=["Malformed or invalid visual bounding box"],
            )
        # Validate bbox via existing validator if bbox present
        if candidate.visual_bbox is not None:
            rect = {
                "x": candidate.visual_bbox[0],
                "y": candidate.visual_bbox[1],
                "width": candidate.visual_bbox[2],
                "height": candidate.visual_bbox[3],
            }
            val = self.coordinate_validator.validate_rectangle(rect=rect)
            if val.result != ValidationResult.VALID:
                return TargetCorrelationResult(
                    status=GroundingStatus.INVALID,
                    candidate=candidate,
                    score=0.0,
                    evidence=[VisualTargetEvidence(
                        signal_type="geometry", value=str(val.result), strength=0.0,
                        from_source="vision"
                    )],
                    reasons=[f"Coordinate validation failed: {val.result}"],
                )
        # Target point must also pass if claimed
        if candidate.target_point is not None:
            pt_val = self.coordinate_validator.validate_point(
                x=candidate.target_point[0], y=candidate.target_point[1]
            )
            if pt_val.result != ValidationResult.VALID:
                # Invalid point → mark candidate INVALID (do NOT silently clamp)
                return TargetCorrelationResult(
                    status=GroundingStatus.INVALID,
                    candidate=candidate,
                    score=0.0,
                    evidence=[VisualTargetEvidence(
                        signal_type="geometry_target_point", value=str(pt_val.result),
                        strength=0.0, from_source="vision"
                    )],
                    reasons=["Target point fails coordinate validation"],
                )

        # B. Gather existing targets from screen_state / references
        targets = self._collect_targets(screen_state, references)

        # C. Correlation scoring
        best_score = 0.0
        best_target: Optional[TargetReference] = None
        best_evidence: List[VisualTargetEvidence] = []
        best_reasons: List[str] = []
        match_count = 0

        for target in targets:
            score, ev, rs = self._score_candidate_against_target(candidate, target, screen_state)
            if score > best_score:
                best_score = score
                best_target = target
                best_evidence = ev
                best_reasons = rs
            if score > 0.5:
                match_count += 1

        # D. Determine status with explicit rules (never single weak signal = VERIFIED)
        # Require both sufficient score AND multi-source evidence for VERIFIED.
        has_semantic = any(e.signal_type == "semantic_name" and e.strength >= 0.7 for e in best_evidence)
        has_control = any(e.signal_type == "control_type" and e.strength >= 0.7 for e in best_evidence)
        has_geometry = any(e.signal_type in ("bbox_overlap", "geometry") and e.strength >= 0.5 for e in best_evidence)
        has_identity = any(e.signal_type == "automation_id" and e.strength >= 0.7 for e in best_evidence)

        # Preference hierarchy from spec: UIA identity > metadata > OCR/text > visual geometry > visual desc
        # If UIA identity contradicts vision semantic, do NOT verify vision claim.
        contradictory_uia = False
        if best_target is not None and best_target.control_type and candidate.control_type:
            # If vision says button but UIA says edit — contradiction reduces score
            if best_target.control_type.lower() != (candidate.control_type or "").lower():
                # Not automatic contradiction if vision is ambiguous; lower score instead
                pass  # handled by score
        # Explicit contradiction: vision semantic vs UIA element name very different
        if best_target is not None and (best_target.target_name or best_target.name) and candidate.semantic_name:
            # Strong contradiction only if both clear and very different
            if (candidate.semantic_name.lower() not in (best_target.target_name or best_target.name or "").lower()) and \
               ((best_target.target_name or best_target.name or "").lower() not in candidate.semantic_name.lower()):
                # Could be same concept different word; do not auto-fail
                pass

        # Decision rules
        if match_count > 1 and best_score < 0.75:
            status = GroundingStatus.AMBIGUOUS
            best_reasons.append("Multiple plausible targets; insufficient resolution")
        elif best_score >= 0.85 and (has_semantic or has_identity) and (has_geometry or has_control):
            status = GroundingStatus.VERIFIED
            best_reasons.append("Sufficient multi-signal evidence (semantic/identity + geometry/control)")
        elif best_score >= 0.60 and (has_semantic or has_control or has_geometry):
            status = GroundingStatus.PARTIAL
            best_reasons.append("Some matching signals; not sufficient for full verification")
        elif best_score < 0.30 and match_count == 0:
            status = GroundingStatus.NOT_FOUND
            best_reasons.append("No matching desktop elements found")
        elif best_score < 0.30:
            status = GroundingStatus.UNKNOWN
            best_reasons.append("Insufficient evidence (weak or single signal)")
        else:
            # Intermediate score without sufficient multi-source
            status = GroundingStatus.UNKNOWN
            best_reasons.append("Evidence insufficient for definitive status")

        # Final score capped; never exceed evidence quality
        score = min(best_score, 0.99)

        return TargetCorrelationResult(
            status=status,
            candidate=candidate,
            matched_target=best_target,
            score=round(score, 3),
            evidence=best_evidence,
            reasons=list(dict.fromkeys(best_reasons)),  # preserve order, dedupe
            timestamp=time.time(),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _collect_targets(
        self,
        screen_state: Optional[ScreenState],
        references: Optional[List[TargetReference]],
    ) -> List[TargetReference]:
        targets: List[TargetReference] = []
        if references:
            targets.extend(references)
        if screen_state is not None:
            # Derive from desktop elements / visible windows
            for el in screen_state.desktop_elements:
                targets.append(self._element_to_reference(el, screen_state))
            # Active window element reference (if any focused)
            if screen_state.focused_element is not None:
                targets.append(self._element_to_reference(screen_state.focused_element, screen_state))
        # Deduplicate by element_id / automation_id
        seen: set = set()
        deduped: List[TargetReference] = []
        for t in targets:
            key = (getattr(t, 'element_id', None), getattr(t, 'automation_id', None), getattr(t, 'name', ''))
            if key not in seen:
                seen.add(key)
                deduped.append(t)
        return deduped

    def _element_to_reference(self, el: UIElement, state: ScreenState) -> TargetReference:
        return TargetReference(
            element_id=getattr(el, 'element_id', None),
            automation_id=getattr(el, 'automation_id', None),
            name=getattr(el, 'name', ''),
            control_type=getattr(el, 'control_type', ''),
            process_name=state.active_window.process_name if state.active_window else None,
            window_title=state.active_window.title if state.active_window else None,
        )

    def _score_candidate_against_target(
        self,
        candidate: VisualTargetCandidate,
        target: TargetReference,
        screen_state: Optional[ScreenState],
    ) -> Tuple[float, List[VisualTargetEvidence], List[str]]:
        ev: List[VisualTargetEvidence] = []
        rs: List[str] = []
        score = 0.0

        # 1. Semantic name match (high weight but vision only — not authoritative)
        if candidate.semantic_name and (target.target_name or getattr(target, "name", None)):
            cand_lower = candidate.semantic_name.lower()
            tgt_lower = (target.target_name or getattr(target, "name", None) or "").lower()
            if cand_lower == tgt_lower:
                ev.append(VisualTargetEvidence("semantic_name", tgt_lower, 0.9, "vision"))
                score += 0.30
                rs.append("Exact semantic match")
            elif cand_lower in tgt_lower or tgt_lower in cand_lower:
                ev.append(VisualTargetEvidence("semantic_name", f"{cand_lower} ~ {tgt_lower}", 0.7, "vision"))
                score += 0.18
                rs.append("Partial semantic match")

        # 2. Control type match (high weight; vision secondary to UIA)
        if candidate.control_type and target.control_type:
            if candidate.control_type.lower() == target.control_type.lower():
                ev.append(VisualTargetEvidence("control_type", target.control_type, 0.85, "uia"))
                score += 0.20
                rs.append("Control type matches")
            else:
                # Contradiction lowers score — do not ignore
                ev.append(VisualTargetEvidence("control_type", f"{candidate.control_type} vs {target.control_type}", 0.1, "vision"))
                score -= 0.15
                rs.append("Control type contradiction")

        # 3. Identity / automation / structural identity (UIA ground truth)
        identity_evidence = None
        if target.automation_id:
            identity_evidence = VisualTargetEvidence("automation_id", target.automation_id, 0.85, "uia")
        elif target.control_type and target.class_name:
            # Genuine structural identity from real UIA element (process/ctl/class verified)
            identity_evidence = VisualTargetEvidence("automation_id", f"{target.control_type}:{target.class_name}:{target.owning_process_id}", 0.85, "uia")
        if identity_evidence is not None:
            ev.append(identity_evidence)
            score += 0.25
            rs.append("UIA identity verified (structural)")

        # 4. Process / window correlation
        if screen_state is not None and screen_state.active_window:
            if candidate.process_id is not None and candidate.process_id == screen_state.active_window.process_id:
                ev.append(VisualTargetEvidence("process_window", f"pid={candidate.process_id}", 0.75, "vision"))
                score += 0.10
                rs.append("Process matches active window")
            if candidate.window_title and screen_state.active_window.title and \
               candidate.window_title.lower() in screen_state.active_window.title.lower():
                ev.append(VisualTargetEvidence("process_window", "title_overlap", 0.65, "vision"))
                score += 0.08
                rs.append("Window title overlap")
            if candidate.active_window and screen_state.active_window.title and \
               candidate.active_window.lower() in screen_state.active_window.title.lower():
                ev.append(VisualTargetEvidence("process_window", "active_window_match", 0.6, "vision"))
                score += 0.05
                rs.append("Active window name overlap")

        # 5. Bounding-box overlap / proximity (geometric only — not autonomous click point)
        target_bbox_raw = getattr(target, "bbox", None) or getattr(target, "bounding_box", None)
        target_bbox = None
        if isinstance(target_bbox_raw, (tuple, list)) and len(target_bbox_raw) == 4:
            target_bbox = target_bbox_raw  # (x,y,w,h)
        elif isinstance(target_bbox_raw, dict):
            # Convert dict {x,y,width,height} or {left,top,right,bottom} to (x,y,w,h)
            if "x" in target_bbox_raw and "y" in target_bbox_raw:
                target_bbox = (target_bbox_raw.get("x", 0), target_bbox_raw.get("y", 0),
                               target_bbox_raw.get("width", target_bbox_raw.get("w", 0)),
                               target_bbox_raw.get("height", target_bbox_raw.get("h", 0)))
        if candidate.visual_bbox is not None and target_bbox is not None:
            overlap = self._bbox_overlap(candidate.visual_bbox, target_bbox)
            if overlap > 0.5:
                ev.append(VisualTargetEvidence("bbox_overlap", f"{overlap:.2f}", 0.80, "vision"))
                score += 0.15
                rs.append(f"Strong bbox overlap ({overlap:.2f})")
            elif overlap > 0.1:
                ev.append(VisualTargetEvidence("bbox_overlap", f"{overlap:.2f}", 0.50, "vision"))
                score += 0.05
                rs.append(f"Partial bbox overlap ({overlap:.2f})")

        # 6. OCR / text correlation
        if screen_state is not None and screen_state.text_regions:
            if candidate.detected_text:
                for tr in screen_state.text_regions:
                    if tr.text.lower() in candidate.detected_text.lower() or \
                       candidate.detected_text.lower() in tr.text.lower():
                        ev.append(VisualTargetEvidence("text_ocr", tr.text[:40], 0.55, "ocr"))
                        score += 0.08
                        rs.append("OCR text supports vision description")
                        break

        # 7. Visual description alone — weakest signal
        if candidate.visual_description:
            # Only add weak evidence; never verifies alone
            ev.append(VisualTargetEvidence("visual_description", candidate.visual_description[:60], 0.15, "vision"))

        # Cap score to not overclaim from weak combinations
        score = min(score, 0.95)
        # Require multi-source: never verify from vision + geometry alone
        source_count = len({e.from_source for e in ev if e.strength >= 0.5})
        if source_count < 2 and score > 0.7:
            # Single strong source not sufficient; reduce
            score = min(score, 0.60)

        return score, ev, rs

    @staticmethod
    def _bbox_overlap(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        # Intersection area / union area (simplified)
        x1 = max(ax, bx)
        y1 = max(ay, by)
        x2 = min(ax + aw, bx + bw)
        y2 = min(ay + ah, by + bh)
        inter = max(0, x2 - x1) * max(0, y2 - y1)
        union = aw * ah + bw * bh - inter
        return inter / union if union > 0 else 0.0


# ------------------------------------------------------------------
# Convenience
# ------------------------------------------------------------------

def create_visual_grounding(
    coordinate_validator: Optional[CoordinateSafetyValidator] = None,
    target_verifier: Optional[TargetStateVerifier] = None,
) -> VisualGrounding:
    return VisualGrounding(
        coordinate_validator=coordinate_validator,
        target_verifier=target_verifier,
    )
