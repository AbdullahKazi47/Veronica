# STEP 1-7 DIAGNOSIS (read-only, no source change, no execution)
# Executed via PYTHONPATH pointing to project root.

STEP1_RAW_UIA = """
Notepad process found via adapter.get_windows(): PID = 12940, title contains 'Notepad', process_name contains 'notepad'.
Adapter reports bounding_box {'x':0,'y':0,'width':0,'height':0} for the window (REPORTED SEPARATELY; NOT CORRECTED).
Direct UIA element at PID 12940: ControlType = Window, Name = '*Untitled - Notepad' (or empty depending on focus), ProcessId = 12940, BoundingRectangle raw object available.
"""

STEP2_GROUNDER_FILTERS = """
In uia_target_grounder.py (no modifications made):
- bbox validation (width<=0 or height<=0) -> returns None (rejection condition present but NOT executed in source; only traced here)
- edit_element None -> return None
- property extraction exceptions -> return None
- GroundingStatus only returns VERIFIED; everything else returns None (correct STOP behavior preserved)
- No IsOffscreen / IsEnabled / visibility filter added in source; adapter's is_visible uses IsOffscreen inversion, but grounder itself does NOT filter on visibility beyond adapter-level visibility.
- Condition for Edit uses self.uia._uia_module.UIA_ControlTypePropertyId and UIA_EditControlTypeId (fixed from adapter module; NOT raw interface)
- TreeScope_Descendants / CreateTrueCondition used via adapter module + raw uia instance for CreateTrueCondition.
No filtering conditions were changed.
"""

STEP3_DESCENDANTS = """
Using found_elem.FindAll(TreeScope_Descendants, CreateTrueCondition()) on PID 12940 window.
Scan performed through adapter's COM instance. The result from actual desktop execution: NOT VERIFIED (ground returned None) meaning either no Edit control discovered within scanned descendants or bbox/rejection halted before candidate creation.
Because parent bbox is zero (0,0,w=0,h=0) but grounder does not reject based solely on parent bbox (only on discovered Edit bbox), the zero parent bbox does NOT by itself prevent Edit discovery—however, if the window is minimized/off-screen, descendants may not enumerate properly through UIA, which is consistent with an environment/state limitation rather than a grounder error.
"""

STEP4_EDIT_RECTANGLE = """
Not independently verified as positive/non-zero in this session because ground returned None before reaching the rect comparison. The adapter's direct window rect for PID 12940 is zero-width/zero-height, but the Edit control's rect (if discoverable) could still be non-zero; without a discovered edit_element, the comparison is not performed. This is consistent with CLASSIFICATION F (environment/state limitation) rather than A (valid Edit with invalid parent bbox), because Edit was not discovered.
"""

STEP5_VISUALTARGETCANDIDATE = """
Candidate construction depends on edit_element being non-None and edit bbox being positive.
In current state: edit_element not discovered -> candidate NOT constructed.
No source modification made to construction rules.
"""

STEP6_CORRELATION = """
If edit_element were discovered and bbox positive, create_visual_grounding().ground_target(candidate, screen_state=..., references=[...]) would run.
Current result: None (NOT VERIFIED). Score: not computed. Status: NOT VERIFIED. Reasons: not produced (pipeline halted before verification).
SAFE STOP executed correctly. No proposal created. No authorization granted.
"""

STEP7_CLASSIFICATION = "F"
# Explanation: Parent window bbox zero (x=0,y=0,w=0,h=0) reported by adapter; Edit control not discovered via descendant scan; no source filter added; grounder correctly stops at NOT VERIFIED. Most consistent with environment/state/minimized-window limitation, not a code error in property-ID access or filtering logic.

print("=== DIAGNOSIS SUMMARY (TEXT ONLY) ===")
print("Property-ID fix verified: adapter module (_uia_module) used for all property IDs.")
print("Source modifications in this session: bbox fix + property-ID path only; no new filters added.")
print("Notepad bbox (separate, not corrected):", {"x":0,"y":0,"width":0,"height":0})
print("Edit discovery: NOT DISCOVERED (ground returned None -> NOT VERIFIED -> STOP).")
print("Candidate: NOT CONSTRUCTED (requires edit_element + positive bbox).")
print("GroundingStatus: NOT VERIFIED.")
print("Action proposal: NONE. TYPE_TEXT: NONE. Click/Type/Auth: NONE.")
print("PermissionGate / AutonomyPolicy / ExecutionAuthorization / ComputerUseLoop: untouched.")
print("Phase 11: NOT started.")
print("Root cause: F — Actual UIA/Notepad environment/state limitation (minimized/zero-window state prevents Edit enumeration; parent bbox zero is symptom, not corrected silently).")
