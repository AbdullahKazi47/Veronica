OPEN_APPLICATION Implementation Complete

## What was implemented
- Added OPEN_APPLICATION execution handler to ComputerUseLoop._execute() in veronica/vision/computer_use.py
- Handler safely activates existing visible Notepad window using win32gui.SetForegroundWindow
- No arbitrary executable paths, no shell/subprocess execution
- Only allows "notepad" application (case-insensitive)
- Preserves all existing security constraints and authorization flows

## Changes made
1. veronica/vision/computer_use.py: Added OPEN_APPLICATION branch in _execute method
2. veronica/vision/action_proposal.py: Removed OPEN_APPLICATION from VISION_ACTION_TYPES (treats as non-vision action)
3. tests/test_action_proposal.py: Added 5 test cases for OPEN_APPLICATION proposal handling

## Verification
- All existing tests pass (action_proposal, target_verification, permissions, closed_loop)
- New OPEN_APPLICATION tests pass
- Custom integration test confirms handler works with real Notepad (PID 25820)
- Forbidden patterns in application names are blocked at proposal creation
- Authorization remains proposal-bound via ExecutionAuthorization
- Budget persistence maintained via AutonomousExecutionBudget
- AutonomyPolicy disabled by default preserved
- No Phase 11 components introduced

## Security compliance
✅ No arbitrary executable paths
✅ No subprocess/os.system/shell=True/powershell/cmd
✅ No redesign of 10F/ActionProposal/AutonomyPolicy/PermissionGate
✅ Authorization proposal-bound preserved
✅ Budget not reset
✅ Real desktop validation completed (Notepad visible and activatable)

The implementation is complete and ready for use.