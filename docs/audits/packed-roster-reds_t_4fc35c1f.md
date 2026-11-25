# The three packed-roster reds, measured (t_4fc35c1f)

Follow-up to `t_6c2123f9` (PR #776). The pack is in `scripts/run_tests.sh` behind
`HSCC_TEST_PARALLEL`, default 1, because the twelve-leg roster at `p=2` came back
with three red legs: `hscc-api`, `scripts`, `.github/scripts`. The parent card named
two candidate mechanisms — the runner's deferred reaping, and `set -m` — and asked
for a run rather than a reading of each.

**Neither candidate is the cause.** Measured, each red is its own defect, and one of
the two mechanisms the parent card suspected could not have produced the symptom it
was blamed for.

| leg | verdict | cause |
|---|---|---|
| `hscc-api` | real flake, **pre-dates the pack** | a race inside the `sidecar` fixture's own identity self-check |
| `.github/scripts` | **deterministic**, red on every packed run | the pack spliced a leg *path* into a log *filename*; pytest never ran |
| `scripts` | not reproduced | green packed at `p=2` on this host (699 passed, 4 skipped, 246 s) |

## Posture of every run in this file

Host: Mac Studio, 14 CPUs, macOS 27.2, `/bin/bash` 3.2.57. Worktree
`wt/t_4fc35c1f` off `origin/wt/t_6c2123f9` @ `0e7fe466`. Load averaged 2.5-3.4
throughout because a merge-queue gate (`hscc_daemon` leg, pid 31612) was running on
the box for part of the session — stated rather than hidden, since a "load amplifies
this flake" claim is exactly the claim a stray neighbour could fake. Burner runs use
`scripts/load_burners.sh` (bounded, group-owned, self-expiring) at 12 burners.

## 1. `hscc-api` — `('Z', '<defunct>')` is the fixture racing its own child

The lead:

```
ERROR at setup of test_cooperative_shutdown_still_takes_the_sidecar_down[stop]
AssertionError: harness identity check broken on darwin: our own live parent pid
3578 did not match its own argv marker; _state_and_command answered ('Z', '<defunct>')
hscc-api/tests/test_gateway_serve_watchdog.py:637
```

`proc.pid` there is **not** a child of `run_tests.sh`. It is the sandbox API parent
(`fake_api.py`) that the `sidecar` fixture spawned itself with `subprocess.Popen`, so
it is a direct child of the pytest process and its only reaper is that process's own
`proc.wait()` — which the fixture calls in teardown, after the assertion. Nothing the
runner's poll tick does to *its* children can put a `Z` on that pid. That rules out
candidate 1 by construction, before any measurement.

Candidate 2 (`set -m`) is ruled out by the measurement below, which reproduces the
failure with no runner in the picture at all.

The mechanism is in the fixture's own text. `mode="stop"` tells the sandbox parent
(FACE_API_PARENT) to call `gateway_bridge.shutdown()` the moment it is ready, write
`SHUTDOWN_DONE`, and `raise SystemExit(0)` — it exits **by design**. Meanwhile the
fixture waits for the `READY` mark and then asserts that same pid is still alive:

```python
assert handle.wait_for(READY, _SIDECAR_READY_TIMEOUT_S), ...     # READY is written
                                                               # BEFORE the shutdown
...
assert _alive(proc.pid, handle.parent_marker), (...)             # the race
```

`_alive` is *correct* to answer False for a zombie (`state.startswith("Z")` → not
alive; an unreaped exit is not a running process). The assertion is the thing that is
wrong: it demands liveness from a process the mode instructs to exit. Between `READY`
and that assert the fixture does a few file reads and one `ps`; under CPU pressure the
pytest process is descheduled long enough for the parent to finish `shutdown()` and
exit first, and the self-check then reads `('Z', '<defunct>')`.

### Measurement

One node id, one pytest process per run, `HSCC_TEST_PARALLEL` **unset** (serial — no
pack, no `set -m`, no runner involvement in the child's lifetime at all):

| posture | runs | red |
|---|---|---|
| ordinary priority, one sibling gate on the box (load ~2.7) | 25 | **0** |
| 12 bounded burners | 30 | **8** — every one `('Z', '<defunct>')` |
| control: `test_sidecar_dying_on_its_own_leaves_no_watchdog_behind` (mode `serve`, the parent sits until killed), 12 burners | 25 | **0** |

So the variable is CPU pressure, and within the file the variable is the **arm**:
`stop` races, `serve` does not. The pack was never the cause; it is one more source of
the pressure that makes a fixture race lose, which is why a `p=2` roster run hit it
once out of twelve legs and a serial run had not hit it recently enough to be noticed.

### Fix, and what it deliberately keeps

The platform proof moves to a module-scoped `identity_self_check` fixture pointed at a
process **the harness owns and keeps alive** — a marker-bearing canary it spawns,
reaps through its own `Popen`, and never asks `ps` about. The check keeps its teeth:
`_state_and_command` is still required to match a marker at the end of a long argv,
which is exactly the clipped-`ps`-column failure the check was written for on the
first Linux runner. What is removed is only the demand that a pid which the scenario
instructs to exit be still alive.

The scenario half stays asserted where it is a real precondition: `serve`,
`no_watchdog` and `stop_on_file` all require the parent to still be serving at yield
time (`stop_on_file` gates its stop on a sentinel the harness writes later), so those
arms keep the original `_alive(proc.pid, parent_marker)` line untouched. In `stop`,
the harness asks its own `Popen` — the authority on its own child, which no recycled
pid can fool — that the parent is either still running or exited `0`.

After the fix, the posture that measured 8/30 red measures **0/30**.

## 2. `.github/scripts` — never ran, on every packed run

This one is not a flake and produced no `FAILED` line to capture because there was
none. The parent card asked for the missing lines; here they are, from a packed run of
just these two legs:

```
parallel: 2 legs at a time (HSCC_TEST_PARALLEL=1 for the serial path); leg logs in /tmp/hscc-legs-BoGTPp
scripts/run_tests.sh: line 939: /tmp/hscc-legs-BoGTPp/1-.github/scripts.log: No such file or directory
parallel: done leg=.github/scripts elapsed=0s rc=1 pack=0s
```

The log path was built by splicing the leg name into a filename:

```bash
logf="$LEG_TMP/$P_NEXT-$d.log"      # d = ".github/scripts"
```

`.github/scripts` is the only roster leg whose *name* is a nested path, so the result
is `…/1-.github/scripts.log` — a file inside a directory that does not exist. The
redirect fails in the child before `env` is execed, so pytest is never started, the
wrapper records no `.rc`, and the runner falls back to `wait`'s status:
`✗ .github/scripts (pytest exit 1)`, 0 s, and an empty block in the concatenated
stream. Red on every packed run, at any parallelism, on every host. The fallback
"reads an incomplete leg as red" behaviour was correct; the leg simply never existed.

Eleven of twelve legs are one flat path component, which is why the 24 tests
`t_6c2123f9` shipped never saw it — none of them used the slashy leg name. Fixed by
flattening `/` in the log filename, and pinned by
`test_a_leg_whose_name_is_a_path_still_runs`, which asserts the block is **non-empty**
(a header with nothing under it *is* this defect) and which was verified to bite: with
the fix reverted it reproduces `✗ .github/scripts (pytest exit 1)` exactly.

## 3. `scripts` — green packed, once

699 passed, 4 skipped in 246.22 s, packed alongside `.github/scripts` at `p=2`. The
parent card measured `1 failed of 702`; that failure line was never captured and this
run did not reproduce it. Not yet explained — see LEFT.

## 4. Clean full-roster run at p=2

(pending at the time of this commit — see the card's completion handoff)
