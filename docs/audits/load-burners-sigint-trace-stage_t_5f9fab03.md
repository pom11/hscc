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

(findings append here as each measurement lands)

## Reproduce / observe

(pair-run recipe and raw traces recorded here)
