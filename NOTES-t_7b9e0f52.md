# t_7b9e0f52 — finish PR #575: combined mixed run green + strike-slate pin restored

Successor to t_1f8e89c9 (PR #575). @hscc-orch rejected the first pass with two
defects; this file records how each was closed. nolive-rule honoured: no fleet
surface run, tests use 192.0.2.x placeholders and patched runners.

## Defect 1 — combined repro 15 ERRORS (root-caused, not papered)

Measured at PR head `53b61f5f`:

    pytest -q hscc_daemon/tests/test_node_cli.py \
        hscc-cluster/tests/test_node_pause_tools.py scripts/tests
    -> 636 passed, 5 skipped, 15 errors   (all 15 = EVERY test in the cluster file)

The ERROR repr names the mechanism precisely:

    CollectError: ImportError while importing test module 'hscc-cluster/__init__.py'
    hscc-cluster/__init__.py:4: from . import schemas as S
    E  ImportError: attempted relative import with no known parent package

Chain: one pytest process with args from two legs -> rootdir is the repo root
-> pytest's default directory collector turns any dir holding `__init__.py`
into a `Package` node -> `hscc-cluster/` carries `__init__.py` because HERMES
loads a plugin payload as a package (`register(ctx)`), not because the
hyphenated name is importable (it never can be) -> pytest synthesizes a module
named `__init__` with no parent package -> the payload's own relative import
raises -> every test under `hscc-cluster/tests/` errors at setup.

Why only the COMBINED run: alone, `hscc-cluster/tests` carries its own
`pytest.ini`, so that leg's rootdir is inside it and the repo-root walk never
happens; `scripts/run_tests.sh` runs one pytest process per leg, so CI never
saw it either. This is the same trap t_0c4989d7 (#572) root-caused
independently — its commit message describes this error class from a sibling
repro (27 errors, same message, same import).

FIX: cherry-picked t_0c4989d7's guard (`db07443b` -> `dbc52847`, `cd86cbf4` ->
`b052f57e`) onto this branch, as this card explicitly sanctions. It is a
repo-root `conftest.py` implementing `pytest_collect_directory` (tryfirst):
dirs whose name fails `str.isidentifier()` are collected as plain `pytest.Dir`
(pytest still descends into them; the payload `__init__.py` is never
imported), validly-named packages return None and keep pytest's `Package`
collector. No file exclusions, no marker-skipping — the acceptance forbade
both, the fix changes *what kind of node* the dir is, not what gets collected.

Overlap note for the reviewer: PR #572 (wt/t_0c4989d7) is still OPEN with the
same content. Whichever lands first, the second is a content-identical
squash and will need a trivial rebase/abandon decision — flagged here rather
than silently diverging the two guards.

## Defect 2 — strike-slate pin restored in the reordered setup

Reject: "you DELETED 'assert heal_breaker.strike_count(W1) == 1' — the proof
the strike slate was earned before the pause — instead of RE-ADDING it after
your reordered setup."

Audit of `git diff 354f4570...HEAD`: the only deleted-then-replaced assert line
is the END-of-test `strike_count(W1) == 1` in
`test_node_cli.py::TestResume::test_a_failing_restart_keeps_the_pause_and_the_strikes`
(kept, message added — it guards that a FAILED resume does not clear the
strike). What the reordered setup lacked was the PRE-PAUSE proof: after moving
`note_heal` ahead of `seed_pause`, only `suspended() > 0.0` remained — a cap
read, not a slate count. Restored/added:

  * `test_node_cli.py` failing-restart: `assert heal_breaker.strike_count(W1) == 1`
    directly after `note_heal(W1)`, before `seed_pause()` — the literal pin the
    reject named, in the position that proves accrual happened pre-pause.
  * `test_node_pause_tools.py` `TestResumeTool::test_confirmed_resume_clears_pause_and_strikes`:
    the equivalent `assert heal_breaker.strike_count(W1) == 7` before the pause
    (that test strikes 7x; without the count pin the cleared-to-zero assertion
    cannot show WHAT was cleared was accrued pre-pause).

Both pins hold in the alone runs (54 passed / 15 passed) and in the combined
run — they are state-sealed per test, so no ordering dependence.

## Measured

  * combined repro, before: `636 passed, 5 skipped, 15 errors in 291.30s`.
  * combined repro, after (guard + pins, head `3e44eb9c`): **`656 passed, 5
    skipped in 703.51s`, 0 errors, exit 0** — exit 0 is the card's acceptance.
    (661 items vs 651 before: the cherry-pick adds the 5 cross-leg collection
    tests; the wall-clock inflation is host contention — three concurrent
    suites on this Mac, zero test-side changes.)
  * alone: **54 passed** / **15 passed** (re-measured with the pins in).
  * full suite `bash scripts/run_tests.sh` at head `3bc5af3e`: **ALL GREEN,
    12/12 legs** (✓ hscc-bootstrap, hscc-commands, hscc-roles, hscc-cluster,
    hscc-project, hscc_daemon, sparkrun-hermes, hscc-api, memori_byodb,
    scripts, .github/scripts, comfyui-sparkrun). The two pre-existing
    `test_api_cli_help` in-leg flakes from the first pass did not recur in
    this run; suite wall-clock ~25 min under host contention (three
    concurrent suites on this Mac).
