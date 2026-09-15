"""
Target-specific computer-action verification.

Verifies that a computer action was applied to the INTENDED TARGET rather than
relying primarily on global foreground/focus state. A click may fully succeed
even when focus moves elsewhere immediately afterward; conversely, a PyAutoGUI
call that returns without exception proves only ACTION_EXECUTED, never
ACTION_VERIFIED.

Semantics (used throughout VERONICA):

    ACTION_EXECUTED  + VERIFIED      -> SUCCESS
    ACTION_EXECUTED  + UNKNOWN       -> UNKNOWN   (recovery policy may decide)
    ACTION_EXECUTED  + FAILED        -> FAILURE   (recovery policy may apply)
    ACTION NOT EXECUTED              -> BLOCKED/FAILED

The verifier captures a stable TargetReference BEFORE the action and re-locates
the target AFTER the action, comparing stable identity signals (control type,
automation id, class name, semantic name, owning window, bounding box). Global
focus and generic screen change are, at most, weak supporting evidence — never
sufficient on their own to produce VERIFIED.

Key principles:
- PyAutoGUI success alone can never produce VERIFIED/SUCCESS.
- Generic screen change can never produce VERIFIED/SUCCESS.
- Global focus is supporting evidence, not the universal success criterion.
- No silent guesses: insufficient evidence yields UNKNOWN.
- Identity uses sha256 signatures, never Python's built-in hash().
- Sensitive element values / text content are never stored or logged.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from veronica.logging_setup import get_logger
from veronica.vision.screen_state import ScreenState, UIElement

logger = get_logger("vision.target_verification")


class ActionVerificationStatus(str, Enum):
    """Structured outcome of physical execution plus target verification.

    Order matters: higher states represent more confidence. The value strings
    are the stable contract used in logs, events, and the Stitch frontend.
    """
    BLOCKED = "action_blocked"              # Never reached physical execution
    FAILED = "action_failed"                # Execution raised, or wrong-target evidence
    EXECUTED = "action_executed"            # Ran, but verification not established
    UNKNOWN = "verification_unknown"        # Ran; evidence insufficient to confirm
    VERIFIED = "verification_confirmed"     # Ran; target-specific evidence confirms


def status_from_value(value: Any) -> Optional["ActionVerificationStatus"]:
    """Resolve a status value string back to the enum (or None)."""
    try:
        return ActionVerificationStatus(value) if value is not None else None
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class TargetReference:
    """Stable identity of the intended target, captured BEFORE the action.

    Deliberately excludes element `value` and any text content so sensitive
    screen data is never stored or logged.
    """
    element_id: Optional[str] = None
    target_name: Optional[str] = None
    control_type: Optional[str] = None
    automation_id: Optional[str] = None
    class_name: Optional[str] = None
    owning_process_id: Optional[int] = None
    owning_window_id: Optional[str] = None
    owning_window_title: Optional[str] = None
    bounding_box: Optional[Dict[str, int]] = None
    target_point: Optional[Tuple[int, int]] = None

    @property
    def has_target_identity(self) -> bool:
        """Whether any target-specific identity signal is present."""
        return bool(
            self.element_id
            or self.target_name
            or self.control_type
            or self.automation_id
            or self.class_name
        )

    @property
    def has_owning_window_info(self) -> bool:
        """Whether owning-window signals were captured before the action."""
        return bool(
            self.owning_process_id is not None
            or self.owning_window_id is not None
            or self.owning_window_title
        )

    def identity_signature(self) -> str:
        """Stable sha256 signature of identity signals.

        Excludes position-based element_id (unstable across observations) and
        bounding box (can shift). Uses sha256, never Python's built-in hash().
        """
        signature = "|".join([
            (self.control_type or "").strip().lower(),
            (self.automation_id or "").strip() or "",
            (self.class_name or "").strip().lower(),
            (self.target_name or "").strip().lower(),
            str(self.owning_process_id if self.owning_process_id is not None else ""),
            self.owning_window_id or "",
        ])
        return hashlib.sha256(signature.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        """Serializable representation. No element values / text content."""
        return {
            "element_id": self.element_id,
            "target_name": self.target_name,
            "control_type": self.control_type,
            "automation_id": self.automation_id,
            "class_name": self.class_name,
            "owning_process_id": self.owning_process_id,
            "owning_window_id": self.owning_window_id,
            "owning_window_title": self.owning_window_title,
            "bounding_box": dict(self.bounding_box) if self.bounding_box else None,
            "target_point": self.target_point,
            "identity_signature": self.identity_signature(),
        }


@dataclass
class VerificationResult:
    """Outcome of target-specific verification."""
    status: ActionVerificationStatus
    confidence: float
    executed: bool = False
    reasons: List[str] = field(default_factory=list)    # why not stronger
    evidence: List[str] = field(default_factory=list)   # supporting signals
    located: Optional[UIElement] = None
    reference: Optional[TargetReference] = None

    @property
    def is_verified(self) -> bool:
        return self.status == ActionVerificationStatus.VERIFIED

    @property
    def is_success(self) -> bool:
        return self.executed and self.is_verified

    def to_dict(self) -> Dict[str, Any]:
        """Serializable result. Located element serialized WITHOUT its value."""
        located_identity = None
        if self.located is not None:
            located_identity = {
                "element_id": self.located.element_id,
                "name": self.located.name,
                "control_type": self.located.control_type,
                "automation_id": self.located.automation_id,
                "class_name": self.located.class_name,
                "bounding_box": dict(self.located.bounding_box),
            }
        return {
            "status": self.status.value,
            "confidence": round(self.confidence, 3),
            "executed": self.executed,
            "is_verified": self.is_verified,
            "reasons": list(self.reasons),
            "evidence": list(self.evidence),
            "located_identity": located_identity,
            "reference": self.reference.to_dict() if self.reference else None,
        }


# ----------------------------------------------------------------------
# Matching helpers (pure, deterministic)
# ----------------------------------------------------------------------

def _same_str(a: Optional[str], b: Optional[str]) -> bool:
    """Case-insensitive equality for non-empty strings."""
    if not a or not b:
        return False
    return a.strip().lower() == b.strip().lower()


def _name_matches(target: str, candidate: str) -> bool:
    """Loose semantic name match (equal or containment either direction)."""
    t = target.strip().lower()
    c = candidate.strip().lower()
    if not t or not c:
        return False
    return t == c or t in c or c in t


def _title_contains(target: str, candidate: Optional[str]) -> bool:
    if not target or not candidate:
        return False
    return target.strip().lower() in candidate.strip().lower()


def _box_contains_point(bb: Optional[Dict[str, int]],
                        point: Optional[Tuple[int, int]]) -> bool:
    if not bb or point is None:
        return False
    width = bb.get("width", 0)
    height = bb.get("height", 0)
    if width <= 0 or height <= 0:
        return False
    x, y = point
    return bb["x"] <= x < bb["x"] + width and bb["y"] <= y < bb["y"] + height


def _iter_elements(elements: Optional[List[UIElement]]):
    """Depth-first traversal of elements including children."""
    for element in elements or []:
        yield element
        if element.children:
            yield from _iter_elements(element.children)


class TargetStateVerifier:
    """
    Compares post-action perception against a pre-action target reference using
    stable, target-specific signals.

    Global focus and generic screen change are weak supporting evidence only.
    """

    # Identity signal weights (only count signals the reference actually knows).
    _WEIGHT_CONTROL_TYPE = 0.30
    _WEIGHT_AUTOMATION_ID = 0.25
    _WEIGHT_CLASS_NAME = 0.15
    _WEIGHT_NAME = 0.15
    _WEIGHT_ELEMENT_ID = 0.15

    # Strong identity is required above this score (0..1).
    _STRONG_IDENTITY = 0.8
    # Partial identity (needs supporting focus+window evidence to verify).
    _PARTIAL_IDENTITY = 0.5

    def __init__(
        self,
        locate_target: Optional[Callable[[ScreenState, TargetReference], Optional[UIElement]]] = None,
        large_displacement_threshold: float = 0.75,
    ):
        """
        Args:
            locate_target: Optional custom target locator. Signature:
                locate_target(after_state, reference) -> Optional[UIElement].
                Used first when provided; the default locator is the fallback.
            large_displacement_threshold: relative center displacement above
                which the target is considered relocated (identity alone then
                requires strong supporting evidence to verify).
        """
        self._locate_override = locate_target
        self.large_displacement_threshold = large_displacement_threshold

    # ------------------------------------------------------------------
    # Reference capture (BEFORE action)
    # ------------------------------------------------------------------

    def capture_reference(self, action: Any, state: Optional[ScreenState] = None) -> Optional[TargetReference]:
        """Capture a target reference from the action + pre-action state."""
        if action is None:
            return None

        element = getattr(action, "target_element", None)
        if element is None and state is not None:
            element = self._find_element_in_state(state, action)

        point = None
        coordinates = getattr(action, "coordinates", None)
        if coordinates:
            point = (int(coordinates[0]), int(coordinates[1]))

        if element is not None:
            return self._reference_from_element(element, state, point)

        if getattr(action, "target_name", None) or point:
            return TargetReference(
                target_name=getattr(action, "target_name", None),
                target_point=point,
                owning_process_id=state.active_window.process_id if state and state.active_window else None,
                owning_window_id=state.active_window.window_id if state and state.active_window else None,
                owning_window_title=state.active_window.title if state and state.active_window else None,
            )
        return None

    def _reference_from_element(self, element: UIElement, state: Optional[ScreenState],
                                point: Optional[Tuple[int, int]]) -> TargetReference:
        return TargetReference(
            element_id=element.element_id,
            target_name=element.name,
            control_type=element.control_type,
            automation_id=element.automation_id,
            class_name=element.class_name,
            owning_process_id=state.active_window.process_id if state and state.active_window else None,
            owning_window_id=state.active_window.window_id if state and state.active_window else None,
            owning_window_title=state.active_window.title if state and state.active_window else None,
            bounding_box=dict(element.bounding_box) if element.bounding_box else None,
            target_point=point,
        )

    def _find_element_in_state(self, state: ScreenState, action: Any) -> Optional[UIElement]:
        """Locate the action's semantic target inside a ScreenState."""
        target_name = getattr(action, "target_name", None)
        control_type = getattr(action, "target_control_type", None)
        if not target_name:
            return None
        best = None
        for element in _iter_elements(state.desktop_elements or []):
            if not element.name:
                continue
            if _name_matches(target_name, element.name):
                if control_type is None or _same_str(control_type, element.control_type):
                    return element
                if best is None:
                    best = element
        return best

    # ------------------------------------------------------------------
    # Locate after action
    # ------------------------------------------------------------------

    def locate_target(self, after_state: ScreenState, reference: TargetReference) -> Optional[UIElement]:
        """Re-locate the target in the post-action state.

        Strategy:
        1. Custom locator (if provided).
        2. Recursive identity search over desktop elements (best identity match).
        3. Coordinate-containment fallback for point-based targets.
        4. Focused element as a candidate.
        """
        if self._locate_override is not None:
            try:
                candidate = self._locate_override(after_state, reference)
                if candidate is not None:
                    return candidate
            except Exception as e:
                logger.warning("Custom target locator failed: %s", e)

        best = None
        best_score = 0.0
        for element in _iter_elements(after_state.desktop_elements or []):
            score, conflict, _ = self._identity_matches(reference, element)
            if conflict:
                continue  # A hard-conflicting candidate is not the target.
            if reference.automation_id and element.automation_id \
                    and reference.automation_id == element.automation_id:
                return element  # Exact automation identity is decisive.
            if score > best_score:
                best, best_score = element, score

        if best is not None and best_score >= self._PARTIAL_IDENTITY:
            return best

        # Coordinate containment fallback.
        if reference.target_point:
            for element in _iter_elements(after_state.desktop_elements or []):
                if _box_contains_point(element.bounding_box, reference.target_point):
                    return element

        # Focused element as candidate.
        focused = after_state.focused_element
        if focused is not None:
            score, conflict, _ = self._identity_matches(reference, focused)
            if not conflict and score >= self._PARTIAL_IDENTITY:
                return focused
            if reference.target_point and _box_contains_point(focused.bounding_box, reference.target_point):
                return focused

        return None

    # ------------------------------------------------------------------
    # Verify
    # ------------------------------------------------------------------

    def verify(
        self,
        action: Any,
        reference: Optional[TargetReference],
        after_state: Optional[ScreenState],
        executed: bool = True,
    ) -> VerificationResult:
        """
        Verify the action against the intended target.

        Args:
            action: The action that was attempted.
            reference: TargetReference captured before the action (may be None).
            after_state: ScreenState observed after the action (may be None).
            executed: Whether the physical action actually ran. When False the
                result is BLOCKED — verification never supersedes execution.

        Returns:
            VerificationResult with a structured status. Result may be VERIFIED,
            UNKNOWN, or FAILED — but never SUCCESS from PyAutoGUI success alone.
        """
        if not executed:
            return VerificationResult(
                status=ActionVerificationStatus.BLOCKED,
                confidence=0.0,
                executed=False,
                reasons=["action was not executed; nothing to verify"],
            )

        if reference is None:
            return VerificationResult(
                status=ActionVerificationStatus.UNKNOWN,
                confidence=0.0,
                executed=True,
                reasons=["no target reference captured before the action"],
            )
        if after_state is None:
            return VerificationResult(
                status=ActionVerificationStatus.UNKNOWN,
                confidence=0.0,
                executed=True,
                reasons=["no after-action observation available"],
            )
        if not reference.has_target_identity and reference.target_point is None:
            return VerificationResult(
                status=ActionVerificationStatus.UNKNOWN,
                confidence=0.0,
                executed=True,
                reasons=["reference carries no target identity or location to verify"],
            )

        located = self.locate_target(after_state, reference)
        owning_present = self._owning_window_present(after_state, reference)
        window_changed = reference.has_owning_window_info and not owning_present

        # Target could not be re-located.
        if located is None:
            if window_changed:
                return VerificationResult(
                    status=ActionVerificationStatus.FAILED,
                    confidence=0.3,
                    executed=True,
                    reference=reference,
                    reasons=["the target's owning window disappeared after the action"],
                )
            return VerificationResult(
                status=ActionVerificationStatus.UNKNOWN,
                confidence=0.15,
                executed=True,
                reference=reference,
                reasons=["target could not be re-located after the action; evidence insufficient"],
            )

        score, conflict, matched = self._identity_matches(reference, located)

        if conflict:
            return VerificationResult(
                status=ActionVerificationStatus.FAILED,
                confidence=0.25,
                executed=True,
                located=located,
                reference=reference,
                reasons=["target identity mismatch after the action (stable property conflict)"],
            )

        if window_changed:
            return VerificationResult(
                status=ActionVerificationStatus.FAILED,
                confidence=0.3,
                executed=True,
                located=located,
                reference=reference,
                reasons=["owning window changed unexpectedly after the action"],
            )

        displacement = self._rectangle_displacement(reference, located)
        displaced = displacement > self.large_displacement_threshold
        focus_matching = self._focus_matches(after_state, reference)

        evidence = list(matched)
        if owning_present:
            evidence.append("owning window still present after action")
        if focus_matching:
            evidence.append("focused element is the target (supporting evidence)")
        if not displaced:
            evidence.append("target remains at expected location")

        if score >= self._STRONG_IDENTITY:
            if displaced and not focus_matching:
                return VerificationResult(
                    status=ActionVerificationStatus.UNKNOWN,
                    confidence=0.5,
                    executed=True,
                    located=located,
                    reference=reference,
                    evidence=evidence,
                    reasons=["target relocated significantly; identity preserved but supporting evidence insufficient"],
                )
            confidence = min(0.95, 0.6 + score * 0.4)
            return VerificationResult(
                status=ActionVerificationStatus.VERIFIED,
                confidence=confidence,
                executed=True,
                located=located,
                reference=reference,
                evidence=evidence,
                reasons=["target identity and window context consistent after the action"],
            )

        if score >= self._PARTIAL_IDENTITY:
            if focus_matching and owning_present:
                evidence.append("focused on candidate within its original owning window")
                return VerificationResult(
                    status=ActionVerificationStatus.VERIFIED,
                    confidence=0.65,
                    executed=True,
                    located=located,
                    reference=reference,
                    evidence=evidence,
                    reasons=["partial target identity supported by focus and window evidence"],
                )
            return VerificationResult(
                status=ActionVerificationStatus.UNKNOWN,
                confidence=0.4,
                executed=True,
                located=located,
                reference=reference,
                evidence=evidence,
                reasons=["only partial target identity evidence; cannot confirm the intended target was acted upon"],
            )

        return VerificationResult(
            status=ActionVerificationStatus.UNKNOWN,
            confidence=0.2,
            executed=True,
            located=located,
            reference=reference,
            evidence=evidence,
            reasons=["located candidate does not strongly match the target identity"],
        )

    # ------------------------------------------------------------------
    # Identity / context signals
    # ------------------------------------------------------------------

    def _identity_matches(self, reference: TargetReference, element: UIElement
                          ) -> Tuple[float, bool, List[str]]:
        """Score identity match 0..1 plus a hard-conflict flag + matched signals.

        Hard conflicts (control type / automation id / class name differ while
        both are known) mean the located element is a different control.
        """
        total = 0.0
        matched = 0.0
        conflict = False
        descriptions = []
        name = element.name
        control_type = element.control_type
        automation_id = element.automation_id
        class_name = element.class_name

        if reference.control_type:
            total += self._WEIGHT_CONTROL_TYPE
            if _same_str(reference.control_type, control_type):
                matched += self._WEIGHT_CONTROL_TYPE
                descriptions.append("same control type")
            else:
                conflict = True
        if reference.automation_id:
            total += self._WEIGHT_AUTOMATION_ID
            if automation_id and _same_str(reference.automation_id, automation_id):
                matched += self._WEIGHT_AUTOMATION_ID
                descriptions.append("same automation identity")
            elif automation_id:
                conflict = True
        if reference.class_name:
            total += self._WEIGHT_CLASS_NAME
            if class_name and _same_str(reference.class_name, class_name):
                matched += self._WEIGHT_CLASS_NAME
                descriptions.append("same control class")
            elif class_name:
                conflict = True
        if reference.target_name:
            total += self._WEIGHT_NAME
            if name and _name_matches(reference.target_name, name):
                matched += self._WEIGHT_NAME
                descriptions.append("same semantic name")
        if reference.element_id:
            total += self._WEIGHT_ELEMENT_ID
            if element.element_id and reference.element_id == element.element_id:
                matched += self._WEIGHT_ELEMENT_ID
                descriptions.append("same element identity")

        if total <= 0:
            return 0.0, conflict, descriptions
        return matched / total, conflict, descriptions

    def _rectangle_displacement(self, reference: TargetReference,
                                located: UIElement) -> float:
        """Relative center displacement of the located box vs reference box.

        Returns relative distance (distance / reference diagonal). Missing or
        degenerate boxes produce 0.0 (no displacement evidence).
        """
        rb = reference.bounding_box
        lb = located.bounding_box
        if not rb or not lb:
            return 0.0
        if rb.get("width", 0) <= 0 or rb.get("height", 0) <= 0:
            return 0.0
        if lb.get("width", 0) <= 0 or lb.get("height", 0) <= 0:
            return 0.0
        rcx = rb["x"] + rb["width"] / 2.0
        rcy = rb["y"] + rb["height"] / 2.0
        lcx = lb["x"] + lb["width"] / 2.0
        lcy = lb["y"] + lb["height"] / 2.0
        distance = math.hypot(lcx - rcx, lcy - rcy)
        diagonal = math.hypot(rb["width"], rb["height"]) or 1.0
        return distance / diagonal

    def _focus_matches(self, after_state: ScreenState, reference: TargetReference) -> bool:
        focused = after_state.focused_element
        if focused is None:
            return False
        if reference.automation_id and focused.automation_id \
                and reference.automation_id == focused.automation_id:
            return True
        if reference.control_type and reference.target_name \
                and focused.control_type and focused.name \
                and _same_str(reference.control_type, focused.control_type) \
                and _name_matches(reference.target_name, focused.name):
            return True
        if reference.target_name and focused.name \
                and reference.target_name.strip().lower() == focused.name.strip().lower():
            return True
        return False

    def _window_matches_ref(self, info: Any, reference: TargetReference) -> bool:
        if reference.owning_process_id is not None and getattr(info, "process_id", None) is not None:
            if info.process_id == reference.owning_process_id:
                return True
        if reference.owning_window_id is not None and getattr(info, "window_id", None) is not None:
            if info.window_id == reference.owning_window_id:
                return True
        if reference.owning_window_title and _title_contains(reference.owning_window_title, getattr(info, "title", None)):
            return True
        return False

    def _owning_window_present(self, after_state: ScreenState, reference: TargetReference) -> bool:
        """Whether the target's owning window still exists after the action.

        Absence can only be asserted when the window list was actually observed;
        an unobserved window list never produces a false FAILED.
        """
        if not reference.has_owning_window_info:
            return True
        active = after_state.active_window
        if active is not None and self._window_matches_ref(active, reference):
            return True
        for window in after_state.visible_windows or []:
            if self._window_matches_ref(window, reference):
                return True
        if not after_state.visible_windows and active is None:
            return True  # No window observation to prove absence.
        return False


def create_target_state_verifier(
    locate_target: Optional[Callable[[ScreenState, TargetReference], Optional[UIElement]]] = None,
    large_displacement_threshold: float = 0.75,
) -> TargetStateVerifier:
    """Factory for TargetStateVerifier."""
    return TargetStateVerifier(
        locate_target=locate_target,
        large_displacement_threshold=large_displacement_threshold,
    )