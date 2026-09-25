# Audit / fix report — t_cc8879e0: Engine-wedge check declares a BUSY unit wedged

Date: 2026-09-25
Branch: wt/wedge-load-aware
Commit: 920f4b6
Scope: hscc_daemon/health.py (+ hscc_daemon/tests/test_engine_wedge.py)

## Problem

The engine-wedge check declared a BUSY unit wedged. Under the flat
`ENGINE_WEDGE_TIMEOUT=10s` wall-clock bound for the whole streaming probe, a
saturated tp=2 304B-class engine (a queued request exceeding 10s to first
token purely from concurrency) is indistinguishable from a wedged (idle,
never-generating) engine. `ENGINE_WEDGE_LOAD_GRACE=240` did not help: it is a
startup grace only (`first_seen` = first probe since daemon START), so after
240s of daemon uptime it can never apply again.

Live consequence: `Engine-wedge check: WEDGED unit(s) detected` logged twice,
macOS "inference engine wedged" notifications every ~15s, and on the first
occurrence the daemon received signal 15 six seconds after the verdict.
Whether the wedge path caused that SIGTERM is NOT established and was not
assumed — noted as a separate investigation if it recurs.

## Fix

Single file `hscc_daemon/health.py`. The verdict is now load-aware; a wedge
is "answered 200 with ZERO tokens while IDLE", not "slow while loaded".

1. Before counting a 200-with-zero-tokens probe as a wedge failure, consult
   the unit's vLLM /metrics (`num_requests_running` + `num_requests_waiting`)
   via `throughput.fetch_node_metrics` — the SAME Prometheus parser the
   G/T/E streams and `verify` already trust (no second, divergent /metrics
   implementation). A unit demonstrably serving other requests is reported
   "busy" (a saturated-but-healthy engine), NOT a wedge; its failure streak is
   reset and `last_success` is updated, because a loaded unit is genuinely
   live.

2. 200-with-zero-tokens while /metrics is IDLE remains the genuine wedge
   signature and is detected unchanged (existing zero-token-while-idle
   detection preserved).

3. When /metrics is unreachable (no busy signal: `fetch_node_metrics` returns
   None), fall back to a per-unit probe bound derived from that unit's own
   observed successful throughput: `max(ENGINE_WEDGE_TIMEOUT, 3 x median of
   recent successful probe latencies)`. A unit that routinely streams fast
   keeps a tight bound (a genuine wedge is still caught quickly); a unit that
   historically runs hot under load gets headroom proportional to its own
   observed norm. The report message is honest about the degraded mode
   ("per-unit bound, no /metrics busy signal"). Crossing the debounced
   threshold against the per-unit bound still declares a wedge — a genuinely
   wedged engine is not missed.

4. `ENGINE_WEDGE_*` defaults are NOT changed. New
   `ENGINE_WEDGE_BUSY_CHECK_TIMEOUT=2s` bounds just the /metrics fetch so a
   loaded probe already burning the full probe bound is not made worse by an
   additional slow metrics pull.

New state key: `busy` in the `engine_wedge` stream; the summary message
appends "N busy (loaded, not wedged)".

## Tests

`hscc_daemon/tests/test_engine_wedge.py` — both directions required by the
card, plus the fallback and helper-level coverage (new class
`TestEngineWedgeLoadAware`):

- (a) `test_busy_unit_is_not_wedged` — 200-with-zero-tokens while /metrics
  shows it serving other requests => reported "busy", NOT wedged, ok stays
  True.
- (b) `test_idle_zero_tokens_is_wedged` — 200-with-zero-tokens while /metrics
  is IDLE => the genuine wedge, still caught (ok False, unit in `wedged`).
- `test_busy_unit_does_not_accumulate_wedge_streak` — repeated busy verdicts
  never accumulate wedge credit.
- `test_idle_below_threshold_debounces` — idle-wedge still debounces.
- `test_no_busy_signal_falls_back_to_per_unit_bound` — /metrics unreachable;
  a unit stalling even the per-unit bound IS wedged, message names the
  per-unit bound (degraded mode).
- `test_unit_probe_timeout_scales_with_observed_latency` — per-unit bound
  scales with the unit's own observed latencies but never drops below the
  flat default.
- `test_unit_busy_reads_running_and_waiting` — `_unit_busy` itself: running+
  waiting > 0 => True, both zero => False, unreachable => None.
- `test_busy_verdict_resets_wedge_streak_then_idle_declares` — a busy verdict
  never masks a later genuine idle wedge.

Existing wedge tests that drive the probe into the `wedged` branch now pin the
busy signal to IDLE (`_idle`) so they deterministically exercise the genuine
idle-wedge path and never make a real HTTP request to the test unit's
10.0.0.x address. Fake probe helpers accept the new `timeout` kwarg.

## Verification (by execution)

Both directions verified by running the suite; `hscc_daemon/tests` is green
under BOTH interpreters and the full multi-plugin suite is ALL GREEN under
both.

Interpreter 1 — ~/.hermes/hermes-agent/venv/bin/python:

  HSCC_TEST_PY=~/.hermes/hermes-agent/venv/bin/python scripts/run_tests.sh
    hscc-bootstrap  272 passed
    hscc-commands   69 passed
    hscc-roles      114 passed
    hscc-cluster    422 passed
    hscc-project    1351 passed
    hscc_daemon     1144 passed
    sparkrun-hermes 12 passed
    hscc-api        786 passed, 1 skipped
    ALL GREEN (exit 0)

  (hscc_daemon alone, same interpreter: 1144 passed in 48.76s)

Interpreter 2 — /Users/desac/miniconda3/envs/p313/bin/python:

  HSCC_TEST_PY=/Users/desac/miniconda3/envs/p313/bin/python scripts/run_tests.sh
    hscc-bootstrap  272 passed
    hscc-commands   69 passed
    hscc-roles      114 passed
    hscc-cluster    404 passed, 14 skipped
    hscc-project    1351 passed
    hscc_daemon     1144 passed
    sparkrun-hermes 12 passed
    hscc-api        786 passed, 1 skipped
    ALL GREEN (exit 0)

No live serving unit was probed or stopped; all verification was in-process
(fake HTTP servers, monkeypatched probes and /metrics). Read-only against the
fleet per the operator rule.

## Report requirements

- branch: wt/wedge-load-aware
- commits ahead of main pre-merge: 1 (920f4b6)
- merged/pushed/deployed: see final kanban handoff metadata.
