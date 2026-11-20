kind: Docs
task: t_0a9bdc40

- **`docs/audits/phone-performance.md` now says what its latency table was
  actually measured by.** The table under "Slowest endpoints (measured
  read-only, median of 5 passes, live API)" is one run of `python3
  scripts/perf_measure.py --passes 5` at `d26a7fb0` (2026-09-02) — still the
  only revision of that script on `main` before PR #490 — and that revision's
  run fired `/v1/verify` five times (each hit a fleet smoke test doing real
  chat round-trips on the GPU units), fired the POST-only `/stop` `/up` `/down`
  family, timed `/v1/logs` without the query the app always sends, and cut
  `/v1/health`'s cold start off at `curl -m 30`. So the heading's "measured
  read-only" was not true of the run that produced the numbers. The audit now
  pins the command, the revision, and that the run predates the never-fire
  rule, so a reader does not take the table as something a current
  `--passes 5` reproduces byte for byte; the repro block states what
  `/v1/verify` and `/v1/health` do differently now (never fired / a
  `HEAVY_ROUTES` `-m 120` of its own) and points at
  `hscc_daemon/api_route_sweep.py`'s tables as the single home of those rules
  instead of re-deriving them per script. Nothing was re-measured and no
  ranking changed: the six routes in the table are read paths, and the note
  says plainly that the contention the five verifies caused is an inference,
  not a corrected number.
