# t_8334f251 — `hscc status` gates RUNNING on durable heartbeat liveness

## Problem

`hscc status` trusted the pid file via `get_pid()`: if the file existed and the
pid answered `os.kill(pid, 0)`, it reported "RUNNING alive (PID …)". But the pid
file is not a reliable liveness signal. Two independent failure modes were
measured:

1. **Stale/wrong pid from the double-fork in `cmd_start()`.** `cmd_start`
   called `save_pid()` in BOTH the parent (after the first fork) AND the
   grandchild (after the second). Under `hscc start` churn the file became a
   race between the short-lived first child and the long-lived serving
   grandchild. Reproduced live: pid file read 8733 (the already-exited first
   child) while the real serving daemon was 8736. `hscc status`/`hscc stop`
   read the dead 8733.
2. **No durable aliveness signal.** A pid that answered `os.kill(p,0)` could be
   a different (reused) process, not the daemon, and the process could have
   died after the pid file was read.

Operator directive: `hscc status` must NEVER report RUNNING when the process is
gone, and an unexpected exit must surface where the operator actually looks.
Prefer durable signals (heartbeat) over in-memory state.

## Fix

Two changes, both in `hscc_daemon/cli.py` (plus its test file), leveraging the
`daemon_liveness()` helper merged by the parent card (t_1c4e8160) — no
heartbeat reimplemented here.

### 1. `cmd_status` gates RUNNING on the durable signal

`cmd_status` now calls `daemon_liveness()` and reports RUNNING ONLY for
`running-fresh` (pid file present + pid alive + heartbeat not stale). Every
other state reports STOPPED, and the dead/stale case prints a distinct wrapped
detail line (a plain `console.print`, which wraps rather than being clipped like
a single-line panel body):

| `daemon_liveness()` state | status | detail line |
|---|---|---|
| running-fresh | RUNNING alive (PID …) | — |
| running-stale-heartbeat | STOPPED stale heartbeat | `possible unexpected exit / stale heartbeat — Heartbeat last advanced <ts> … treat as dead daemon.` |
| running-no-heartbeat | STOPPED | `pid file exists but no heartbeat written — cannot confirm alive` |
| pid-gone, pid file present | STOPPED stale PID file | `PID file names <pid> but that process is not alive — possible unexpected exit` |
| pid-gone, pid file absent | STOPPED (clean) | — |

### 2. `cmd_start` writes daemon.pid ONCE, in the grandchild only

Removed `save_pid()` from the parent branch of the double-fork. The pid file is
now written exactly once by the grandchild after the final fork — the ONE
process that actually serves — matching `cmd_start_daemon` (the launchd path).
`start`, `stop`, `status` and launchd now agree on one ownership model.

## Verification (execution, not narrative)

Full suite green under BOTH interpreters (scripts/run_tests.sh, per-dir):

| dir | py3.11 | py3.13 |
|---|---|---|
| hscc-bootstrap | 272 passed | 272 passed |
| hscc-commands | 69 passed | 69 passed |
| hscc-roles | 126 passed | 126 passed |
| hscc-cluster | 422 passed | 404 passed, 14 skipped |
| hscc-project | 1351 passed | 1351 passed |
| hscc_daemon | 1179 passed | 1176 passed, 3 skipped |
| sparkrun-hermes | 12 passed | 12 passed |
| hscc-api | 786 passed, 1 skipped | 786 passed, 1 skipped |
| memori_byodb | 5 passed | 5 passed |
| **ALL GREEN** | yes | yes |

New tests: `TestCmdStatusLiveness` (5) — fresh→RUNNING (incl. regression that
"RUNNING alive" survives), stale→NOT running + note, pid gone→NOT running,
pid-file missing→clean stop, running-no-heartbeat→NOT running. Plus
`TestCmdStartPidOwnership` (2) — parent branch does NOT save_pid, grandchild
saves exactly once + runs the loop.

## Execution demo

Controlled demo driving the REAL `cmd_status` + `daemon_liveness` chain against
real files in a tmp dir (`/tmp/demo_status_liveness.py`), using the harness's
own live pid as "alive" — no real daemon needed, never touches live ~/.hscc:

```
CASE: healthy: pid alive + fresh heartbeat
  verdict: RUNNING alive
  OK  HSCC Daemon Status — RUNNING alive (PID 15515)

CASE: stale heartbeat: pid alive + OLD heartbeat
  verdict: NOT RUNNING
  WARN  HSCC Daemon Status — STOPPED stale heartbeat
  ! possible unexpected exit / stale heartbeat — Heartbeat last advanced
2026-09-25T16:54:50.603870+00:00 (older than HEARTBEAT_STALE_AFTER); PID 15515
still present but not supervised, treat as dead daemon.

CASE: pid gone: pid file names a dead pid
  verdict: NOT RUNNING
  WARN  HSCC Daemon Status — STOPPED stale PID file
  ! PID file names 99999 but that process is not alive — possible unexpected
exit; pid file cleaned up.

CASE: clean stop: pid file missing
  verdict: NOT RUNNING
  WARN  HSCC Daemon Status — STOPPED
```

Live daemon validation (deployed code, real `hscc` CLI):

```
$ hscc start                    # daemon forks to background
$ cat ~/.hscc/daemon.pid → 15843   # now the ACTUAL serving process (not a dead child)
$ hscc status
  OK  HSCC Daemon Status — RUNNING alive (PID 15843)
$ hscc stop
  OK  hscc_daemon stopped (PID 15843)
$ cat ~/.hscc/daemon.pid → (removed)   # clean stop
$ hscc status → STOPPED
```

Before the fix the `hscc start` → status cycle reproduced the exact bug live
(pid file 8733, real daemon 8736, status "RUNNING alive" per old code); after
the fix the pid file names the real serving process and status/corresponding
stop agree. The dead/stale-with-note and pid-gone cases are shown by the
controlled demo (all four states above).

## Merge / deploy

- Branch: `wt/t_8334f251` (based on main @ 5426d33)
- Commits: `956d850` (status gating), `9050e83` (pid ownership) — fast-forward
  merged to main, pushed to origin/main → `9050e83`
- Deployed: `python3 hscc-bootstrap/install_payload.py` (installed, missing [])
- Gateway untouched. Daemon stopped/restarted (permitted) during validation.
- No real addresses, secrets, or AI attribution. No working notes at repo root.
