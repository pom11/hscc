kind: Added
task: t_b4ac0a6f

- **`hscc verify` now reports deployed-vs-RELEASED drift (GAP-E-2).**
  `check_plugin_payload` diffs the deployed tree against two levels up from
  `verify.py` itself — from the deployed/keg tree that is a self-diff, so
  288 lines of live deployed-vs-released drift coexisted with green gates.
  The new `deployed_vs_released` check compares each deployed payload
  file's sha256 against the installed release's payload
  (`<Cellar>/hscc/<ver>/libexec/hscc`, overridable via
  `$HSCC_RELEASED_PAYLOAD`) and names every drifted, missing, or extra
  file. It deliberately does NOT diff against `origin/main` — that would
  false-alarm on every pre-release deploy. Read-only. Files the GAP-E-1
  guard already preserved (`plugins-backups/<plugin>.drifted.bak-<stamp>/`)
  or the operator pinned report as **known-preserved**, not as failures.
  No keg installed (dev checkout) ⇒ `unverified`, never a false failure.
  Pairs with the release card's operator-run per-file cmp step (GAP-A-6):
  same sha256 idea — one in-repo gate, one post-install proof.
---
kind: Verified
task: t_b4ac0a6f

- `hscc_daemon/tests/test_verify.py`: +14 cases (drift named / clean tree /
  missing / extra / known-preserved via backup + manifest / pin annotation /
  no-keg ⇒ `ok=None` / redirected-HOME acceptance / read-only proof / the
  self-approval regression that documents the old check green-lighting real
  drift). `run_all` row-coverage assertions are now roster-based
  (`EXPECTED_RUN_ALL_ROWS` + `assert_run_all_rows` in test_verify.py, consumed
  by test_verify_state_schema.py too) instead of four bare count pins across
  two files — the count pins broke on every added check (the combined stamp
  caught the #147 13-pin vs GAP-E-2 row-14 collision, and #161 then bumped
  the same pins 13→15 for autodown_interlock + dispatcher_self_heal; this
  branch's rebase re-solved them to the 16-row union once, then deleted the
  pin class entirely); the roster asserts the
  expected row NAMES are present exactly once with the row contract intact,
  so the next added check no longer forces a cross-file pin hunt.
