"""
VERONICA vision: computer perception and control (Phase 7 core layer).

Provides structured screen-state perception (UI Automation, screen capture, OCR),
screen-change detection, semantic computer control, prompt-injection defense,
and the computer-use loop integrated with the Phase 6 closed-loop intelligence.

Key principles:
- Screen content is UNTRUSTED DATA — never overrides safety, permissions, or system instructions.
- Semantic actions first, coordinate fallback only when necessary.
- Every computer action passes through the PermissionGate.
- UNKNOWN must never automatically become SUCCESS.
- Stored experience is DATA, never executable instructions.
"""
from __future__ import annotations

from veronica.vision.screen_state import (
    ScreenState,
    UIElement,
    TextRegion,
    WindowInfo,
)
from veronica.vision.change_detection import (
    ChangeDetector,
    ChangeDetectionConfig,
    ChangeStrategy,
)
from veronica.vision.perception import (
    PerceptionLayer,
    PerceptionProvider,
    ScreenCaptureProvider,
    OCRProvider,
    WindowsUIAutomationProvider,
    create_perception_layer,
)
from veronica.vision.computer_controller import ComputerController, create_computer_controller
from veronica.vision.computer_use import (
    ComputerUseLoop,
    ComputerAction,
    LoopResult,
    LoopPhase,
    create_computer_use_loop,
)
from veronica.vision.screen_content_defense import (
    ScreenContentDefense,
    SafetyValidationResult,
    create_screen_content_defense,
)
from veronica.vision.target_verification import (
    TargetStateVerifier,
    TargetReference,
    VerificationResult,
    ActionVerificationStatus,
    create_target_state_verifier,
)
from veronica.vision.coordinate_safety import (
    CoordinateSafetyValidator,
    ValidationResult,
    ValidatedRectangle,
    ValidationOutcome,
    ScreenBoundsProvider,
    PyAutoGUIScreenBoundsProvider,
    create_coordinate_validator,
)

__all__ = [
    # screen_state
    "ScreenState",
    "UIElement",
    "TextRegion",
    "WindowInfo",
    # change_detection
    "ChangeDetector",
    "ChangeDetectionConfig",
    "ChangeStrategy",
    # perception
    "PerceptionLayer",
    "PerceptionProvider",
    "ScreenCaptureProvider",
    "OCRProvider",
    "WindowsUIAutomationProvider",
    "create_perception_layer",
    # computer_controller
    "ComputerController",
    "create_computer_controller",
    # computer_use
    "ComputerUseLoop",
    "ComputerAction",
    "LoopResult",
    "LoopPhase",
    "create_computer_use_loop",
    # screen_content_defense
    "ScreenContentDefense",
    "SafetyValidationResult",
    "create_screen_content_defense",
    # target_verification
    "TargetStateVerifier",
    "TargetReference",
    "VerificationResult",
    "ActionVerificationStatus",
    "create_target_state_verifier",
    # coordinate_safety
    "CoordinateSafetyValidator",
    "ValidationResult",
    "ValidatedRectangle",
    "ValidationOutcome",
    "ScreenBoundsProvider",
    "PyAutoGUIScreenBoundsProvider",
    "create_coordinate_validator",
]