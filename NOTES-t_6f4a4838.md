# NOTES — t_6f4a4838: the fixture-ordering class #568/#567 created

Tests-only. No shipped behaviour touched; the paused-node short-circuit in
`heal_breaker.note_heal()` is the contract and stays exactly as #568 shipped it.

## Where the class came from

`note_heal()` accrues NOTHING for a node the pause overlay covers — no strike, no
ledger write, `tripped` False (heal_breaker.py, "A PAUSED NODE IS NOT AN
OUTAGE"). That landed in #568. `TestResume` landed twelve minutes later in #567,
authored against a pre-#568 base, and its fixtures seed the pause and *then*
call `note_heal()`. So no strike was ever recorded, `strike_count()` stayed 0 and
`suspended()` stayed 0.0 for the whole test.

Three of those assertions demanded a slate and so went red. A fourth asserted
`strike_count() == 0` after the resume — a clearing assertion on a slate that was
never written — and has been green on main ever since. That fourth one is what
this card added on top of PR #575's reorder:

    hscc_daemon/tests/test_node_cli.py::TestResume
      ::test_resume_without_a_recorded_reason_still_clears_strikes

It is the dangerous shape of the class: a test that passes precisely because the
setup silently did nothing.

## The guard, and why it is asymmetric

`TestNoStrikeAccrualBehindAPause`, one copy per leg (the legs are separate pytest
processes; `hscc-cluster` cannot import from `hscc_daemon/tests` and vice versa).
It parses its own file and fails when a test parks a node and then expects a
strike to accrue for that same node.

The asymmetry is the whole design:

* Fires only on a DEMAND: `strike_count(...) == n` (n non-zero),
  `note_heal(...)["count"] == n` (n non-zero), or `suspended(...) > x`.
* `strike_count(...) == 0` is NOT a demand. A parked node accruing nothing is
  #568's contract, pinned in `test_heal_breaker_node_pause_t_e6ee7e60.py`; a
  guard that flagged it would eat the pin it stands on, and the first person to
  hit that red would delete the guard rather than the fixture.
* Host matching is by name token (`W1`, an IP literal, a `host=` keyword), so
  parking W1 while striking W2 stays legal, and `W1` is not mistaken for `W10`.

## Measured

At `e06865cc`, hermes venv via `scripts/run_tests.sh`:

* `HSCC_TEST_LEGS="hscc_daemon hscc-cluster" bash scripts/run_tests.sh` → exit 0
  * `hscc-cluster`: 1157 passed, 3 skipped (51.54s)
  * `hscc_daemon`: 4501 passed, 1 skipped (177.61s)
* `python3 scripts/changelog_fragments.py check` → OK (75 fragment entries), no
  fragment added: no operator-visible behaviour changes.

Baseline that made the class visible, at `40879c16` (before #575 landed):
`test_node_cli.py` 2 failed / 52 passed and `test_node_pause_tools.py`
1 failed / 14 passed — the three tests the card named.

## Probe: proving the guard bites (9/9 as expected)

Each probe runs ONLY the guard node, so the verdict measures the guard and not
the behaviour tests it watches. Pristine text is captured from
`git show HEAD:<path>` (never from the file about to be overwritten), every probe
resets and byte-verifies the restore, `PYTHONDONTWRITEBYTECODE=1`, and the run
ends with `git status --porcelain` empty.

| # | mutation | expected | got |
|---|---|---|---|
| P0 | control, unmutated | green | green |
| P1 | park statement moved before the strike in the 4th test | red | red |
| P2 | park moved before the strike loop in `test_apply_restarts...` | red | red |
| P3 | park moved before the strike loop in the cluster tool test | red | red |
| P4 | decoy test: park, then assert NOTHING accrued | green (whole file) | green |
| P5 | decoy test: park W1 (span W1 only), strike W2 seven times | green (whole file) | green |
| P6 | cluster copy of the P4 shape | green (whole file) | green |
| P7 | P1 **and** `seed_pause` removed from `PARKING` | green | green |
| P8 | P1 **and** `_demands()` short-circuited to `return False` | green | green |

P1-P3 are the teeth. P4-P6 are the false-positive controls, and they run the
whole file so the decoy has to be a genuinely passing test, not merely an
unflagged one. P7-P8 are the negative side of the same claim: removing either
detector makes the guard blind to a real violation, which is what shows the
guard fires *because* of those detectors rather than always.

Re-deriving the driver: the mutations are (a) cut the statement whose leaf name
is in `PARKING` and re-insert it before the smallest-lineno statement inside the
test that contains the first `note_heal` call, re-indenting to that statement's
column; (b) append a decoy test function; (c) one-line replacements inside the
guard class, each asserted to match exactly once.
