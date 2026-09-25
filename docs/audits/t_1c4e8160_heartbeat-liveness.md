# t_1c4e8160 — Durable daemon liveness heartbeat + daemon_liveness()

## Summary

The daemon's only durable liveness artifact before this change was
`~/.hscc/daemon.pid`, which is REMOVED on clean stop (`write_stopped`) — so an
absent pid file was indistinguishable between "cleanly stopped" and
"unexpectedly crashed". That gap is what let `hscc status` report "RUNNING
alive (PID …)" yesterday while the pid file was already gone (the status path
trusted stale in-memory / stale-file state).

This change adds a **durable heartbeat file** `~/.hscc/heartbeat` written by
the daemon's own supervision loop, plus a `daemon_liveness()` helper that
combines pid-file pid + aliveness + heartbeat freshness so the status path can
distinguish every dead-daemon case. The heartbeat is NEVER deleted on stop: a
clean stop simply stops advancing it, so a dead daemon leaves a heartbeat that
goes stale and cannot be missed.

Scope: `hscc_daemon/daemon_ops.py` (implementation), `hscc_daemon/tests/
test_daemon_ops.py` (tests), `hscc_daemon/tests/conftest.py` (one-line
isolation addition). No other file's behavior touched; `cli.py` left alone for
the dependent card (t_8334f251) which consumes `daemon_liveness()`.

## Design

### Constants (daemon_ops.py)

- `HEARTBEAT_FILE = ~/.hscc/heartbeat` — alongside `PID_FILE` (daemon_ops.py:17).
- `HEARTBEAT_INTERVAL = 300` — touch cadence, aligned with
  `PERIODIC_INTERVALS["heartbeat"]` (the daemon's own heartbeat-stream cadence)
  so the durable signal tracks real supervision cadence — one source of truth
  for "how often the daemon supervises".
- `HEARTBEAT_STALE_AFTER = max(2 * HEARTBEAT_INTERVAL, 120)` → **600s**. The
  daemon may miss up to two consecutive touches (slow/iowait ticks) before the
  heartbeat is judged stale — never cries wolf on a healthy-but-slow daemon.
  Staleness is DERIVED from the constant (test asserts it), not hardcoded.

### `touch_heartbeat(heartbeat_file=None)`

Writes `datetime.now(timezone.utc).isoformat()` (UTC, e.g.
`2026-09-25T15:00:00.123456+00:00`) to the heartbeat file, creating the parent
dir. Never raises (a failed heartbeat write must never take the daemon down —
same guarantee as `log()`); returns the ISO string or `None` on failure.

### `run_heartbeat_loop(stop_event, interval=None)` — main-loop body

Calls `touch_heartbeat()` once per `interval` (default `HEARTBEAT_INTERVAL`)
until `stop_event` is set, sleeping 1s between checks. It is the **body of
`run_daemon_loop`'s main `while` loop** — the loop that runs the periodic
supervisor — so it adds **no separate thread** to leak. Both daemon entry
points (`cmd_start` and `cmd_start_daemon` in cli.py) reach `run_daemon_loop`,
so the heartbeat is written exactly once per cadence by whichever main loop
runs the periodic supervisor, not duplicated per entry point. On clean stop it
returns and the file simply stops advancing (never deleted).

Extracted as a named function so the throttled-touch behavior is directly
testable without booting the monolithic `run_daemon_loop`.

### `daemon_liveness(pid_file=None, heartbeat_file=None)`

Reads BOTH the pid file and the heartbeat file (neither trusted alone) and
returns a dict:

| field | type | meaning |
|---|---|---|
| `pid` | int\|None | pid read from pid file (None if absent/invalid) |
| `pid_file_present` | bool | pid file existed on disk |
| `alive` | bool | pid is a live process (`os.kill(pid, 0)`) |
| `last_heartbeat` | str\|None | last heartbeat ISO timestamp |
| `heartbeat_present` | bool | heartbeat file existed + parsed |
| `stale` | bool | heartbeat older than `HEARTBEAT_STALE_AFTER` |
| `state` | str | one of the four labels below |

Four distinguishable states (what the status path consumes):

- `running-fresh` — pid alive + heartbeat present & fresh
- `running-stale-heartbeat` — pid alive + heartbeat present but stale
- `running-no-heartbeat` — pid alive but no heartbeat yet written
- `pid-gone` — pid file absent, or pid not alive

## Tests

New hermetic tests in `hscc_daemon/tests/test_daemon_ops.py` (all use
`tmp_path` / monkeypatched `PID_FILE`+`HEARTBEAT_FILE`, never touch live
`~/.hscc`):

- `TestTouchHeartbeat` — writes parseable UTC-aware ISO, advances on successive
  calls, creates parent dir, never raises on a bad path.
- `TestRunHeartbeatLoop` — advances while running and **stops advancing when
  the loop exits**; staleness floor derived from the constant.
- `TestDaemonLiveness` — correctness across every state: pid file missing,
  pid present + process gone (`pid-gone`), pid alive + no heartbeat
  (`running-no-heartbeat`), fresh (`running-fresh`), stale past threshold
  (`running-stale-heartbeat`), just-within-threshold not stale (no cry-wolf),
  corrupt heartbeat treated absent. Stale boundary computed from
  `HEARTBEAT_STALE_AFTER`, not hardcoded.

`conftest.py` `_isolate_hscc` now also redirects `daemon_ops.HEARTBEAT_FILE` to
a per-test tmp path (one line), keeping the whole-directory hermeticity
guarantee intact for the new file.

## Verification

Branch: `wt/t_1c4e8160` → merged to `main` as `7d69a1d`
(merge commit, pushed: `492f16b..7d69a1d main -> main`).
Deployed with `python3 hscc-bootstrap/install_payload.py` (merge SHA installed
into `~/.hermes/plugins`). Note: main has since advanced (a concurrent
sibling task `t_57e5b3f7` merged and its own `install_payload` run briefly
reverted the runtime copy of `daemon_ops.py`; re-deployed from current main,
which still contains this change via merge history — verified the installed
plugin carries the heartbeat code again).

Full suite green under BOTH interpreters via `scripts/run_tests.sh` (one
pytest process per plugin dir, true isolation):

`~/.hermes/hermes-agent/venv/bin/python` (Python 3.11):
- hscc-bootstrap 272, hscc-commands 69, hscc-roles 126, hscc-cluster 422,
  hscc-project 1351, hscc_daemon 1172, sparkrun-hermes 12, hscc-api 786
  (1 skipped). **ALL GREEN** (exit 0).

`/Users/desac/miniconda3/envs/p313/bin/python` (Python 3.13):
- hscc-bootstrap 272, hscc-commands 69, hscc-roles 126, hscc-cluster 404
  (14 skipped), hscc-project 1351, hscc_daemon 1169 (3 skipped),
  sparkrun-hermes 12, hscc-api 786 (1 skipped). **ALL GREEN** (exit 0).

Direct daemon-test runs: `test_daemon_ops.py` 47 passed (hermes venv), daemon
dir 1169 passed (p313).

### Execution demo (live daemon, deployed code)

Using the real `~/.hscc/heartbeat` and `daemon_liveness()` from the installed
plugin (script at /tmp/hb_liveness_demo.py):

1. **Baseline (daemon stopped, no heartbeat):** `daemon_liveness()` →
   `{"pid": null, "alive": false, "heartbeat_present": false, "state":
   "pid-gone"}`.

2. **Start daemon** (`hscc start`, detached): pid file written, heartbeat
   written on the first supervision-cycle touch
   (`2026-09-25T15:34:53.034326+00:00`); `daemon_liveness()` →
   `{"pid": 93073, "alive": true, "stale": false, "state": "running-fresh"}`.

3. **Heartbeat ADVANCES while running** — sampled the real file through one
   full cadence:
   `15:34:53.034326+00:00` → `15:39:53.701737+00:00` (exactly one
   `HEARTBEAT_INTERVAL` = 300s later). `daemon_liveness()` continued to report
   `running-fresh`.

4. **Stale when the heartbeat stops** (function-level, real live pid
   87080 + a heartbeat older than the threshold):
   `{"pid": 87080, "alive": true, "last_heartbeat": "…-05s",
   "stale": true, "state": "running-stale-heartbeat"}` — the exact
   `running-stale-heartbeat` case the status path sees for a wedged-but-alive
   daemon. Also observed on the real file: after the previous daemon stopped
   advancing, the heartbeat aged past `HEARTBEAT_STALE_AFTER` and reported
   `stale: true`.

The daemon restarted with this change (expected and permitted; gateway NOT
touched). It is currently running (pid 93073) with a live, advancing
heartbeat and reports `running-fresh`.
