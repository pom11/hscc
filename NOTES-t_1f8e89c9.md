# t_1f8e89c9 — fix red tests: strike the node BEFORE pausing it

## State
Branch `wt/t_1f8e89c9`, base `08441f22` (origin/main tip, which is above `c28f3b2c`/
`de02ac9e`). Measured red at this base (matches the card exactly):

- `hscc-cluster/tests/test_node_pause_tools.py::TestResumeTool::test_confirmed_resume_clears_pause_and_strikes`
  → `assert heal_breaker.suspended(W1) > 0.0` gets `0.0` (1 failed, 14 passed)
- `hscc_daemon/tests/test_node_cli.py::TestResume::test_apply_restarts_then_clears_pause_and_strikes`
  → `assert heal_breaker.strike_count(W1) == 7` gets `0`
- `hscc_daemon/tests/test_node_cli.py::TestResume::test_a_failing_restart_keeps_the_pause_and_the_strikes`
  → `assert heal_breaker.strike_count(W1) == 1` gets `0`

## Root cause (confirmed against source)
`heal_breaker.note_heal()` (hscc_daemon/heal_breaker.py:199-208) returns early for a
node the pause overlay holds — no strike appended, `suspended_s` 0.0 — exactly what
#568 (`de02ac9e`) shipped. The three tests seed the pause FIRST, then call `note_heal`,
so no strike ever lands and the strike/suspended assertions can never hold.

This is a merge-order collision (#568 merged before #567; #567's tests were authored on
a pre-#568 base), not a bad commit. The shipped code is CORRECT. **Do not touch the
shipped behaviour** — that reverses t_e6ee7e60 and needs its author in the loop.

## The fix: reorder the SETUP, keep every assertion
Strike the node BEFORE the pause in all three. `suspended() > 0.0` moves to after the
strikes / before the pause (it reads the ledger only, so it is pause-independent and
still true). Add one assertion that the pause is in force at the moment of the resume,
so the scenario cannot silently degrade into a plain "resume an unparked node" case.
Keep `HEAL_BREAKER_MAX` untouched; keep the 7-strike (== cap+1) so the node is genuinely
suspended before resume.

Contract this proves (per #568 docstring "strikes earned before the pause still count
after the resume"): a slate earned while healing survives a pause, and a confirmed
`hscc node resume` clears it. `heal_breaker.clear()` runs only on the success path
(hscc.py:1727, after `resume_units` ok + `node_pauses.clear`), so the failing-restart
test keeps both the pause AND the strike.

## Hermeticity (unchanged by the reorder)
`test_node_pause_tools.py` has its own autouse `_seal_hscc_state` (HSCC_DIR +
HEAL_BREAKER_FILE); `test_node_cli.py` relies on the `hscc_daemon` conftest
`_isolate_hscc` (same two seams). Both already seal per-test; a pause write lands in tmp,
never the operator's `~/.hscc`. No shipped file touched → no changelog fragment needed.

## Teeth to prove (mutation-probe AFTER the reorder)
Remove `heal_breaker.clear()` at hscc.py:1727 → `test_confirmed_resume_clears_pause_and_strikes`
AND `test_apply_restarts_then_clears_pause_and_strikes` must go RED (strike_count stays
7). If either stays green, the test no longer tests what it claims.
