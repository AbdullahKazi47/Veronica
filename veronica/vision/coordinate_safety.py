"""
Coordinate Safety Validator for VERONICA.

Provides a strict validation boundary between UIA perception/target selection
and physical computer control (PyAutoGUI). Corrupted or invalid coordinates
are rejected before they can reach the permission gate or PyAutoGUI.

Key principles:
- No silent clamping, guessing, or repair of invalid coordinates
- Structured validation results with clear failure reasons
- Support for legitimate negative virtual-desktop origins (multi-monitor)
- Dependency injection for testability without real mouse movement
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple, Dict, Any

from veronica.logging_setup import get_logger

logger = get_logger("vision.coordinate_safety")


class ValidationResult(str, Enum):
    """Structured coordinate validation outcomes."""
    VALID = "valid"
    INVALID_RECTANGLE = "invalid_rectangle"
    TARGET_COORDINATES_INVALID = "target_coordinates_invalid"
    SCREEN_BOUNDS_UNAVAILABLE = "screen_bounds_unavailable"
    RECTANGLE_CENTER_OFFSCREEN = "rectangle_center_offscreen"
    RECTANGLE_OFFSCREEN = "rectangle_offscreen"
    MALFORMED_DATA = "malformed_data"


@dataclass(frozen=True)
class ValidatedRectangle:
    """A rectangle that has passed all validation checks."""
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> Tuple[int, int]:
        """Center point of the rectangle."""
        return (self.x + self.width // 2, self.y + self.height // 2)

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def to_dict(self) -> Dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(frozen=True)
class ValidationOutcome:
    """Result of coordinate validation."""
    result: ValidationResult
    message: str
    validated: Optional[ValidatedRectangle] = None
    details: Dict[str, Any] = None

    def __post_init__(self):
        if self.details is None:
            object.__setattr__(self, "details", {})

    @property
    def is_valid(self) -> bool:
        return self.result == ValidationResult.VALID

    def to_dict(self) -> Dict[str, Any]:
        return {
            "result": self.result.value,
            "message": self.message,
            "validated": self.validated.to_dict() if self.validated else None,
            "details": self.details,
        }


class ScreenBoundsProvider:
    """
    Abstract provider for virtual-desktop bounds.

    Allows dependency injection for testing and supports Windows multi-monitor
    configurations where the virtual desktop origin may be negative.
    """

    def get_virtual_desktop_bounds(self) -> Optional[Tuple[int, int, int, int]]:
        """
        Get the virtual desktop bounds.

        Returns:
            Tuple of (left, top, width, height) in screen coordinates,
            or None if unavailable.
        """
        raise NotImplementedError


class PyAutoGUIScreenBoundsProvider(ScreenBoundsProvider):
    """Screen bounds provider using PyAutoGUI (production implementation)."""

    def __init__(self, pyautogui_module=None):
        self._pyautogui = pyautogui_module
        self._cached_bounds: Optional[Tuple[int, int, int, int]] = None

    def get_virtual_desktop_bounds(self) -> Optional[Tuple[int, int, int, int]]:
        if self._cached_bounds is not None:
            return self._cached_bounds

        try:
            if self._pyautogui is None:
                import pyautogui as _pyautogui
                self._pyautogui = _pyautogui

            # PyAutoGUI's size() returns the primary monitor size
            # For virtual desktop bounds, we need platform-specific approach
            # On Windows, use GetSystemMetrics to get virtual screen bounds
            try:
                import ctypes
                user32 = ctypes.windll.user32
                # Virtual screen metrics
                SM_XVIRTUALSCREEN = 76
                SM_YVIRTUALSCREEN = 77
                SM_CXVIRTUALSCREEN = 78
                SM_CYVIRTUALSCREEN = 79

                left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
                top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
                width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
                height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)

                self._cached_bounds = (left, top, width, height)
                logger.debug(
                    "Virtual desktop bounds: left=%d, top=%d, width=%d, height=%d",
                    left, top, width, height
                )
                return self._cached_bounds
            except Exception:
                # Fallback to primary monitor size
                try:
                    primary_size = self._pyautogui.size()
                    # Assume origin at (0,0) for primary monitor
                    self._cached_bounds = (0, 0, primary_size.width, primary_size.height)
                    logger.debug(
                        "Fallback to primary monitor bounds: (0, 0, %d, %d)",
                        primary_size.width, primary_size.height
                    )
                    return self._cached_bounds
                except Exception as e:
                    logger.warning("Could not determine screen bounds via PyAutoGUI: %s", e)

        except Exception as e:
            logger.warning("PyAutoGUIScreenBoundsProvider failed: %s", e)

        return None


class CoordinateSafetyValidator:
    """
    Validates coordinate rectangles before they reach physical computer control.

    This validator sits between UIA perception (which can produce corrupted
    coordinates) and the PermissionGate/PyAutoGUI execution path.

    Usage:
        validator = CoordinateSafetyValidator(screen_bounds_provider=provider)
        outcome = validator.validate_rectangle(raw_rect)
        if not outcome.is_valid:
            # Reject action - do NOT proceed to PermissionGate or PyAutoGUI
        else:
            # outcome.validated is a safe ValidatedRectangle
    """

    # Maximum reasonable rectangle dimension (to catch absurdly large UIA corruption)
    MAX_REASONABLE_DIMENSION = 100000
    # Maximum reasonable absolute coordinate value (for center check)
    MAX_REASONABLE_COORDINATE = 50000

    def __init__(
        self,
        screen_bounds_provider: Optional[ScreenBoundsProvider] = None,
    ):
        """
        Initialize the validator.

        Args:
            screen_bounds_provider: Provider for virtual desktop bounds.
                Defaults to PyAutoGUIScreenBoundsProvider for production.
        """
        self._provider = screen_bounds_provider or PyAutoGUIScreenBoundsProvider()
        self._virtual_desktop: Optional[Tuple[int, int, int, int]] = None

    def _get_virtual_desktop(self) -> Optional[Tuple[int, int, int, int]]:
        """Get virtual desktop bounds, caching the result."""
        if self._virtual_desktop is None:
            self._virtual_desktop = self._provider.get_virtual_desktop_bounds()
        return self._virtual_desktop

    def validate_rectangle(
        self,
        rect: Dict[str, Any],
        source_description: str = "UIA rectangle"
    ) -> ValidationOutcome:
        """
        Validate a rectangle before it can be used for coordinate-based actions.

        Args:
            rect: Dictionary with keys x, y, width, height (numeric values)
            source_description: Human-readable description for audit logging

        Returns:
            ValidationOutcome with result and (if valid) normalized ValidatedRectangle
        """
        # 1. Check rectangle is a dict with required keys
        if not isinstance(rect, dict):
            return ValidationOutcome(
                result=ValidationResult.MALFORMED_DATA,
                message=f"{source_description}: not a dictionary",
                details={"type": type(rect).__name__},
            )

        required_keys = ("x", "y", "width", "height")
        for key in required_keys:
            if key not in rect:
                return ValidationOutcome(
                    result=ValidationResult.MALFORMED_DATA,
                    message=f"{source_description}: missing required key '{key}'",
                    details={"missing": key, "available": list(rect.keys())},
                )

        # 2. Extract and validate numeric values
        try:
            x = rect["x"]
            y = rect["y"]
            width = rect["width"]
            height = rect["height"]

            # Check for NaN or infinity
            for name, val in [("x", x), ("y", y), ("width", width), ("height", height)]:
                if not isinstance(val, (int, float)):
                    return ValidationOutcome(
                        result=ValidationResult.MALFORMED_DATA,
                        message=f"{source_description}: '{name}' is not numeric",
                        details={name: {"type": type(val).__name__, "value": str(val)}},
                    )
                if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                    return ValidationOutcome(
                        result=ValidationResult.MALFORMED_DATA,
                        message=f"{source_description}: '{name}' is NaN or infinity",
                        details={name: {"value": val}},
                    )

            # Convert to int (truncating float, as UIA returns floats)
            x = int(x)
            y = int(y)
            width = int(width)
            height = int(height)

        except (TypeError, ValueError, OverflowError) as e:
            return ValidationOutcome(
                result=ValidationResult.MALFORMED_DATA,
                message=f"{source_description}: could not convert to integers",
                details={"error": str(e), "raw": {k: str(rect[k]) for k in required_keys}},
            )

        # 3. Reject zero or negative dimensions
        if width <= 0:
            return ValidationOutcome(
                result=ValidationResult.INVALID_RECTANGLE,
                message=f"{source_description}: width must be > 0, got {width}",
                details={"width": width, "height": height},
            )
        if height <= 0:
            return ValidationOutcome(
                result=ValidationResult.INVALID_RECTANGLE,
                message=f"{source_description}: height must be > 0, got {height}",
                details={"width": width, "height": height},
            )

        # 4. Reject absurdly large dimensions (corrupted UIA)
        if width > self.MAX_REASONABLE_DIMENSION:
            return ValidationOutcome(
                result=ValidationResult.INVALID_RECTANGLE,
                message=f"{source_description}: width {width} exceeds maximum reasonable dimension {self.MAX_REASONABLE_DIMENSION}",
                details={"width": width, "max": self.MAX_REASONABLE_DIMENSION},
            )
        if height > self.MAX_REASONABLE_DIMENSION:
            return ValidationOutcome(
                result=ValidationResult.INVALID_RECTANGLE,
                message=f"{source_description}: height {height} exceeds maximum reasonable dimension {self.MAX_REASONABLE_DIMENSION}",
                details={"height": height, "max": self.MAX_REASONABLE_DIMENSION},
            )

        # 5. Get screen bounds for further validation
        screen_bounds = self._get_virtual_desktop()
        if screen_bounds is None:
            return ValidationOutcome(
                result=ValidationResult.SCREEN_BOUNDS_UNAVAILABLE,
                message=f"{source_description}: cannot validate position - screen bounds unavailable",
                details={"screen_bounds": None},
            )

        screen_left, screen_top, screen_width, screen_height = screen_bounds
        screen_right = screen_left + screen_width
        screen_bottom = screen_top + screen_height

        # 6. Check rectangle center is within virtual desktop
        center_x = x + width // 2
        center_y = y + height // 2

        if not (screen_left <= center_x < screen_right and screen_top <= center_y < screen_bottom):
            # Check if this is just a slightly negative origin (legitimate multi-monitor)
            # but the center is still way off
            return ValidationOutcome(
                result=ValidationResult.RECTANGLE_CENTER_OFFSCREEN,
                message=f"{source_description}: rectangle center ({center_x}, {center_y}) is outside virtual desktop ({screen_left}, {screen_top}, {screen_width}, {screen_height})",
                details={
                    "center": (center_x, center_y),
                    "screen_bounds": (screen_left, screen_top, screen_width, screen_height),
                    "rect": (x, y, width, height),
                },
            )

        # 7. Check rectangle has some overlap with virtual desktop
        rect_right = x + width
        rect_bottom = y + height

        # No overlap at all = completely off-screen
        if rect_right <= screen_left or x >= screen_right or rect_bottom <= screen_top or y >= screen_bottom:
            return ValidationOutcome(
                result=ValidationResult.RECTANGLE_OFFSCREEN,
                message=f"{source_description}: rectangle ({x}, {y}, {width}, {height}) is completely outside virtual desktop ({screen_left}, {screen_top}, {screen_width}, {screen_height})",
                details={
                    "rect": (x, y, width, height),
                    "screen_bounds": (screen_left, screen_top, screen_width, screen_height),
                },
            )

        # 8. Check for clearly corrupted UIA coordinates (absurd absolute values)
        # even if they somehow pass the screen bounds check
        max_abs = max(abs(x), abs(y), abs(rect_right), abs(rect_bottom), abs(center_x), abs(center_y))
        if max_abs > self.MAX_REASONABLE_COORDINATE:
            return ValidationOutcome(
                result=ValidationResult.TARGET_COORDINATES_INVALID,
                message=f"{source_description}: coordinates contain absurdly large values (max_abs={max_abs} > {self.MAX_REASONABLE_COORDINATE})",
                details={
                    "x": x, "y": y, "width": width, "height": height,
                    "center": (center_x, center_y),
                    "max_abs": max_abs,
                    "limit": self.MAX_REASONABLE_COORDINATE,
                },
            )

        # All checks passed - return validated rectangle
        validated = ValidatedRectangle(x=x, y=y, width=width, height=height)
        logger.debug(
            "%s validated: x=%d, y=%d, w=%d, h=%d, center=(%d, %d)",
            source_description, x, y, width, height, center_x, center_y
        )
        return ValidationOutcome(
            result=ValidationResult.VALID,
            message=f"{source_description}: validation passed",
            validated=validated,
            details={
                "original": (rect.get("x"), rect.get("y"), rect.get("width"), rect.get("height")),
                "validated": (x, y, width, height),
                "center": (center_x, center_y),
                "screen_bounds": (screen_left, screen_top, screen_width, screen_height),
            },
        )

    def validate_point(self, x: Any, y: Any, source_description: str = "point") -> ValidationOutcome:
        """
        Validate a single point coordinate.

        Args:
            x: X coordinate
            y: Y coordinate
            source_description: Human-readable description for audit logging

        Returns:
            ValidationOutcome (validated will be None for points, use coordinates directly)
        """
        # Check numeric
        try:
            if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
                return ValidationOutcome(
                    result=ValidationResult.MALFORMED_DATA,
                    message=f"{source_description}: x is NaN or infinity",
                )
            if isinstance(y, float) and (math.isnan(y) or math.isinf(y)):
                return ValidationOutcome(
                    result=ValidationResult.MALFORMED_DATA,
                    message=f"{source_description}: y is NaN or infinity",
                )
            x = int(x)
            y = int(y)
        except (TypeError, ValueError, OverflowError) as e:
            return ValidationOutcome(
                result=ValidationResult.MALFORMED_DATA,
                message=f"{source_description}: could not convert to integers: {e}",
            )

        # Check absurd values
        if abs(x) > self.MAX_REASONABLE_COORDINATE or abs(y) > self.MAX_REASONABLE_COORDINATE:
            return ValidationOutcome(
                result=ValidationResult.TARGET_COORDINATES_INVALID,
                message=f"{source_description}: coordinates ({x}, {y}) exceed reasonable bounds",
                details={"x": x, "y": y, "limit": self.MAX_REASONABLE_COORDINATE},
            )

        # Check screen bounds
        screen_bounds = self._get_virtual_desktop()
        if screen_bounds is None:
            return ValidationOutcome(
                result=ValidationResult.SCREEN_BOUNDS_UNAVAILABLE,
                message=f"{source_description}: screen bounds unavailable",
            )

        screen_left, screen_top, screen_width, screen_height = screen_bounds
        if not (screen_left <= x < screen_left + screen_width and screen_top <= y < screen_top + screen_height):
            return ValidationOutcome(
                result=ValidationResult.RECTANGLE_CENTER_OFFSCREEN,
                message=f"{source_description}: point ({x}, {y}) outside virtual desktop",
                details={"point": (x, y), "screen_bounds": screen_bounds},
            )

        return ValidationOutcome(
            result=ValidationResult.VALID,
            message=f"{source_description}: point validation passed",
            details={"point": (x, y)},
        )


def create_coordinate_validator(
    screen_bounds_provider: Optional[ScreenBoundsProvider] = None,
) -> CoordinateSafetyValidator:
    """Factory function to create a CoordinateSafetyValidator."""
    return CoordinateSafetyValidator(screen_bounds_provider)