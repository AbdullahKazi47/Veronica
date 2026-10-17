# STEP 4 — Tests (to be added to existing test file)
# Must cover:
# A. Realistic UIA identity fields -> identity_evidence created (signal_type='automation_id')
# B. Empty Name + empty AutomationId + present ctl/class/pid -> identity recognized (not FALSE)
# C. Missing PID + missing ctl/class -> identity_evidence None -> NOT_VERIFIED
# D. Wrong PID vs reference -> identity not matching (no evidence insertion mismatch handled at score; no automatic contradiction needed)
# E. Wrong control_type -> control contradiction lowers score (line356) -> NOT_VERIFIED
# F. Wrong class -> identity_evidence uses real class; no direct contradiction mechanism besides structural identity check — but identity strength high means it supports verification; mismatch only at correlation level
# G. UIA-only identity -> source_count = 1 (uia only, no vision geometry/semantic) -> cap line414-417 activates: score capped to 0.60, best_score < 0.85 -> NOT_VERIFIED
# H. Threshold 0.85 unchanged (line257)
# I. Coordinate validation unchanged (line180-208, validate_rectangle/validate_point)
