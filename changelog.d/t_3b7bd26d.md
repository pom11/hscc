kind: Fixed
task: t_3b7bd26d

* **`test_a_timing_out_tool_lands_in_errors_and_the_pass_continues` failed under CPU
  contention, and failed for a reason unrelated to the behaviour it names.** The case
  plants a fake `brew` that sleeps 5 s, then shrank `wc.CACHE_TIMEOUT` from its
  production **300 s to 0.2 s** and required every OTHER command in the same pass
  (`uv cache`, `npm cache`) to still reach status `ran`. That constant is the cap for
  **one command** — `_run_cache_command` hands one to `_run` per action, which is the
  whole reason the cap is per command rather than per run, so a wedged `brew cleanup`
  cannot wedge the verb — but patching the module constant compressed the **whole
  pass**. Every unrelated subprocess then had to spawn, run and be reaped inside
  200 ms. Measured through the engine's own `_run` on a 14-core host with load avg 2.0:
  the sandbox shim costs **median 3.6 ms, p95 4.1 ms** — and **max 174.6 ms** in one
  sample of 120, i.e. one measurement on a nearly idle host already sat 13 % inside the
  budget the case was enforcing on it.
* Reproduced at `f2f8bc2d` in the CI shape (leg interpreter 3.14.10 / pytest 9.1.1,
  `scripts/run_tests.sh`'s sealed `HOME`, `nice -n 10` + `taskpolicy -c background`,
  which is the scheduling class a `ProcessType => Background` merge-queue gate actually
  gets) against **three concurrent `hscc_daemon` legs**: **3 red in 16 samples**, always
  at the same line, always while the pass had in fact continued — one failure read
  `assert {'npm cache', 'uv cache'} <= {'hscc UI-test temp dir'}`, i.e. the tools were
  being **killed by the test's own budget** and then reported as evidence that the pass
  had stopped. CPU burners alone do NOT reproduce it (max 62.5 ms at load avg 9.4); the
  concurrent legs do.
* The compressed cap now goes to **the planted hang only**, injected at `_run` — the
  seam the module names for exactly this ("Test seam: patch this, not
  `subprocess.run`") — and every other command is handed back the cap production uses,
  so nothing but the hang is racing the clock. `CACHE_TIMEOUT` is still the patched
  constant, so the `killed after Ns` detail stays true for the one command that tripped
  it. Three pins keep the injection from quietly becoming a no-op: exactly one entry
  lands `timed-out`, every cache command was **asked** for the patched constant, and at
  least two ran under the production cap.
* **The pass-continuation claim could not fail, and now can.** Mutation M1 — `break`
  after the error append in `_execute_cache`, i.e. the engine really does stop on the
  hang — left the filed case **green (1 passed)**: `uv cache` and `npm cache` are
  planned BEFORE the hang, so the only evidence demanded that the pass had continued
  was about commands that had already run. It now asserts one entry per planned cache
  action, in plan order, plus no entry left `planned`; M1 is red with
  `never attempted: ['pip cache', 'simulators', 'pytest temp dir', 'hscc UI-test cache']`.
---
kind: Verified
task: t_3b7bd26d

* Red-before executed on a pristine detached `f2f8bc2d` worktree, matched A/B under ONE
  induced storm (3 concurrent `hscc_daemon` legs; before-tree and fixed-tree samples
  interleaved in the same contention window, same scheduling class, load avg 9.6-11.2):
  **before-tree FAILED 3 of 12 pairs, fixed-tree PASSED 12 of 12**. Every before-tree
  failure is the same line 467 assertion with the pass having continued.
* Mutation battery at the final tip (product code mutated, never the assertion; each
  mutant restores and the worktree is proved clean before and after):
  **M1** pass aborts on the first failing command → **red** (survived twice against the
  filed case and against a first, wrong pin — the loop appends each entry *before*
  running it, so an abort leaves those actions with **no** entry rather than a
  `planned` one, which is why the pin compares the roster against the plan).
  **M2** a killed command is no longer reported as `timed-out` → red.
  **M5** the per-command injection made a no-op (the old global shrink) → red.
  **M6** `def _run_cache_command(argv, timeout=CACHE_TIMEOUT)` — the cap frozen at def
  time, the exact trap where patching a constant silently stops biting → red on the
  "asked for the patched constant" pin. **M0** no-op control green.
* `test_workspace_clean_deep.py` + `test_workspace_clean.py` + `test_workspace_clean_cli.py`:
  **115 passed, 1 skipped** (the pre-existing `real_hermes` skip), leg interpreter.
* Full `hscc_daemon` leg at the tip: see the PR body (posted after the storm host
  emptied, so the timing is unopposed).
* Hermeticity: test-only diff, no product change, no new probe surface. Everything stays
  inside the file's own `cache_sandbox` (HOME/TMPDIR under `tmp_path`, every one of the
  seven cache binaries resolved into the shim dir); the injection delegates to the
  engine's own `_run`, so no command reached a real `brew`/`uv`/`npm`/`conda`, and the
  reproduction runs under the runner's sealed `HOME` like every other leg.
