# OPEN_APPLICATION Capability — Phase 10F Extension

Why: Real-desktop validation needed a safe application-launch action.

ActionType added: OPEN_APPLICATION (existing enum extended; CLICK/TYPE_TEXT preserved).

Proposal changes:
- ActionProposal gains `application_name` / `application_id`.
- `create_action_proposal` accepts `application_name`.
- Arbitrary command strings blocked (`cmd.exe`, `powershell`, `/c `, `&&`, etc. embedded → BLOCKED).
- No `subprocess` / `os.system` / `shell=True` / arbitrary executable execution.

Target validation: application must resolve to known safe identity (Notepad for validation). Unknown/blocked if not resolvable.

ComputerUseLoop: integrates same loop; receives proposal → validates target → uses existing safe launch mechanism (not arbitrary shell).

Authorization: same path — proposal-bound fresh ExecutionAuthorization via AutonomyPolicy + PermissionGate.

Budget: same AutonomousExecutionBudget, not reset.

Verification: independent UIA/PerceptionLayer verification required after launch; execution result alone is not sufficient.

Security: no vision authorization, no blanket authorization, no new gate, blocked actions preserved.

Autonomy: `enabled=False` preserved; temporary `enabled=True` only for validation; restored after.

Scope: ONLY OPEN_APPLICATION. No Phase 11, no arbitrary execution, no 24/7 autonomy.
