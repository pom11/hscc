kind: Changed
order: 3
task: t_2b2a6c3b

- **`scripts/run_tests.sh` now seals `HOME` for every leg** (roster-wide, placed after
  the two lines that derive a value from `HOME` and before the leg loop):
  `HOME`+`USERPROFILE` = a fresh empty `mktemp -d` dir under a **literal** root — `/tmp`,
  or `/var/tmp` when `/tmp` IS the login home — never `$TMPDIR`, and never inside the
  login home (still refused with rc 3, because hermes resolves a home under `~/.hermes`
  back to the live root, t_d39c720e). The runner **prints the root and the path it
  sealed**, so a `11614 passed` line in a redirected roster log is attributable to the
  seal rather than to whatever else the machine looked like, and `HSCC_TEST_NO_SEAL=1`
  restores the operator's HOME in one env var. `SPARKRUN_SRC`, `HERMES_AGENT_PATH` and
  `HSCC_TAP_DIR` are **declared** from this machine's own checkouts when the operator did
  not set one — the three paths hscc-cluster's source cross-checks, the live-hermes cases
  and the formula-drift guard resolve through `~` by default, exported as paths and never
  as the home, so the seal does not quietly retire them as skips (measured at `96a9f38a`
  without the third declaration: `scripts` went `601 passed / 5 skipped` → `598 / 8`).
- **The root is decided before `mktemp`, from the login home alone** (t_9c1ff8e1, which
  this card's own CI caught: `leg scripts` red at `58653473`/`962ea063` while main was
  green). On the Linux/CI default host `HOME` **is** `/tmp`, so "create it under /tmp"
  and "never inside the login home" have no answer — the runner exited 3 before leg
  selection, no leg could run at all, and a typo'd leg name lost its contracted 64. The
  fallback picks the other literal root up front; the refusal still applies to whatever
  `mktemp` returns, so a host that contains both roots still fails closed rc 3. The
  fallback is deliberately NOT triggered by "mktemp answered inside the login home" —
  that swallows the refusal and would let the containment guard survive its own mutation
  probe. Pinned from both sides: a `HOME=/tmp` case that seals under `/var/tmp` and names
  the root in the banner, and a `mktemp`-shim case at `HOME=/tmp` that still refuses rc 3.
- **Measured two-arm at `0cee5d49`** (sealed vs `HSCC_TEST_NO_SEAL=1`, same sha,
  back-to-back, canonical runner, one pytest process per leg dir): **`11614 passed,
  10 skipped, 0 failed` in BOTH arms**, 12/12 legs rc 0, item counts identical, zero
  passed→skipped moves, every leg's wall ratio ≈1.0 (worst deviations `bootstrap` 0.954
  and `scripts` 1.067, in opposite directions = noise) — the seal costs no coverage and
  buys no speed. CI's 12-leg run IS a sealed arm: nothing in the repo sets
  `HSCC_TEST_NO_SEAL`. Pinned by `scripts/tests/test_run_tests_home_seal.py` (15 executed
  cases: the block's text and its position in the file, the executed seal, the escape
  hatch, both fail-closed refusals via a `mktemp` shim, `TMPDIR` cannot move it, the three
  declarations' win/lose arms, and the `HOME=/tmp` posture on both sides of the fallback).
