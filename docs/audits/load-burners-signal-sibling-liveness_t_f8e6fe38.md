# The load_burners SIGINT case stalls only when a sibling suite shares the host

Card: `t_f8e6fe38`. Base: `origin/main@5d5ab25d`. Measured on the 14-core macOS
host these gates run on, 2026-11-25.

Read this before changing any number in `scripts/tests/test_load_burners.py`:
the measurement says the failing wait is **not** tight, so a timeout is not the
fix.

## What was asked

`test_signal_to_the_harness_takes_every_burner_with_it[2-130]` was the sole
load-related failure in both of two concurrently-running `scripts/tests` suites
(t_34c5a3ed's verification), and passed alone. The card asked: which of the two
waits times out — `proc.wait(timeout=10)` or the `_wait_for(_survivors)` drain —
and how long each actually takes with a sibling suite alive. Answer below.

## Finding 1 — reproduced, 2/2, on a clean base

Two full `scripts/tests` suites launched together, two worktrees, both
`origin/main@5d5ab25d`:

```
suite A  2 failed, 732 passed, 4 skipped in 175.03s
suite B  2 failed, 732 passed, 4 skipped in 173.29s
```

Identical in both. Of the two failures, `test_the_per_leg_logs_do_not_pile_up_in_tmp`
is the glob flake t_34c5a3ed already fixed on its own branch (PR #823, not yet on
`main`) — expected here, not this card. The other is this card's case.

## Finding 2 — WHICH wait: `proc.wait(timeout=10)`, and it never returns

```
    assert _burners_of(tag, 2), "no burners to prove the trap against"   # passed
    proc.send_signal(sig)
>   assert proc.wait(timeout=10) == status
E   subprocess.TimeoutExpired: Command '[.../scripts/load_burners.sh', '2', '60', ...]'
                               timed out after 10 seconds
scripts/tests/test_load_burners.py:110
```

`_burners_of` (10 s) passed. The `_wait_for(_survivors)` drain (10 s) was
**never reached** — the harness never exited, so no survivor was ever reported.

## Finding 3 — the harness was ALIVE at the timeout, and the host was not busy

- The traceback line `self = <Popen: returncode: 143 ...>` is the test's own
  `terminate()` from `_running`'s `finally`. The harness survived 10 s of SIGINT
  and died at once to a SIGTERM.
- Peak 1-min load over the failing window: **6.81 on 14 cpus** (`vm.loadavg`,
  sampled every 2 s). Not saturation. A trap that completes in ~0.35 s normally
  does not need 10 s because the box is 2× busy.
- **Only the SIGINT arm fails.** SIGTERM (143) and SIGHUP (129) passed every
  time, including in the same test function a moment earlier. Load is
  signal-agnostic; this is signal-specific.

## Finding 4 — the 10 s budget is nowhere near binding

`probe_signal_case.py` drives the real `_running` / `_burners_of` / `_wait_for`
helpers (imported, not copied) with timers around exactly the three waits:

| wait | alone (8 reps) | with a sibling suite alive (60 reps) | budget |
|---|---|---|---|
| `_burners_of(tag, 2)` | 0.10–0.22 s | 0.06–0.21 s | 10 s |
| `proc.wait(10)` | 0.22–0.48 s, rc 130 | 0.22–0.48 s, rc 130, **0/60 stalls** | 10 s |
| survivor drain | 0.02–0.04 s | never reached | 10 s |

The healthy path spends 2–5 % of its budget. **Raising 10 s would not fix this**:
the stalled run does not finish at 10.4 s, it finishes when the harness's own
60 s watchdog fires. The card's "do not raise the timeout" is confirmed by
measurement rather than assumed.

## Finding 5 — host fact that changes how the harness reads

`setsid` and `gsetsid` are **MISSING** on this host, so
`scripts/load_burners.sh:145-158` always takes the python3 `os.setsid()`
fallback: every burner, watchdog and command child pays a CPython startup, and
carries the burner tag in its argv until `execvp`.

## Finding 6 — the SIG_IGN-inheritance hypothesis is KILLED (measured, not reasoned)

Worth recording because it is the hypothesis the evidence most strongly
suggested, and it is wrong.

Signal-disposition inheritance *would* explain everything: if the suite process
held SIGINT = SIG_IGN when it spawned the harness, `/bin/sh` (bash 3.2) inherits
it ignored and, per POSIX and bash's own rule, **cannot install a trap for an
ignored signal** — `trap 'exit 130' INT` inert, `send_signal(SIGINT)` a no-op,
the harness parked in `wait "$COMMAND_PID"` until its 60 s watchdog. SIGINT-only,
hard 10 s timeout, idle-ish host: all three fit. The repo even documents this
hazard three times (`scripts/run_tests.sh:737`, `scripts/leg_heartbeat.sh:57`,
`scripts/tests/test_leg_heartbeat.py:229`), and CPython's `subprocess` restores
SIGPIPE/SIGXFSZ/SIGXCPU in children but **not** SIGINT, so it would propagate.

`sigdump.py` (a pytest `-p` plugin off PYTHONPATH — no repo file touched)
records the suite process's own INT/TERM/HUP dispositions before **every** test,
and probes whether a `/bin/sh` child spawned at that instant can still trap
SIGINT. Over a full paired run (568 tests sampled, sibling suite alive):

```
tests where a spawned /bin/sh could NOT trap SIGINT: 0
  test_signal_to_the_harness_takes_every_burner_with_it[2-130]
      INT=<built-in function default_int_handler>  child_can_trap_INT=True
```

So at the exact node id that fails, SIGINT is the ordinary Python handler and a
child spawned right there **can** trap it. The harness is never born unable to
trap SIGINT. Whatever stalls it happens after the signal is delivered, inside
bash — which moves the next probe from the test's process into
`scripts/load_burners.sh` itself.

Second thing this run showed: **the stall did not reproduce with the plugin
loaded.** A `/bin/sh` probe spawn inserted before every test — a few ms of
scheduling delay in front of the case — was enough to hide it. That is itself
evidence: the failure is a tight race in when the signal lands relative to
something the harness is doing, not a stable property of the environment.

## Where the next worker should look

The remaining suspects are all inside bash's signal path, and the two that fit
"SIGINT specifically" are:

1. **A trap landing during bash 3.2's own fork/exec window.** The harness forks
   constantly (`new_group` per child, and `sleep 0.2` inside `cleanup`). Bash
   defers pending traps around some of those windows; a SIGINT that arrives in
   one of them may be held until the wait it interrupts is re-entered.
2. **`wait "$pid"` inside `cleanup()` (load_burners.sh:122-124) blocking on a
   child that has not died yet.** Under a sibling the KILL lands but the reap
   needs the target to be scheduled; the `while :; do :; done` burners are the
   most starvable processes on the box *by construction*.

Probe shape that settles it: have `load_burners.sh` touch a marker at trap entry
and at each cleanup stage (a `HSCC_LOAD_BURNERS_TRACE` env var, off by default so
nothing shipped changes), then read which stage the stalled harness reached.

## Reproduce it

Two full suites, launched together from two worktrees of the same commit
(`pair.sh` — the shape that failed 2/2):

```bash
cd <worktreeA> && env -u HERMES_DELEGATED_CHILD_CONTEXT \
  HERMES_DISABLE_LAZY_INSTALLS=1 "$PY" -m pytest -q -rs scripts/tests \
  -p no:cacheprovider -m "not loadstress" > logA 2>&1 &
cd <worktreeB> && env -u HERMES_DELEGATED_CHILD_CONTEXT \
  HERMES_DISABLE_LAZY_INSTALLS=1 "$PY" -m pytest -q -rs scripts/tests \
  -p no:cacheprovider -m "not loadstress" > logB 2>&1 &
wait
```

The per-test signal/disposition probe is loaded without touching the tree:

```bash
PYTHONPATH=<scratch> … -m pytest … -p sigdump      # SIGDUMP_LOG=<file>
```

## Left, for the next worker

1. Finish the `sigdump` readout at the `test_load_burners.py` node ids — that
   either confirms or kills the SIG_IGN hypothesis.
2. If confirmed: find which test in the earlier part of `scripts/tests` leaves
   SIGINT ignored in the suite process (the flip is invisible when the file runs
   alone, which is exactly why "green alone, red with a sibling" holds), and fix
   that test to restore the disposition. That is the root cause; `test_load_burners.py`
   is the victim.
3. If killed: the harness's own signal path is the suspect — instrument
   `load_burners.sh` (a marker file at trap entry) rather than the test.
4. Whatever the cause, consider a non-vacuity guard for the case itself: it can
   currently pass by never observing the signal at all.
