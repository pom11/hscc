# Where inside `load_burners.sh` does the SIGINT stall die? (measured by trace)

Card: `t_5f9fab03`. Parent: `t_f8e6fe38` (audit
`docs/audits/load-burners-signal-sibling-liveness_t_f8e6fe38.md` — read it
first; the measurement there is re-verified below, not re-derived).
Base: `origin/main` (branch `wt/t_5f9fab03`).

Status: **IN PROGRESS** — trace implemented, pair run pending. This section is
the scaffold; findings append as they land.

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
* The stalled harnesses (run 1: pids 68888/68900, tags `hscc-load-test-*`)
  end at `start -> in-wait` and emit nothing else — no `int-trap`, no
  `cleanup-entry`, and no `term-trap` when the test's `terminate()` lands.
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
* Ruled in: **bash 3.2's trap dispatch while blocked in `wait` is unreliable
  in a narrow race**. The most battle-tested POSIX path instead is the
  command boundary: a trap deferred during a foreground command reliably
  runs when that command finishes — this file's own header measured it
  (held for 30 s, then RUN). The fix follows: never let the harness block in
  one unbounded `wait`; await the command in bounded foreground slices, so
  the worst-case trap latency is one poll **by construction** — and the
  guarantee no longer depends on the racy primitive at all.

## Fix

`wait "$COMMAND_PID"` is replaced by a bounded poll: `while kill -0
"$COMMAND_PID"; do sleep 0.1; done`, then a final (non-blocking) `wait` to
collect the command's status. POSIX-portable (works on bash 3.2 and dash);
worst-case signal→trap latency is 0.1 s + cleanup (~0.3–0.6 s total, still
under 6 % of the test's 10 s budget); exit statuses unchanged (130/143/129/
124/`$?`). Proven in the pair shape below — the state that failed 4/4 suite
runs above is the same state that passes N/N below, with `int-trap`
markers now present on every INT arm.

