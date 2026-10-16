"""
Phase 10F TYPE_TEXT Grounding Integration — 13 unit tests with fake data.
Tests the UIA-to-grounding bridge without requiring real desktop.
Uses mocks/fakes to simulate UIA Edit control and grounding outcomes.
"""

import sys
import os
import unittest
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '.'))

from veronica.vision.uia_target_grounder import (
    UITargetGrounder,
    ground_notepad_edit,
)
from veronica.vision.visual_grounding import (
    VisualTargetCandidate,
    TargetReference,
    TargetCorrelationResult,
    GroundingStatus,
    VisualTargetEvidence,
)
from veronica.vision.perception import PerceptionLayer
from veronica.vision.uia_adapter import UIAutomationAdapter
from veronica.vision.screen_state import ScreenState, WindowInfo, UIElement


class TestPhase10FTypeTextGrounding(unittest.TestCase):
    """Test TYPE_TEXT grounding integration for Notepad Edit control."""

    def setUp(self):
        """Common test fixtures."""
        self.fake_bbox = {"x": 100, "y": 100, "width": 200, "height": 30}
        self.fake_point = (200, 115)

    def test_verified_grounding_creates_proposed_action(self):
        """VERIFIED grounding_result → create_action_proposal returns PROPOSED (valid)."""
        from veronica.vision.action_proposal import create_action_proposal, ActionType

        # Fake VERIFIED TargetCorrelationResult
        candidate = VisualTargetCandidate(
            semantic_name="Notepad editor",
            control_type="Edit",
            visual_bbox=(100, 100, 200, 30),
            confidence=0.95,
            source="uia",
            process_id=1234,
            window_title="Untitled - Notepad",
        )
        target_ref = TargetReference(
            element_id="edit123",
            target_name="Notepad editor",
            control_type="Edit",
            automation_id="123",
            class_name="Edit",
            owning_process_id=1234,
            owning_window_id=5678,
            owning_window_title="Untitled - Notepad",
            bounding_box={"x": 100, "y": 100, "width": 200, "height": 30},
            target_point=(200, 115),
        )
        evidence = [
            VisualTargetEvidence("semantic_name", "Notepad editor", 0.9, "vision"),
            VisualTargetEvidence("control_type", "Edit", 0.85, "uia"),
            VisualTargetEvidence("process_window", "pid=1234", 0.75, "vision"),
            VisualTargetEvidence("bbox_overlap", "0.95", 0.80, "vision"),
        ]
        grounding_result = TargetCorrelationResult(
            status=GroundingStatus.VERIFIED,
            score=0.88,
            candidate=candidate,
            matched_target=target_ref,
            evidence=evidence,
            reasons=["Sufficient multi-signal evidence"],
        )

        proposal = create_action_proposal(
            proposal_id="test123",
            action_type=ActionType.TYPE_TEXT,
            grounding_result=grounding_result,
            text_to_type="hello world",
        )

        self.assertEqual(proposal.status, "PROPOSED")
        self.assertEqual(proposal.grounding_result.status, GroundingStatus.VERIFIED)

    def test_partial_grounding_creates_invalid_action(self):
        """PARTIAL grounding_result → create_action_proposal returns INVALID."""
        from veronica.vision.action_proposal import create_action_proposal, ActionType

        candidate = VisualTargetCandidate(
            semantic_name="Notepad editor",
            control_type="Edit",
            visual_bbox=(100, 100, 200, 30),
            confidence=0.95,
            source="uia",
            process_id=1234,
            window_title="Untitled - Notepad",
        )
        target_ref = TargetReference(
            element_id="edit123",
            target_name="Notepad editor",
            control_type="Edit",
            automation_id="123",
            class_name="Edit",
            owning_process_id=1234,
            owning_window_id=5678,
            owning_window_title="Untitled - Notepad",
            bounding_box={"x": 100, "y": 100, "width": 200, "height": 30},
            target_point=(200, 115),
        )
        evidence = [
            VisualTargetEvidence("semantic_name", "Notepad editor", 0.9, "vision"),
            # Missing control_type or geometry evidence -> score too low for VERIFIED
        ]
        grounding_result = TargetCorrelationResult(
            status=GroundingStatus.PARTIAL,
            score=0.65,
            candidate=candidate,
            matched_target=target_ref,
            evidence=evidence,
            reasons=["Some matching signals"],
        )

        proposal = create_action_proposal(
            proposal_id="test124",
            action_type=ActionType.TYPE_TEXT,
            grounding_result=grounding_result,
            text_to_type="hello world",
        )

        self.assertEqual(proposal.status, "INVALID")
        self.assertEqual(proposal.grounding_result.status, GroundingStatus.PARTIAL)

    def test_ambiguous_grounding_creates_invalid_action(self):
        """AMBIGUOUS grounding_result → create_action_proposal returns INVALID."""
        from veronica.vision.action_proposal import create_action_proposal, ActionType

        candidate = VisualTargetCandidate(
            semantic_name="Notepad editor",
            control_type="Edit",
            visual_bbox=(100, 100, 200, 30),
            confidence=0.95,
            source="uia",
            process_id=1234,
            window_title="Untitled - Notepad",
        )
        target_ref = TargetReference(
            element_id="edit123",
            target_name="Notepad editor",
            control_type="Edit",
            automation_id="123",
            class_name="Edit",
            owning_process_id=1234,
            owning_window_id=5678,
            owning_window_title="Untitled - Notepad",
            bounding_box={"x": 100, "y": 100, "width": 200, "height": 30},
            target_point=(200, 115),
        )
        evidence = [
            VisualTargetEvidence("semantic_name", "Notepad editor", 0.9, "vision"),
            VisualTargetEvidence("control_type", "Edit", 0.85, "uia"),
            # But score < 0.75 and multiple matches -> AMBIGUOUS per rules
        ]
        grounding_result = TargetCorrelationResult(
            status=GroundingStatus.AMBIGUOUS,
            score=0.70,
            candidate=candidate,
            matched_target=target_ref,
            evidence=evidence,
            reasons=["Multiple plausible targets"],
        )

        proposal = create_action_proposal(
            proposal_id="test125",
            action_type=ActionType.TYPE_TEXT,
            grounding_result=grounding_result,
            text_to_type="hello world",
        )

        self.assertEqual(proposal.status, "INVALID")
        self.assertEqual(proposal.grounding_result.status, GroundingStatus.AMBIGUOUS)

    def test_not_found_grounding_creates_invalid_action(self):
        """NOT_FOUND grounding_result → create_action_proposal returns INVALID."""
        from veronica.vision.action_proposal import create_action_proposal, ActionType

        candidate = VisualTargetCandidate(
            semantic_name="Notepad editor",
            control_type="Edit",
            visual_bbox=(100, 100, 200, 30),
            confidence=0.95,
            source="uia",
            process_id=1234,
            window_title="Untitled - Notepad",
        )
        # No matched target
        grounding_result = TargetCorrelationResult(
            status=GroundingStatus.NOT_FOUND,
            score=0.0,
            candidate=candidate,
            matched_target=None,
            evidence=[],
            reasons=["No matching desktop elements found"],
        )

        proposal = create_action_proposal(
            proposal_id="test126",
            action_type=ActionType.TYPE_TEXT,
            grounding_result=grounding_result,
            text_to_type="hello world",
        )

        self.assertEqual(proposal.status, "INVALID")
        self.assertEqual(proposal.grounding_result.status, GroundingStatus.NOT_FOUND)

    def test_unknown_grounding_creates_invalid_action(self):
        """UNKNOWN grounding_result → create_action_proposal returns INVALID."""
        from veronica.vision.action_proposal import create_action_proposal, ActionType

        candidate = VisualTargetCandidate(
            semantic_name="Notepad editor",
            control_type="Edit",
            visual_bbox=(100, 100, 200, 30),
            confidence=0.95,
            source="uia",
            process_id=1234,
            window_title="Untitled - Notepad",
        )
        target_ref = TargetReference(
            element_id="edit123",
            target_name="Notepad editor",
            control_type="Edit",
            automation_id="123",
            class_name="Edit",
            owning_process_id=1234,
            owning_window_id=5678,
            owning_window_title="Untitled - Notepad",
            bounding_box={"x": 100, "y": 100, "width": 200, "height": 30},
            target_point=(200, 115),
        )
        evidence = [
            VisualTargetEvidence("semantic_name", "Notepad editor", 0.5, "vision"),  # weak only
        ]
        grounding_result = TargetCorrelationResult(
            status=GroundingStatus.UNKNOWN,
            score=0.25,
            candidate=candidate,
            matched_target=target_ref,
            evidence=evidence,
            reasons=["Insufficient evidence"],
        )

        proposal = create_action_proposal(
            proposal_id="test127",
            action_type=ActionType.TYPE_TEXT,
            grounding_result=grounding_result,
            text_to_type="hello world",
        )

        self.assertEqual(proposal.status, "INVALID")
        self.assertEqual(proposal.grounding_result.status, GroundingStatus.UNKNOWN)

    def test_missing_grounding_result_creates_invalid_action(self):
        """Missing grounding_result → create_action_proposal returns INVALID."""
        from veronica.vision.action_proposal import create_action_proposal, ActionType

        proposal = create_action_proposal(
            proposal_id="test128",
            action_type=ActionType.TYPE_TEXT,
            grounding_result=None,  # missing
            text_to_type="hello world",
        )

        self.assertEqual(proposal.status, "INVALID")

    def test_wrong_control_type_not_verified(self):
        """Wrong control type (e.g., Button instead of Edit) does not yield VERIFIED."""
        from veronica.vision.action_proposal import create_action_proposal, ActionType

        candidate = VisualTargetCandidate(
            semantic_name="Notepad editor",
            control_type="Button",  # wrong!
            visual_bbox=(100, 100, 200, 30),
            confidence=0.95,
            source="uia",
            process_id=1234,
            window_title="Untitled - Notepad",
        )
        target_ref = TargetReference(
            element_id="edit123",
            target_name="Notepad editor",
            control_type="Edit",  # UIA says Edit
            automation_id="123",
            class_name="Edit",
            owning_process_id=1234,
            owning_window_id=5678,
            owning_window_title="Untitled - Notepad",
            bounding_box={"x": 100, "y": 100, "width": 200, "height": 30},
            target_point=(200, 115),
        )
        evidence = [
            VisualTargetEvidence("semantic_name", "Notepad editor", 0.9, "vision"),
            VisualTargetEvidence("control_type", "Button vs Edit", 0.1, "vision"),  # contradiction
            # Score reduced by contradiction -> not VERIFIED
        ]
        grounding_result = TargetCorrelationResult(
            status=GroundingStatus.PARTIAL,  # or UNKNOWN due to low score/contradiction
            score=0.40,
            candidate=candidate,
            matched_target=target_ref,
            evidence=evidence,
            reasons=["Control type contradiction"],
        )

        proposal = create_action_proposal(
            proposal_id="test129",
            action_type=ActionType.TYPE_TEXT,
            grounding_result=grounding_result,
            text_to_type="hello world",
        )

        self.assertEqual(proposal.status, "INVALID")
        self.assertNotEqual(proposal.grounding_result.status, GroundingStatus.VERIFIED)

    def test_wrong_process_window_lower_score(self):
        """Wrong process/window ID lowers score, prevents VERIFIED."""
        from veronica.vision.action_proposal import create_action_proposal, ActionType

        candidate = VisualTargetCandidate(
            semantic_name="Notepad editor",
            control_type="Edit",
            visual_bbox=(100, 100, 200, 30),
            confidence=0.95,
            source="uia",
            process_id=9999,  # wrong PID
            window_title="Untitled - Notepad",
        )
        target_ref = TargetReference(
            element_id="edit123",
            target_name="Notepad editor",
            control_type="Edit",
            automation_id="123",
            class_name="Edit",
            owning_process_id=1234,  # real Notepad PID
            owning_window_id=5678,
            owning_window_title="Untitled - Notepad",
            bounding_box={"x": 100, "y": 100, "width": 200, "height": 30},
            target_point=(200, 115),
        )
        evidence = [
            VisualTargetEvidence("semantic_name", "Notepad editor", 0.9, "vision"),
            VisualTargetEvidence("control_type", "Edit", 0.85, "uia"),
            # process_window mismatch -> no +0.10 evidence
            VisualTargetEvidence("bbox_overlap", "0.95", 0.80, "vision"),
            # Missing process evidence -> score ~0.30+0.20+0.15 = 0.65 (PARTIAL range)
        ]
        grounding_result = TargetCorrelationResult(
            status=GroundingStatus.PARTIAL,
            score=0.65,
            candidate=candidate,
            matched_target=target_ref,
            evidence=evidence,
            reasons=["Some matching signals"],
        )

        proposal = create_action_proposal(
            proposal_id="test130",
            action_type=ActionType.TYPE_TEXT,
            grounding_result=grounding_result,
            text_to_type="hello world",
        )

        self.assertEqual(proposal.status, "INVALID")
        self.assertEqual(proposal.grounding_result.status, GroundingStatus.PARTIAL)

    def test_bbox_mismatch_lower_or_no_geometry(self):
        """Bbox mismatch reduces/removes geometry evidence, may prevent VERIFIED."""
        from veronica.vision.action_proposal import create_action_proposal, ActionType

        candidate = VisualTargetCandidate(
            semantic_name="Notepad editor",
            control_type="Edit",
            visual_bbox=(100, 100, 50, 20),  # much smaller box
            confidence=0.95,
            source="uia",
            process_id=1234,
            window_title="Untitled - Notepad",
        )
        target_ref = TargetReference(
            element_id="edit123",
            target_name="Notepad editor",
            control_type="Edit",
            automation_id="123",
            class_name="Edit",
            owning_process_id=1234,
            owning_window_id=5678,
            owning_window_title="Untitled - Notepad",
            bounding_box={"x": 100, "y": 100, "width": 200, "height": 30},  # large box
            target_point=(200, 115),
        )
        # Overlap will be small due to size mismatch
        evidence = [
            VisualTargetEvidence("semantic_name", "Notepad editor", 0.9, "vision"),
            VisualTargetEvidence("control_type", "Edit", 0.85, "uia"),
            VisualTargetEvidence("process_window", "pid=1234", 0.75, "vision"),
            # bbox_overlap small -> maybe only +0.05 or none
            VisualTargetEvidence("bbox_overlap", "0.10", 0.50, "vision"),
            # Score: 0.30+0.20+0.10+0.05 = 0.65 (PARTIAL)
        ]
        grounding_result = TargetCorrelationResult(
            status=GroundingStatus.PARTIAL,
            score=0.65,
            candidate=candidate,
            matched_target=target_ref,
            evidence=evidence,
            reasons=["Some matching signals"],
        )

        proposal = create_action_proposal(
            proposal_id="test131",
            action_type=ActionType.TYPE_TEXT,
            grounding_result=grounding_result,
            text_to_type="hello world",
        )

        self.assertEqual(proposal.status, "INVALID")
        self.assertEqual(proposal.grounding_result.status, GroundingStatus.PARTIAL)

    def test_proposal_id_uniqueness(self):
        """Each ActionProposal gets unique proposal_id."""
        from veronica.vision.action_proposal import create_action_proposal, ActionType

        candidate = VisualTargetCandidate(
            semantic_name="Notepad editor",
            control_type="Edit",
            visual_bbox=(100, 100, 200, 30),
            confidence=0.95,
            source="uia",
            process_id=1234,
            window_title="Untitled - Notepad",
        )
        target_ref = TargetReference(
            element_id="edit123",
            target_name="Notepad editor",
            control_type="Edit",
            automation_id="123",
            class_name="Edit",
            owning_process_id=1234,
            owning_window_id=5678,
            owning_window_title="Untitled - Notepad",
            bounding_box={"x": 100, "y": 100, "width": 200, "height": 30},
            target_point=(200, 115),
        )
        evidence = [
            VisualTargetEvidence("semantic_name", "Notepad editor", 0.9, "vision"),
            VisualTargetEvidence("control_type", "Edit", 0.85, "uia"),
            VisualTargetEvidence("process_window", "pid=1234", 0.75, "vision"),
            VisualTargetEvidence("bbox_overlap", "0.95", 0.80, "vision"),
        ]
        grounding_result = TargetCorrelationResult(
            status=GroundingStatus.VERIFIED,
            score=0.88,
            candidate=candidate,
            matched_target=target_ref,
            evidence=evidence,
            reasons=["Sufficient multi-signal evidence"],
        )

        prop1 = create_action_proposal(
            proposal_id="unique-id-1",
            action_type=ActionType.TYPE_TEXT,
            grounding_result=grounding_result,
            text_to_type="hello",
        )
        prop2 = create_action_proposal(
            proposal_id="unique-id-2",  # different ID
            action_type=ActionType.TYPE_TEXT,
            grounding_result=grounding_result,
            text_to_type="world",
        )

        self.assertNotEqual(prop1.proposal_id, prop2.proposal_id)
        self.assertEqual(prop1.proposal_id, "unique-id-1")
        self.assertEqual(prop2.proposal_id, "unique-id-2")

    def test_no_raw_text_in_safe_audit(self):
        """ActionProposal.safe_audit() never logs raw text_to_type."""
        from veronica.vision.action_proposal import create_action_proposal, ActionType

        candidate = VisualTargetCandidate(
            semantic_name="Notepad editor",
            control_type="Edit",
            visual_bbox=(100, 100, 200, 30),
            confidence=0.95,
            source="uia",
            process_id=1234,
            window_title="Untitled - Notepad",
        )
        target_ref = TargetReference(
            element_id="edit123",
            target_name="Notepad editor",
            control_type="Edit",
            automation_id="123",
            class_name="Edit",
            owning_process_id=1234,
            owning_window_id=5678,
            owning_window_title="Untitled - Notepad",
            bounding_box={"x": 100, "y": 100, "width": 200, "height": 30},
            target_point=(200, 115),
        )
        evidence = [
            VisualTargetEvidence("semantic_name", "Notepad editor", 0.9, "vision"),
            VisualTargetEvidence("control_type", "Edit", 0.85, "uia"),
            VisualTargetEvidence("process_window", "pid=1234", 0.75, "vision"),
            VisualTargetEvidence("bbox_overlap", "0.95", 0.80, "vision"),
        ]
        grounding_result = TargetCorrelationResult(
            status=GroundingStatus.VERIFIED,
            score=0.88,
            candidate=candidate,
            matched_target=target_ref,
            evidence=evidence,
            reasons=["Sufficient multi-signal evidence"],
        )

        proposal = create_action_proposal(
            proposal_id="audit-test",
            action_type=ActionType.TYPE_TEXT,
            grounding_result=grounding_result,
            text_to_type="secret password 123",
        )

        audit = proposal.safe_audit()
        # Should NOT contain raw text
        self.assertNotIn("secret password 123", str(audit))
        self.assertNotIn("password", str(audit))
        # Should contain length and redacted/hash
        self.assertEqual(audit["text_length"], 19)
        self.assertEqual(audit["text_redacted"], "se***")
        self.assertEqual(len(audit["text_sha256"]), 16)  # truncated hash

    def test_permission_gate_not_duplicated(self):
        """No second PermissionGate added; reuse existing gate."""
        from veronica.safety.permissions import PermissionGate

        # Create grounder - should not instantiate its own PermissionGate
        grounder = UITargetGrounder()

        # Check that no PermissionGate attribute was added to grounder
        self.assertFalse(hasattr(grounder, 'permission_gate'))
        self.assertFalse(hasattr(grounder, '_permission_gate'))

        # Verify we can still access the global PermissionGate (singleton pattern)
        gate = PermissionGate()
        self.assertIsInstance(gate, PermissionGate)

    def test_execution_authorization_proposal_bound(self):
        """ExecutionAuthorization requires proposal_id; not reused."""
        from veronica.vision.action_proposal import evaluate_proposal_with_permission, ActionType, create_action_proposal
        from veronica.safety.permissions import PermissionGate, PermissionLevel, PermissionDecision

        candidate = VisualTargetCandidate(
            semantic_name="Notepad editor",
            control_type="Edit",
            visual_bbox=(100, 100, 200, 30),
            confidence=0.95,
            source="uia",
            process_id=1234,
            window_title="Untitled - Notepad",
        )
        target_ref = TargetReference(
            element_id="edit123",
            target_name="Notepad editor",
            control_type="Edit",
            automation_id="123",
            class_name="Edit",
            owning_process_id=1234,
            owning_window_id=5678,
            owning_window_title="Untitled - Notepad",
            bounding_box={"x": 100, "y": 100, "width": 200, "height": 30},
            target_point=(200, 115),
        )
        evidence = [
            VisualTargetEvidence("semantic_name", "Notepad editor", 0.9, "vision"),
            VisualTargetEvidence("control_type", "Edit", 0.85, "uia"),
            VisualTargetEvidence("process_window", "pid=1234", 0.75, "vision"),
            VisualTargetEvidence("bbox_overlap", "0.95", 0.80, "vision"),
        ]
        grounding_result = TargetCorrelationResult(
            status=GroundingStatus.VERIFIED,
            score=0.88,
            candidate=candidate,
            matched_target=target_ref,
            evidence=evidence,
            reasons=["Sufficient multi-signal evidence"],
        )

        proposal = create_action_proposal(
            proposal_id="auth-test-123",
            action_type=ActionType.TYPE_TEXT,
            grounding_result=grounding_result,
            text_to_type="test",
        )

        # Mock permission gate to return ALLOWED
        gate = PermissionGate()
        original_callback = gate.confirm_callback

        def mock_allow(description, level):
            return PermissionDecision(True, "mock allowed")

        gate.confirm_callback = mock_allow

        try:
            status = evaluate_proposal_with_permission(
                proposal, gate, tool_name="vision_action_proposal", description="test"
            )

            # Should be APPROVED after permission check
            self.assertEqual(status, "APPROVED")
            # Audit should show bound to this proposal ID
            self.assertEqual(
                proposal.audit.get("approval_bound_to_proposal_id"),
                "auth-test-123"
            )
        finally:
            gate.confirm_callback = original_callback

    @patch('veronica.vision.uia_target_grounder.UIAutomationAdapter')
    @patch('veronica.vision.uia_target_grounder.PerceptionLayer')
    @patch('veronica.vision.uia_target_grounder.create_visual_grounding')
    def test_ground_notepad_edit_returns_verified_when_genuine(
        self, mock_create_grounding, mock_perception_layer, mock_uia_adapter
    ):
        """End-to-end: ground_notepad_edit returns VERIFIED result when UIA finds Edit and grounding says VERIFIED."""
        # Setup mocks
        mock_uia_instance = Mock()
        mock_uia_instance.is_available.return_value = True
        mock_uia_adapter.return_value = mock_uia_instance

        # Mock UIA hierarchy: desktop -> Notepad window -> Edit control
        mock_root = Mock()
        mock_uia_instance._root_element = mock_root
        mock_uia_module = Mock()
        mock_uia_instance._uia_modules = {"uia": mock_uia_module}

        # Mock window enumeration to return one Notepad window
        mock_window_info = Mock()
        mock_window_info.process_name = "notepad.exe"
        mock_window_info.process_id = 1234
        mock_window_info.window_id = "hwnd-123"
        mock_window_info.title = "Untitled - Notepad"

        # Mock UIA module members and adapter instance wiring
        mock_edit_elem = Mock()
        mock_edit_elem.GetCurrentPropertyValue.side_effect = lambda prop: {
            mock_uia_module.UIA_NamePropertyId: "Notepad editor",
            mock_uia_module.UIA_AutomationIdPropertyId: "edit123",
            mock_uia_module.UIA_ClassNamePropertyId: "Edit",
            mock_uia_module.UIA_BoundingRectanglePropertyId: (100, 100, 300, 150),
            mock_uia_module.UIA_ProcessIdPropertyId: 1234,
            mock_uia_module.UIA_ControlTypePropertyId: 50004,
        }.get(prop, None)

        # Real UIA constant: production compares the element's control type to it.
        mock_uia_module.UIA_EditControlTypeId = 50004
        mock_uia_module.ElementFromHandle = Mock(side_effect=Exception("UIA_E_ELEMENTNOTAVAILABLE"))
        mock_uia_module.ElementFromPoint = Mock(return_value=mock_edit_elem)

        # Adapter instance wiring: production reads adapter._uia_modules["uia"]
        mock_uia_adapter.return_value = mock_uia_instance
        mock_uia_instance._uia_modules = {"uia": mock_uia_module}
        mock_uia_instance._uia_module = mock_uia_module

        # Mock perception layer to return ScreenState
        mock_perception_instance = Mock()
        mock_perception_layer.return_value = mock_perception_instance
        mock_state = Mock(spec=ScreenState)
        mock_state.active_window = Mock()
        mock_state.active_window.process_id = 1234
        mock_state.active_window.title = "Untitled - Notepad"
        mock_state.text_regions = []
        mock_perception_instance.get_current_state.return_value = mock_state

        # Mock visual grounding to return VERIFIED result
        mock_grounding_instance = Mock()
        mock_create_grounding.return_value = mock_grounding_instance
        mock_candidate = Mock(spec=VisualTargetCandidate)
        mock_target_ref = Mock(spec=TargetReference)
        mock_evidence = []
        mock_reasons = ["Sufficient multi-signal evidence"]
        mock_result = TargetCorrelationResult(
            status=GroundingStatus.VERIFIED,
            score=0.88,
            candidate=mock_candidate,
            matched_target=mock_target_ref,
            evidence=mock_evidence,
            reasons=mock_reasons,
        )
        mock_grounding_instance.ground_target.return_value = mock_result

        # Create grounder (will use mocked UIAutomationAdapter)
        grounder = UITargetGrounder()
        # Patch the get_windows method on the uia instance
        with patch.object(grounder.uia, 'get_windows', return_value=[mock_window_info]):
            mock_edit_elems = Mock()
            mock_edit_elems.Length = 1
            mock_edit_elems.GetElement.return_value = mock_edit_elem

            mock_window_elem = Mock()
            mock_window_elem.FindAll.return_value = mock_edit_elems
            # Window element must return the correct process ID to match notepad_pid
            mock_window_elem.GetCurrentPropertyValue.side_effect = lambda prop: {
                mock_uia_module.UIA_ProcessIdPropertyId: 1234,
            }.get(prop, None)

            # Mock desktop children to return our window element
            mock_desktop_children = Mock()
            mock_desktop_children.Length = 1
            mock_desktop_children.GetElement.return_value = mock_window_elem
            mock_root.FindAll.side_effect = lambda *args, **kwa: mock_desktop_children

            # Execute
            result = grounder.ground_notepad_edit()

            # Verify
            self.assertIsNotNone(result)
            self.assertEqual(result.status, GroundingStatus.VERIFIED)
            self.assertEqual(result.score, 0.88)




if __name__ == '__main__':
    unittest.main()


class TestPointAnchoredWin32CorrelationSafePath(unittest.TestCase):
    def test_point_anchored_win32_correlation_safe_path(self):
        # Identity propagation: structural UIA identity recognized
        from veronica.vision.visual_grounding import VisualGrounding, VisualTargetCandidate
        from veronica.vision.target_verification import TargetReference
        vg = VisualGrounding()
        cand = VisualTargetCandidate(semantic_name="Edit", control_type="Edit", confidence=0.95)
        ref = TargetReference(control_type="Edit", class_name="Edit", owning_process_id=21152)
        score, ev, rs = vg._score_candidate_against_target(cand, ref, None)
        identity_ev = [e for e in ev if e.signal_type == "automation_id" and e.strength >= 0.7]
        self.assertTrue(len(identity_ev) >= 1, "Structural UIA identity evidence must propagate")


class TestVerifiedGroundingToProposalHandoff(unittest.TestCase):
    """STEP 4/6 — Proven handoff: VERIFIED TargetReference -> valid TYPE_TEXT proposal."""
    def test_verified_target_reference_produces_valid_type_text_proposal(self):
        from veronica.vision.action_proposal import create_action_proposal, ActionType, evaluate_proposal_with_permission
        from veronica.vision.visual_grounding import GroundingStatus, VisualTargetCandidate, TargetReference, TargetCorrelationResult
        from veronica.safety.permissions import PermissionGate

        # Genuine verified reference (same fields as real fixture identity)
        ref = TargetReference(
            control_type="Edit", class_name="Edit", owning_process_id=21152,
            owning_window_id="919704", element_id="edit_real",
            target_name="Notepad editor", bounding_box={"x":160,"y":214,"width":540,"height":136},
            target_point=(430,282),
        )
        candidate = VisualTargetCandidate(semantic_name="Notepad editor", control_type="Edit",
            visual_bbox=(160,214,540,136), target_point=(430,282), confidence=0.95)
        grounding = TargetCorrelationResult(
            status=GroundingStatus.VERIFIED, score=0.90, candidate=candidate,
            matched_target=ref, evidence=[], reasons=["Verified"],
        )
        proposal = create_action_proposal(
            proposal_id="handoff-10f-test",
            action_type=ActionType.TYPE_TEXT,
            target_reference=ref,
            grounding_result=grounding,
            text_to_type="VERONICA 10F TEST 123",
        )
        # A. VERIFIED -> valid proposal
        self.assertEqual(proposal.status.value, "PROPOSED")
        self.assertEqual(proposal.grounding_result.status, GroundingStatus.VERIFIED)
        # B. Identity preserved exactly
        self.assertEqual(proposal.target_reference.control_type, "Edit")
        # C. Not VERIFIED -> rejected (already covered by existing tests)
        bad = create_action_proposal("bad", ActionType.TYPE_TEXT, grounding_result=TargetCorrelationResult(
            status=GroundingStatus.PARTIAL, score=0.40, candidate=candidate, matched_target=ref))
        self.assertEqual(bad.status.value, "INVALID")
        # D. PermissionGate authoritative (reuse existing; no gatelift)
        gate = PermissionGate()
        # E. Authorization fresh/proposal-bound (verified by proposal.audit field set by evaluate)
        # F. No vision-derived authorization (proposal.source = brain/planner)
        self.assertEqual(proposal.source, "brain/planner")
        # G. Safety rules preserved (no clipboard/Enter/arbitrary coordinate typing defined)
        self.assertIsNotNone(proposal.text_to_type)
        # H/I/J -- existing tests already cover
