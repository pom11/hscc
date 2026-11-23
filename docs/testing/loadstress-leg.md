# The `loadstress` leg — the teardown class under bounded CPU pressure

Card: `t_b0bcc49c`. Introduced with PR #705 / #717's hardened teardown files.

## Why this leg exists

The stop/sidecar teardown class is only dead when something runs those files
**loaded**. Every case in

* `hscc-api/tests/test_api_server_stop_signal.py`
* `hscc_daemon/tests/test_api_cli_stop_sidecar.py`

passes on an idle box and fails on a loaded one — that is the entire class. The
fleet's history is "green locally, red in the gate": #628 hardened one of the two
files and PR #673's queue gate still bounced on the other at 10:40Z on
2026-11-23. Nothing in the roster enforced the property, so a stress leg is now
a leg.

## The one command a reviewer runs

```bash
HSCC_TEST_LEGS="loadstress" bash scripts/run_tests.sh
```

That is the whole seam, and it is the same shape as every other leg: the
`HSCC_TEST_LEGS` selector in `scripts/run_tests.sh`, the roster-wide HOME seal,
the kanban-env scrub and the SIGTERM forensics all apply to it. Extra argv is
forwarded to pytest, so a selection check without running anything is:

```bash
HSCC_TEST_LEGS="loadstress" bash scripts/run_tests.sh --collect-only
# loadstress: 20 items selected by -m loadstress across 2 files (collect-only; nothing was run)
```

`loadstress` is **selectable and never default**: it is deliberately absent from
the runner's `DIRS` array (that array *is* the full-suite roster, so a queue gate
or a `run_tests.sh` with no selector can never pay for the repeats).

## Measured, not claimed

Run on this host (14 cores, 1-min load ~3 at rest) with the interpreter the
runner selects (`/Users/desac/.hermes/hermes-agent/venv/bin/python`). Two runs,
both quoted rather than averaged, because a single run of a stress leg proves
nothing:

```
loadstress: 20 items x 3 repeats at load 8.40 -> 60 passed, 0 failures   # 3ab28e4d
loadstress: 20 items x 3 repeats at load 7.83 -> 60 passed, 0 failures   # fe272c1c
```

| fact | value |
|---|---|
| items selected (`-m loadstress`) | **20** (11 of 20 in the api file, 9 of 16 in the daemon file) |
| repeats per file | **3** → 6 file-passes, one pytest process per file per pass |
| passes / failures | **60 passed, 0 failed** in both runs |
| burners | **7** (`min(8, ceil(cores/2))`), owned by `scripts/load_burners.sh` |
| load average created | peak 1-min **8.40** / **7.83**, sampled every 2 s inside the window (52 / 51 samples over 107 / 105 s) |
| survivors afterwards | **0** in both runs |
| ordinary legs, same posture | `hscc_daemon`: **5064 passed, 1 skipped, 9 deselected** · `hscc-api`: **1392 passed, 1 skipped, 11 deselected, 1 xfailed** |

The peak is sampled *during* the run because a 1-minute average read afterwards
has already forgotten the run that produced it. The two legs' `N deselected`
counts are the proof that the ordinary gates do not pay for the marked cases:
11 and 9, exactly the marked sets.

## How the pressure is made, and how it dies

Burners are not this leg's problem and it does not own one. They come from
`scripts/load_burners.sh` (t_71ed3b1c), which owns each burner's **process
group**, traps `EXIT/INT/TERM/HUP` and holds a **hard timeout** — the shape that
exists because a hand-written spinner loop orphaned twelve burners at ppid 1 for
3 h 40 m at load 24. On top of that contract this leg adds three things:

* it **sweeps its own burner tag on the way in**, so a run that was SIGKILLed
  (untrappable on macOS — no `PR_SET_PDEATHSIG`) cannot stack burners onto the
  next one;
* it **fails red** if a single tagged burner is alive after the harness returns;
* it forwards its own `TERM/INT/HUP` into the harness, so an interactive Ctrl-C
  takes the burners at signal speed instead of at timeout speed.

**CI posture:** tiny on purpose. Defaults are `--burners min(8, ceil(cores/2))`
(→ 2 on a 4-vCPU runner), `--repeats 3`, `--timeout 1200`. Raise them for a real
measurement, never for a gate:

```bash
HSCC_TEST_LEGS=loadstress bash scripts/run_tests.sh -- --repeats 5 --burners 12 --timeout 900
```

## Why the marked cases run in exactly ONE place

`scripts/run_tests.sh` passes `-m "not loadstress"` to **every** roster leg, so
the marked cases do not run in the ordinary gates at all. That is a deliberate
trade, not an oversight: an idle-box pass over a case that only fails loaded
proves nothing new, while the repeats that make the stress leg worth running are
wall time no normal gate should pay for. What keeps the trade honest is
`scripts/tests/test_loadstress_leg.py`, which runs in the ordinary `scripts`
leg and measures per file that `marked + unmarked == collected`, so a case can
never fall between the two legs and run nowhere.

The static source pins in the same two files (`TestStopPathHoldsNothing`,
`TestStopPathIsCooperative`, the budget arithmetic, the marker-name pins) are
**not** marked: they read source text and measure the same thing on any machine,
so they stay in every ordinary leg.

## What guards the guard

`scripts/tests/test_loadstress_leg.py` (16 arms, runs in the `scripts` leg — so
rot reddens a normal gate, not a stress run nobody invokes): the marker is
registered in `pytest.ini`; both files carry it; the selection is non-empty,
scoped to those two files and pinned to the measured per-file counts (11 / 9);
the runner's deselect is the exact complement of the leg's select; `loadstress`
is selectable but not in `DIRS`; an empty selection exits **2**; the leg's own
survivor detector bites on a live burner; a signal to the leg leaves no burners;
and no non-test script in `scripts/` inlines a burner loop.

Four of those were proven by mutation probes — disabling the empty-selection
guard, silently un-marking a case, turning the deselect into a no-op, and
removing the leg's signal forwarding. The last one came back green (see the
arm's docstring: the harness dies on `SIGPIPE` when the leg's pipe closes, which
masks the forwarding), and is recorded as a known weakness of that arm rather
than as a proof.
