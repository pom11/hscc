# Error-copy audit — every message says what happened and what to do

Task t_b89d0b9a. Audit of every user-facing error and empty-state string in the
HSCC iOS app. For each: does it name what failed (Q4) and what the operator
should do next (Q5)? Flag vague ("Something went wrong"), leaky (raw
NSError/internal symbol), or user-blaming. Rewrite the weak ones.

Status: IN PROGRESS (inventory complete; rewrites + build verification pending).

## Diagnosis (the single root weakness)

The whole app funnels errors through `HSCCError.localizedDescription`, which is
already specific and actionable. The weakness is the FALLBACK used when the
thrown error is NOT an `HSCCError` (an unexpected internal error that escaped
the typed client path): that fallback is a vague dead-end.

- 14 occurrences of the literal `"Something went wrong."` spread across 12 files.
- 1 occurrence of `"Connection failed."` (ContentView ping banner).

These violate the app's rule "a failure is never presented as absence": they
name neither what failed nor what to do.

## The fix

Add one shared helper and collapse all 15 sites onto it, so there is a single
source of truth for the non-HSCCError fallback instead of 15 hand-copied
dead-ends.

See `APIError.swift` (operatorErrorMessage) + the per-file call-site edits.

Full before/after table and per-file inventory below.

---
(inventory + before/after to follow in this file)
