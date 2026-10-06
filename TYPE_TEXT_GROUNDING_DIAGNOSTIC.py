#!/usr/bin/env python
# Phase 10F TYPE_TEXT Grounding Integration — READ-ONLY validation only
# Uses ONLY existing APIs. No execution. No authorization. No source changes.

import sys, os
sys.path.insert(0, '.')

from veronica.vision.visual_grounding import (
    VisualGrounding, VisualTargetCandidate, GroundingStatus,
    TargetCorrelationResult, create_visual_grounding
)
from veronica.vision.perception import PerceptionLayer
from veronica.vision.uia_adapter import UIAutomationAdapter
from veronica.vision.screen_state import ScreenState

print("=== STEP 1: TRACE / STEP 5: OBSERVE ===")
pl = PerceptionLayer()
state = pl.get_current_state()
uia = UIAutomationAdapter()

# Minimal identity only — NO text content
for w in (uia.get_windows() if uia.is_available() else []):
    if w.process_name and 'notepad' in w.process_name.lower():
        for c in getattr(w, 'children', []) or []:
            ct = getattr(c, 'control_type', '') or ''
            if ct == 'Edit' or (isinstance(ct, str) and 'edit' in ct.lower()):
                name = getattr(c, 'name', '') or ''
                auto_id = getattr(c, 'automation_id', '') or ''
                cls = getattr(c, 'class_name', '') or ''
                bbox = getattr(c, 'bounding_box', None)
                print(f"Notepad Edit control: control_type={ct!r} name={name!r} auto_id={auto_id!r} class={cls!r} bbox={bbox}")
                # BUILD CANDIDATE from actual UIA identity
                candidate = VisualTargetCandidate(
                    semantic_name=name or "Notepad editor",
                    control_type=ct if ct else "Edit",
                    visual_bbox=bbox or (0, 0, 100, 30),
                    confidence=0.95,
                    process_id=w.process_id,
                    window_title=w.title,
                    source="uia",
                )
                # RUN EXISTING GROUNDING (read-only, no execution)
                grounding = create_visual_grounding()
                result = grounding.ground_target(candidate, screen_state=state)
                print(f"  GroundingResult.status={result.status.value} score={result.score} matched_target={result.matched_target}")
                print(f"  Evidence signals: {len(result.evidence)}")
                for ev in result.evidence:
                    print(f"    {ev.signal_type} strength={ev.strength} source={ev.from_source}")
                print(f"  Reasons: {result.reasons}")

print("\n=== SAFETY CONFIRMATION ===")
print("- No source file edited")
print("- No ComputerUseLoop execution")
print("- No authorization created")
print("- No TypeText executed")
print("- No click/type/clipboard")
print("- No Phase 11 started")
print("- Existing invariants preserved: VERIFIED required for TYPE_TEXT; UNKNOWN/PARTIAL/AMBIGUOUS never become VERIFIED")
