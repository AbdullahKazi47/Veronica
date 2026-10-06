#!/usr/bin/env python
# Integration test for OPEN_APPLICATION in ComputerUseLoop
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '.'))

from veronica.vision.computer_use import ComputerUseLoop, ComputerAction
from veronica.tools.base import ToolResult

def test_open_application_integration():
    """Test OPEN_APPLICATION through ComputerUseLoop"""

    # Create computer use loop
    loop = ComputerUseLoop()

    # Test valid notepad action
    action = ComputerAction(
        action_type="OPEN_APPLICATION",
        target_name="notepad",
        description="Open Notepad for integration test"
    )

    result = loop._execute(action)
    print(f"Integration test result: success={result.success}, message='{result.message}'")

    # Verify it worked
    assert result.success == True, f"OPEN_APPLICATION should succeed, got: {result}"
    assert "Activated Notepad window" in result.message
    assert loop._last_notepad_hwnd is not None

    print("Integration test passed!")
    return True

if __name__ == "__main__":
    test_open_application_integration()