# Bootstrap audit 1/3 — install_payload / install_scripts / install_soul / apply_patches (correctness + silent-failure)

Task: t_24b4a9ba · branch wt/t_24b4a9ba · scope: audit ONLY these 4 files.

## GOAL (from card)
Correctness + silent-failure, specifically:
- swallows an exception and reports success (anti-pattern `except Exception: return None`)
- writes config without saying so
- reverts an operator's choice
- an ENVIRONMENT fault rendered as a fact about DATA

## Findings

### F1 — apply_patches: missing patch dir = `ok:true` quiet success (ENVIRONMENT fault → data fact)
Evidence:
- `hscc-bootstrap/apply_patches.py:29-30` `list_patches` returns `[]` both when the set's patch dir is genuinely empty AND when it does not exist.
- `hscc-bootstrap/apply_patches.py:45-48` `apply_set` treats any empty list as "Intentionally empty patch set (patches landed upstream) — quiet success" → `{"ok": True, ...}`.
- Impact: `hscc-bootstrap/bootstrap.sh:236-241` runs `apply_patches.py --check`, greps for `"ok": true`, and on match applies + prints `ok "hermes patches applied"`. If `patches/hermes/` were deleted/mangled, `--check` would emit `ok:true` and bootstrap would apply nothing yet claim success. A fresh checkout silently never gets the kanban review/resume patches.
- Classification: the empty-set guard was added deliberately for the legitimately-empty `sparkrun` set (commit 3f4c388), but it conflates "empty because patches landed upstream" with "patch dir missing" — an environment fault rendered as a data fact ("zero patches needed").

### F2 — install_soul.install_personality: malformed YAML crashes instead of documented no-op
Evidence:
- `hscc-bootstrap/install_soul.py:208` docstring: "Returns an action string. No-op on missing/bad config."
- `hscc-bootstrap/install_soul.py:214-215` `yaml.safe_load(fh)` raises `yaml.YAMLError` on malformed config — it does NOT return `"bad-config"` (which is only reached for a non-dict top-level / non-dict personalities). Behavior violates the documented contract.
- Impact: `install_soul.py` __main__ writes SOUL.md (lines 246-247) THEN calls `install_personality` (line 248); a malformed config crashes the process non-zero mid-step (bootstrap.sh:253 warns), leaving SOUL updated but personality not — a partial write surfaced only as a generic warn. Per its own contract this should be a clean no-op.

## Reviewed and accepted (no change)
- `install_payload._sweep_old_backups` (`install_payload.py:33-52`) silently swallows OSErrors — documented as best-effort hygiene; a sweep failure cannot shadow a fresh install (the main loop backs up `<name>` from the sibling dir, not `.bak-*`). Not a data-fact claim; leaving as-is.
- `install_payload` copy loop (84-105) and `install_scripts` copy loop (51-60) have NO try/except — a failed copy raises (loud), which is the correct behaviour per the audit's goal. No change.
- `install_scripts` distinguishes missing `scripts/` dir → `skipped:true` + reason (line 32-37); contrast with F1 in apply_patches. Correct.

## Fixes
- [x] F1 fix: `apply_patches.apply_set` now checks the patch dir exists before the empty-set quiet-success; a missing dir → `{"ok": False, "error": "patch directory not found: ..."}`. Regression test `test_missing_patch_dir_is_fault_not_quiet_success` in `tests/test_apply_patches.py`.
- [x] F2 fix: `install_soul.install_personality` catches `yaml.YAMLError` and returns `"bad-config"` (no write, no crash) — matches the "No-op on ... bad config" docstring. Regression test `test_personality_malformed_yaml_noops_not_crashes` in `tests/test_install_soul.py`.

## Verification
- [x] hscc-bootstrap suite green (host interpreter): 258 passed in 395.65s
- [x] hscc-bootstrap suite green (p313 interpreter): 258 passed in 395.60s
- [x] full `scripts/run_tests.sh` suite (host interpreter default): ALL GREEN — 8 packages, exit 0
- [ ] full `scripts/run_tests.sh` suite (p313 interpreter via HSCC_TEST_PY)
- [ ] merge to main + push
- [ ] deploy (install_payload.py)

