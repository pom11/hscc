# t_5b362400 — content-level loss list (WIP)

Task restated: extend `hscc-roles/role_soul_check.py` so that, per offending
profile, it reports the CONTENT-level loss list — the live SOUL lines that
`compose_soul()` cannot reproduce — not only the non-emittable heading names.
Read-only, non-fatal WARNING, reusing the existing `_role_soul_scan` seam.

## Findings so far

- `role_soul_check.py` exists only on branch `wt/t_c823ebc9` (PR #115, still
  OPEN), so this branch is based on that head: `wt/t_5b362400` off
  `origin/wt/t_c823ebc9` @ 52734548.
- Live fleet measured read-only (25 worker-signature profiles judged, 23
  excluded): exactly 1 profile has non-reproducible lines — the same one the
  heading sweep already flags (7 lines). Confirms the parent card's numbers.
- (pending) classifier design + tests.
