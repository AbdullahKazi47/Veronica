"""
Windows UI Automation adapter for VERONICA perception.

This module provides a Windows-native UI perception adapter using Microsoft's
Windows UI Automation (UIA) framework via comtypes. It exposes UI elements,
windows, applications, and their properties in a VERONICA-friendly format.

The adapter gracefully handles unavailability (non-Windows platforms, missing
COM libraries) by returning empty perception data rather than crashing.

Key capabilities:
- Discover windows, applications, and their properties
- Enumerate UI elements (buttons, text fields, menus, etc.)
- Get element names, control types, bounding boxes, states
- Access focused element and cursor position
- Integrate with VERONICA's ScreenState abstraction

Usage:
    adapter = UIAutomationAdapter()
    if adapter.is_available():
        elements = adapter.get_desktop_elements()
        windows = adapter.get_windows()
        focused = adapter.get_focused_element()
    else:
        # Fallback to other perception methods (OCR, etc.)
"""
from __future__ import annotations

import platform
import time
from typing import Dict, List, Optional
from veronica.logging_setup import get_logger
from veronica.vision.screen_state import UIElement, WindowInfo, TextRegion

logger = get_logger("vision.uia_adapter")


class UIAutomationAdapter:
    """
    Windows UI Automation perception adapter.

    Uses comtypes to access Microsoft UI Automation API for discovering
    and inspecting UI elements on Windows desktops.
    """

    def __init__(self):
        """Initialize the UIA adapter."""
        self._uia_available = False
        self._uia_modules = {}
        self._root_element = None
        self._initialize_uia()

    @property
    def _uia_module(self):
        """Get the UI Automation COM module."""
        return self._uia_modules.get("uia_module")

    def _initialize_uia(self) -> None:
        """Initialize UI Automation components if available."""
        # Only available on Windows
        if platform.system() != "Windows":
            logger.debug("UI Automation adapter only available on Windows")
            return

        try:
            import comtypes
            from comtypes.client import GetModule, CreateObject

            # Load UI Automation COM types
            uia_module = GetModule("UIAutomationCore.dll")

            # Try to import UIAutomationClient if available (may not be generated yet)
            UIAutomationClient = None
            try:
                from comtypes.gen import UIAutomationClient
            except ImportError:
                logger.debug("UIAutomationClient not in comtypes.gen - using uia_module directly")

            self._uia_modules = {
                "comtypes": comtypes,
                "GetModule": GetModule,
                "CreateObject": CreateObject,
                "UIAutomationClient": UIAutomationClient,
                "uia_module": uia_module
            }

            # Try multiple ways to create UI Automation instance
            uia = None
            initialization_errors = []

            # Method 1: Try ProgID (most common)
            try:
                uia = CreateObject(
                    "UIAutomationClient.CUIAutomation",
                    interface=uia_module.IUIAutomation
                )
                logger.debug("UI Automation initialized via ProgID")
            except Exception as e:
                initialization_errors.append(f"ProgID failed: {e}")

            # Method 2: Try CLSID if ProgID failed
            if uia is None:
                try:
                    uia = CreateObject(
                        "{FF48DBA4-60EF-4201-AA87-54103EEF594E}",
                        interface=uia_module.IUIAutomation
                    )
                    logger.debug("UI Automation initialized via CLSID")
                except Exception as e:
                    initialization_errors.append(f"CLSID failed: {e}")

            # Method 3: Try CoCreateInstance as fallback
            if uia is None:
                try:
                    uia = comtypes.client.CoCreateInstance(
                        "{FF48DBA4-60EF-4201-AA87-54103EEF594E}",
                        interface=uia_module.IUIAutomation
                    )
                    logger.debug("UI Automation initialized via CoCreateInstance")
                except Exception as e:
                    initialization_errors.append(f"CoCreateInstance failed: {e}")

            # Method 4: Try direct instantiation from generated module if available
            if uia is None and UIAutomationClient is not None:
                try:
                    uia = UIAutomationClient.CUIAutomation()
                    logger.debug("UI Automation initialized via direct instantiation")
                except Exception as e:
                    initialization_errors.append(f"Direct instantiation failed: {e}")

            # Method 5: Try using comtypes.client.CreateObject without interface specification
            if uia is None:
                try:
                    uia = CreateObject("UIAutomationClient.CUIAutomation")
                    logger.debug("UI Automation initialized via ProgID without interface")
                except Exception as e:
                    initialization_errors.append(f"ProgID (no interface) failed: {e}")

            if uia is None:
                raise RuntimeError(f"All UI Automation initialization attempts failed: {'; '.join(initialization_errors)}")

            self._uia_modules["uia"] = uia

            # Get root element (desktop)
            self._root_element = uia.GetRootElement()
            self._uia_available = True

            logger.info("UI Automation adapter initialized successfully")

        except ImportError as e:
            logger.debug(f"UI Automation dependencies not available: {e}")
            self._uia_available = False
        except Exception as e:
            logger.warning(f"Failed to initialize UI Automation: {e}")
            self._uia_available = False

    def is_available(self) -> bool:
        """Check if UI Automation is available and initialized.

        Returns:
            True if UIA is ready to use, False otherwise
        """
        return self._uia_available and self._uia_modules.get("uia") is not None

    def get_windows(self) -> List[WindowInfo]:
        """Get all visible windows on the desktop.

        Returns:
            List of WindowInfo objects representing visible windows
        """
        if not self.is_available():
            return []

        try:
            uia = self._uia_modules["uia"]
            # Condition for visible windows
            condition = uia.CreatePropertyCondition(
                self._uia_module.UIA_ControlTypePropertyId,
                self._uia_module.UIA_WindowControlTypeId
            )

            # Find all window elements
            windows_elements = self._root_element.FindAll(
                self._uia_module.TreeScope_Descendants,
                condition
            )

            windows = []
            for i in range(windows_elements.Length):
                try:
                    element = windows_elements.GetElement(i)
                    window_info = self._element_to_window_info(element)
                    if window_info and window_info.is_visible:
                        windows.append(window_info)
                except Exception as e:
                    logger.debug(f"Skipping window element {i}: {e}")
                    continue

            logger.debug(f"Found {len(windows)} visible windows via UIA")
            return windows

        except Exception as e:
            logger.warning(f"Failed to get windows via UIA: {e}")
            return []

    def get_active_window(self) -> Optional[WindowInfo]:
        """Get the currently active/focused window.

        Returns:
            WindowInfo for active window, or None if not available
        """
        if not self.is_available():
            return None

        try:
            uia = self._uia_modules["uia"]
            focused_element = uia.GetFocusedElement()
            if not focused_element:
                return None

            # PRIMARY: match focused element's PID against enumerated windows
            try:
                focused_pid = focused_element.GetCurrentPropertyValue(
                    self._uia_module.UIA_ProcessIdPropertyId
                )
                if focused_pid is not None:
                    focused_pid = int(focused_pid)
                    windows = self.get_windows()
                    for w in windows:
                        if w.process_id == focused_pid:
                            return w
            except Exception:
                pass

            # FALLBACK: walk up to find a UIA Window ancestor
            window_element = self._find_window_ancestor(focused_element)
            if window_element:
                return self._element_to_window_info(window_element)

            return None

        except Exception as e:
            logger.warning(f"Failed to get active window via UIA: {e}")
            return None

    def get_desktop_elements(self) -> List[UIElement]:
        """Get top-level UI elements on the desktop.

        Returns:
            List of UIElement objects representing desktop-level UI
        """
        if not self.is_available():
            return []

        try:
            uia = self._uia_modules["uia"]

            # Get immediate children of desktop (taskbar, desktop icons, etc.)
            # Use FindAll with TreeScope_Children instead of GetCachedChildren
            children = self._root_element.FindAll(
                self._uia_module.TreeScope_Children,
                uia.CreateTrueCondition()
            )

            elements = []
            if children:
                for i in range(children.Length):
                    try:
                        element = children.GetElement(i)
                        ui_element = self._element_to_ui_element(element, include_children=False)
                        if ui_element:
                            elements.append(ui_element)
                    except Exception as e:
                        logger.debug(f"Skipping desktop element {i}: {e}")
                        continue

            logger.debug(f"Found {len(elements)} desktop elements via UIA")
            return elements

        except Exception as e:
            logger.warning(f"Failed to get desktop elements via UIA: {e}")
            return []

    def get_focused_element(self) -> Optional[UIElement]:
        """Get the currently focused UI element.

        Returns:
            UIElement for focused element, or None if not available
        """
        if not self.is_available():
            return None

        try:
            uia = self._uia_modules["uia"]
            focused_element = uia.GetFocusedElement()
            if focused_element:
                return self._element_to_ui_element(focused_element, include_children=False)
            return None

        except Exception as e:
            logger.warning(f"Failed to get focused element via UIA: {e}")
            return None

    def get_element_at_point(self, x: int, y: int) -> Optional[UIElement]:
        """Get UI element at specific screen coordinates.

        Args:
            x: X coordinate in screen pixels
            y: Y coordinate in screen pixels

        Returns:
            UIElement at coordinates, or None if not available/not found
        """
        if not self.is_available():
            return None

        try:
            uia = self._uia_modules["uia"]
            point = uia.CreatePoint(x, y)
            element = uia.ElementFromPoint(point)
            if element:
                return self._element_to_ui_element(element, include_children=False)
            return None

        except Exception as e:
            logger.debug(f"Failed to get element at point ({x},{y}): {e}")
            return None

    def _find_window_ancestor(self, element) -> Optional[object]:
        """Walk up the element tree to find a window ancestor.

        Args:
            element: Starting UI Automation element

        Returns:
            Window element or None if not found
        """
        try:
            uia = self._uia_modules["uia"]
            current = element

            while current:
                # Check if current element is a window
                control_type = current.GetCurrentPropertyValue(
                    self._uia_module.UIA_ControlTypePropertyId
                )
                if control_type == self._uia_module.UIA_WindowControlTypeId:
                    return current

                # Get parent
                try:
                    current = current.GetCurrentParent()
                except:
                    break  # No parent or error

            return None

        except Exception as e:
            logger.debug(f"Error finding window ancestor: {e}")
            return None

    def _element_to_window_info(self, element) -> Optional[WindowInfo]:
        """Convert UI Automation element to WindowInfo.

        Args:
            element: UI Automation element representing a window

        Returns:
            WindowInfo object or None if conversion fails
        """
        try:
            uia = self._uia_modules["uia"]

            # Get basic properties
            title = element.GetCurrentPropertyValue(
                self._uia_module.UIA_NamePropertyId
            ) or ""

            class_name = element.GetCurrentPropertyValue(
                self._uia_module.UIA_ClassNamePropertyId
            ) or ""

            # Try to get process ID
            process_id = 0
            try:
                pid = element.GetCurrentPropertyValue(
                    self._uia_module.UIA_ProcessIdPropertyId
                )
                if pid is not None:
                    process_id = int(pid)
            except:
                pass

            # Get bounding rectangle — use left/right/top/bottom, NOT nonexistent width()/height()
            bounding_box = {"x": 0, "y": 0, "width": 0, "height": 0}
            try:
                rect = element.GetCurrentPropertyValue(
                    self._uia_module.UIA_BoundingRectanglePropertyId
                )
                if rect:
                    bounding_box = self._rect_to_bounding_box(rect)
            except:
                pass

            # Get window state
            is_visible = True
            try:
                is_visible = bool(element.GetCurrentPropertyValue(
                    self._uia_module.UIA_IsOffscreenPropertyId
                )) == False  # IsOffscreen inverted
            except:
                pass

            is_enabled = True
            try:
                is_enabled = bool(element.GetCurrentPropertyValue(
                    self._uia_module.UIA_IsEnabledPropertyId
                ))
            except:
                pass

            # Determine if active (has focus)
            is_active = False
            try:
                focused = uia.GetFocusedElement()
                if focused:
                    # Check if this window contains the focused element
                    is_active = self._element_contains(focused, element)
            except:
                pass

            return WindowInfo(
                window_id=str(element.GetCurrentPropertyValue(
                    self._uia_module.UIA_AutomationIdPropertyId
                )) or f"window_{hash(str(title) + str(class_name))}",
                title=str(title),
                process_name=self._get_process_name(process_id),
                process_id=process_id,
                bounding_box=bounding_box,
                is_active=is_active,
                is_visible=is_visible and is_enabled,
                is_minimized=False,  # Would need additional property checks
                is_maximized=False   # Would need additional property checks
            )

        except Exception as e:
            logger.debug(f"Failed to convert element to WindowInfo: {e}")
            return None

    def _element_to_ui_element(self, element, include_children: bool = True) -> Optional[UIElement]:
        """Convert UI Automation element to UIElement.

        Args:
            element: UI Automation element to convert
            include_children: Whether to include child elements

        Returns:
            UIElement object or None if conversion fails
        """
        try:
            uia = self._uia_modules["uia"]

            # Get basic properties
            name = element.GetCurrentPropertyValue(
                self._uia_module.UIA_NamePropertyId
            ) or ""

            # Get control type
            control_type_id = element.GetCurrentPropertyValue(
                self._uia_module.UIA_ControlTypePropertyId
            )
            control_type = self._control_type_id_to_string(control_type_id)

            # Get automation ID
            automation_id = element.GetCurrentPropertyValue(
                self._uia_module.UIA_AutomationIdPropertyId
            )

            # Get class name
            class_name = element.GetCurrentPropertyValue(
                self._uia_module.UIA_ClassNamePropertyId
            ) or ""

            # Get bounding rectangle — use left/right/top/bottom, NOT nonexistent width()/height()
            bounding_box = {"x": 0, "y": 0, "width": 0, "height": 0}
            try:
                rect = element.GetCurrentPropertyValue(
                    self._uia_module.UIA_BoundingRectanglePropertyId
                )
                if rect:
                    bounding_box = self._rect_to_bounding_box(rect)
            except:
                pass

            # Get element states
            is_enabled = True
            try:
                is_enabled = bool(element.GetCurrentPropertyValue(
                    self._uia_module.UIA_IsEnabledPropertyId
                ))
            except:
                pass

            is_visible = True
            try:
                is_visible = (bool(element.GetCurrentPropertyValue(
                    self._uia_module.UIA_IsOffscreenPropertyId
                )) == False)  # IsOffscreen inverted
            except:
                pass

            # Get value (for text fields, etc.)
            value = None
            try:
                value = element.GetCurrentPropertyValue(
                    self._uia_module.UIA_ValueValuePropertyId
                )
                if value is not None:
                    value = str(value)
            except:
                pass

            # Create UIElement
            ui_element = UIElement(
                element_id=str(automation_id) if automation_id else f"elem_{hash(str(name) + str(control_type))}",
                name=str(name),
                control_type=control_type,
                bounding_box=bounding_box,
                is_enabled=is_enabled,
                is_visible=is_visible,
                value=value,
                automation_id=str(automation_id) if automation_id else None,
                class_name=str(class_name) if class_name else None
            )

            # Optionally include children
            if include_children:
                try:
                    uia = self._uia_modules["uia"]
                    # Use FindAll instead of GetCachedChildren to avoid caching issues
                    children = element.FindAll(
                        self._uia_module.TreeScope_Children,
                        uia.CreateTrueCondition()
                    )
                    child_elements = []
                    if children:
                        for i in range(min(children.Length, 10)):  # Limit children to prevent explosion
                            try:
                                child_element = children.GetElement(i)
                                child_ui = self._element_to_ui_element(
                                    child_element, include_children=False
                                )
                                if child_ui:
                                    child_elements.append(child_ui)
                            except:
                                continue
                    ui_element.children = child_elements
                except:
                    pass  # Children are optional

            return ui_element

        except Exception as e:
            logger.debug(f"Failed to convert element to UIElement: {e}")
            return None

    @staticmethod
    def _rect_to_bounding_box(rect) -> Dict[str, int]:
        """Convert a UIA RECT-like object to a bounding box dict.

        Handles both tuple/list format (left, top, right, bottom) and
        attribute format (.left, .top, .right, .bottom).

        Args:
            rect: Tuple/list of 4 numbers, or object with left/top/right/bottom attrs

        Returns:
            Dict with x, y, width, height keys
        """
        # Tuple/list format: (left, top, right, bottom)
        if isinstance(rect, (tuple, list)) and len(rect) >= 4:
            left, top, right, bottom = rect[:4]
        else:
            # Attribute format
            left = getattr(rect, 'left', 0) or 0
            top = getattr(rect, 'top', 0) or 0
            right = getattr(rect, 'right', 0) or 0
            bottom = getattr(rect, 'bottom', 0) or 0

        left = int(left)
        top = int(top)
        right = int(right)
        bottom = int(bottom)
        return {"x": left, "y": top, "width": right - left, "height": bottom - top}

    def _control_type_id_to_string(self, control_type_id: int) -> str:
        """Convert UIA control type ID to human-readable string.

        Args:
            control_type_id: UIA control type ID

        Returns:
            Human-readable control type string
        """
        # Common UIA control type IDs
        control_type_map = {
            50000: "Button",
            50001: "Calendar",
            50002: "CheckBox",
            50003: "ComboBox",
            50004: "Edit",
            50005: "Hyperlink",
            50006: "Image",
            50007: "ListItem",
            50008: "List",
            50009: "Menu",
            50010: "MenuBar",
            50011: "MenuItem",
            50012: "ProgressBar",
            50013: "RadioButton",
            50014: "ScrollBar",
            50015: "Separator",
            50016: "Slider",
            50017: "Spinner",
            50018: "StatusBar",
            50019: "Tab",
            50020: "TabItem",
            50021: "Text",
            50022: "ToolBar",
            50023: "ToolTip",
            50024: "Tree",
            50025: "TreeItem",
            50026: "Custom",
            50027: "Group",
            50028: "Thumb",
            50029: "DataGrid",
            50030: "DataItem",
            50031: "Document",
            50032: "SplitButton",
            50033: "Header",
            50034: "HeaderItem",
            50035: "Table",
            50036: "TitleBar",
            50037: "Separator",
            50038: "SemanticZoom",
            50039: "Animation",
            50040: "Cell",
            50041: "DataGrid",
            50042: "Header",
            50043: "HeaderItem",
            50044: "Table",
            50045: "TitleBar",
        }

        return control_type_map.get(control_type_id, f"ControlType_{control_type_id}")

    def _get_process_name(self, process_id: int) -> str:
        """Get process name from process ID.

        Args:
            process_id: Windows process ID

        Returns:
            Process name (executable filename) or "unknown"
        """
        if process_id <= 0:
            return "unknown"

        try:
            import psutil
            process = psutil.Process(process_id)
            return process.name()
        except:
            # Fallback to tasklist if psutil not available
            try:
                import subprocess
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {process_id}", "/FO", "CSV"],
                    capture_output=True, text=True, timeout=2
                )
                if result.returncode == 0:
                    lines = result.stdout.strip().split('\n')
                    if len(lines) >= 2:  # Header + data
                        parts = lines[1].split(',')
                        if len(parts) >= 2:
                            # Remove quotes
                            return parts[0].strip('"')
            except:
                pass
            return "unknown"

    def _element_contains(self, child, parent) -> bool:
        """Check if one element contains another in the UIA tree.

        Args:
            child: Potential child element
            parent: Potential parent element

        Returns:
            True if child is contained in parent, False otherwise
        """
        try:
            # Walk up from child to see if we reach parent
            current = child
            while current:
                if current == parent:
                    return True
                try:
                    current = current.GetCurrentParent()
                except:
                    break
            return False
        except:
            return False

    def get_perception_sources(self) -> List[str]:
        """Get list of perception sources this adapter provides.

        Returns:
            List of source identifiers
        """
        if self.is_available():
            return ["windows_ui_automation"]
        return []


# Convenience function for easy usage
def create_uia_adapter() -> UIAutomationAdapter:
    """Create and return a UIAutomationAdapter instance.

    Returns:
        UIAutomationAdapter: Initialized adapter (may be unavailable)
    """
    return UIAutomationAdapter()