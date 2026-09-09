# REPORT t_a6697001 — hscc_daemon loaded but dead: root cause, decision, fix

Date: 2026-09-09 (EEST, UTC+03:00)
Assignee: devops-engineer

## 1. Why it is "loaded but dead" (evidence)

`launchctl list | grep hscc_daemon` shows:
```
-   0   com.hermes.hscc_daemon
```
`-` = no PID; `0` = last exit status 0 (clean exit). The job is loaded but the
launchd-supervised process is not running.

The plist `~/Library/LaunchAgents/com.hermes.hscc_daemon.plist` runs
`/Users/desac/.hermes/hermes-agent/venv/bin/python .../hscc_daemon/hscc.py
start-daemon`. That path's own log is `~/.hscc/daemon.log` (NOT the plist's
StandardOutPath `~/Library/Logs/hscc_daemon.log`, which launchd never creates
because the daemon writes to its own log file while a PID file exists — see
`daemon_ops.log()`).

From `~/.hscc/daemon.log` (grep of lifecycle markers):
- `3822032: [2026-09-08T18:35:54.707658+00:00] start-daemon invoked (service-supervised mode)`
- `3822033: [2026-09-08T18:35:54.709011+00:00] Daemon loop started`
- (starts all 15 check threads: dgx/gateway/local/heartbeat/nas/idle/workers/
  engine_wedge/dispatcher/autodown/engine-wedge-recover/dispatcher-wedge-recover/
  watchdog/trigger — `3822033`..`3822047`)
- `3822090: [2026-09-08T18:36:06.827028+00:00] Received signal 15, stopping...`
- `3822091: [2026-09-08T18:36:06.828007+00:00] Daemon loop stopped`

`grep -n "start-daemon crashed\|Daemon crashed" ~/.hscc/daemon.log` → empty.
So the launchd job did NOT crash. It was cleanly SIGTERM'd (signal 15) 12 seconds
after starting, at 18:36:06 on 2026-09-08.

The plist sets:
```
<key>KeepAlive</key>
<dict><key>SuccessfulExit</key><false/></dict>
```
Meaning: restart on crash (nonzero exit), do NOT restart on a clean exit.
Because the SIGTERM produced exit status 0 (a "successful exit"), launchd will
never revive the job. Loaded-but-dead, permanently, until someone kicks it.

The SIGTERM at 18:36:06 landed during the cluster outage the operator described
on the card (auto-heal force-recreating serving units mid-model-load, no
startup grace). The log right before it shows `Auto-heal ... force-recreate via
template apply` (`3821965`-ish region) and repeated vLLM restarts — i.e. the
daemon was torn down in the middle of outage churn and nobody restarted it.

## 2. The duplicate untracked daemon

The daemon loop is CURRENTLY running, but NOT under launchd. Evidence:
- `~/.hscc/daemon.pid` → `11690`
- `ps -p 11690` →
  `/Users/desac/miniconda3/envs/p313/bin/python3.13 .../hscc start`
- `~/.hscc/daemon.log` is actively appended (last entry ~2026-09-09T03:48 UTC,
  all check streams firing: Gateway/DGX/Workers/Dispatcher-wedge etc.)
- `3823283: Daemon loop started` at 18:57:27 with NO preceding
  "start-daemon invoked" line — that is the `hscc start` (manual, untracked)
  path, not launchd.

So there is a split-supervision half-state, exactly as the operator flagged:
one daemon running untracked (`hscc start`, PID 11690) while the launchd job
`com.hermes.hscc_daemon` looks supervised but is dead. Both write the same
`~/.hscc/daemon.pid` / `~/.hscc/daemon.log`.

## 3. Decision: it should run, under launchd (option a)

The daemon is the periodic verify/drift watchdog — the very thing this card
exists to protect (an 18-day-old plugin was served because "the drift check
existed but nothing ran it"). It is NOT obsolete. The launchd plist is valid and
demonstrably starts all threads correctly (proven at 18:35:54). The root cause
was operational, not a bug: the job was cleanly stopped during the (now
resolved) outage and `KeepAlive.SuccessfulExit=false` correctly refuses to
revive a cleanly-stopped job — so nothing came back.

Fix: retire the untracked duplicate, then bring up ONE launchd-supervised
daemon and verify it persists.

## 4. What was done

Sequence (all against the live runtime — this IS the task's job, and it is not
`hscc doctor`/bootstrap, so ~/.hermes/config.yaml was never touched):

1. Retired the untracked duplicate:
   `/Users/desac/miniconda3/envs/p313/bin/hscc stop` →
   `Stopping hscc_daemon (PID 11690)... hscc_daemon stopped (PID 11690)`.
   Verified: `ps -p 11690` → gone; `~/.hscc/daemon.pid` removed;
   `daemon.log:3867744 [03:57:31] Received signal 15, stopping... Daemon loop stopped`.

2. Started the single launchd-supervised daemon:
   `launchctl start com.hermes.hscc_daemon` (exit 0).
   Verified: `daemon.log:3867783 [03:58:11] start-daemon invoked (service-supervised
   mode)` → `Daemon loop started`; all 15 threads started
   (`3867784`..`All threads started, daemon loop running (polling mode)`).

3. Check streams confirmed running (from this incarnation's thread-start lines):
   dgx(60s), gateway(60s), local(60s), heartbeat(300s), nas(900s), idle(300s),
   workers(60s), proxy(60s), engine_wedge(60s), dispatcher(60s), autodown(30s),
   engine-wedge recovery(60s), dispatcher-wedge recovery(60s), watchdog(30s),
   trigger engine(15s).

4. Persistence check: (fill in result after the wait).

## 5. What I did NOT do (and why)

- Did NOT run `hscc doctor` or bootstrap.sh against the live runtime (they
  MUTATE ~/.hermes/config.yaml) — per task rules.
- Did NOT use Telegram in any test; no mutating POSTs at live state.
- Did NOT raise any cap.
- No host/IP hardcoded in anything committed (test_no_real_addresses_committed.py).
