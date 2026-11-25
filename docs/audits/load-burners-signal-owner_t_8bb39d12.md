# Can `load_burners.sh` take its own SIGINT? Only if nobody launched it with `&`.

Card: `t_8bb39d12`. Parents: `t_5f9fab03` (stage trace, `LEFT-2`) and
`t_44a09412` (signal-state readout, whose "Left" section named the two
questions this card had to answer first). Constraint source:
`docs/audits/load-burners-signal-sibling-liveness_t_f8e6fe38.md` and
`docs/audits/load-burners-sigint-trace-stage_t_5f9fab03.md` — both carried
forward here, and both corrected on one point (see F5).

Status: **answered, and fixed.** The signal-owning process does not need to
stop being bash 3.2; it needs to be born able to trap.

## The two questions this card had to answer first

`t_44a09412` Left-1: *name the link that hands down the inherited `SIG_IGN`.*
Left-2: *reconcile `t_f8e6fe38`'s 0/60 stalls alone against 4/4 + 2/2 paired* —
an unconditional inherited ignore would fail every run, not the paired ones.

Both answers are the same link, and it is not inside the harness.

## F1 — the link is the launching shell's asynchronous-command frame

POSIX (`sh` specification, "Asynchronous Command"): when job control is **off**
— i.e. any non-interactive shell — the shell shall arrange for **SIGINT and
SIGQUIT to be ignored** in an `&` command. The ignore survives `fork` and
`execve`. Measured on this host (`docs/audits/support/t_8bb39d12/async_rule.sh`),
same child, one variable:

```
A) foreground child:                              INT=handler QUIT=DFL
B) asynchronous child (`cmd &`, job control off): INT=IGN     QUIT=IGN
C) `( ... ) &` — the pair_chase.sh shape:         INT=IGN     QUIT=IGN
D) `cmd &` under job control (`set -m`):          INT=handler QUIT=DFL
```

That is the whole chain: shell-`&` → suite process → `Popen` → harness.
CPython only installs its own SIGINT handler on an inherited `SIG_DFL`, and
`execve` preserves an ignore, so the ignore rides all the way into bash — and
bash 3.2 is not permitted to `trap` a signal it inherited ignored. So the link
that owns it is the **`&`**, not pytest, not bash's queue, not `load_burners.sh`.

The two launchers that use that shape are both in this repo:

* `docs/audits/support/t_44a09412/pair_chase.sh`, the reproduction harness —
  `( suite "$A" … ) &` / `( suite "$B" … ) &`; and
* `scripts/run_tests.sh:887-899`, the **pack (parallel) mode of the shipped
  runner**: every leg's pytest is launched as `( … ) > "$logf" 2>&1 &`.

## F2 — Left-2: "alone" was foreground, "paired" was `&` — the sibling suite was never the variable

The real test file, launched four ways, no sibling suite and no load
(`docs/audits/support/t_8bb39d12/pytest_shape.sh`, `scripts/tests/
test_load_burners.py`, `HSCC_LOAD_BURNERS_TRACE` on):

| shape | how launched | pytest's own `ignored INT` | result |
|---|---|---|---|
| `fg` | synchronous child of a non-interactive `sh` | 0 | **7 passed** in 4.14 s |
| `bg` | the same command as `cmd &` | 1 (`caught INT=0`) | **1 failed, 6 passed** in 14.09 s |

The one failure is `test_signal_to_the_harness_takes_every_burner_with_it[2-130]`
with `subprocess.TimeoutExpired ... timed out after 10 seconds` at
`scripts/tests/test_load_burners.py:110` — the same arm, the same line, the
same message the pair shape produced, reproduced in 14 s with **one test file**.

So the alone/paired split was never about a sibling suite or CPU pressure: it
was about whether the launcher used `&`. `t_f8e6fe38` F4's 0/60-alone and this
card's 1/1-`bg` are the same experiment with the launcher as the only
difference. (The load and the second suite in the pair shape are what made the
race *look* like a race; the disposition is not a race at all — under `&` the
arm fails deterministically.)

## F3 — the harness-level 2×2: the disposition is the cause, and resetting it is the cure

The real `scripts/load_burners.sh` sitting in `wait` on `sleep 60`, probed from
outside with `sigprobe`, then sent the SIGINT the test sends
(`docs/audits/support/t_8bb39d12/harness_shape.sh`):

| shape | harness `ignored INT` | `caught INT` | SIGINT | exit |
|---|---|---|---|---|
| `fg` (sync) | 0 | 1 | delivered | **130** |
| `bg` (`cmd &`) | 1 | 0 | **not delivered** | 137 (KILL only) |
| `fg_ign` (sync, INT forced `SIG_IGN`, no `&`) | 1 | 0 | **not delivered** | 137 |
| `bg_reset` (`&`, dispositions reset to `SIG_DFL` first) | 0 | 1 | delivered | **130** |

Row 3 says the `&` is not needed — the disposition alone is sufficient. Row 4
says the `&` is not sufficient either — with the disposition cleared, a
backgrounded harness traps INT normally. The `ignored INT=1 / caught INT=0`
with `caught TERM=1 HUP=1` signature is the one `t_44a09412` N6 read off two
live stalled harnesses, produced here on demand.

## F4 — what the fix therefore is, and what it is not

The harness documents (header, "TRAP") that INT, TERM and HUP all route through
`cleanup()`, whose whole reason for existing is that a load harness which dies
without cleanup orphans its burners (the 3h40m/load-24 incident, `t_71ed3b1c`).
Any launcher can put the harness in the deaf state — `cmd &` is how the shipped
pack mode starts legs — so the guarantee has to hold *regardless of launch
shape*. That makes it the harness's own invariant, not the launcher's, and not
the test's (the test's `_running` cannot reach `loadstress_leg.py`'s or an
operator's invocation anyway).

Implemented in `scripts/load_burners.sh`, before any fork or `mktemp`: ask
`python3` (already a dependency of `new_group` on hosts without `setsid`)
whether SIGINT is inherited as `SIG_IGN`, and if so re-exec once through an
interpreter that puts it back to `SIG_DFL` — the card's candidate (a), with the
`sigprocmask` part dropped because `t_44a09412` N4.3 measured that bash 3.2
clears an inherited *mask* at startup and the mask was never the state.

Deliberately **not** done, with the reason:

* **TERM/HUP are left alone.** `nohup` ignores HUP, and a caller that ignores
  TERM is making a deliberate choice. With TERM or HUP ignored the harness
  *survives* (its watchdog still fires), so the no-orphan invariant holds;
  with INT ignored the harness silently voids a trap it installs. SIGINT is
  also the only trapped signal the POSIX async rule sets.
* **`set -m` is not the fix.** Row D of F1 shows job control suppresses the
  ignore, but the harness header records why job control is deliberately not
  used: on Linux a job-controlled background child can stay in the harness's
  own PGID, which is how this passed on macOS and failed on CI.
* Not a timeout raise, not a `loadstress` marker, no test file touched, no
  wait/trap-shape change (`t_5f9fab03`'s rejected-fix section).
* `HSCC_LOAD_BURNERS_KEEP_SIGINT=1` opts an operator out.

## F5 — correction to the parent audits' mechanism sentence

`docs/audits/load-burners-sigint-trace-stage_t_5f9fab03.md` F2 and
`…_t_44a09412.md` N6 both describe the stalled harness as a *race* ("bash 3.2
can LOSE a SIGINT delivered in a narrow startup race"). The measurement here
says the loss is not narrow and not a race in the paired/`&` shape: it is
deterministic, 100 % under `&` (6/6 signal-arm harnesses per suite in
`t_44a09412`, 1/1 files here), and the trigger is how the harness was launched.
The "race" reading survives only for whatever made the arm fail in a
*foreground* run — and no such failure has been measured: `t_f8e6fe38` F4
recorded 0/60 alone, and this card's `fg` row passed 7/7.

## F6 — proof

See "Proof" below (written after the runs land).

## Left for the next worker

* `scripts/run_tests.sh` pack mode is the production instance of the async
  frame. Other shipped launchers should get the same look for `( … ) &`
  around anything whose signal handling matters (this card measured
  `load_burners.sh` only).
* The shipped test still cannot see the difference between "the trap ran" and
  "the case never observed the signal" — `HSCC_LOAD_BURNERS_TRACE`'s markers
  are asserted here, in the audit, not in `test_load_burners.py`, per
  `t_f8e6fe38` LEFT-4.
