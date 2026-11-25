kind: Fixed
task: t_4fc35c1f

* runner: **a packed leg whose name is a path now runs at all.** The leg log was
  built by splicing the leg name into a filename (`$LEG_TMP/$P_NEXT-$d.log`), and
  `.github/scripts` is the one roster leg whose name is nested, so its log path was
  `…/1-.github/scripts.log` — a file inside a directory that was never created. The
  redirect failed in the child before `env` was execed, so pytest never started, the
  wrapper recorded no `.rc`, and the leg read red off `wait`'s status: `elapsed=0s
  rc=1`, an empty block, and no `FAILED` line for anyone to capture. **It was red on
  every packed run, at every parallelism, on every host** — one of the three reds that
  kept `HSCC_TEST_PARALLEL`'s default at 1 was never a concurrency problem at all.
  Eleven of twelve leg names are one flat component, which is why the 24 tests that
  shipped with the pack could not see it. `/` is now flattened in the log filename;
  roster order stays in the index prefix and the leg's real name stays in the
  heartbeat TSV and the summary rows.
* test harness: **the `hscc-api` watchdog leg's `('Z', '<defunct>')` setup error was a
  race inside the fixture, not a side effect of leg packing** — and the two mechanisms
  the parent card named (`t_6c2123f9`: the runner's deferred reaping, and `set -m`)
  cannot produce it. The pid in that message is the sandbox API parent the `sidecar`
  fixture spawned itself, so it is a direct child of the pytest process and only that
  process's teardown can reap it; no runner tick touches it. In `mode="stop"` the
  parent is instructed to call `shutdown()` and `raise SystemExit(0)` the moment it is
  ready, and the fixture then asserted that same pid was still alive — a demand that
  the fixture, not the scenario, win the scheduler. Measured on one node id, serial,
  `HSCC_TEST_PARALLEL` unset: **0 red in 25 runs at ordinary priority, 8 red in 30
  under twelve bounded burners**, every one `('Z', '<defunct>')`, and **0 red in 25**
  for the `serve` arm under the same load — so the variable is CPU pressure, and the
  arm. The pack was one more source of pressure, not a new mechanism.
  The platform half of the self-check now runs against a marker-bearing canary the
  harness starts and holds itself, so it still reddens on a clipped `ps` column, and
  the liveness assertion stays where it is a real precondition (`serve`,
  `no_watchdog`, `stop_on_file` — that one gates its stop on a sentinel the harness
  writes later). In `stop` the harness asks its own `Popen`, the authority on its own
  child and immune to pid recycling, that the parent is either still running or exited
  `0`. The posture that measured 8/30 measures **0/30** after.
  `docs/audits/packed-roster-reds_t_4fc35c1f.md`.
---
kind: Verified
task: t_4fc35c1f

* runner: `scripts/tests/test_run_tests_leg_parallelism.py` — **25 tests**, green. The
  new one packs `.github/scripts` with a flat-named leg and requires the leg's block to
  be NON-EMPTY, because the runner prints the `━━━ <leg> ━━━` header whether or not the
  log opened: a header with nothing under it is this defect, and a green stub cannot
  produce one. Verified to bite — with the fix reverted it reproduces `✗
  .github/scripts (pytest exit 1)` exactly.
* test harness: `hscc-api/tests/test_gateway_serve_watchdog.py` — **19 passed, 1
  xfailed** unchanged from before the fix at the file level (the flake is invisible in a
  single quiet run, which is the point), plus the repeat evidence above: 30 runs of the
  racing node under the same burner load, red 8 → green 0.
* runner: `scripts` measured **green packed** — 699 passed, 4 skipped in 246.22 s at
  `p=2` beside `.github/scripts`. The `1 failed of 702` the parent card recorded did not
  reproduce and its failure line is still un-captured; recorded as unresolved rather
  than absorbed.
