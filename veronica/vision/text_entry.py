"""Target-specific text-entry verification for VERONICA TEST 4.

Determines whether typed disposable test text ('VERONICA TEST 123') was
entered into the intended Notepad Text editor by comparing target identity
(above) plus the exact text value (below), never relying on generic screen
change or PyAutoGUI success alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

from veronica.vision.screen_state import ScreenState, UIElement
from veronica.vision.target_verification import (
    TargetStateVerifier,
    TargetReference,
    VerificationResult,
    ActionVerificationStatus,
    create_target_state_verifier,
)
from veronica.vision.perception import PerceptionLayer


EXPECTED_TEST_TEXT = "VERONICA TEST 123"


@dataclass
class TextVerificationResult:
    """Structured outcome of a text-entry action.

    `observed_text` and `expected_text` are included ONLY for comparison.
    The result's `to_dict()` NEVER serializes arbitrary document content,
    and never includes any value other than the known-test string (if it
    matches expected). Sensitive screen text is excluded by design.
    """
    status: str  # SUCCESS / UNKNOWN / FAILED / BLOCKED
    executed: bool = False
    exact_match: bool = False
    expected_text: Optional[str] = None
    observed_text: Optional[str] = None  # only set when it equals expected
    located_element: Optional[UIElement] = None
    reference: Optional[TargetReference] = None
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        # Sanitize reasons: never include raw document text
        safe_reasons = [r for r in self.reasons if "value '" not in r and "content '" not in r]
        d: Dict[str, Any] = {
            "status": self.status,
            "executed": self.executed,
            "exact_match": self.exact_match,
            "expected_text": self.expected_text,
            "reasons": safe_reasons,
        }
        # Only expose observed text if it matches the harmless test string
        if self.observed_text == EXPECTED_TEST_TEXT:
            d["observed_text"] = EXPECTED_TEST_TEXT
        else:
            d["observed_text"] = None  # redacted / not exposed
        # Never serialize element value fields from located_element
        if self.located_element is not None:
            d["located_identity"] = {
                "element_id": self.located_element.element_id,
                "name": self.located_element.name,
                "control_type": self.located_element.control_type,
                "automation_id": self.located_element.automation_id,
                "class_name": self.located_element.class_name,
            }
        if self.reference is not None:
            d["reference"] = self.reference.to_dict()
        return d


def normalize_rectangle(bb: Optional[Dict[str, Any]]) -> Optional[Dict[str, int]]:
    if bb is None:
        return None
    return {
        "x": int(bb.get("x", 0)),
        "y": int(bb.get("y", 0)),
        "width": max(1, int(bb.get("width", 0))),
        "height": max(1, int(bb.get("height", 0))),
    }


class TextStateVerifier:
    """Verifies typed disposable test text against an intended editor."""

    def __init__(self, target_verifier: Optional[TargetStateVerifier] = None):
        self.target_verifier = target_verifier or create_target_state_verifier()

    def verify(
        self,
        action: Any,
        reference: Optional[TargetReference],
        after_state: Optional[ScreenState],
        executed: bool = True,
        expected_text: str = EXPECTED_TEST_TEXT,
    ) -> TextVerificationResult:
        if not executed:
            return TextVerificationResult(
                status="BLOCKED", executed=False, reasons=["action not executed"],
                expected_text=expected_text,
            )
        if after_state is None:
            return TextVerificationResult(
                status="UNKNOWN", executed=True, exact_match=False,
                expected_text=expected_text,
                reasons=["no post-action observation"],
                reference=reference,
            )

        # 1. Locate editor via target verifier
        located = None
        if self.target_verifier is not None and reference is not None:
            located = self.target_verifier.locate_target(after_state, reference)

        # 1a. Identity-conflict check: if the located element has conflicting
        # identity signals with the reference (different automation_id, class,
        # or control type), it is the WRONG target.
        identity_conflict = False
        if located is not None and reference is not None:
            if (reference.automation_id and located.automation_id
                    and reference.automation_id != located.automation_id):
                identity_conflict = True
            elif (reference.class_name and located.class_name
                    and reference.class_name.lower() != located.class_name.lower()):
                identity_conflict = True

        if identity_conflict:
            return TextVerificationResult(
                status="FAILED",
                executed=True,
                exact_match=False,
                expected_text=expected_text,
                observed_text=None,
                located_element=located,
                reference=reference,
                reasons=["located element has conflicting identity with the intended target (wrong control)"],
            )

        # 2. Read exact text from the located editor (UIA value property)
        observed_text = None
        if located is not None and located.value is not None:
            observed_text = str(located.value)

        # If editor not located via verifier, try focused element (supporting only)
        if located is None and after_state.focused_element is not None:
            fe = after_state.focused_element
            # Confirm it is the intended control type / class (identity match)
            if reference is not None and reference.control_type and fe.control_type == reference.control_type:
                located = fe
                if fe.value is not None:
                    observed_text = str(fe.value)

        # 3. Exact-text comparison — primary evidence
        exact_match = (observed_text == expected_text)

        if exact_match:
            # Confirm owning-window / identity context as supporting evidence
            reasons = ["exact expected text verified in editor value property"]
            if located is not None:
                reasons.append(f"editor located: {located.name} ({located.control_type})")
            return TextVerificationResult(
                status="SUCCESS",
                executed=True,
                exact_match=True,
                expected_text=expected_text,
                observed_text=expected_text,
                located_element=located,
                reference=reference,
                reasons=reasons,
            )

        # Not matched — determine if wrong target or just missing
        if located is None:
            return TextVerificationResult(
                status="UNKNOWN", executed=True, exact_match=False,
                expected_text=expected_text,
                observed_text=observed_text,
                located_element=None,
                reference=reference,
                reasons=["editor could not be re-located; text not verifiable"],
            )
        # Editor found but text differs -> wrong/different content
        return TextVerificationResult(
            status="FAILED", executed=True, exact_match=False,
            expected_text=expected_text,
            observed_text=observed_text,
            located_element=located,
            reference=reference,
            reasons=[
                f"editor located ({located.name}) but value '{observed_text or ''}' != expected '{expected_text}'",
            ],
        )


def find_editor_target(perception_layer: PerceptionLayer) -> Optional[UIElement]:
    """Obtain the Notepad Text editor element via UIA (get_element_at_point
    or focused_element), since desktop_elements excludes deep descendants."""
    adapter = getattr(perception_layer, "uia_provider", None)
    if adapter is not None and hasattr(adapter, "_adapter") and adapter._adapter and adapter._adapter.is_available():
        # Try to find a focused Edit control; if unavailable, return None
        focused = adapter._adapter.get_focused_element()
        if focused is not None and focused.control_type == "Edit":
            return focused  # type: ignore[return-value]
    # Fallback through perception layer state
    state = perception_layer.get_current_state()
    if state.focused_element is not None and state.focused_element.control_type == "Edit":
        return state.focused_element
    return None
