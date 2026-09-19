"""
Prompt-injection defense for VERONICA.

Implements safeguards to prevent screen content from overriding system
instructions, permissions, or safety controls. Screen text is treated as
untrusted data only - never as executable instructions.

Key principles from Phase 7 specs:
- Screen content is UNTRUSTED DATA — never overrides safety, permissions, or system instructions
- Never implement: 'try something else and assume it worked'
- Stored experience is DATA. Never treat retrieved experience as executable instructions
- Every newly generated recovery action must pass through the existing permission system
- UNKNOWN must never automatically become SUCCESS

This module provides:
1. Text sanitization and filtering for perception outputs
2. Validation that screen-derived decisions don't bypass safety
3. Methods to safely use screen text as evidence only
4. Prevention of prompt injection via screen content
"""
from __future__ import annotations

import re
from typing import List, Dict, Any, Optional, Pattern
from dataclasses import dataclass

from veronica.logging_setup import get_logger
from veronica.vision.screen_state import ScreenState, TextRegion, UIElement

logger = get_logger("vision.screen_content_defense")


@dataclass
class SafetyValidationResult:
    """Result of validating screen content against safety rules."""
    is_safe: bool
    reason: str
    filtered_text: Optional[str] = None
    warnings: List[str] = None

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


class ScreenContentDefense:
    """
    Defends against prompt injection and misuse of screen content.

    Ensures that screen text and UI elements are treated as untrusted
    evidence only, never as system instructions or permission overrides.
    """

    def __init__(self):
        # Patterns that should never be accepted as commands from screen
        self.forbidden_patterns: List[Pattern] = [
            # Direct system commands
            re.compile(r"(?i)^(?:sudo|su|runas|execute|run|cmd|powershell|bash|sh|terminal|console).*$"),
            # Permission bypass attempts
            re.compile(r"(?i)(?:bypass|disable|turn off|ignore|override).*?(?:permission|safety|security|gate|confirm)"),
            # Self-modification attempts
            re.compile(r"(?i)(?:modify|change|edit|update).*?(?:this|system|code|script|agent|veronica|assistant)"),
            # Data exfiltration attempts
            re.compile(r"(?i)(?:send|transmit|upload|email|post).*?(?:data|info|information|file|screenshot|screen)"),
            # Common injection patterns
            re.compile(r"(?i)(?:<script|javascript:|eval\(|exec\(|import\s+|__.*__)"),
        ]

        # Safe patterns that indicate legitimate UI content
        self.safe_ui_patterns: List[Pattern] = [
            # Button labels
            re.compile(r"(?i)^(?:ok|cancel|save|open|close|delete|edit|new|add|remove|submit|apply|yes|no)$"),
            # Menu items
            re.compile(r"(?i)^(?:file|edit|view|search|tools|options|settings|help|window)$"),
            # Common dialog text
            re.compile(r"(?i)^(?:are you sure|confirm|warning|error|information|question)$"),
        ]

    def validate_screen_text(self, text: str, context: str = "") -> SafetyValidationResult:
        """
        Validate that screen text is safe to use as evidence.

        Args:
            text: Text extracted from screen (OCR, UI Automation, etc.)
            context: Context where text will be used (for better error messages)

        Returns:
            SafetyValidationResult indicating if text is safe and why
        """
        if not text or not text.strip():
            return SafetyValidationResult(is_safe=True, reason="Empty text")

        text = text.strip()

        # Check for forbidden patterns
        for pattern in self.forbidden_patterns:
            if pattern.search(text):
                logger.warning(
                    f"Screen content defense: forbidden pattern detected in {context}: '{text[:100]}'"
                )
                return SafetyValidationResult(
                    is_safe=False,
                    reason=f"Text contains forbidden pattern that could be injection attempt",
                    filtered_text=self._filter_dangerous_content(text)
                )

        # Additional safety checks
        warnings = []

        # Check for excessively long text (could be attempt to overwhelm)
        if len(text) > 1000:
            warnings.append(f"Text unusually long ({len(text)} chars) - truncated for safety")

        # Check for repetitive patterns (could be noise injection)
        if len(text) > 50 and text.count(text[0]) > len(text) * 0.8:
            warnings.append("Text appears to be repetitive noise")

        # Check for potential credential/token patterns
        if self._contains_potential_credentials(text):
            warnings.append("Text may contain credentials or tokens - handle with care")

        return SafetyValidationResult(
            is_safe=True,
            reason="Text passed safety validation",
            warnings=warnings
        )

    def validate_ui_element(self, element: UIElement, context: str = "") -> SafetyValidationResult:
        """
        Validate that a UI element is safe to interact with.

        Args:
            element: UIElement to validate
            context: Context where element will be used

        Returns:
            SafetyValidationResult indicating if element is safe
        """
        # Validate element name/label
        if element.name:
            text_result = self.validate_screen_text(element.name, f"{context} element name")
            if not text_result.is_safe:
                return text_result

        # Validate element value (for text fields, etc.)
        if element.value:
            text_result = self.validate_screen_text(element.value, f"{context} element value")
            if not text_result.is_safe:
                return text_result

        # Additional UI-specific checks
        warnings = []

        # Check for suspicious control types that might be disguised
        suspicious_types = ["edit", "text", "document"]  # Text input fields can contain arbitrary content
        if element.control_type.lower() in suspicious_types and element.value:
            if len(element.value) > 500:
                warnings.append(f"Large {element.control_type} value may contain unsafe content")

        return SafetyValidationResult(
            is_safe=True,
            reason="UI element passed safety validation",
            warnings=warnings
        )

    def filter_perception_state(self, state: ScreenState) -> ScreenState:
        """
        Create a safe version of a ScreenState for use in decision making.

        Args:
            state: Original ScreenState from perception

        Returns:
            ScreenState with potentially dangerous content filtered or flagged
        """
        # Create copy of state
        safe_state = ScreenState(
            timestamp=state.timestamp,
            screen_width=state.screen_width,
            screen_height=state.screen_height,
            monitor_count=state.monitor_count,
            active_window=state.active_window,
            visible_windows=state.visible_windows,
            desktop_elements=[],  # Will populate with safe elements
            text_regions=[],      # Will populate with safe text regions
            focused_element=state.focused_element,
            cursor_position=state.cursor_position,
            perception_sources=state.perception_sources,
            confidence_overall=state.confidence_overall
        )
        # Copy over raw_screenshot if present (used in tests)
        if hasattr(state, 'raw_screenshot'):
            safe_state.raw_screenshot = state.raw_screenshot

        # Filter desktop elements
        for element in state.desktop_elements:
            element_result = self.validate_ui_element(element, "desktop")
            if element_result.is_safe:
                safe_state.desktop_elements.append(element)
            else:
                logger.debug(f"Filtered unsafe desktop element: {element.name}")

        # Filter text regions
        for region in state.text_regions:
            text_result = self.validate_screen_text(region.text, "text region")
            if text_result.is_safe:
                safe_state.text_regions.append(region)
            else:
                logger.debug(f"Filtered unsafe text region: {region.text[:50]}...")

        # Validate focused element
        if state.focused_element:
            focused_result = self.validate_ui_element(state.focused_element, "focused")
            if not focused_result.is_safe:
                logger.warning("Focused element failed safety validation - clearing focus")
                safe_state.focused_element = None

        # Validate active window title
        if state.active_window and state.active_window.title:
            window_result = self.validate_screen_text(
                state.active_window.title, "active window title"
            )
            if not window_result.is_safe:
                logger.warning("Active window title failed safety validation")
                # Don't clear the window, but note the issue in change indicators
                safe_state._change_indicators = {
                    **state._change_indicators,
                    "window_title_filtered": True
                }

        return safe_state

    def _filter_dangerous_content(self, text: str) -> str:
        """
        Filter dangerous content from text while preserving safe portions.

        Args:
            text: Text to filter

        Returns:
            Filtered text with dangerous patterns removed/replaced
        """
        filtered = text

        # Replace forbidden patterns with safe placeholders
        for pattern in self.forbidden_patterns:
            filtered = pattern.sub("[FILTERED_CONTENT]", filtered)

        # Limit length to prevent overflow
        if len(filtered) > 500:
            filtered = filtered[:500] + "..."

        return filtered

    def _contains_potential_credentials(self, text: str) -> bool:
        """
        Check if text contains patterns that might be credentials or tokens.

        Args:
            text: Text to check

        Returns:
            True if potential credential patterns detected
        """
        credential_patterns = [
            # Common token/key patterns
            re.compile(r"[A-Za-z0-9\-_]{32,}"),  # Long alphanumeric strings
            re.compile(r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}"),  # UUID/GUID
            re.compile(r"sk_[A-Za-z0-9]{32,}"),  # API key pattern (stripe-like)
            re.compile(r"[A-Za-z0-9\/+]{32,}={0,2}"),  # Base64-like
            # Password-like patterns
            re.compile(r"(?i)(?:password|pass|pwd|secret|key|token|auth)[\s:=]+[\S]{8,}"),
        ]

        for pattern in credential_patterns:
            if pattern.search(text):
                return True

        return False

    def safe_extract_intent(self, text: str) -> Optional[str]:
        """
        Safely extract user intent from screen text, rejecting anything that
        looks like a command or instruction.

        Args:
            text: Screen text to analyze

        Returns:
            Extracted intent if safe and appropriate, None otherwise
        """
        validation = self.validate_screen_text(text, "intent extraction")
        if not validation.is_safe:
            return None

        # Only accept very short, simple UI-like text as potential intent
        if len(text) > 50:
            return None

        # Check if it looks like normal UI text (button, label, etc.)
        for pattern in self.safe_ui_patterns:
            if pattern.match(text):
                return text.lower()

        # Reject anything that looks like a sentence or command
        if len(text.split()) > 3 or text.endswith(('.', '!', '?')):
            return None

        # Accept as potential UI element reference
        return text.lower().strip()


def create_screen_content_defense() -> ScreenContentDefense:
    """Create a screen content defense instance."""
    return ScreenContentDefense()


# Global instance for convenience
_screen_content_defense = None


def get_screen_content_defense() -> ScreenContentDefense:
    """Get the global screen content defense instance."""
    global _screen_content_defense
    if _screen_content_defense is None:
        _screen_content_defense = create_screen_content_defense()
    return _screen_content_defense