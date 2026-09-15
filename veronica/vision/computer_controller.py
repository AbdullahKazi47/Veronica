"""
Computer controller for VERONICA.

Provides controlled computer interaction with semantic actions prioritized
over coordinate-based actions. All actions pass through the PermissionGate.

Action hierarchy:
1. Semantic actions (find element by name/type/role → interact)
2. Coordinate-based actions (specific screen coordinates)
3. Fallback actions (keyboard shortcuts, etc.)

All actions must be confirmed for MODERATE+ permission levels.

Key principles:
- Semantic actions first: "click button named 'Save'" vs "click at (100,200)"
- Coordinate fallback only when semantic fails
- Every action passes through PermissionGate
- Never perform destructive actions without explicit confirmation
- COORDINATE SAFETY: All coordinates validated before PermissionGate/PyAutoGUI
- Designed for Windows primary target (uses pyautogui, UI Automation)
"""
from __future__ import annotations

import time
import platform
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass

from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel, PermissionGate
from veronica.tools.base import Tool, ToolResult
from veronica.vision.screen_state import ScreenState, UIElement, WindowInfo
from veronica.vision.perception import PerceptionLayer, WindowsUIAutomationProvider
from veronica.vision.coordinate_safety import (
    CoordinateSafetyValidator,
    ValidationResult,
    ValidatedRectangle,
    ScreenBoundsProvider,
    PyAutoGUIScreenBoundsProvider,
)

logger = get_logger("vision.computer_controller")


@dataclass
class ClickAction:
    """Represents a click action to be performed."""
    method: str  # "semantic", "coordinate", "element"
    target: str | Tuple[int, int] | UIElement  # element name, (x,y), or UIElement
    button: str = "left"  # left, right, middle
    clicks: int = 1
    permission_level: PermissionLevel = PermissionLevel.MODERATE


@dataclass
class TypeAction:
    """Represents a typing action to be performed."""
    text: str
    permission_level: PermissionLevel = PermissionLevel.MODERATE


@dataclass
class KeyAction:
    """Represents a keyboard action to be performed."""
    key: str  # e.g., "enter", "escape", "ctrl+c"
    permission_level: PermissionLevel = PermissionLevel.MODERATE


class ComputerController:
    """
    Controls computer interaction with semantic actions prioritized.

    Provides methods for clicking, typing, and keyboard actions that
    prefer semantic targeting (find by name) over coordinate-based actions.
    All actions are permission-checked.

    Coordinate Safety:
    - Every coordinate rectangle is validated by CoordinateSafetyValidator
    - Validation occurs BEFORE PermissionGate check
    - Invalid coordinates terminate the action immediately
    - No silent clamping, guessing, or repair of invalid coordinates
    """

    def __init__(
        self,
        permission_gate: Optional[PermissionGate] = None,
        perception_layer: Optional[PerceptionLayer] = None,
        coordinate_validator: Optional[CoordinateSafetyValidator] = None,
        screen_bounds_provider: Optional[ScreenBoundsProvider] = None,
    ):
        """Initialize the computer controller.

        Args:
            permission_gate: Permission gate for action authorization
            perception_layer: Perception layer for screen state
            coordinate_validator: Validator for coordinate safety (created if None)
            screen_bounds_provider: Provider for virtual desktop bounds
        """
        self.permission_gate = permission_gate or PermissionGate()
        self.perception_layer = perception_layer or PerceptionLayer()
        self._pyautogui = None
        self._initialize_pyautogui()

        # Coordinate safety validator - created with injected or default provider
        if coordinate_validator is not None:
            self._coordinate_validator = coordinate_validator
        else:
            provider = screen_bounds_provider or PyAutoGUIScreenBoundsProvider(self._pyautogui)
            self._coordinate_validator = CoordinateSafetyValidator(provider)

    def _initialize_pyautogui(self) -> None:
        """Initialize pyautogui if available."""
        try:
            import pyautogui
            self._pyautogui = pyautogui
            # Enable failsafe - move mouse to corner to abort
            self._pyautogui.FAILSAFE = True
            logger.debug("PyAutoGUI initialized for computer control")
        except ImportError:
            logger.warning("pyautogui not available - computer control limited")
        except Exception as e:
            logger.warning(f"Failed to initialize pyautogui: {e}")

    def is_available(self) -> bool:
        """Check if computer control is available.

        Returns:
            True if pyautogui is ready, False otherwise
        """
        return self._pyautogui is not None

    def _validate_point(self, x: Any, y: Any, source: str) -> ValidationResult:
        """Validate a point coordinate. Returns validation result enum."""
        outcome = self._coordinate_validator.validate_point(x, y, source)
        if not outcome.is_valid:
            logger.warning("Coordinate validation failed: %s - %s", source, outcome.message)
        return outcome.result

    def _validate_rectangle(self, rect: Dict[str, Any], source: str) -> Optional[ValidatedRectangle]:
        """
        Validate a rectangle coordinate.

        Returns:
            ValidatedRectangle if valid, None if invalid (logs warning)
        """
        outcome = self._coordinate_validator.validate_rectangle(rect, source)
        if not outcome.is_valid:
            logger.warning("Rectangle validation failed: %s - %s", source, outcome.message)
            return None
        return outcome.validated

    def click_at(self, x: int, y: int, button: str = "left", clicks: int = 1,
                    permission_level: PermissionLevel = PermissionLevel.MODERATE) -> ToolResult:
        """Click at specific screen coordinates.

        Args:
            x: X coordinate in screen pixels
            y: Y coordinate in screen pixels
            button: Mouse button ("left", "right", "middle")
            clicks: Number of clicks
            permission_level: Permission level for this action (MODERATE or higher)

        Returns:
            ToolResult indicating success or failure
        """
        # COORDINATE SAFETY: Validate BEFORE permission check
        validation = self._validate_point(x, y, f"click_at ({x},{y})")
        if validation != ValidationResult.VALID:
            return ToolResult.fail(f"Coordinate validation failed: {validation.value}")

        if not self.is_available():
            return ToolResult.fail("Computer control not available - pyautogui not installed")

        # Use declared permission level - NEVER downgrade
        # Coordinate actions are at least MODERATE
        effective_level = max(permission_level, PermissionLevel.MODERATE)

        # Check permission (AFTER coordinate validation)
        decision = self.permission_gate.check(
            "click_at_coordinates",
            effective_level,
            f"Click {button} at ({x},{y}) {clicks} time(s)"
        )

        if not decision.allowed:
            return ToolResult.fail(f"Permission denied: {decision.reason}")

        try:
            self._pyautogui.click(x, y, clicks=clicks, button=button)
            return ToolResult.ok(
                f"Clicked {button} at ({x},{y}) {clicks} time(s)",
                x=x, y=y, button=button, clicks=clicks
            )
        except Exception as e:
            logger.exception("Failed to click at coordinates")
            return ToolResult.fail(f"Could not click at ({x},{y}): {e}")

    def click_element(
        self,
        element: UIElement,
        button: str = "left",
        clicks: int = 1,
        permission_level: PermissionLevel = PermissionLevel.MODERATE
    ) -> ToolResult:
        """Click a specific UI element.

        Args:
            element: UIElement to click
            button: Mouse button ("left", "right", "middle")
            clicks: Number of clicks
            permission_level: Permission level for this action (never downgraded)

        Returns:
            ToolResult indicating success or failure
        """
        # COORDINATE SAFETY: Validate element's bounding box before proceeding
        if not element.bounding_box:
            return ToolResult.fail("Cannot click element: no bounding box")

        validated = self._validate_rectangle(element.bounding_box, f"click_element '{element.name}'")
        if validated is None:
            return ToolResult.fail("Cannot click element: coordinate validation failed")

        # Calculate center of VALIDATED element
        center_x = validated.x + validated.width // 2
        center_y = validated.y + validated.height // 2

        return self.click_at(center_x, center_y, button, clicks, permission_level=permission_level)

    def click_by_name(
        self,
        name: str,
        control_type: Optional[str] = None,
        button: str = "left",
        clicks: int = 1,
        exact_match: bool = False,
        permission_level: PermissionLevel = PermissionLevel.MODERATE
    ) -> ToolResult:
        """Click an element by its name (semantic action).

        Args:
            name: Element name to search for (e.g., "Save", "OK", "Cancel")
            control_type: Optional control type filter (e.g., "Button", "Edit")
            button: Mouse button to use
            clicks: Number of clicks
            exact_match: Whether to require exact name match
            permission_level: Permission level for this action (never downgraded)

        Returns:
            ToolResult indicating success or failure
        """
        # Get current screen state
        state = self.perception_layer.get_current_state()

        # Search for matching elements
        matches = []
        for element in state.desktop_elements:
            if not element.is_visible or not element.is_enabled:
                continue

            # Check name match
            name_match = (
                element.name.lower() == name.lower() if exact_match
                else name.lower() in element.name.lower()
            )

            # Check control type if specified
            type_match = True
            if control_type:
                type_match = element.control_type.lower() == control_type.lower()

            if name_match and type_match:
                matches.append(element)

        if not matches:
            return ToolResult.fail(
                f"No visible, enabled element found with name containing '{name}'"
                + (f" and control type '{control_type}'" if control_type else "")
            )

        # Prefer the first match (could be improved with better heuristics)
        target_element = matches[0]
        logger.debug(f"Selected element '{target_element.name}' ({target_element.control_type}) from {len(matches)} matches")

        # Click the element with declared permission level (validates coordinates internally)
        return self.click_element(target_element, button, clicks, permission_level=permission_level)

    def type_text(self, text: str,
                    permission_level: PermissionLevel = PermissionLevel.MODERATE) -> ToolResult:
        """Type text at current cursor position.

        Args:
            text: Text to type
            permission_level: Permission level for this action (never downgraded)

        Returns:
            ToolResult indicating success or failure
        """
        if not self.is_available():
            return ToolResult.fail("Computer control not available - pyautogui not installed")

        # Typing is at least MODERATE (can modify documents, etc.)
        effective_level = max(permission_level, PermissionLevel.MODERATE)

        # Check permission
        decision = self.permission_gate.check(
            "type_text",
            effective_level,
            f"Type text: '{text[:50]}{'...' if len(text) > 50 else ''}'"
        )

        if not decision.allowed:
            return ToolResult.fail(f"Permission denied: {decision.reason}")

        try:
            self._pyautogui.write(text)
            return ToolResult.ok(f"Typed text: '{text}'", length=len(text))
        except Exception as e:
            logger.exception("Failed to type text")
            return ToolResult.fail(f"Could not type text: {e}")

    def press_key(self, key: str,
                    permission_level: PermissionLevel = PermissionLevel.MODERATE) -> ToolResult:
        """Press a keyboard key or key combination.

        Args:
            key: Key to press (e.g., "enter", "escape", "ctrl+c", "alt+tab")
            permission_level: Permission level for this action (never downgraded)

        Returns:
            ToolResult indicating success or failure
        """
        if not self.is_available():
            return ToolResult.fail("Computer control not available - pyautogui not installed")

        # Check for potentially destructive combinations - minimum SENSITIVE
        destructive_combos = ["alt+f4", "ctrl+shift+esc", "win+l"]  # Task manager, lock, etc.
        min_level = PermissionLevel.SENSITIVE if any(combo in key.lower() for combo in destructive_combos) else PermissionLevel.MODERATE

        # Use the higher of declared level and minimum required
        effective_level = max(permission_level, min_level)

        # Check permission
        decision = self.permission_gate.check(
            f"press_key_{key.replace('+', '_')}",
            effective_level,
            f"Press key: {key}"
        )

        if not decision.allowed:
            return ToolResult.fail(f"Permission denied: {decision.reason}")

        try:
            self._pyautogui.press(key)
            return ToolResult.ok(f"Pressed key: {key}", key=key)
        except Exception as e:
            logger.exception("Failed to press key")
            return ToolResult.fail(f"Could not press key '{key}': {e}")

    def hotkey(self, *keys: str, permission_level: PermissionLevel = PermissionLevel.MODERATE) -> ToolResult:
        """Press a keyboard hotkey (combination).

        Args:
            *keys: Keys to press together (e.g., "ctrl", "c" for Ctrl+C)
            permission_level: Permission level for this action (never downgraded)

        Returns:
            ToolResult indicating success or failure
        """
        if not self.is_available():
            return ToolResult.fail("Computer control not available - pyautogui not installed")

        key_combo = "+".join(keys)

        # Check for destructive combinations - minimum SENSITIVE
        destructive_combos = ["alt+f4", "ctrl+shift+esc", "win+l", "ctrl+alt+del"]
        min_level = PermissionLevel.SENSITIVE if any(combo in key_combo.lower() for combo in destructive_combos) else PermissionLevel.MODERATE

        # Use the higher of declared level and minimum required
        effective_level = max(permission_level, min_level)

        # Check permission
        decision = self.permission_gate.check(
            f"hotkey_{'+'.join(keys)}",
            effective_level,
            f"Press hotkey: {key_combo}"
        )

        if not decision.allowed:
            return ToolResult.fail(f"Permission denied: {decision.reason}")

        try:
            self._pyautogui.hotkey(*keys)
            return ToolResult.ok(f"Pressed hotkey: {key_combo}", keys=list(keys))
        except Exception as e:
            logger.exception("Failed to press hotkey")
            return ToolResult.fail(f"Could not press hotkey '{key_combo}': {e}")

    def find_and_click(
        self,
        target_name: str,
        control_type: Optional[str] = None,
        button: str = "left",
        clicks: int = 1,
        exact_match: bool = False,
        permission_level: PermissionLevel = PermissionLevel.MODERATE
    ) -> ToolResult:
        """Find an element by name and click it (preferred semantic action).

        This is the primary method for computer interaction - semantic first,
        coordinate fallback only if needed (though not implemented here).

        Args:
            target_name: Name of element to find and click
            control_type: Optional control type to filter by
            button: Mouse button to use
            clicks: Number of clicks
            exact_match: Whether to require exact name match
            permission_level: Permission level for this action (never downgraded)

        Returns:
            ToolResult indicating success or failure
        """
        # Try semantic action first
        result = self.click_by_name(
            target_name,
            control_type=control_type,
            button=button,
            clicks=clicks,
            exact_match=exact_match,
            permission_level=permission_level
        )

        if result.success:
            return result

        # Semantic failed - log but don't fall back to coordinates in this implementation
        # Coordinate fallback would require additional context about where to look
        logger.debug(f"Semantic click failed for '{target_name}': {result.message}")
        return result

    def get_cursor_position(self) -> Optional[Tuple[int, int]]:
        """Get current cursor position.

        Returns:
            (x, y) tuple or None if unavailable
        """
        if not self.is_available():
            return None

        try:
            pos = self._pyautogui.position()
            return (pos.x, pos.y)
        except Exception as e:
            logger.warning(f"Failed to get cursor position: {e}")
            return None

    def move_to(self, x: int, y: int, duration: float = 0.5,
                  permission_level: PermissionLevel = PermissionLevel.SAFE) -> ToolResult:
        """Move mouse to specific coordinates.

        Args:
            x: X coordinate
            y: Y coordinate
            duration: Time to take for movement (seconds)
            permission_level: Permission level for this action (never downgraded)

        Returns:
            ToolResult indicating success or failure
        """
        # COORDINATE SAFETY: Validate BEFORE permission check
        validation = self._validate_point(x, y, f"move_to ({x},{y})")
        if validation != ValidationResult.VALID:
            return ToolResult.fail(f"Coordinate validation failed: {validation.value}")

        if not self.is_available():
            return ToolResult.fail("Computer control not available - pyautogui not installed")

        # Mouse movement uses declared level (SAFE minimum)
        effective_level = permission_level

        # Check permission (AFTER coordinate validation)
        decision = self.permission_gate.check(
            "move_mouse",
            effective_level,
            f"Move mouse to ({x},{y})"
        )

        if not decision.allowed:
            return ToolResult.fail(f"Permission denied: {decision.reason}")

        try:
            self._pyautogui.moveTo(x, y, duration=duration)
            return ToolResult.ok(f"Moved mouse to ({x},{y})", x=x, y=y)
        except Exception as e:
            logger.exception("Failed to move mouse")
            return ToolResult.fail(f"Could not move mouse to ({x},{y}): {e}")

    def drag_to(self, x: int, y: int, button: str = "left", duration: float = 0.5,
                  permission_level: PermissionLevel = PermissionLevel.MODERATE) -> ToolResult:
        """Drag mouse to specific coordinates.

        Args:
            x: X coordinate to drag to
            y: Y coordinate to drag to
            button: Mouse button to hold
            duration: Time to take for drag (seconds)
            permission_level: Permission level for this action (never downgraded)

        Returns:
            ToolResult indicating success or failure
        """
        # COORDINATE SAFETY: Validate BEFORE permission check
        validation = self._validate_point(x, y, f"drag_to ({x},{y})")
        if validation != ValidationResult.VALID:
            return ToolResult.fail(f"Coordinate validation failed: {validation.value}")

        if not self.is_available():
            return ToolResult.fail("Computer control not available - pyautogui not installed")

        # Dragging can modify things - at least MODERATE
        effective_level = max(permission_level, PermissionLevel.MODERATE)

        # Check permission (AFTER coordinate validation)
        decision = self.permission_gate.check(
            "drag_mouse",
            effective_level,
            f"Drag mouse to ({x},{y}) with {button} button"
        )

        if not decision.allowed:
            return ToolResult.fail(f"Permission denied: {decision.reason}")

        try:
            self._pyautogui.dragTo(x, y, duration=duration, button=button)
            return ToolResult.ok(f"Dragged mouse to ({x},{y})", x=x, y=y, button=button)
        except Exception as e:
            logger.exception("Failed to drag mouse")
            return ToolResult.fail(f"Could not drag mouse to ({x},{y}): {e}")

    def scroll(self, clicks: int = 3,
                permission_level: PermissionLevel = PermissionLevel.SAFE) -> ToolResult:
        """Scroll mouse wheel.

        Args:
            clicks: Number of scroll clicks (positive = up, negative = down)
            permission_level: Permission level for this action (never downgraded)

        Returns:
            ToolResult indicating success or failure
        """
        if not self.is_available():
            return ToolResult.fail("Computer control not available - pyautogui not installed")

        # Scrolling is generally safe - use declared level
        effective_level = permission_level

        # Check permission
        decision = self.permission_gate.check(
            "scroll_mouse",
            effective_level,
            f"Scroll mouse wheel {clicks} click(s)"
        )

        if not decision.allowed:
            return ToolResult.fail(f"Permission denied: {decision.reason}")

        try:
            self._pyautogui.scroll(clicks)
            return ToolResult.ok(f"Scrolled mouse wheel {clicks} click(s)", clicks=clicks)
        except Exception as e:
            logger.exception("Failed to scroll mouse")
            return ToolResult.fail(f"Could not scroll mouse wheel: {e}")


def create_computer_controller(
    permission_gate: Optional[PermissionGate] = None,
    perception_layer: Optional[PerceptionLayer] = None,
    coordinate_validator: Optional[CoordinateSafetyValidator] = None,
    screen_bounds_provider: Optional[ScreenBoundsProvider] = None,
) -> ComputerController:
    """Create a computer controller with optional dependencies.

    Args:
        permission_gate: Permission gate for authorization (creates default if None)
        perception_layer: Perception layer for screen state (creates default if None)
        coordinate_validator: Coordinate safety validator (creates default if None)
        screen_bounds_provider: Screen bounds provider for validator

    Returns:
        ComputerController instance
    """
    return ComputerController(
        permission_gate=permission_gate,
        perception_layer=perception_layer,
        coordinate_validator=coordinate_validator,
        screen_bounds_provider=screen_bounds_provider,
    )