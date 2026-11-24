kind: Verified
task: t_3b38aca4

* **Swept every literal fixture clock in `hscc_daemon/tests/` against the wall clock
  under two harnesses: no forward date-bomb remains.** Each of the eight modules that
  fixes an instant was run at the real clock, bracketing its own literal, frozen at
  2020-01-01 and at 2200-01-01 (with and without `time.time()` frozen), and under the
  shipped `wallclock_shift` at ±400 days. Nothing reddens in the forward direction — the
  direction that reddened `leg hscc_daemon` at 2026-11-22T13:00Z — and the five
  backward-direction bombs t_ed416771 found in `test_cron_watchers.py` are green at every
  arm on this tree, so that file's fixture clock is now the only clock its cases see.
  This card had independently fixed those same five cases; `origin/main`'s version was
  taken in the merge rather than a second edit re-landed on top.
* **One case stays clock-sensitive past the reach of the shipped rule:
  `test_autodown.py::TestProbeKanbanActivity::test_kanban_wake_seam_when_down`.** Its
  `down_since` is the literal `2020-01-01T00:00:00+00:00`, while the probe path it
  exercises stamps activity through the product's clock (`autodown.py:138` →
  `state.now_iso`); only that floor sitting behind both clocks is what makes the wake
  decision deterministic, and the only thing asserting that was a docstring sentence.
  Measured: 206 cases green at the real clock, on both sides of its own literal, at
  2200-01-01 and at ±400 days; 2 failed at 2020-01-01 — the case, and the guard under it,
  which fails there by design. It is therefore NOT a future-date risk (real time moves
  away from a 2020 floor), and `test_single_clock_rule.py` cannot see it: 400 days back
  from any plausible date still leaves 2020 behind the clock. The floor is now a named
  constant, `WAKE_SEAM_FLOOR`, and `TestWakeSeamFloorIsExecutable` asks the product's own
  predicate `_fresh_activity_since_down` whether it still holds — re-pinning the floor to
  2027 reddens 3 cases (the case plus both guards), so neither guard is vacuous.
  Test-only: no shipped module changed, no skip or xfail added.
