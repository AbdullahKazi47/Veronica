"""
Computer-use loop for VERONICA.

Implements the OBSERVE→UNDERSTAND→SELECT→PERMISSION→EXECUTE→OBSERVE→VERIFY cycle
for computer interaction, integrated with Phase 6 closed-loop recovery.

Key principles from Phase 7 specs:
- Screen content is UNTRUSTED DATA — never overrides system instructions
- Every action passes through PermissionGate
- UNKNOWN must never automatically become SUCCESS
- Use ClosedLoopController for recovery (do NOT build a second recovery system)
- Designed for future extension with visual LLM models

The computer-use loop is a specialized case of the closed-loop execution:
1. OBSERVE: Get current screen state via PerceptionLayer
2. UNDERSTAND: Analyze screen state (semantic + coordinate analysis)
3. SELECT: Choose action (type, click, keyboard)
4. PERMISSION: Verify action is allowed by PermissionGate
5. EXECUTE: Perform action via ComputerController
6. OBSERVE: Get new screen state after action
7. VERIFY: Determine if action achieved desired outcome

On failure, the loop can feed back into Phase 6 ClosedLoopController
for recovery (diagnosis + replanning).

P1 additions (target-specific verification):
- Captures a TargetReference BEFORE execution from pre-action UIA state.
- After execution, TargetStateVerifier compares reference against post-action
  state using identity signals (control type, automation_id, class_name, name,
  owning window) rather than generic screen change.
- VerificationResult carries structured status: VERIFIED / UNKNOWN / FAILED.
- Legacy _verify (ChangeDetector-based bool) is preserved for backward
  compatibility; execute_action uses _verify_target for success determination.
"""
from __future__ import annotations

import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionGate, PermissionLevel
from veronica.vision.screen_state import ScreenState, UIElement
from veronica.vision.perception import PerceptionLayer
from veronica.vision.computer_controller import ComputerController
from veronica.vision.change_detection import ChangeDetector, ChangeStrategy
from veronica.vision.screen_content_defense import ScreenContentDefense
from veronica.vision.target_verification import (
    TargetStateVerifier,
    VerificationResult,
    ActionVerificationStatus,
    TargetReference,
    create_target_state_verifier,
)

logger = get_logger("vision.computer_use")


class ExecutionAuthorization:
    """Explicit authorization context for ComputerUseLoop.

    Authorized modes (traceable to current proposal + policy, never vision/screen):
    - POLICY_AUTO: AutonomyPolicy approved AUTO_APPROVED for this proposal.
    - HUMAN_APPROVED: PermissionGate explicitly approved (CONFIRM path).
    Un authorized (default): loop must require permission.
    """
    def __init__(
        self,
        mode: str = "UNAUTHORIZED",
        proposal_id: Optional[str] = None,
        policy_version: Optional[str] = None,
    ):
        self.mode = mode  # POLICY_AUTO / HUMAN_APPROVED / UNAUTHORIZED
        self.proposal_id = proposal_id
        self.policy_version = policy_version

    def is_authorized(self) -> bool:
        return self.mode in ("POLICY_AUTO", "HUMAN_APPROVED")


class LoopPhase(str, Enum):
    """Phases of the computer-use loop."""
    OBSERVE = "observe"
    UNDERSTAND = "understand"
    SELECT = "select"
    PERMISSION = "permission"
    EXECUTE = "execute"
    VERIFY = "verify"
    SUCCESS = "success"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass
class ComputerAction:
    """Represents an action to be performed on the computer."""
    action_type: str  # "click", "type", "hotkey", "scroll", "move"
    target_name: Optional[str] = None  # Semantic target name
    target_element: Optional[UIElement] = None  # Direct element reference
    coordinates: Optional[Tuple[int, int]] = None  # Fallback coordinates
    text: Optional[str] = None  # Text to type
    key: Optional[str] = None  # Key(s) to press
    button: str = "left"
    clicks: int = 1
    description: str = ""  # Human-readable description for permission check
    permission_level: PermissionLevel = PermissionLevel.MODERATE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_type": self.action_type,
            "target_name": self.target_name,
            "coordinates": self.coordinates,
            "text": self.text[:100] if self.text else None,  # Truncate for logging
            "key": self.key,
            "button": self.button,
            "clicks": self.clicks,
            "description": self.description,
            "permission_level": self.permission_level.name,
        }


@dataclass
class LoopResult:
    """Result of a computer-use loop execution."""
    success: bool
    phase_reached: LoopPhase
    action_performed: Optional[ComputerAction] = None
    initial_state: Optional[ScreenState] = None
    final_state: Optional[ScreenState] = None
    error: Optional[str] = None
    observations: List[Dict[str, Any]] = field(default_factory=list)
    recovery_suggested: bool = False
    # P1: target-specific verification result
    verification: Optional[VerificationResult] = None

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "success": self.success,
            "phase_reached": self.phase_reached.value,
            "action_performed": self.action_performed.to_dict() if self.action_performed else None,
            "error": self.error,
            "observations_count": len(self.observations),
            "recovery_suggested": self.recovery_suggested,
        }
        if self.verification is not None:
            result["verification"] = self.verification.to_dict()
        return result


class ComputerUseLoop:
    """
    Manages the computer-use loop: OBSERVE→UNDERSTAND→SELECT→PERMISSION→EXECUTE→VERIFY.

    Integrates with:
    - PerceptionLayer for screen observation
    - ComputerController for action execution
    - ChangeDetector for legacy verification (bool, backward compat)
    - TargetStateVerifier for target-specific verification (P1)
    - ScreenContentDefense for safety
    - Phase 6 ClosedLoopController for failure recovery

    This is NOT a standalone recovery system - it feeds into the existing
    closed-loop controller when actions fail.
    """

    def __init__(
        self,
        perception_layer: Optional[PerceptionLayer] = None,
        computer_controller: Optional[ComputerController] = None,
        change_detector: Optional[ChangeDetector] = None,
        screen_defense: Optional[ScreenContentDefense] = None,
        permission_gate: Optional[PermissionGate] = None,
        coordinate_validator: Optional[Any] = None,
        event_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ):
        """Initialize the computer-use loop.

        Args:
            perception_layer: Layer for screen observation
            computer_controller: Controller for computer actions
            change_detector: Detector for screen changes
            screen_defense: Defense against prompt injection
            permission_gate: Permission system (uses controller's if not provided)
            coordinate_validator: Optional coordinate safety validator (P0)
            event_callback: Event emission callback (for UI)
        """
        self.perception = perception_layer or PerceptionLayer()
        # P0: shared gate — loop and controller MUST use the exact same PermissionGate
        shared_gate = permission_gate or PermissionGate()
        self.controller = computer_controller or ComputerController(
            permission_gate=shared_gate,
            perception_layer=self.perception,
            coordinate_validator=coordinate_validator,
        )
        self.change_detector = change_detector or ChangeDetector()
        self.screen_defense = screen_defense or ScreenContentDefense()
        self.permission_gate = shared_gate
        self.event_callback = event_callback

        # State
        self._cancelled: bool = False
        # P1: target-specific verification
        self._target_reference: Optional[TargetReference] = None
        self._verifier: TargetStateVerifier = create_target_state_verifier()

    # ------------------------------------------------------------------
    # Public control
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """Cancel the current computer-use operation."""
        self._cancelled = True
        logger.info("Computer-use loop cancelled")

    def reset_cancel(self) -> None:
        """Reset cancellation state."""
        self._cancelled = False

    # ------------------------------------------------------------------
    # Execute action (main entry point)
    # ------------------------------------------------------------------

    @property
    def authorization(self) -> Optional[ExecutionAuthorization]:
        return getattr(self, "_authorization", None)

    @authorization.setter
    def authorization(self, auth: Optional[ExecutionAuthorization]) -> None:
        self._authorization = auth
        # Audit log: authorization bound to proposal (not vision/screen)
        if auth is not None:
            logger.info("Loop authorization set: mode=%s proposal=%s",
                        auth.mode, auth.proposal_id)

    def execute_action(
        self,
        action: ComputerAction,
        verify_change: bool = True,
        verification_strategy: ChangeStrategy = ChangeStrategy.ACTION,
        authorization: Optional[ExecutionAuthorization] = None,
    ) -> LoopResult:
        """Execute a single computer action through the full loop.

        Success is determined by TARGET-SPECIFIC verification (P1), not by
        generic screen change.  The legacy ChangeDetector-based ``_verify``
        is preserved for backward-compatible tests but is NOT used for
        ``LoopResult.success``.

        Args:
            action: Action to perform
            verify_change: Whether to verify action after execution
            verification_strategy: Strategy for legacy change detection

        Returns:
            LoopResult with outcome
        """
        if self._cancelled:
            return LoopResult(
                success=False,
                phase_reached=LoopPhase.BLOCKED,
                verification=VerificationResult(
                    status=ActionVerificationStatus.BLOCKED,
                    confidence=0.0,
                    executed=False,
                    reasons=["computer use cancelled before execution"],
                ),
                error="Computer use cancelled"
            )

        observations: List[Dict[str, Any]] = []
        initial_state: Optional[ScreenState] = None

        try:
            # Phase 1: OBSERVE - Get current screen state
            self._emit_event("action_observing", {"action": action.to_dict()})
            initial_state = self._observe()
            observations.append({
                "phase": "observe",
                "timestamp": time.time(),
                "state_fingerprint": initial_state.get_fingerprint() if initial_state else None,
            })

            # Phase 2: UNDERSTAND - Analyze screen state and validate
            self._emit_event("action_understanding", {"action": action.to_dict()})
            initial_state = self._understand(initial_state, action)
            observations.append({
                "phase": "understand",
                "timestamp": time.time(),
                "validated": True,
            })

            # Phase 3: SELECT - Action is already selected, just validate
            self._emit_event("action_selecting", {"action": action.to_dict()})
            observations.append({
                "phase": "select",
                "timestamp": time.time(),
                "action_selected": action.action_type,
            })

            # Phase 4: PERMISSION - Consume authorization context
            auth = authorization or getattr(self, "_authorization", None)
            if auth is not None and auth.is_authorized():
                if auth.proposal_id is None:
                    return LoopResult(
                        success=False,
                        phase_reached=LoopPhase.PERMISSION,
                        action_performed=action,
                        initial_state=initial_state,
                        verification=VerificationResult(
                            status=ActionVerificationStatus.BLOCKED,
                            confidence=0.0,
                            executed=False,
                            reasons=["authorization missing proposal binding"],
                        ),
                        error="Authorization missing proposal binding",
                        observations=observations,
                    )
                observations.append({
                    "phase": "permission",
                    "timestamp": time.time(),
                    "allowed": True,
                    "authorization_mode": auth.mode,
                    "proposal_id": auth.proposal_id,
                    "policy_version": auth.policy_version,
                })
            else:
                self._emit_event("action_permission_check", {"action": action.to_dict()})
                permission_result = self._check_permission(action)
                if not permission_result[0]:
                    return LoopResult(
                        success=False,
                        phase_reached=LoopPhase.PERMISSION,
                        action_performed=action,
                        initial_state=initial_state,
                        verification=VerificationResult(
                            status=ActionVerificationStatus.BLOCKED,
                            confidence=0.0,
                            executed=False,
                            reasons=["permission denied before execution"],
                        ),
                        error=f"Permission denied: {permission_result[1]}",
                        observations=observations,
                    )
                observations.append({
                    "phase": "permission",
                    "timestamp": time.time(),
                    "allowed": True,
                })

            # Phase 5: EXECUTE - Perform the action
            # P1: capture target reference from the pre-action state BEFORE any
            # physical action runs (after UNDERSTAND resolved target_element).
            self._target_reference = self._capture_reference(action, initial_state)
            self._emit_event("action_executing", {"action": action.to_dict()})
            execute_result = self._execute(action)
            observations.append({
                "phase": "execute",
                "timestamp": time.time(),
                "success": execute_result.success,
                "message": execute_result.message,
            })

            if not execute_result.success:
                return LoopResult(
                    success=False,
                    phase_reached=LoopPhase.EXECUTE,
                    action_performed=action,
                    initial_state=initial_state,
                    verification=VerificationResult(
                        status=ActionVerificationStatus.FAILED,
                        confidence=0.0,
                        executed=False,
                        reasons=["action execution failed"],
                    ),
                    error=execute_result.message,
                    observations=observations,
                    recovery_suggested=True,
                )

            # Phase 6: OBSERVE (after action) - Get new screen state
            final_state: Optional[ScreenState] = None
            if verify_change:
                self._emit_event("action_observing_after", {"action": action.to_dict()})
                final_state = self._observe()
                observations.append({
                    "phase": "observe_after",
                    "timestamp": time.time(),
                    "state_fingerprint": final_state.get_fingerprint() if final_state else None,
                })

            # Phase 7: VERIFY - Target-specific verification (P1)
            if verify_change:
                self._emit_event("action_verifying", {"action": action.to_dict()})
                verification = self._verify_target(
                    action, self._target_reference, final_state,
                )
                observations.append({
                    "phase": "verify",
                    "timestamp": time.time(),
                    "verified": verification.is_verified,
                    "verification_status": verification.status.value,
                })

                # Emit completed event with verification status
                self._emit_event("action_completed", {
                    "action": action.to_dict(),
                    "verified": verification.is_verified,
                    "verification_status": verification.status.value,
                })

                return LoopResult(
                    success=verification.is_verified,
                    phase_reached=(
                        LoopPhase.SUCCESS if verification.is_verified
                        else LoopPhase.VERIFY
                    ),
                    action_performed=action,
                    initial_state=initial_state,
                    final_state=final_state,
                    verification=verification,
                    observations=observations,
                    recovery_suggested=(
                        verification.status != ActionVerificationStatus.VERIFIED
                    ),
                )

            # verify_change=False: execution succeeded, no verification requested
            self._emit_event("action_completed", {
                "action": action.to_dict(),
                "verified": False,
            })
            return LoopResult(
                success=True,
                phase_reached=LoopPhase.SUCCESS,
                action_performed=action,
                initial_state=initial_state,
                final_state=final_state,
                observations=observations,
            )

        except Exception as e:
            logger.exception("Exception in computer-use loop: %s", e)
            self._emit_event("action_failed", {
                "action": action.to_dict(),
                "error": str(e),
            })
            return LoopResult(
                success=False,
                phase_reached=LoopPhase.FAILED,
                action_performed=action,
                initial_state=initial_state,
                verification=VerificationResult(
                    status=ActionVerificationStatus.FAILED,
                    confidence=0.0,
                    executed=False,
                    reasons=["exception during action execution"],
                ),
                error=str(e),
                observations=observations,
                recovery_suggested=True,
            )

    # ------------------------------------------------------------------
    # Loop phases (internal)
    # ------------------------------------------------------------------

    def _observe(self) -> ScreenState:
        """Observe current screen state.

        Returns:
            ScreenState from perception layer, filtered through defense.
        """
        raw_state = self.perception.get_current_state()
        # Apply safety filtering to screen content
        safe_state = self.screen_defense.filter_perception_state(raw_state)
        return safe_state

    def _understand(
        self,
        state: ScreenState,
        action: ComputerAction,
    ) -> ScreenState:
        """Understand screen state and prepare for action.

        This phase analyzes the screen state and ensures the action
        target is valid. In the future, this would use a visual LLM.

        Args:
            state: Current screen state
            action: Action to perform

        Returns:
            Validated/modified screen state
        """
        if not state:
            return state

        # For now, this just validates the state is usable
        # Future: Use visual LLM to understand screen content
        # Future: Map semantic target to UI element if not already set

        if action.action_type == "click" and action.target_name:
            # Try to find the target element in the screen state
            found = False
            for element in state.desktop_elements:
                if (element.name.lower() == action.target_name.lower() and
                        element.is_visible and element.is_enabled):
                    action.target_element = element
                    found = True
                    break

            if not found:
                logger.debug(
                    "Semantic target '%s' not found in current state",
                    action.target_name,
                )

        return state

    def _check_permission(
        self,
        action: ComputerAction,
    ) -> Tuple[bool, str]:
        """Check if action is allowed by permission gate.

        Args:
            action: Action to check permission for

        Returns:
            Tuple of (allowed, reason)
        """
        decision = self.permission_gate.check(
            f"computer_use_{action.action_type}",
            action.permission_level,
            action.description or f"{action.action_type} action",
        )
        return decision.allowed, decision.reason

    def _execute(self, action: ComputerAction) -> Any:
        """Execute the computer action via controller.

        Also captures the target reference from the current (pre-action)
        observation state for later P1 verification.

        Args:
            action: Action to execute

        Returns:
            ToolResult from controller
        """
        if action.action_type == "click":
            if action.target_element:
                return self.controller.click_element(
                    action.target_element,
                    button=action.button,
                    clicks=action.clicks,
                )
            elif action.target_name:
                return self.controller.click_by_name(
                    action.target_name,
                    button=action.button,
                    clicks=action.clicks,
                )
            elif action.coordinates:
                return self.controller.click_at(
                    action.coordinates[0],
                    action.coordinates[1],
                    button=action.button,
                    clicks=action.clicks,
                )
            else:
                from veronica.tools.base import ToolResult
                return ToolResult.fail("Click action has no valid target")

        elif action.action_type == "type":
            return self.controller.type_text(action.text)

        elif action.action_type == "hotkey":
            if action.key:
                keys = action.key.split("+")
                return self.controller.hotkey(*keys)
            else:
                from veronica.tools.base import ToolResult
                return ToolResult.fail("Hotkey action has no key specified")

        elif action.action_type == "scroll":
            clicks = action.clicks if action.clicks != 1 else 3
            return self.controller.scroll(clicks)

        elif action.action_type == "move":
            if action.coordinates:
                return self.controller.move_to(
                    action.coordinates[0],
                    action.coordinates[1],
                )
            else:
                from veronica.tools.base import ToolResult
                return ToolResult.fail("Move action has no coordinates")

        else:
            from veronica.tools.base import ToolResult
            return ToolResult.fail(f"Unknown action type: {action.action_type}")

    # ------------------------------------------------------------------
    # P1: Target-specific verification
    # ------------------------------------------------------------------

    def _capture_reference(
        self,
        action: ComputerAction,
        state: Optional[ScreenState],
    ) -> Optional[TargetReference]:
        """Capture a stable target reference from the action and pre-action state.

        This is called BEFORE execution to record what the target looked like
        while it was still in its original context.

        Priority:
        1. Explicit target_ref on the action (if present)
        2. target_element → TargetReference with full UIA properties
        3. target_name / coordinates fallback → name + owning window info

        Returns:
            TargetReference or None if nothing to capture
        """
        # Prefer explicit target reference on action
        target_ref = getattr(action, "target_ref", None)
        if target_ref is not None:
            return target_ref

        # Try to find element in state
        element = getattr(action, "target_element", None)
        if element is not None:
            return self._reference_from_element(element, state)

        # Fallback: use name and maybe coordinates
        target_name = getattr(action, "target_name", None)
        target_point = getattr(action, "coordinates", None)
        if target_name or target_point:
            owning_process_id = None
            owning_window_id = None
            owning_window_title = None
            if state and state.active_window:
                owning_process_id = state.active_window.process_id
                owning_window_id = state.active_window.window_id
                owning_window_title = state.active_window.title
            return TargetReference(
                target_name=target_name,
                target_point=target_point,
                owning_process_id=owning_process_id,
                owning_window_id=owning_window_id,
                owning_window_title=owning_window_title,
            )
        return None

    def _reference_from_element(
        self,
        element: UIElement,
        state: Optional[ScreenState],
    ) -> TargetReference:
        """Create TargetReference from a UIElement.

        Args:
            element: The UI element to create a reference for
            state: Current screen state (for owning window info)

        Returns:
            TargetReference capturing the element's identity signals
        """
        owning_process_id = None
        owning_window_id = None
        owning_window_title = None
        if state and state.active_window:
            owning_process_id = state.active_window.process_id
            owning_window_id = state.active_window.window_id
            owning_window_title = state.active_window.title
        return TargetReference(
            element_id=element.element_id,
            target_name=element.name,
            control_type=element.control_type,
            automation_id=element.automation_id,
            class_name=element.class_name,
            owning_process_id=owning_process_id,
            owning_window_id=owning_window_id,
            owning_window_title=owning_window_title,
            bounding_box=dict(element.bounding_box) if element.bounding_box else None,
            target_point=None,
        )

    def _verify_target(
        self,
        action: ComputerAction,
        reference: Optional[TargetReference],
        after_state: Optional[ScreenState],
    ) -> VerificationResult:
        """Verify action result using target-specific identity comparison (P1).

        Uses TargetStateVerifier to compare the captured reference against the
        post-action screen state based on UIA identity signals.

        Args:
            action: The action that was performed
            reference: TargetReference captured before execution
            after_state: Screen state after the action

        Returns:
            VerificationResult with structured status
        """
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

        verification = self._verifier.verify(
            action,
            reference,
            after_state,
            executed=True,
        )

        # Emit verification event if callback provided
        if self.event_callback:
            self.event_callback(
                f"action_verification_{verification.status.value}",
                {
                    "action": action.to_dict(),
                    "status": verification.status.value,
                    "confidence": verification.confidence,
                    "executed": verification.executed,
                },
            )

        return verification

    # ------------------------------------------------------------------
    # Legacy verification (backward-compatible, returns bool)
    # ------------------------------------------------------------------

    def _verify(
        self,
        before_state: Optional[ScreenState],
        after_state: Optional[ScreenState],
        action: ComputerAction,
        strategy: ChangeStrategy = ChangeStrategy.ACTION,
    ) -> bool:
        """Legacy verify using ChangeDetector — returns bool.

        PRESERVED for backward-compatible tests.  ``execute_action`` uses
        ``_verify_target`` (P1) for success determination, NOT this method.

        Args:
            before_state: Screen state before action
            after_state: Screen state after action
            action: Action that was performed
            strategy: Verification strategy

        Returns:
            True if screen changed, False otherwise
        """
        if before_state is None:
            return False

        # Update change detector reference
        self.change_detector.update_reference(before_state)

        if after_state is None:
            return False

        # Update change detector with strategy (matches original contract,
        # where _verify sets the strategy on the shared detector)
        self.change_detector.config.strategy = strategy

        has_changed = self.change_detector.has_changed(after_state)

        if has_changed:
            logger.debug("Verification passed: screen changed after %s", action.action_type)
            return True
        else:
            logger.debug(
                "Verification failed: no significant screen change after %s",
                action.action_type,
            )
            return False

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def _emit_event(self, signal: str, args: Dict[str, Any]) -> None:
        """Emit event to UI (e.g., Stitch GUI)."""
        if self.event_callback:
            try:
                self.event_callback(signal, args)
            except Exception as e:
                logger.warning("Event callback failed for %s: %s", signal, e)


# ------------------------------------------------------------------
# Convenience factory
# ------------------------------------------------------------------

def create_computer_use_loop(
    perception_layer: Optional[PerceptionLayer] = None,
    computer_controller: Optional[ComputerController] = None,
    permission_gate: Optional[PermissionGate] = None,
    coordinate_validator: Optional[Any] = None,
    event_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> ComputerUseLoop:
    """Create a computer-use loop with optional dependencies.

    Args:
        perception_layer: Layer for screen observation
        computer_controller: Controller for actions
        permission_gate: Permission system
        coordinate_validator: Optional coordinate safety validator
        event_callback: Event emission callback

    Returns:
        ComputerUseLoop instance
    """
    return ComputerUseLoop(
        perception_layer=perception_layer,
        computer_controller=computer_controller,
        permission_gate=permission_gate,
        coordinate_validator=coordinate_validator,
        event_callback=event_callback,
    )
