kind: Fixed
task: t_7bccacef

* **A merge-queue gate no longer runs at background priority.** The queue's
  gate is a child of the `com.hermes.hscc_daemon` launchd job, whose plist sets
  `ProcessType => Background`, so every pytest leg inherited background QoS and
  was scheduled behind ordinary work. Measured A/B on ONE leg in the SAME
  worktree: warm at default QoS 144.9 s vs warm at background QoS 484.7 s —
  **3.34x**, and 3.80x under the queue's exact condition (no-bytecode +
  background). The suite itself needs 1092.6 s unopposed, i.e. 61% of the
  1800 s budget: it was the execution environment exhausting the budget, not
  the suite. `_run_gate` now runs the gate as `taskpolicy -c utility /bin/sh -c
  <gate>` on macOS, so the shell and every pytest leg under it inherit the
  elevated class. The operator's gate string still reaches `/bin/sh` whole and
  `shell=True` is deliberately no longer used: with a list argv, POSIX
  `shell=True` execs only `argv[0]`, which would have run a bare `taskpolicy`,
  exited 64 and recorded `red-gate` against code that was never compiled
  (measured). Fail-safe: no `taskpolicy` binary, or a non-macOS host, means no
  prefix and today's behaviour. The daemon's launchd plist is deliberately
  untouched — it is live operator-owned host state.
* **A gate that runs out of budget no longer leaves its pytest running.**
  Without `start_new_session` the timeout killed the `sh` but not the process
  group, so the real pytest survived its own budget and kept competing with the
  *next* attempt — the queue re-gated 68 s later and ran attempt 2 against
  attempt 1's orphan. `_run_gate` now starts the gate in its own session and
  kills that whole process group on timeout, then reaps it, so no leg can
  outlive its budget.
* **A timed-out gate's handback now carries evidence.** `TimeoutExpired.stdout`
  arrives as `bytes` even when the request asked for text, and the old
  `isinstance(..., str)` test threw it away — which is why both measured
  handbacks (PR #360, PR #460) ended as `['', '', 'timeout after 1800s']` and
  nobody could tell a timeout from a failure by reading the comment. The tail
  now decodes `bytes` (with `errors="replace"`) as readily as `str`.
---
kind: Verified
task: t_7bccacef

* `hscc_daemon/tests/test_merge_queue.py` — new cases, injected runners only
  (no real repo, no network, no wall-clock assertion): the argv `_run_gate`
  builds carries the QoS mechanism (asserted on the constructed argv, and on a
  host with no `taskpolicy` the gate keeps its pre-fix argv rather than gaining
  a prefix that would fail the gate); a timeout kills the *recorded* process
  group — asserted on which pgid `os.killpg` was aimed at, not on a sleep — and
  a gate with no recorded group still returns the `timeout` verdict, so the
  kill can never become the only thing that stops a leg; and a
  `TimeoutExpired` carrying `stdout=b"real tail line"` yields a handback that
  contains `real tail line`.
* One case does not use an injected runner and is the more valuable for it: it
  builds the argv `_run_gate` builds and executes it for real against a `printf`
  gate in a temp dir, asserting the whole operator string — quoting, `&&` and
  all — reaches the shell and exits 0. Shape assertions on a fake `Popen` cannot
  see an argv the OS cannot run; this one can, in milliseconds, with no repo,
  network or wall-clock claim.
* Non-regression pins kept green: a genuine non-zero gate still records
  `red-gate` and still drops the entry, and a registry without `timeout_s`
  still gates on the 1800 s `DEFAULT_GATE_TIMEOUT_S` — asserted through a real
  tick, on the budget the leg was actually handed.
