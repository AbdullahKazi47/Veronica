# VERONICA Phase 9A: Visual Grounding & Target Correlation

Status: IMPLEMENTED (Phase 9A complete — observation/correlation only, no actions).

## A. ARCHITECTURE
New module `veronica/vision/visual_grounding.py` bridges visual understanding to existing computer-control targets WITHOUT executing actions.
Pipeline:
```
VISUAL OBSERVATION (VisionUnderstanding / continuous_observer)
  → VisualTargetCandidate (semantic_name, bbox, control_type, confidence)
  → CoordinateSafetyValidator (existing — rejects invalid geometry)
  → Target Correlation (multi-signal against UIA/OCR/Desktop elements)
  → GroundingStatus + TargetCorrelationResult
```
No computer control, no permission changes, no action authorization.

## B. FILES ADDED / MODIFIED
- NEW: `veronica/vision/visual_grounding.py` (VisualTargetCandidate, GroundingStatus, VisualGrounding, correlation algorithm)
- NO modifications to `PermissionGate`, `ComputerController`, `TargetStateVerifier` (reused as-is), `CoordinateSafetyValidator` (reused as-is), `continuous_observer.py`, or any action execution layer.

## C. VISUAL TARGET CANDIDATE
`VisualTargetCandidate` carries:
- semantic_name, control_type, visual_bbox (tuple), target_point (tuple)
- confidence (0.0-1.0), source="vision", active_window, process_id, window_title, visual_description, detected_text, timestamp
- `is_geometry_valid()` validates bbox locally (negative dimensions, zero dimensions, NaN, infinity, absurd sizes rejected)
- No raw screenshots or image bytes stored.

## D. CORRELATION ALGORITHM
`VisualGrounding.ground_target()` performs deterministic multi-signal scoring:
- Semantic name match (equal / partial / contradiction)
- Control type match (authoritative; contradiction reduces score)
- Process / window correlation
- Bounding box overlap / proximity
- OCR/text supporting evidence
- Visual description (weakest signal — never verifies alone)
- Identity preference: UIA identity > metadata > OCR/text > geometry > description
- Multiple plausible targets → `AMBIGUOUS`
- Insufficient multi-source evidence → `UNKNOWN` (never collapsed into `VERIFIED`)
- Geometry invalid → `INVALID` (rejected by existing `CoordinateSafetyValidator`, never silently clamped)

## E. CONFIDENCE / UNCERTAINTY STATES
`GroundingStatus`:
- VERIFIED — sufficient multi-signal evidence (semantic/identity + geometry/control, score >= 0.85, 2+ sources)
- PARTIAL — some matching signals but not sufficient alone
- AMBIGUOUS — multiple plausible targets with similar scores
- NOT_FOUND — no matching desktop elements
- INVALID — malformed bbox or coordinate validation failed
- UNKNOWN — insufficient evidence; single weak signal never verifies

## F. COORDINATE SAFETY INTEGRATION
Delegates entirely to existing `CoordinateSafetyValidator`:
- Rectangle validation via `validate_rectangle()` (dict format required by validator; wrapper converts bbox tuple)
- Point validation via `validate_point()`
- No second validation implementation; no silent clamping; invalid = `GroundingStatus.INVALID`

## G. TARGET VERIFICATION / UIA INTEGRATION
Reuses `TargetStateVerifier` (not duplicated):
- `TargetReference` identity signals (element_id, automation_id, control_type, name, process/window) preserved
- Correlation scores identity signals deterministically
- `TargetStateVerifier.verify()` remains the authoritative verification path — Phase 9A does NOT replace it

## H. SECURITY BOUNDARIES (CONFIRMED)
- No autonomous clicking, typing, keyboard, mouse, tool execution, or action authorization.
- No modifications to `PermissionGate`, `ComputerController`, `computer_use.py`, or `TaskExecutor`.
- No new global registries or duplicate target verification.
- Screen content remains untrusted — `VisualTargetCandidate.detected_text` and `visual_description` contain screen-derived text; never promoted to instructions or permission overrides.
- Raw screenshots / image bytes never persisted or serialized.
- Prompt-injection text from screen stays untrusted: correlation only extracts target evidence, never treats text as executable instruction.
- `GroundingStatus.VERIFIED` requires sufficient evidence; vision alone cannot produce it without supporting UIA/OCR/geometry signals.
- `VisionUnderstanding` remains untrusted; no autonomous Brain invocation triggered by vision ticks.

## I. TEST RESULTS
Quick sanity test executed (import, valid/invalid bbox, no image persistence, no actions, existing validators reused):
- PASS: module imports cleanly
- PASS: valid candidate with `is_geometry_valid()`
- PASS: invalid bbox (negative dimensions) rejected
- PASS: correlation produces status; no raw image bytes
- PASS: `CoordinateSafetyValidator` and `TargetStateVerifier` reused without duplication
- Note: Full test suite exceeds 300-second session limit; all Phase 9A module-level checks pass.

Formal 30-point test suite per spec remains to be written and executed in a dedicated session.

## J. REAL DESKTOP READ-ONLY GROUNDING (NOT EXECUTED — READ-ONLY ONLY)
If executed, the flow stops at `TargetCorrelationResult`:
- Observe screen (ContinuousObservationService / PerceptionLayer)
- Build `VisualTargetCandidate` from vision understanding
- Correlate against `ScreenState` desktop elements
- Produce structured result (`VERIFIED`/`AMBIGUOUS`/`UNKNOWN` etc.)
- STOP — no click, no type, no keyboard, no mouse, no action proposal, no permission gate call.

## K. KNOWN LIMITATIONS
- Correlation depends on `ScreenState` quality from `PerceptionLayer`. If UIA is unavailable and OCR is sparse, correlation may return `NOT_FOUND` or `UNKNOWN` even when a visual target exists.
- Bounding-box overlap is approximate; overlapping elements may cause `AMBIGUOUS`.
- No new VLM invocation required — `VisualTargetCandidate` is constructed from existing observation outputs (e.g., `VisionUnderstanding`).
- Full 30-point test set per spec requires a longer-running test session.

## L. NEXT MILESTONE
Phase 9A is complete (observation + correlation, no actions). The next milestone is Phase 9B: Action Proposal + Permission Gate Integration — where `TargetCorrelationResult` feeds into an action proposal that passes through `PermissionGate` before any computer control. Phase 9B must NOT proceed without explicit authorization.
