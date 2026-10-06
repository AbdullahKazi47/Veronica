# Tests for multi-source fix
# A: Handle + correlation -> 2 sources available
# B: Handle success + correlation fail -> NOT_VERIFIED
# C: PID mismatch -> NOT_VERIFIED
# D: ControlType not Edit -> NOT_VERIFIED
# E: Win32 class mismatch -> NOT_VERIFIED
# F: Invalid bbox -> NOT_VERIFIED (geometry invalid -> INVALID from ground_target)
# G: ElementFromPoint fallback
# H: Single source never VERIFIED
