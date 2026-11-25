# Where inside `load_burners.sh` does the SIGINT stall die? (measured by trace)

Card: `t_5f9fab03`. Parent: `t_f8e6fe38` (audit
`docs/audits/load-burners-signal-sibling-liveness_t_f8e6fe38.md` — read it
first; the measurement there is re-verified below, not re-derived).
Base: `origin/main` (branch `wt/t_5f9fab03`).

Status: **probe shipped; fix open, with the stage localized by measurement
and one wait-shape fix measured-and-rejected.** The card's decision table
answered at outcome (1) — see F1–F4 and "Left for the next worker".

## What the parent established (constraints carried forward)

- The failing wait is `proc.wait(timeout=10)` at
  `scripts/tests/test_load_burners.py:110`; `_burners_of` passes; the
  `_wait_for(_survivors)` drain is never reached.
- Do not raise the timeout (healthy path 0.22–0.48 s of 10 s; the stalled run
  never returns — it ends at the harness's own 60 s watchdog).
- SIGINT-specific (TERM 143 / HUP 129 pass), sibling-suite dependent, and a
  tight race (a few ms of per-test scheduling delay hides it).
- SIGINT `SIG_IGN` inheritance measured dead (568 tests, 0 untrappable).

## The probe

`scripts/load_burners.sh` gains an opt-in trace behind
`HSCC_LOAD_BURNERS_TRACE=<path>`. Unset ⇒ zero behaviour change: `lb_trace`
returns before any syscall. When set it appends
`<harness-pid> <burner-tag> <stage> [detail]` per stage. Every marker write
carries `2>/dev/null` *before* the append redirection (so a broken trace path
cannot print), and nothing ever goes to stdout — the harness output contract
holds.

Stage points (parent card's list 1–6, plus two disambiguators):

| stage | position | proves |
|---|---|---|
| `start` | after the INT/TERM/HUP traps are installed | harness got past setup |
| `in-wait <cmdpid>` | just before `wait "$COMMAND_PID"` (`:180`) | signal must land during `wait` if it lands after this |
| `int-trap` / `term-trap` / `hup-trap` | entry to each signal trap (`:132-134`) | the trap RAN (list item 1) |
| `cleanup-entry` | `cleanup()` after `status=$?` capture (`:92`) | list item 2 |
| `after-term-loop` | after the TERM loop (`:104-108`) | list item 3 |
| `after-sleep` | after `sleep 0.2` (`:112`) | list item 4 |
| `after-kill-loop` | after the KILL loop (`:113-117`) | list item 5 |
| `reap-wait <pid>` / `reap-ok <pid>` | per-pid inside the reap loop (`:122-124`) | WHICH pid's `wait` blocks (list item 6) |
| `cleanup-exit <status>` | just before `exit "$status"` (`:128`) | cleanup completed |

Expected readings:

- stalled harness with `start, in-wait` and **no `int-trap`** (only a
  `term-trap` when the test's own `terminate()` lands) ⇒ bash 3.2 held the
  trap; the fix is how the harness waits, and no test timeout is boundable.
- stalled harness whose last line is `reap-wait <pid>` ⇒ `wait "$pid"` blocks
  on a burner the KILL has not taken down; the fix is to bound the reap and
  prove no survivor.

## Findings

### F1 — the pair reproduced on current main, and the stalled harness stops
### at `in-wait`: the INT trap never ran (pair runs 0 and 1)

Two full `scripts/tests` suites launched together (two worktrees of
`wt/t_5f9fab03@1304de0f`, each tracing to its own file), run twice:

```
run 0: suite A 3 failed, 736 passed, 4 skipped in 208.91s
       suite B 3 failed, 736 passed, 4 skipped in 209.79s
run 1: suite A 3 failed, 736 passed, 4 skipped in 169.37s
       suite B 3 failed, 736 passed, 4 skipped in 170.57s
```

In all four suites the failures include
`test_signal_to_the_harness_takes_every_burner_with_it[2-130]` (the SIGINT
arm). The other two failures are not this card:
`test_the_per_leg_logs_do_not_pile_up_in_tmp` (t_34c5a3ed's glob fix still
unmerged on main at `cdc382b5`) and `test_repo_readmes_pass_the_lint`
(fires under a sibling suite the same class; not touched here).

The traces (`docs/audits/support/t_5f9fab03/stalled-harness-traces.txt`)
answer the card's question with the card's own decision table, outcome **(1)**:

* **Zero `int-trap` markers in either suite's whole trace** (4 suite-traces,
  0) while the same traces carry the TERM and HUP arms' `term-trap`/`hup-trap`
  — the signal-specificity the parent measured is reproduced inside the
  harness this time.
* The stalled harnesses (run 1: pids 68888/68900; run 2: 16834/16835, tags
  `hscc-load-test-*`) emit no marker after `start -> in-wait` when the INT
  lands — no `int-trap`, no `cleanup-entry`. When the test's rescue
  `terminate()` arrives ~10 s later, the **TERM trap runs and cleanup
  completes normally** (`term-trap -> cleanup-entry -> … -> cleanup-exit
  143`, in every capture) — matching the parent's `returncode: 143` (the
  positive exit, i.e. the trap's `exit 143`, not a default kill). So the
  rescue path is healthy and ONLY the INT is lost.
* (Analyzer note: pids tagged `hscc-loadstress-guard-*` also stop at
  `in-wait` BY DESIGN — that guard test SIGKILLs its harness; they are not
  stalls. The signal arms' harnesses are tagged `hscc-load-test-*`.)

### F2 — external facts at +4 s of the stall: alive, sleeping, burners spinning

`watch_stall.sh` snapshot (run 1, `docs/audits/support/t_5f9fab03/`):

```
harness 68888: STAT Ss (blocked in sleep), 4 children alive
burners 68892/68893 (and the sibling suite's pair): Rs — RUNNING at 100% CPU
watchdog + command sleep 60: Ss, alive
load averages at capture: 3.62 (14 cpus) — not contention, re-confirming F3 of the parent audit
```

This kills the "cleanup ran but stalled in the reap" reading (the card's
outcome 2): cleanup's FIRST act is TERMeshing every group, so a harness that
had entered `cleanup()` could not still own two RUNNING burners at +4 s. The
harness never entered `cleanup()`: bash 3.2 queued the INT and the dispatch
while blocked in `wait "$COMMAND_PID"` never happened — the same
"trap deferred by a foreground child" hazard this script's header already
documents (measured there: TERM held for the full 30 s of a foreground
`sleep`), in a `wait`-specific race the header's "wait is interruptible"
claim assumed away.

### F3 — what this rules out and rules in

* Ruled out: reap-loop blocking (F2), marker-write failure as the
  explanation (a harness whose trap ran would have completed cleanup and
  killed the burners — F2), and any timeout sizing: the stalled state has no
  internal end.
* Ruled in: the loss happens BEFORE bash runs any trap — see the next two
  sections for the polling experiment that pins it there (the
  command-boundary flush this bullet originally proposed was measured and
  did NOT recover the trap).

## Attempted fix — bounded-slice polling: MEASURED AND REJECTED

Hypothesis: replace `wait "$COMMAND_PID"` with
`while kill -0 "$pid"; do sleep 0.1; done` so the trap only ever waits
behind a bounded foreground command, whose completion POSIX shells flush
deferred traps at (the header's own 30 s measurement). Commit `926a712c`
shipped it; pair run 2 then showed:

```
run 2: suite A 3 failed, 736 passed, 4 skipped in 166.28s   (SIGINT arm FAILED)
       suite B 3 failed, 736 passed, 4 skipped in 166.92s   (SIGINT arm FAILED)
       int-trap markers in either suite's whole trace: 0
```

`watch_stall.sh` caught the two stalled harnesses **on the polling build**
(their child lists show the 0.1 s `sleep` slice cycling) and `/usr/bin/sample`
put bash in `__wait4` — i.e. the harness crossed ~105 command boundaries in
the stalled state and STILL never ran the INT trap. The command-boundary
flush hypothesis is therefore dead: **bash 3.2 never delivered the signal to
its own handler** (the loss is in bash's signal mask/queue internals — most
likely SIGINT left blocked/pending by a `sig_block()` window during the
startup fork storm, where this host's missing `setsid` forces a python3
`new_group` shim per child). No trap shape, wait shape, or timeout inside
the POSIX harness can recover a signal whose handler never runs. The polling
change was reverted (`git revert`-equivalent in the follow-up commit) — a
disproved behaviour change does not ship.

## Left for the next worker (measured ground, no re-derivation needed)

1. **lldb-level confirmation of the mask/queue state**: catch a stalled
   harness (the `watch_stall.sh` shape pins it within ~0.5 s of the stall;
   the stalled harness is the one with no marker after `in-wait`), attach
   with `lldb -p` and read the signal mask (`sigprocmask` state) and pending
   set — one attach answers "blocked in mask" vs "queued but stuck".
2. **If blocked-in-mask (the evidence now favours it)**: the harness cannot
   fix its own interpreter, so the fix is structural — the signal-owning
   process must not be bash 3.2 for the duration of the race. Cheapest
   shape on the table: start the harness under a tiny signal-proxy that
   `sigprocmask`s to a known state and `exec`s the script *before* any
   forks (python3 is already a hard dependency of `new_group` on this
   host) — or move the harness to the python3 `new_group` shape wholesale
   (bigger diff, breaks the POSIX-shell identity and dash on CI). Decide
   with a card; do not start from "raise the timeout", which F4 of
   t_f8e6fe38 and this card both rule out.
3. **Reproduce recipe unchanged**: `pair.sh` shape from the parent audit
   (two full `scripts/tests` suites, two worktrees, same commit) failed the
   SIGINT arm 4/4 suite-runs before the polling experiment and 2/2 after
   it — the race is reliably present under that shape on this host.

## t_44a09412 — the lldb signal-mask readout: what this host can and cannot answer

The card asked for one lldb attach to a stalled harness reading the blocked mask
and the pending set, to separate "SIGINT blocked in the process signal mask
(never delivered)" from "queued internally and stuck". **Neither of the two ways
to read that state exists on this host**, and both were measured rather than
assumed (`docs/audits/support/t_44a09412/`).

### N1 — a debugger cannot attach here at all, so the card's method is not runnable

* `lldb -p <pid>` on the harness's interpreter (`/bin/sh`, a harden platform
  binary): `attach failed (Not allowed to attach to process)`.
* `lldb -p <pid>` on a third-party, non-platform `python3` (the venv build):
  no output for 240 s — the attach never completes either. So this is not
  platform-hardening alone.
* Why: `csrutil status` → *enabled*; `DevToolsSecurity --status` → **Developer
  mode is currently disabled**; `sudo -n` → password required. Without a
  debugger entitlement or developer-mode authorisation, `task_for_pid` is
  refused for every target.
* Even a successful attach would not have yielded bash's own state: `/bin/bash`
  ships stripped (612 dynamic symbols; no `sigstate`, `blocked_set`,
  `caught_set` or `pending_traps`), so bash's internal queue is not addressable
  by name.

So the two-state question must be answered from outside the process, and the
mask/pending *values* the card asked for are unobtainable as numbers. What
follows is the strongest substitute this host supports, plus the calibration that
says whether that substitute is worth anything.

### N2 — the kernel's own mask/pending fields are dead; the disposition fields are live

`sysctl KERN_PROC_PID` exposes `p_sigmask` (blocked), `p_siglist` (pending),
`p_sigignore`, `p_sigcatch`. `sigprobe --selftest` puts a *known* state in a
child (SIGINT blocked, HUP+TERM ignored, USR1 caught) and reads it back from
outside:

```
known: blocked{INT} ignored{HUP,TERM} caught{USR1}
  blocked  bits0=0x00000000  INT=0        <- p_sigmask: does not track reality
  pending  bits0=0x00000000  INT=0
  ignored  bits0=0x1848c001  INT=0 TERM=1 HUP=1     <- live
  caught   bits0=0x20000000  USR1=1                  <- live
```

and `--selftest-pending` sends a SIGINT to a child that has SIGINT blocked —
where XNU must retain it — and `pending` still reads `0x00000000` on all three
samples. **`p_sigmask` and `p_siglist` are deprecated and unpopulated on this
XNU; `p_sigignore`/`p_sigcatch` are populated and trustworthy.** Any "blocked"
or "pending" claim read off those two fields would have been an artefact — which
is why the selftest ships with the tool.

### N3 — the disposition fields *do* discriminate the card's two states

A process cannot trap a signal it inherited as `SIG_IGN` (POSIX; bash 3.2 obeys
it). That makes `p_sigignore`/`p_sigcatch` a direct read of the two hypotheses
for a stalled harness, and it is measured with no debugger and no perturbation:

| reading on a stalled harness | meaning |
|---|---|
| `ignored INT=1`, `caught INT=0` | bash never had an INT handler → the kernel **discarded** the SIGINT → *never delivered* (state A) |
| `caught INT=1`, `ignored INT=0` | the handler was installed → the kernel would deliver on an unblocked mask → the loss is **inside bash** (state B) |

### N4 — the two calibration traps this instrument fell into first (recorded so they are not re-trod)

1. **A non-interactive launcher shell makes the harness permanently deaf to
   INT.** My first synthetic states were launched from this worker's shell and
   all read the same; the readout said why: `ignored INT=1 TERM=0`, `caught
   INT=0` — the harness had inherited `SIG_IGN` for INT/QUIT/TSTP and bash 3.2
   could not install its `trap … INT`. This is the parent card's killed
   hypothesis, *reborn on the launcher side*: it says the fix candidate in the
   sibling card (a python3 signal-proxy that `sigprocmask`s and then `exec`s the
   script) **must reset those dispositions before exec**, or the proxy itself
   will hand the harness an INT it can never trap.
2. **SIGTSTP is not a canary against bash.** `caught` shows bit 20 set: bash 3.2
   installs a SIGTSTP handler even in a non-interactive script, so "TSTP did not
   stop it" says nothing about the mask. QUIT (bit 3) and TTIN/TTOU are in
   neither `caught` nor `ignored`, so their default action (terminate / stop) is
   the observable canary.
3. Related: `sigprobe --prep u2,u3,u20,b2,b3,b20` builds a child whose mask
   *provably* blocks INT+QUIT+TSTP before `execve` (exec preserves the mask) —
   and the harness it execs then behaved exactly like a clean-mask harness. So
   bash 3.2 clears the inherited blocked mask on startup, and the parent's
   "left-over `sig_block()` window" hypothesis cannot be reproduced by
   *inheriting* a mask: if that state is real in the stalled runs, bash itself
   must be creating it inside the running script.

### N6 — the live verdict: the stalled harness had NO INT handler (state A, never delivered)

`pair_chase.sh` (the parent's pair shape, `MINAGE=5`) caught a stalled harness in
**each** suite on the first run; both were probed inside the ~10 s window
(`stalled-harness-signal-state-A.log`, `stalled-harness-signal-state-B.log`):

```
pid=2056 ppid=89307 pgid=2056 stat=2 comm=bash
  blocked  bits0=0x00000000     <- unreadable field (N2), says nothing
  pending  bits0=0x00000000     <- unreadable field (N2), says nothing
  ignored  bits0=0x18408006  INT=1  TERM=0 HUP=0
  caught   bits0=0x63887ef9  INT=0  TERM=1 HUP=1 USR1=1 CHLD=1
... same on all three samples, and on the sibling suite's harness (pid 2249)
TSTP NOT DELIVERED ; SECOND SIGINT NOT DELIVERED ; QUIT DELIVERED (died)
```

So, for the card's binary: **neither of its two arms, and closer to the first.**
The SIGINT was not blocked in the mask and was not queued inside bash — it was
**discarded by the kernel as ignored**, because bash 3.2 never had an INT handler
to run: `caught INT=0, ignored INT=1`. The same harness has live TERM and HUP
handlers (`caught TERM=1 HUP=1`), which is exactly why the parent saw the rescue
TERM trap work and the INT trap never fire; and `QUIT DELIVERED` proves the
process was alive and responsive to a signal it had neither handler nor ignore
for, so this is signal-specific, not a deaf process. The whole trace carries
**zero `int-trap` markers across 6 signal-arm harnesses in each suite**, with
`1 failed, 738 passed, 4 skipped` per suite (the only failure being this arm).

The control that makes this a cause and not a coincidence is N4.1: the *same*
harness script, launched from the *same* non-interactive chain but with the
dispositions reset to `SIG_DFL` first (`sigprobe --prep u2,u3,u20`), reads
`caught INT=1` and traps INT normally (exit 130). So no wait shape, trap shape or
timeout in the harness can recover a signal the kernel was never allowed to
deliver — the parent's conclusion stands, but its mechanism was wrong: the state
to chase is an **inherited `SIG_IGN`**, which `t_f8e6fe38` F6 declared dead after
measuring the *suite* process rather than the harness it produced.

### Left for the next worker (t_44a09412)

1. **Name the link that hands down the ignore.** `chain_walk.sh <repo> <sigprobe>
   <out>` reads SIGINT's disposition at every link of the real launch chain
   (worker shell → `sh` → python → `/bin/sh` harness) from outside; the first link
   reading `ignored INT=1` owns it. CPython only installs its SIGINT handler when
   the inherited disposition is `SIG_DFL`, and `execve` preserves an ignore, so an
   ignore set anywhere upstream is sticky all the way into bash — which then is
   not permitted to `trap` it. This run did not capture that link (a key-name bug
   in the first version of `chain_walk.sh` read pid 0; fixed after the capture).
2. **Reconcile with the "passes alone" fact** (`t_f8e6fe38` F4: 0/60 stalls
   alone, and 4/4 + 2/2 paired): if the ignore were inherited unconditionally from
   the worker shell, the arm would fail every time, paired or not. So either the
   ignore is set *conditionally* (a test earlier in `scripts/tests` that leaves
   SIGINT ignored in the suite process — which is what the sibling suite changes),
   or the paired/alone difference lies elsewhere. Item 1 answers which.
3. **Then decide the fix on `t_8bb39d12`** — and note for whoever designs the
   python3 signal-proxy candidate there: it must reset INT/QUIT/TSTP to
   `SIG_DFL` before `exec`, or it will hand the harness the very ignore this
   section measured. Do not raise the test timeout (`t_f8e6fe38` F4, parent F4).


