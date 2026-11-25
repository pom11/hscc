kind: Docs
task: t_f8e6fe38

- **`docs/audits/load-burners-signal-sibling-liveness_t_f8e6fe38.md`**:
  measurement of why `test_signal_to_the_harness_takes_every_burner_with_it[2-130]`
  stalls only when a second `scripts/tests` suite shares the host. The failing
  wait is `proc.wait(timeout=10)` (`test_load_burners.py:110`); the
  `_wait_for(_survivors)` drain is never reached. The harness is still alive at
  the timeout (the traceback's `rc 143` is the test's own `terminate()`), peak
  load was 6.81 on 14 cpus, and only the SIGINT arm fails while TERM and HUP
  pass — so this is signal-specific, not load-specific, and the 10 s budget
  (2–5 % spent on the healthy path, 0/60 stalls under a live sibling) is not the
  binding constraint. The audit also records the SIGINT-`SIG_IGN`-inheritance
  hypothesis as measured-and-killed, so the next reader does not spend a run on
  it. No behaviour change: no shipped file is modified.
---
kind: Verified
order: 1
task: t_f8e6fe38

- Reproduced the card's condition 2/2 on `origin/main@5d5ab25d`: two full
  `scripts/tests` suites launched together in two worktrees — **2 failed, 732
  passed, 4 skipped in 175.03 s** and **2 failed, 732 passed, 4 skipped in
  173.29 s**, the load_burners SIGINT case failing identically in both (the
  other failure is the `hscc-legs-*` glob flake t_34c5a3ed already fixes on its
  own branch).
- Timed the three real waits through the test's own helpers (imported, not
  copied): alone 8 reps and with a sibling suite alive 60 reps —
  `_burners_of` 0.06–0.22 s, `proc.wait` 0.22–0.48 s returning 130, survivor
  drain 0.02–0.04 s, **0/60 stalls**, against a 10 s budget on each.
- `sigdump.py` (pytest `-p` plugin off PYTHONPATH, no repo file touched) sampled
  the suite process's INT/TERM/HUP dispositions before 568 tests with a sibling
  alive: `0` tests where a spawned `/bin/sh` could not trap SIGINT, including at
  the failing node id itself.
