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


