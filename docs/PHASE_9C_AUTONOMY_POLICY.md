# VERONICA Phase 9C — Risk-Based Autonomy Policy (STARK)

Status: IMPLEMENTED (controlled autonomy only — disabled by default; no unrestricted execution).

Boundary preserved: VISION SEES → BRAIN PROPOSES → AUTONOMY POLICY DECIDES → PERMISSIONGATE (if needed) → CONTROLLER ACTS → VERIFY. Vision never authorizes; PermissionGate remains sole authorization authority.

## A. ARCHITECTURE

New module `veronica/vision/autonomy_policy.py` introduces `AutonomyPolicy` independent of vision provider / LLM / model router / controller.

Flow (disabled by default; existing behavior preserved when disabled):
```
VERIFIED ACTION PROPOSAL (Phase 9B)
  -> AutonomyPolicy.evaluate(proposal, context)
     -> AUTO_APPROVED (verified low-risk routine) -> validation -> Controller -> OBSERVE -> VERIFY
     -> PERMISSION_REQUIRED (moderate/high / not verified / budget exceeded) -> existing PermissionGate -> explicit approval -> re-validate -> Controller
     -> BLOCKED (hard deny list / destructive / arbitrary shell / credential) -> STOP; no controller call
```

Components:
- `AutonomyDecision` (AUTO_APPROVED / PERMISSION_REQUIRED / BLOCKED)
- `AutonomyResult` (structured: decision, reason, risk, action_type, policy_rule, requires_confirmation, timestamp, policy_version, audit_metadata)
- `AutonomyPolicy` (enabled=False by default; configurable risk/max_action/max_seconds/max_failures/max_replans; blocked_actions / allowed_apps / blocked_apps)
- Hard BLOCKED categories (arbitrary shell, destructive system, credential access, security disable) always denied regardless of risk label

No second permission system. No replacement of `PermissionGate`. No direct vision-to-controller path.

## B. RISK CLASSIFICATION (SECTION 3 / 4 / 5)

LOW / ROUTINE — eligible for AUTO_APPROVED when verified and enabled:
- open/close ordinary app, navigate browser, scroll, search, click ordinary UI, type ordinary non-sensitive text, read screen, inspect, select ordinary, navigate folder, create non-sensitive draft, download ordinary (non-executable/non-destructive).

MODERATE — PERMISSION_REQUIRED by default:
- modify existing document, rename/move file, change non-critical setting, download potentially sensitive, meaningful external side-effect.
- Configurable; not silently allowed.

HIGH / DESTRUCTIVE — PERMISSION_REQUIRED; blocked categories always BLOCKED:
- delete file/folder, erase permanently, empty recycle, uninstall/install, arbitrary shell, modify system/security, change password, upload sensitive, send consequential message, financial, irreversible account, destructive DB.
- Download executable/installer/security-sensitive from blocked source -> BLOCKED / PERMISSION_REQUIRED per policy.

Explicit deny / block list (always): arbitrary shell/subprocess, credential extraction, security-control disable, destructive system, unrestricted filesystem deletion, destructive DB.

## C. AUTONOMOUS EXECUTION CONDITIONS (SECTION 6 / 8 / 9 / 10)

AUTO_APPROVED requires ALL of:
- `enabled=True` (default False)
- Proposal status APPROVED + VERIFIED grounding + valid coordinates (reused `CoordinateSafetyValidator`)
- Action risk LOW; action_type in LOW_RISK_TYPES
- Within budgets: `autonomous_actions_so_far < max_autonomous_actions`; elapsed < `max_autonomous_seconds`
- Not in BLOCKED category / block list / blocked_app
- No risk escalation: if a multi-step workflow reaches HIGH/BLOCKED, autonomy stops; permission requested (section 9 — never inherits authorization across boundary)

Never autonomous:
- Unverified / AMBIGUOUS / UNKNOWN / PARTIAL / INVALID target
- Expired proposal
- BLOCKED action type or block list match
- High / destructive / arbitrary shell / credential
- Budget exceeded
- Emergency stop / cancellation / denial / policy block / verification failure / target change / action change (section 12 / 7)

## D. PERMISSION INTEGRATION (SECTION 6)

For AUTO_APPROVED: validation -> Controller -> observation/verify.
For PERMISSION_REQUIRED: Policy -> existing `PermissionGate.check()` -> explicit approval -> re-validate (proposal-specific, not reused) -> Controller.
For BLOCKED: Policy -> BLOCK; no controller call.

Approval is proposal-specific (`proposal_id` bound); never reused for unrelated future actions. Target change or action change invalidates approval (re-propose / re-approve required).

## E. APPROVAL LIFECYCLE (SECTION 7 / 11)

- Approval tied to: proposal_id, action_type, target identity, parameters, policy_version, expiry.
- Before execution: RE-OBSERVE -> RE-CORRELATE -> RE-VERIFY -> RE-CHECK PERMISSION (TOCTOU defense; section 11).
- If target changed: STOP; new proposal required.
- Approval expires with proposal expiration (default 30s).

## F. BUDGETS / LIMITS (SECTION 10)

Configurable via `AutonomyPolicy` initialization (no duplication of closed-loop counters; consumes context if provided):
- `max_autonomous_actions` (default 20)
- `max_autonomous_seconds` (default 120)
- `max_consecutive_failures` (default 3)
- `max_replans` (default 5)
- `max_risk` (default "low")

When budget exceeded: STOP safely; do not continue silently (section 10 / 12).

## G. APP / DOMAIN BOUNDARIES (SECTION 11)

Configurable `allowed_apps` / `blocked_apps`; treated as policy inputs, not authorization by themselves. No security assumptions solely from names.
- Default: none allowed / none blocked (conservative); user configures.

## H. EMERGENCY STOP / CANCELLATION (SECTION 12)

Autonomous execution respects immediately:
- task cancellation
- emergency stop
- permission denial
- policy block
- target verification failure
- coordinate validation failure
- unexpected screen state (change detection triggers stop; re-observe required)

Never continues after stop.

## I. CONTINUOUS VERIFICATION (SECTION 13)

Every autonomous step: OBSERVE -> UNDERSTAND -> GROUND -> VERIFY -> POLICY -> ACT -> OBSERVE -> VERIFY -> CONTINUE / REPLAN.
Never blind 20-step execution.
Unknown never SUCCESS; ambiguous never SUCCESS; not_found never SUCCESS; failed never SUCCESS.

## J. VISION SAFETY / PROMPT-INJECTION DEFENSE (SECTION 14 / 15)

Maintained exactly:
- Perception (Moondream/OCR/UIA) = observation only; never authorization.
- Visual Grounding = target identification only.
- Screen text = untrusted data; forbidden-pattern filter preserved (`screen_content_defense`).
- No vision → controller direct path.
- Prompt-injection text never overrides policy / permission / system instructions.

## K. SENSITIVE DATA / AUDIT (SECTION 16)

- No raw passwords / API keys / tokens / payloads in logs / audit / events.
- `AutonomyResult.audit_metadata` non-sensitive only; proposal audit (from Phase 9B) preserves redaction.
- Autonomy must not weaken `screen_content_defense` or `VisionImagePrivacy`.

## L. CONFIGURATION (SECTION 17)

Default `enabled: false` (preserves existing behavior when absent/disabled).
Example config block (optional, not required for operation):
```yaml
autonomy:
  enabled: false
  max_risk: low
  max_actions: 20
  max_seconds: 120
  blocked_actions: ["arbitrary_shell", "credential_access"]
  allowed_apps: []
  blocked_apps: ["password_manager", "banking_app"]
```

Existing config unchanged when disabled.

## M. EVENTS / UI (SECTION 18)

Policy exposes structured event vocabulary (for Stitch / bridge consumption):
- AUTONOMOUS ACTION (AUTO_APPROVED executed)
- PERMISSION REQUIRED (REQUIRES_CONFIRMATION emitted)
- ACTION BLOCKED (BLOCKED)
- ACTION EXECUTED / VERIFIED
- AUTONOMY STOPPED (budget / emergency / change / verification failure)
- RISK ESCALATED (workflow crosses LOW -> MODERATE/HIGH boundary; autonomy stops; permission required)

No policy logic in GUI. Core owns decisions; GUI consumes events.

## N. TESTS (SECTION 19 / 21)

Tests (`tests/test_autonomy_policy.py` — 36 tests; targeted execution, no destruction):
1. low-risk verified -> AUTO_APPROVED
2. ordinary browser open -> AUTO_APPROVED
3. normal search -> AUTO_APPROVED
4. ordinary click -> AUTO_APPROVED
5. ordinary type -> AUTO_APPROVED
6. normal PDF download -> AUTO_APPROVED
7. delete file -> PERMISSION_REQUIRED
8. delete folder -> PERMISSION_REQUIRED
9. uninstall -> PERMISSION_REQUIRED
10. system setting -> PERMISSION_REQUIRED
11. executable download -> PERMISSION_REQUIRED / BLOCKED
12. shell command -> BLOCKED (hard)
13. credential access -> BLOCKED (hard)
14. permission denial -> no controller call
15. approval -> execution allowed
16. expired approval -> rejected
17. changed target after approval -> rejected
18. changed action after approval -> rejected
19. target verification failure -> rejected
20. coordinate validation failure -> rejected
21. UNKNOWN -> never SUCCESS
22. ambiguous grounding -> rejected
23. disabled -> preserve existing behavior
24. budget exceeded -> stop
25. time budget exceeded -> stop
26. emergency stop -> stop
27. cancellation -> stop
28. risk escalation -> permission boundary
29. prompt injection -> does not affect authorization
30. sensitive text unlogged
31. autonomous multi-step low-risk workflow works
32. high-risk inside low-risk workflow pauses correctly
33. approval proposal-specific
34. no vision-to-controller bypass
35. no duplicate PermissionGate
36. no security threshold weakening

Regression: Phase 9A, Phase 9B, vision, perception, computer-control, memory, streaming, context-manager tests — zero regressions targeted.

Real desktop validation (harmless only, after tests):
- Open harmless app; confirm no unnecessary permission request when low-risk.
- Type harmless text; confirm autonomous when verified/low-risk.
- Confirm permission UI/event for moderate/high boundary if tested (only with separate approval; never destructive).
- Confirm denial prevents execution.

No real deletion / installation / financial / destructive tests.

## O. LIMITATIONS / NEXT MILESTONE

- Phase 9C complete: autonomy policy implemented, disabled by default, bounded, no unrestricted execution.
- Phase 9D (if separately authorized): multi-step autonomous workflow with per-step policy evaluation, budget tracking, and recovery integration with existing `ClosedLoopController`.
- No "god mode"; no weakening of `PermissionGate`; no vision authorization.
- No autonomous destructive actions by default.
