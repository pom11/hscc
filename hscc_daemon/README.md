# HSCC Monitoring Daemon

Continuous monitoring daemon for the DGX Spark cluster, installed as the
Hermes plugin `hscc_daemon`. It runs a set of periodic **health streams** and
heals a down or wedged vLLM unit under durable guards: a down keep-alive
**worker** unit is relaunched/recreated per-unit, and a **wedged** unit —
orchestrator included — is restarted only by the guarded engine-wedge
recovery (`recover.py`). It never resurrects an intentional autodown and the
pipeline watchdog never launches `hscc cluster up`-owned units (a fleet-level
restart stays operator scope: `/cluster-restart` re-applies the active
template). On startup it self-cleans dead `~/.hscc` cruft (`.corrupt-*`
snapshots / `.stale` flags + caps each `<file>.bak.*` group at the newest 5).

## Architecture (the real modules)

Modularised out of the old monolithic `hscc.py`. The CLI entry point is
`hscc_daemon.hscc.main()`; every `hscc` command is its own process that
dispatches into one of the modules below. Human-facing output renders through
`cli_theme.py` (Rich, Hermes-default palette); machine `--json` output stays
byte-identical.

Core modules (not exhaustive — `ls hscc_daemon/*.py` is the census):

| Module | Responsibility |
|--------|----------------|
| `hscc.py` | CLI entry, dispatch, help, cluster/template/profiles/project/api/autodown/kanban groups |
| `cli.py` | Daemon lifecycle commands: start/stop/status/check/watch/triggers/notify/log + heal-status/heal-clear |
| `cli_theme.py` | Rich theme foundation (single palette source for every `hscc` render) |
| `health.py` | Check functions: dgx, gateway, local, heartbeat, nas, idle, workers (keep-alive supervision + debounced per-unit auto-heal), proxy, engine_wedge, dispatcher + the lease/breaker/guard wiring |
| `heal_lease.py` | **Durable single-flight heal lease** (HEAL_CASCADE F2): file under `~/.hscc/heal-lease.json`, bounded TTL (`HSCC_HEAL_LEASE_TTL_S`, default 600s) — one repair at a time across all four repair paths; expired leases self-heal |
| `heal_breaker.py` | **Per-node heal circuit breaker** (F5): `~/.hscc/heal-breaker.json`; after `HSCC_HEAL_BREAKER_MAX` (default 6) actions on one node in `HSCC_HEAL_BREAKER_WINDOW_S` (default 900s) the node is heal-suspended + alerted once; operator clears with `hscc heal-clear <node>` |
| `launch_guard.py` | **One-vLLM-per-host invariant** (A2): before any launch, a vLLM workload on the host outside the unit's declared serving span ⇒ refuse + alert, never preempt. Undeclared co-location is the fault; declared co-location (e.g. orchestrator :8000 + embedding :8010) is legal |
| `recover.py` | Guarded engine-wedge **recovery** (acting half of the E stream): N consecutive wedges (`HSCC_RECOVER_CONSECUTIVE_WEDGES`, default 3), per-unit cooldown (default 30 min), attempt cap then `gave_up` — stops ONLY the wedged unit's container, then one fleet `hscc cluster up` |
| `serving.py` | Cluster topology (`cluster.json` / `serving.json`) resolution |
| `lifecycle.py` | Pipeline watchdog, vLLM auto-restart (lease-first, registered-unit refusal, verified-success only), trigger backoff |
| `trigger.py` | Trigger engine (rule evaluation, cooldowns, event firing) |
| `daemon_ops.py` | Daemon life-cycle: PID/log/rotation, supervised check threads, main loop, startup cleanup |
| `state.py` | Thread-safe state dir reads/writes |
| `install.py` | Service management (launchd on macOS, systemd on Linux) |
| `desktop.py` | macOS desktop notifications + event emitter |
| `dispatcher_wedge.py` | Dispatcher-wedge watchdog + guarded self-heal |
| `kanban_blocked.py` / `kanban_cli.py` | `hscc kanban blocked` (why a card is blocked + recovery) / `hscc kanban stale` (board hygiene the autodown C3 interlock reads) |
| `escalate.py` / `escalate_watcher.py` | Failure-escalation decision logic (pure) + the live watcher (reassign to strong-tier, desktop notify) |
| `autodown.py` / `autodown_cli.py` | Idle autodown/autoup core (`~/.hscc/autodown.json`, the `classify()` decision table, the O_EXCL lock) + `hscc autodown` verbs |
| `kill_switch.py` | Backend for stopping a runaway kanban worker (wrapped by the API + iOS app) |
| `qr_code.py` | Pure-stdlib QR encoder for the API pairing QR (no `qrcode`/Pillow by policy) |
| `cluster_live.py` | `hscc cluster monitor --live` renderer — **operator-owned: quote, never edit** |
| `verify.py` / `cluster_render.py` | `hscc verify` smoke-test / cluster+template human renderers |

The heal trio + `recover.py` exist because of one incident: see
[`docs/audits/HEAL_CASCADE_2026-10-14.md`](../docs/audits/HEAL_CASCADE_2026-10-14.md).

## Installation

Ships in the `hscc_daemon/` package (installed to
`~/.hermes/plugins/hscc_daemon/` by bootstrap / `install_payload.py`). Needs
**Rich** in its Python environment (top-level imports in `cli_theme.py`; PyYAML
is lazy-imported on YAML-reading check paths) — install it into the same
environment as the `hscc` CLI. Entry point: `hscc_daemon.hscc.main`
(also `python -m hscc_daemon`).

## State files (`~/.hscc`)

| File / dir | Purpose |
|------------|---------|
| `~/.hscc/daemon.pid` | PID file (present while running) |
| `~/.hscc/heartbeat` | Durable liveness heartbeat — never deleted on stop, so a dead daemon's stale heartbeat is impossible to miss |
| `~/.hscc/daemon.log` | Rolled/rotating daemon log |
| `~/.hscc/state/<stream>.json` | One JSON file per stream, overwritten each check (dgx, gateway, local, heartbeat, nas, watchdog, triggers, engine_wedge, dispatcher, idle, proxy, workers) |
| `~/.hscc/heal-lease.json` | Single-flight repair lease (F2) |
| `~/.hscc/heal-breaker.json` | Per-node strike ledger + latch (F5) |
| `~/.hscc/recover.json` | Engine-wedge recovery cooldowns / attempt counts / `gave_up` |
| `~/.hscc/watchdog-block.json` | Pipeline watchdog block (failures, backoff, `intentional == "autodown"`) |
| `~/.hscc/autodown.json` + `autodown.lock` | Autodown config/state + its O_EXCL exclusivity lock |

Health checks are configured by the daemon code + environment variables
(e.g. `HSCC_WORKER_AUTOHEAL_DEBOUNCE`,
`HSCC_WORKER_AUTOHEAL_COOLDOWN_MINUTES`, `HSCC_WORKER_AUTOHEAL_LOAD_GRACE`),
not by a per-handler JSON config — there is no `~/.hscc/daemon/config.json`.

## Commands (`hscc`, the merged CLI)

| Command | Description |
|---------|-------------|
| `hscc start` | Start the daemon in the background (forks; PID in `~/.hscc/daemon.pid`) |
| `hscc stop` | Graceful shutdown (SIGTERM; waits up to ~10s, then SIGKILL) |
| `hscc status` | Daemon status + last result of every health stream (RUNNING only when pid **and** fresh heartbeat confirm) |
| `hscc check [stream\|all]` | Run one check cycle now (does NOT persist state) |
| `hscc check --repo <path>` | Address-guard posture for that checkout (armed / unarmed / armed-but-absent) |
| `hscc watch [stream]` | Live-tail check results |
| `hscc triggers` | Show trigger-engine rules + recent firings |
| `hscc notify <msg>` | Send a manual macOS notification |
| `hscc heal-status` | List per-node heal-breaker strikes (read-only) |
| `hscc heal-clear <node>` | Clear one node's strikes after investigating (MUTATES the ledger) |
| `hscc install` / `hscc uninstall` | Install / remove the launchd (macOS) or systemd (Linux) service |
| `hscc plist` | Print the launchd plist (no install) |

Also read-only verbs: `verify`, `stats [days]`, `throughput`, `autoscale`,
`escalate`, and the groups `cluster`, `template`, `profiles`, `project`, `api`,
`autodown`, `kanban` (`blocked` / `stale`).

`start`/`stop`/`status` are safe to run from a terminal. `start` is idempotent.
Service-supervised runs use `start-daemon` (foreground loop, so
launchd/systemd supervises the right process).

**While the service is loaded, do not restart with `stop` + `start`.**
`start` double-forks out of the supervisor (PPID 1) and runs under whatever
interpreter the `hscc` console script uses, which on a Homebrew-installed CLI is
*brew* python — a different process than the service, with no macOS Local Network
(TCC) grant, so it reads every cluster node as unreachable while launchd still
reports the job as registered with no pid. Restart supervised instead:
`launchctl kickstart -k gui/$(id -u)/com.hermes.hscc_daemon` on macOS, or
`systemctl --user restart hscc_daemon.service` on Linux — `com.hermes.hscc_daemon.service`
on a box set up by `hscc install`, which writes the unit under that name (or
`bash ~/.hermes/plugins/hscc-bootstrap/restart_daemon.sh`, which also verifies
the PID turned over and refuses to start a twin beside an orphan). This is the
2026-10-29 operator rule; the 2.14.0 deploy is the incident that earned it.

## Health status model

Each stream's `~/.hscc/state/<stream>.json` carries an `ok` / `blocked` field,
derived by `status` into: OK (healthy) · FAIL (explicit failure) · BLOCKED
(watchdog/trigger blocked, needs attention) · — (no state on disk yet).

## Safety Guarantees

Repairs are **sequential, finite, and never blind** (the HEAL_CASCADE 2026-10-14
lesson: uncoordinated unbounded healing destroyed the fleet):

1. **Heal lease (F2)** — every repair path (`auto-heal`, worker relaunch, proxy
   relaunch, engine-wedge recovery, watchdog restart) takes the durable
   single-flight lease first; a held lease means *defer this tick*, and the TTL
   guarantees a leaked lease self-heals.
2. **Heal breaker (F5)** — per-node strikes are durable; after the limit the
   node is heal-suspended and the operator is alerted once. `hscc heal-status`
   to inspect, `hscc heal-clear <node>` to resume.
3. **Launch guard (A2)** — never start a vLLM outside its declared serving
   footprint; refuse + alert, never preempt another tenant's workload.
4. **Debounced auto-heal** — a unit must stay down across
   `HSCC_WORKER_AUTOHEAL_DEBOUNCE` (default 3) consecutive checks before its
   unit is recreated — per-unit `sparkrun stop <recipe> --hosts <span>` + a
   re-issued run for THAT unit only (since F3, never a whole-template
   `--force-recreate`).
5. **Cooldown + durable load grace** — `HSCC_WORKER_AUTOHEAL_COOLDOWN_MINUTES`
   (default 10) between force-recreates of a unit; a container younger than
   `HSCC_WORKER_AUTOHEAL_LOAD_GRACE` (default 1200s) that has never served is
   loading, not dead — and that check survives daemon restarts.
6. **Recovery is capped** — engine-wedge recovery acts only after N
   consecutive declared wedges, per-unit cooldown, and gives up acting (keeps
   alerting) after `HSCC_RECOVER_MAX_ATTEMPTS` (default 3).
7. **Never fight the operator** — while an intentional autodown is in effect
   the watchdog, worker relaunch and wedge recovery all stand down.
8. **Handler exceptions caught** — a check crash never kills the daemon; a dead
   check thread is respawned.
9. **Ad-hoc `hscc check` never writes shared state** — a manual failure must
   not masquerade as a fleet failure in `hscc status`.

## How to test / Troubleshooting

Tests: `tests/` — from this dir: `python -m pytest tests/ -q` (or the full
fleet suite via `scripts/run_tests.sh`). The heal-trio suites are
`test_heal_lease.py` / `test_heal_breaker.py` / `test_launch_guard.py` /
`test_recover.py`.

| Problem | Solution |
|---------|----------|
| `hscc status` shows STOPPED | If the service is loaded, `launchctl kickstart -k gui/$(id -u)/com.hermes.hscc_daemon` on macOS / `systemctl --user restart hscc_daemon.service` on Linux (or `hscc install` to (re)install it). Only on a machine with no service: `hscc start` |
| A node is not being healed | `hscc heal-status` — if suspended, investigate then `hscc heal-clear <node>` |
| `hscc` says unknown command | `hscc help` / `hscc help advanced` list the real verbs |
| `hscc` not on PATH | It is installed in the Hermes venv (`~/.hermes/hermes-agent/venv/bin/hscc`) — see [hscc-cli](../hscc-cli/README.md) |
| Daemon log missing | `hscc status` uses `~/.hscc/state/*.json`; `hscc log` tails `~/.hscc/daemon.log` |
| `deps not met` error | The daemon needs **Rich** in its Python env (Hermes venv) |
