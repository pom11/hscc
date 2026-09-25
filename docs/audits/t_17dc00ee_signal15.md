# INVESTIGATION: who sends SIGTERM (signal 15) to the hscc daemon?

Card: t_17dc00ee
Date: 2026-09-25
Status: COMPLETE
Type: findings / audit report (docs-only; no production-code change)

## Scope

Establish or rule out engine-wedge -> SIGTERM causation for the signal-15
events observed on the hscc daemon, and classify each event as benign
(operator/agent stop, deploy restart, launchd restart) vs unexpected. The
deliverable is this findings report; any definite bug is tracked on a separate
child card, NOT fixed here.

## 1. Methodology

Every number below was reproduced with the exact command noted. The daemon log
is `~/.hscc/daemon.log`; its timestamps are UTC (local is UTC+3). All signal
lines were extracted with:

    grep -n "Received signal 15" ~/.hscc/daemon.log

`grep -c` earlier reported 11; it is now 12 (one more arrived at
2026-09-25T12:36:51Z during this investigation day). The `scratch_sig.py`
helper in docs/audits reproduces the full per-signal context window (±25 lines)
used for classification; raw context is saved at
`docs/audits/t_17dc00ee_context.txt`.

Which code produced these lines: `grep -rn "Received signal" hscc_daemon/*.py`
shows three distinct wording sites:
- `daemon_ops.py:376` — logs `Received signal {signum}, stopping...`  (MAIN daemon)
- `cli.py:104`  — logs `Received signal {signum}, shutting down...` (legend)
- `api_cli.py:255` — logs `Received signal {signum}, shutting down...` + writes to api.log

ALL 12 log lines in daemon.log say "stopping..." → all 12 were handled by the
**main daemon's** handler at `daemon_ops.py:375-380`. `hscc api stop`
(`api_cli.py:401`) writes its stop log to api.log and uses the "shutting
down..." handler, so it is NOT a source of these 12 lines.

## 2. The signal-15 list — reproduced

Run `python3 docs/audits/analyze.py` to reproduce this table (it parses the live
log). Raw list:

| # | signal-15 time (UTC) | "Daemon stop requested" | restart gap | next restart line |
|---|----------------------|------------------------|-------------|-------------------|
| 1 | 2026-09-21T08:12:26Z | YES | 68s  | start-daemon (service-supervised) 08:13:34Z |
| 2 | 2026-09-21T21:14:19Z | no  | 0s   | start-daemon (service-supervised) 21:14:19Z (~same second) |
| 3 | 2026-09-22T08:42:10Z | no  | 94085s (~26h) | start-daemon (service-supervised) 09-23 10:50:15Z |
| 4 | 2026-09-23T16:32:33Z | no  | 16538s (~4.6h) | start-daemon (service-supervised) 09-23 21:08:11Z |
| 5 | 2026-09-24T10:42:19Z | no  | 71462s (~19.8h) | Daemon starting 09-25 06:33:20Z |
| 6 | 2026-09-25T06:42:59Z | no  | 76s  | Daemon starting 06:44:15Z |
| 7 | 2026-09-25T06:55:41Z | no  | 6768s (~1.9h) | Daemon starting 08:48:30Z |
| 8 | 2026-09-25T08:49:10Z | YES | 1190s (~20m) | Daemon starting 09:09:00Z |
| 9 | 2026-09-25T09:09:09Z | no  | 102s | Daemon starting 09:10:52Z |
|10 | 2026-09-25T09:22:42Z | no  | 432s | Daemon starting 09:29:54Z |
|11 | 2026-09-25T10:35:11Z | YES | 3s   | Daemon starting 10:35:14Z  (KNOWN 2.2.0 release restart, card context) |
|12 | 2026-09-25T12:36:51Z | no  | ~0.8s (checks resumed, no Daemon-starting line) | — |

Two log vocabularies reflect two deployed code generations:
- `start-daemon invoked (service-supervised mode)` (`cli.py:443`) = launchd
  supervised start (used by signals 1-4's restarts).
- `Daemon starting` (`cli.py:55`) = `hscc start`/`cmd_start_daemon` (foreground,
  manual/agent start; used by signals 5-12's restarts).

## 3. Code-level: can engine-wedge SIGTERM the daemon? NO.

I audited every in-daemon path that could deliver a signal to the daemon
process itself. `grep -rn "os.kill|SIGTERM" hscc_daemon/*.py` (excluding
tests/api_cli/cli/install) shows the only in-daemon os.kill uses are:
- `autodown.py:1681` — `os.kill(pid, 0)` liveness probe (signal 0, harmless).
- `daemon_ops.py:49` — `os.kill(pid, 0)` liveness probe.
- `daemon_ops.py:379` — the daemon's own SIGTERM handler registration.
- `kill_switch.py` — SIGTERMs *reclaimed worker processes* (other PIDs), never
  the daemon's own pid.

The engine-wedge subsystem (`check_engine_wedge` + `recover.recover_engine_wedge`)
never touches the daemon pid. `hscc_daemon/recover.py` documentation and code
show recovery restarts "JUST that unit's container", guarded by resolve guards.
In the live log, every recovery action line for the wedged unit is:

    Engine-wedge recovery: unit '...' streak=N — resolved container_id=None
    Engine-wedge recovery: unit '...' — no container_id resolved; no stop issued (fail-safe)

(e.g. daemon.log lines 181576/181577, 181612/181613, 181643/181644,
181674/181675) — the recovery thread could not even resolve the unit container
and explicitly issued NO stop. And on 09-25 (signals 6/7) there are NO recovery
action lines at all around the same incident.

=> Mechanism verdict: **RULED OUT.** No hscc code path sends SIGTERM to the
daemon process in reaction to an engine-wedge verdict. The verdict only (a)
logs, (b) notifies, (c) hands the wedged unit to a recovery thread that acts
only on the unit container.

Therefore all 12 SIGTERMs came from EXTERNAL senders. The in-repo external
senders are:
- `cli.py:135` `cmd_stop` — `hscc stop`, logs `Daemon stop requested` first.
- `install.py:136` `_stop_running_daemon` — install/uninstall deploy stop; does
  NOT log "Daemon stop requested" (silent SIGTERM + 2s sleep), then removes the
  pid file. Used by `install`/`uninstall` flows.
- launchd `KeepAlive = { SuccessfulExit = false }` (plist
  `com.hermes.hscc_daemon.plist.template`) — restarts the daemon whenever the
  process exits with a NONZERO status (i.e. not a clean exit-0), which is what
  an unhandled/default SIGTERM produces.
- Any direct `kill <pid>` by an operator/agent (no in-log fingerprint beyond the
  daemon's own "Received signal 15" line).

## 4. Engine-wedge -> SIGTERM hypothesis: tested per signal

For each signal-15 I checked whether a WEDGED/verdict event preceded it in the
log (within ~60s) and whether the wedge recovery attempted any action.

| # | signal-15 time (UTC) | wedge verdict immediately before? | recovery action? | verdict link |
|---|----------------------|-----------------------------------|------------------|--------------|
| 1 | 09-21 08:12:26Z | no (engine 2/2 ok @08:11:45) | none | no |
| 2 | 09-21 21:14:19Z | no (engine 2/2 ok) | none | no |
| 3 | 09-22 08:42:10Z | no — check said 2/2 ok @08:42:09.5; stale wedge notifications only | none | no (coincidence) |
| 4 | 09-23 16:32:33Z | no (engine 2/2 ok @16:31:56) | none | no |
| 5 | 09-24 10:42:19Z | wedge notifications present; recovery streak=3..6 all fail-safe (no action) @10:38-10:41 | NO ACTION (fail-safe) | no (coincidence) |
| 6 | 09-25 06:42:59Z | YES — WEDGED @06:42:53 (6s before) | none this window | coincident, not causal |
| 7 | 09-25 06:55:41Z | YES — WEDGED @06:55:26 (15s before) | none this window | coincident, not causal |
| 8 | 09-25 08:49:10Z | no (explicit `hscc stop`) | — | no |
| 9 | 09-25 09:09:09Z | no (engine loading, not wedged) | none | no |
|10 | 09-25 09:22:42Z | no (engine 2/2 ok) | none | no |
|11 | 09-25 10:35:11Z | no — known 2.2.0 release restart | — | no |
|12 | 09-25 12:36:51Z | no (engine 2/2 ok @12:36:33) | none | no |

Signals 6 and 7 sit a few seconds after a WEDGED verdict, but the code path that
would turn a wedge verdict into a daemon SIGTERM does not exist. Both events are
symptoms of the same stuck-engine incident that morning: the engine was genuinely
wedged, the daemon logged and notified, and independently an actor stopped the
daemon. Temporal adjacency is coincidence, not cause.

## 5. Per-signal classification

- **#1 (09-21 08:12:26Z)** — BENIGN. `Daemon stop requested` then SIGTERM
  (explicit `hscc stop`), restart 68s later. Operator/agent stop+start. Engine OK.
- **#2 (09-21 21:14:19Z)** — BENIGN. No `Daemon stop requested` but restart in
  the SAME second via launchd (`start-daemon invoked (service-supervised)`).
  Fingerprint of the `install.py:136` deploy stop followed by launchd/installer
  restart, or a `launchctl kickstart -k`. Engine OK.
- **#3 (09-22 08:42:10Z)** — UNEXPECTED. No stop-request, no restart for ~26h
  (daemon found down). Engine check said "2/2 ok" 0.6s before; the wedge
  notifications are stale. A direct kill with no supervision to resurrect it.
- **#4 (09-23 16:32:33Z)** — UNEXPECTED. No stop-request, daemon down ~4.6h.
  Engine OK before.
- **#5 (09-24 10:42:19Z)** — UNEXPECTED. No stop-request, daemon down ~19.8h
  (found down). Wedge notifications + fail-safe recovery present but recovery
  explicitly issued NO stop. Kill not attributable to any auto-heal.
- **#6 (09-25 06:42:59Z)** — UNEXPECTED (temporal coincidence with wedge, no
  causation). Wedge WEDGED @06:42:53; no recovery action; restart 76s later
  (agent/operator start). 
- **#7 (09-25 06:55:41Z)** — UNEXPECTED (same incident as #6). Wedge WEDGED
  @06:55:26; no recovery action; daemon down ~1.9h.
- **#8 (09-25 08:49:10Z)** — BENIGN. `Daemon stop requested` 40s after the
  08:48:30 start (agent/operator `hscc stop` during the morning restart flurry).
- **#9 (09-25 09:09:09Z)** — UNEXPECTED. No stop-request; SIGTERM 10s after the
  09:09:00 `hscc start`. Engine loading (not wedged). Killed soon after start.
- **#10 (09-25 09:22:42Z)** — UNEXPECTED. No stop-request; engine "2/2 ok".
  Agent-driven stop/start churn; restart 432s later.
- **#11 (09-25 10:35:11Z)** — BENIGN / EXPECTED. `Daemon stop requested` +
  restart 3s. This is the KNOWN HSCC 2.2.0 release daemon restart
  (card context; PID 15527-era restart, started 13:35 local = 10:35 UTC).
- **#12 (09-25 12:36:51Z)** — UNEXPECTED-ish (deploy/agent churn). No
  stop-request, engine "2/2 ok". Checks resumed ~0.8s later without a fresh
  `Daemon starting` line — see section 6 re the pid-file divergence.

Summary: **explicit `hscc stop`** — #1, #8, #11 (3 benign). **deploy/launchd
restart** — #2 (1 benign). **UNEXPECTED (no stop-request, no mechanism)** —
#3, #4, #5, #6, #7, #9, #10, #12 (8). Of the 12, only #11 is the card's known
2.2.0 release restart; the other 11 are the genuinely "nothing surfaced it"
cases the card asked to classify.

## 6. Root-cause observation: stale pid file / lost-daemon identity

The current live daemon is PID 89645 (`ps -o pid,lstart` -> started
2026-09-25T12:10:52 local = 09:10:52Z, command `.../venv/bin/hscc start`;
`lsof ~/.hscc/daemon.log` -> FD 3w owned by PID 89645). It has been the single
writer of daemon.log since 09:10:52Z. Yet the daemon log shows signals #10
(09:22:42Z), #11 (10:35:11Z), #12 (12:36:51Z) "Daemon loop stopped" — if those
SIGTERMs had reached PID 89645, it would have exited (a clean `Daemon loop
stopped` -> `run_daemon_loop` returns -> process exit-0). It did not exit, so
**those SIGTERMs were delivered to a DIFFERENT pid than the live writer** —
the pid stored in `~/.hscc/daemon.pid` at the time, which was stale/wrong.

Confirmed at write time: `~/.hscc/daemon.pid` does NOT exist while the daemon is
ALIVE (PID 89645) — `ls ~/.hscc/daemon.pid` -> No such file or directory. This is
exactly the hazard noted in the card: "hscc status reported RUNNING alive (PID
...) while daemon.pid had been DELETED". `hscc status` and `hscc stop` read pid
from the pid file (`get_pid()`), so a missing or wrong pid file makes them
misreport / target the wrong process.

Why the pid file diverges: `cmd_start` (`cli.py:58-90`) double-forks and calls
`save_pid()` in BOTH the child (`cli.py:61`) and the grandchild (`cli.py:90`).
`save_pid()` overwrites the single file. During the 09-25 09:09-09:30 flurry
there were FOUR `Daemon starting` events (09:09:00, 09:10:52, 09:12:44,
09:29:54) — each `hscc start` overwrote the pid file with a fresh (child)
pid, while the long-lived grandchild/daemon pid (89645) was what the log-writer
really was. Any `hscc stop`/`kill` reading the file then SIGTERMs a pid that may
differ from the actual serving daemon. `write_stopped()` then removes the file,
and the real daemon continues orphaned with no pid file — the current state.

This is a real defect (pid-file is not a reliable handle on the running daemon;
status trust it as source of truth). It is trackable and fixable, but it is NOT
engine-wedge related and it is NOT the SIGTERM sender itself — recommendation
below.

## 7. Causation verdict

**ENGINE-WEDGE -> SIGTERM: RULED OUT.**
No hscc code path sends SIGTERM to the daemon on an engine-wedge verdict; the
recovery subsystem acts only on the wedged unit container and explicitly fails
safe ("no stop issued") when it cannot resolve one; on the 09-25 incident it
took no action at all. Signals #6/#7 that sit seconds after a WEDGED verdict are
coincidental — both are outputs of the same stuck-engine morning, not cause and
effect.

The SIGTERMs themselves are all from EXTERNAL senders: three are confirmed
`hscc stop` commands (#1, #8, #11), one is a launchd/deploy restart (#2), and
the remaining eight have no in-log stop-command fingerprint and are consistent
with direct `kill`s / agent-driven stop-start churn / pid-file-mismatched stop
attempts. None are engine-wedge.

## 8. Follow-up card (recommended, opened)

A separable, evidence-backed defect emerged that warrants its own child card
(per the task: do not fix here):

- **Card t_d733f7c8** (OPENED 2026-09-25, assignee worker, parent t_17dc00ee):
  "Daemon pid-file is not a reliable handle — live daemon coexists with missing
  / wrong `~/.hscc/daemon.pid`; `hscc status`/`hscc stop` misreport or target
  the wrong pid." Root cause: `cmd_start` double-fork writes the pid file twice
  (`cli.py:61` child and `cli.py:90` grandchild) — races + fraternal pids; a
  killed `hscc stop` / direct kill can then hit a stale pid while the true
  daemon runs on unfiled. Suggested fix: a durable heartbeat file/state (already
  requested by t_1c4e8160) as source of truth for liveness/status and stop,
  plus making `hscc start`/`stop` and launchd agree on one ownership model
  (avoid coexisting launchd-supervised + double-forked `hscc start` daemons).

## 9. Repro commands (all verified this run)

    grep -c "Received signal 15" ~/.hscc/daemon.log          # -> 12
    grep -n "Received signal 15" ~/.hscc/daemon.log          # the 12 timestamps
    python3 docs/audits/analyze.py                           # per-signal table above
    grep -rn "Received signal\|os.kill\|SIGTERM" hscc_daemon/recover.py   # wedge recovery acts only on units
    ls -la ~/.hscc/daemon.pid                                # -> No such file (daemon alive)
    lsof ~/.hscc/daemon.log                                  # -> FD 3w owned by PID 89645
    ps -o pid,lstart,command -p 89645                        # started 09:10:52Z, hscc start
    sed -n '43,55p' hscc_daemon/com.hermes.hscc_daemon.plist.template  # KeepAlive SuccessfulExit=false

## 10. Scrubbing note
LAN/tailnet addresses in the daemon log (the wedged-unit host:8000 endpoints and
the relaunch-*.log filenames) are scrubbed to the documented placeholders
(100.64.0.1 for a tailnet host, 10.0.0.x for a LAN node) in this report per the
public-repo rule. Redacted any api_key/secret values. No AI attribution.

## 11. Delivery
- Branch: wt/t_17dc00ee -> merged to main.
- Merge SHA: (filled at merge time).
- Squash/merge single docs commit; `install_payload.py` run for consistency
  (no-op for docs-only) per operator rules.
