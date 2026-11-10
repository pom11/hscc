# hscc-bootstrap installer-test strategy: leave as-is (t_b9e517a9)

Status: decision only — **no test, fixture, or runner change** on branch
`oc/t_b9e517a9`. Card: *Decide hscc-bootstrap installer test strategy: share,
split, or leave.*

The orchestrator ruling on the card (comment 1793, 2026-11-09) already settles
that neither share-installed-home nor split-leg is to be implemented: sharing
trades away test isolation, and splitting moves wall time without reducing it
on the serial runner. This document re-measures the leg, checks the ruling's
numbers against the current tree, and records **leave as-is** as the decision.

## Measurement (this host, 2026-11-10)

Exact command, with the two knobs the card requires:

```
HERMES_DISABLE_LAZY_INSTALLS=1 HSCC_SKIP_COMPACT_PROBE=1 \
  ~/.hermes/hermes-agent/venv/bin/python -m pytest -q --durations=0 \
  hscc-bootstrap/tests -p no:cacheprovider
```

Host: 14 logical CPUs. Load average **2.13 / 2.11 / 2.11** before the run and
**2.53 / 2.43 / 2.26** after (run window 12:36:21–12:41:13 +0200).

Result: **1183 passed in 292.03 s**. The `--durations=0` table sums to
**289.95 s = 99.3 % of wall**, so the per-file numbers below account for the
leg rather than sampling it.

| file | duration | share of leg |
|---|---:|---:|
| `test_bootstrap_isolated.py` | 86.49 s | 29.6 % |
| `test_restart_daemon.py` | 67.67 s | 23.2 % |
| `test_bootstrap_dry_run.py` | 54.35 s | 18.6 % |
| `test_bootstrap_node_recovery_stage.py` | 36.70 s | 12.6 % |
| `test_video_gen_openai_compat.py` | 12.16 s | 4.2 % |
| `test_migrate_systemd_unit_name.py` | 6.90 s | 2.4 % |
| `test_install_gpu_clock_cap.py` | 6.65 s | 2.3 % |
| `test_install_notifier_mask.py` | 5.77 s | 2.0 % |
| all 29 remaining files | 13.26 s | 4.5 % |

The three installer-subprocess files the card names —
`test_bootstrap_isolated.py`, `test_bootstrap_dry_run.py`,
`test_bootstrap_node_recovery_stage.py` — total **177.54 s = 60.8 %** of the
leg. That reproduces the card's "~62 %" premise.

The cost is real installer execution, not a timeout:

| case | duration |
|---|---:|
| `test_bootstrap_isolated.py::test_rerun_is_idempotent` (two real installs) | 30.04 s |
| `test_bootstrap_isolated.py::test_status_lines_are_not_polluted_by_rich_panels` | 14.36 s |
| `test_bootstrap_isolated.py::test_installer_completes_and_is_self_contained` | 14.36 s |
| `test_bootstrap_isolated.py::test_fresh_home_says_not_wired_instead_of_claiming_success` | 13.87 s |
| `test_bootstrap_isolated.py::test_fresh_bootstrap_leaves_every_orchestrator_reviewable` | 13.86 s |
| `test_bootstrap_node_recovery_stage.py::test_dry_run_never_claims_a_node_was_armed` | 9.98 s |
| `test_bootstrap_dry_run.py::test_dry_run_respects_skip_flags_and_stays_idempotent` | 9.06 s |

Every duration in the trio is spread (4.61–30.04 s), not clustered on an exact
5.0 / 10.0 / 15.0 boundary. The 30 s case runs the installer twice; the 14 s
cases run it once. There is no hidden timeout to recover here.

The direct run is 55 s above the ruling's 236.90 s, and the delta sits in
`test_restart_daemon.py` — **67.67 s here vs 11.66 s** recorded in
`changelog.d/archive/2.15.6/t_ecb84764.md`. The canonical-runner gate then ran
the same leg at **240.36 s** (below), within 3.5 s of the ruling's number, so
the 67.67 s was run-to-run variance in that file rather than a persistent
regression. Its 37 cases are spread 0.01–4.47 s (each spawns bash plus a Python
stub, with `POLL_INTERVAL=0.05`), so it is not a quantised timeout either. It
does not change this card's decision: that file is not one of the three
installer-subprocess files.

## Full-suite gate (same host, same day)

`scripts/run_tests.sh` run once, foreground, after the decision commit:
**ALL GREEN, 12/12 legs**, 9398 passed / 7 skipped / 1 xfailed / 0 failed,
21 m 33 s wall (12:43:55–13:05:28 +0200, load 2.07 before / 1.90 after).

| leg | duration |
|---|---:|
| `scripts` | **582.53 s** |
| `hscc-api` | 245.62 s |
| `hscc-bootstrap` | **240.36 s** |
| `hscc_daemon` | 97.73 s |
| `hscc-cluster` | 60.90 s |
| `comfyui-sparkrun` | 35.05 s |
| `.github/scripts` | 14.13 s |
| `hscc-project` | 8.77 s |
| `hscc-roles` | 2.62 s |
| `hscc-commands` | 1.65 s |
| `memori_byodb` | 0.47 s |
| `sparkrun-hermes` | 0.01 s |

This is the ruling's trigger (a): `hscc-bootstrap` is no longer the long pole —
`scripts` is, at 582.53 s. Per the ruling, that makes the open question *leg
budgeting for `scripts`*, not a reason to share or split these 20 installer
cases. The decision on this card stays **leave as-is**; a separate leg-budget
card for `scripts` is the follow-up, not a change here.

## Options, measured against the ruling

### 1. Share one installed home — rejected

The isolated file's whole contract is "run the REAL installer into a throwaway
home and assert it stays there" (`hscc-bootstrap/tests/test_bootstrap_isolated.py:1`),
with `HERMES_HOME` / `HSCC_DIR` / `HERMES_SCRIPTS` pointed into `tmp_path`
(`test_bootstrap_isolated.py:58-69,72-84`). The dry-run file's contract is
"assert ZERO filesystem writes outside the (empty) expected set"
(`test_bootstrap_dry_run.py:1-8`). Sharing installed state across cases would
make each case's result depend on what ran before it — the most expensive class
of test bug, because it fails differently per order and gets blamed on the
host.

The existing hermeticity guard does not make this safe: it redirects hooks and
`serving.json` into a process-wide sandbox before bootstrap writers import
(`hscc-bootstrap/tests/conftest.py:37-48,50-61`), which is a different
property from "this case's installed tree was created by this case". A
copy-on-write shared home would need every path consumer to stay inside the
copy; one careless `Path` object reopens the leak. Per the ruling, this option
is permanently off the table if an installer case ever fails in an
order-correlated way.

### 2. Split the leg — rejected

`scripts/run_tests.sh` is serial (`for d in "${DIRS[@]}"`), so moving the
177.54 s of installer cases into another row leaves the full-suite wall time
unchanged and adds another pytest startup. The only thing split buys is per-leg
visibility, and `scripts/leg_heartbeat.sh` already provides elapsed wall + CPU
per leg, which is the slow-vs-stopped signal the split was meant to give.
Runner complexity with no wall-time win is not justified.

### 3. Leave as-is — chosen

177.54 s is real installer work that the tests exist to perform. Buying back at
most that much on one leg would cost either test isolation (option 1) or runner
complexity for zero net wall time (option 2). The leg's accounting is complete
(99.3 %), no case sits on a hidden timeout boundary, and the observability gap
that motivated a split was already closed by the heartbeat. Leave the lifetime,
fixtures, and leg structure untouched.

## Decision and revisit triggers

**Decision: leave as-is.** No implementation on this card; the PR is this
document plus its index entry.

Revisit only on the ruling's triggers:

1. `hscc-bootstrap` stops being the long pole and a different leg needs the
   budget — then the question is leg budgeting, not these 20 cases; or
2. an installer case fails in a way that correlates with what ran before it —
   then strengthen isolation (option 1 becomes permanently off the table).

Otherwise re-measure after the ruling's `SCHEDULED_UNTIL=2026-12-07`.

## Contract checks

- Baseline leg total and host load: above (292.03 s, 14 CPUs, load 2.13/2.53).
- Chosen option: **leave as-is**.
- No test/fixture/runner change, so `pytest -q -rs hscc-bootstrap/tests` and the
  `HERMES_DISABLE_LAZY_INSTALLS=1` / `HSCC_SKIP_COMPACT_PROBE=1` exports in
  `scripts/run_tests.sh` are untouched.
- No process was stopped; no `pkill` was used.
- Full-suite gate: `scripts/run_tests.sh` run once, foreground — ALL GREEN,
  12/12 legs (above).
