#!/usr/bin/env python
# Test OPEN_APPLICATION execution handler
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '.'))

from veronica.vision.computer_use import ComputerUseLoop, ComputerAction
from veronica.tools.base import ToolResult

def test_open_application_handler():
    """Test that OPEN_APPLICATION action is handled correctly"""

    # Create a mock computer use loop (we don't need full initialization for this test)
    loop = ComputerUseLoop()

    # Test 1: Valid notepad action
    action = ComputerAction(
        action_type="OPEN_APPLICATION",
        target_name="notepad",
        description="Open Notepad for testing"
    )

    result = loop._execute(action)
    print(f"Valid notepad action result: success={result.success}, message='{result.message}'")

    # Test 2: Invalid application name
    action_invalid = ComputerAction(
        action_type="OPEN_APPLICATION",
        target_name="invalidapp",
        description="Try to open invalid application"
    )

    result_invalid = loop._execute(action_invalid)
    print(f"Invalid application result: success={result_invalid.success}, message='{result_invalid.message}'")

    # Test 3: Missing application name
    action_missing = ComputerAction(
        action_type="OPEN_APPLICATION",
        target_name="",
        description="Open application with empty name"
    )

    result_missing = loop._execute(action_missing)
    print(f"Missing application result: success={result_missing.success}, message='{result_missing.message}'")

    # Test 4: Unknown action type (should still work as before)
    action_unknown = ComputerAction(
        action_type="UNKNOWN_ACTION",
        target_name="test",
        description="Unknown action"
    )

    result_unknown = loop._execute(action_unknown)
    print(f"Unknown action result: success={result_unknown.success}, message='{result_unknown.message}'")

    # Verify results
    assert result.success == True, f"Valid notepad action should succeed, got: {result}"
    assert result_invalid.success == False, f"Invalid application should fail, got: {result_invalid}"
    assert result_missing.success == False, f"Missing application should fail, got: {result_missing}"
    assert result_unknown.success == False, f"Unknown action should fail, got: {result_unknown}"

    print("All tests passed!")
    return True

if __name__ == "__main__":
    test_open_application_handler()