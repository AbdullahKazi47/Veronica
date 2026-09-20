"""
VERONICA vision: computer perception and control (Phase 7 core layer).

Provides structured screen-state perception (UI Automation, screen capture, OCR),
screen-change detection, semantic computer control, prompt-injection defense,
continuous background observation, and the computer-use loop integrated with the
Phase 6 closed-loop intelligence.

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
from veronica.vision.continuous_observer import (
    ContinuousObservationService,
    ObservationConfig,
    PerceptionEvent,
    ImportanceFilter,
    ObservationStatus,
    create_continuous_observation_service,
)
from veronica.vision.understanding import (
    VisionUnderstanding,
    ActiveWindowInfo,
    ElementSummary,
    TextSummary,
    CursorInfo,
)
from veronica.vision.vision_provider import (
    VisionProvider,
    VisionProviderError,
    VisionTimeoutError,
    VisionCancellationError,
    CancellationToken,
    StubVisionProvider,
)
from veronica.vision.ollama_vision_provider import OllamaVisionProvider
from veronica.vision.vision_job import VisionJobExecutor
from veronica.vision.vision_policy import VisionPolicy, VisionPolicyDecision
from veronica.vision.vision_image_privacy import VisionImagePrivacy
from veronica.vision.visual_analysis import (
    VisualAnalysis,
    DetectedElement,
    DetectedTextRegion,
)

from veronica.vision.autonomous_workflow import (
    AutonomousWorkflow,
    WorkflowStep,
    WorkflowStatus,
    StepStatus,
    execute_workflow,
    run_step,
    handle_recovery,
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
    # continuous_observer
    "ContinuousObservationService",
    "ObservationConfig",
    "PerceptionEvent",
    "ImportanceFilter",
    "ObservationStatus",
    "create_continuous_observation_service",
    # vision_provider
    "VisionProvider",
    "VisionProviderError",
    "VisionTimeoutError",
    "VisionCancellationError",
    "CancellationToken",
    "StubVisionProvider",
    "OllamaVisionProvider",
    # vision_job
    "VisionJobExecutor",
    # vision_policy
    "VisionPolicy",
    "VisionPolicyDecision",
    # vision_image_privacy
    "VisionImagePrivacy",
    # visual_analysis
    "VisualAnalysis",
    "DetectedElement",
    "DetectedTextRegion",
    # understanding
    "VisionUnderstanding",
    "ActiveWindowInfo",
    "ElementSummary",
    "TextSummary",
    "CursorInfo",
    # autonomous_workflow (Phase 10E)
    "AutonomousWorkflow",
    "WorkflowStep",
    "WorkflowStatus",
    "StepStatus",
    "execute_workflow",
    "run_step",
    "handle_recovery",
]