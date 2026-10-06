TASK COMPLETED: Implement missing OPEN_APPLICATION execution handler in ComputerUseLoop._execute

Summary:
- Added safe OPEN_APPLICATION handler to veronica/vision/computer_use.py
- Handler only activates existing visible Notepad window (no arbitrary launch)
- Security constraints preserved: no subprocess/shell, no arbitrary executables
- Authorization, budget, permission gate, autonomy policy unchanged
- Added comprehensive tests for OPEN_APPLICATION proposal and execution
- All existing tests continue to pass
- Real desktop validation confirms Notepad visible and activatable
- No Phase 11 work started
- No redesign of 10F/ActionProposal/AutonomyPolicy/PermissionGate

Files changed:
1. veronica/vision/computer_use.py - Added OPEN_APPLICATION execution branch
2. tests/test_action_proposal.py - Added 5 OPEN_APPLICATION test cases
3. veronica/vision/action_proposal.py - Removed OPEN_APPLICATION from VISION_ACTION_TYPES (so it's treated as non-vision action)

All verification steps passed.