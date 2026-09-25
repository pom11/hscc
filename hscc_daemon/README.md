# HSCC Monitoring Daemon

Continuous monitoring daemon for the DGX Spark cluster, installed as the
Hermes plugin `hscc_daemon`. It runs a set of periodic **health streams** and
auto-heals a crashed **worker** vLLM unit. An **orchestrator** wedge is NOT
auto-restarted (too disruptive): the daemon logs the alert while the active
fallback keeps the gateway answering, and a human runs `/cluster-restart`
(which re-applies the active template). On startup it also self-cleans dead
`~/.hscc` cruft (`.corrupt-*` snapshots / `.stale` flags + caps each
`<file>.bak.*` group at the newest 5).

## Architecture (the real modules)

The daemon was modularised out of the old monolithic `hscc.py`. The CLI entry
point is `hscc_daemon.hscc.main()`; every `hscc` command is its own process
that dispatches into one of the modules below. The human-facing `hscc` CLI
output is rendered through `cli_theme.py` (Rich, Hermes-default palette);
machine `--json` output stays byte-identical.

| Module | Responsibility |
|--------|----------------|
| `hscc.py` | CLI entry, dispatch, help, cluster/template/profiles/project/api/autodown/kanban groups |
| `cli.py` | Daemon lifecycle commands: start/stop/status/check/watch/triggers/notify/log |
| `cli_theme.py` | Rich theme foundation (single palette source for every `hscc` render) |
| `health.py` | Check functions: dgx, gateway, local, heartbeat, nas, idle, workers, engine_wedge + worker auto-heal |
| `serving.py` | Cluster topology (`cluster.json` / `serving.json`) resolution |
| `lifecycle.py` | Agent reconciliation, vLLM auto-restart, pipeline watchdog |
| `trigger.py` | Trigger engine (rule evaluation, cooldowns, event firing) |
| `daemon_ops.py` | Daemon life-cycle: PID/log/rotation, stream watcher, main loop, startup cleanup |
| `state.py` | Thread-safe state dir reads/writes |
| `install.py` | Service management (launchd on macOS, systemd on Linux) |
| `desktop.py` | macOS desktop notifications + event emitter |
| `dispatcher_wedge.py` | Dispatcher-wedge watchdog |
| `verify.py` / `cluster_render.py` | `hscc verify` smoke-test / cluster+template human renderers |

## Installation

The daemon ships in the `hscc_daemon/` package (installed to
`~/.hermes/plugins/hscc_daemon/` by bootstrap / `install_payload.py`). It
depends on **Rich** (for the themed CLI) plus the Python standard library —
install it into the same Python environment as the `hscc` CLI. The daemon
entry point is `hscc_daemon.hscc.main` (also usable as `python -m hscc_daemon`).

## State files (`~/.hscc`)

The daemon keeps its runtime state under `~/.hscc`:

| File / dir | Purpose |
|------------|---------|
| `~/.hscc/daemon.pid` | PID file (present while running) |
| `~/.hscc/daemon.log` | Rolled/rotating daemon log |
| `~/.hscc/state/<stream>.json` | One JSON file per health stream, overwritten each check (dgx, gateway, local, heartbeat, nas, watchdog, triggers, engine_wedge, dispatcher, idle, proxy, workers) |

There is no `~/.hscc/daemon/config.json` in the modular daemon — health checks
are configured by the daemon code + environment variables (e.g.
`HSCC_WORKER_AUTOHEAL_DEBOUNCE`, `HSCC_WORKER_AUTOHEAL_COOLDOWN_MINUTES`,
`HSCC_WORKER_AUTOHEAL_LOAD_GRACE`), not by a per-handler JSON config.

## Commands (`hscc`, the merged CLI)

| Command | Description |
|---------|-------------|
| `hscc start` | Start the daemon in the background (forks; PID in `~/.hscc/daemon.pid`) |
| `hscc stop` | Graceful shutdown (SIGTERM; waits up to ~10s, then SIGKILL) |
| `hscc status` | Daemon status + last result of every health stream |
| `hscc check [stream\|all]` | Run one check cycle now (does NOT persist state) |
| `hscc watch [stream]` | Live-tail check results |
| `hscc triggers` | Show trigger-engine rules + recent firings |
| `hscc notify <msg>` | Send a manual macOS notification |
| `hscc install` / `hscc uninstall` | Install / remove the launchd (macOS) or systemd (Linux) service |
| `hscc plist` | Print the launchd plist (no install) |

Also read-only health/monitoring verbs: `verify`, `stats [days]`,
`throughput`, `autoscale`, `escalate`, and groups `cluster`, `template`,
`profiles`, `project`, `api`, `autodown`, `kanban`.

`start`/`stop`/`status` are safe to run from a terminal. `start` is idempotent
(no-op if already running). Service-supervised runs use `start-daemon` (runs
the loop in the foreground so launchd/systemd supervises the right process);
`start` would fork a child and exit, which launchd would mistrack.

## Health status model

Each stream's `~/.hscc/state/<stream>.json` carries an `ok` / `blocked` field,
derived by `status` into:

| Display | Meaning |
|---------|---------|
| OK | Stream healthy |
| FAIL | Explicit failure |
| BLOCKED | Watchdog/trigger blocked (needs attention) |
| — (never) | Stream has no state on disk yet |

Worker vLLM units are supervised per (node, port). A crashed worker is
relaunched; a unit that stays DOWN across `HSCC_WORKER_AUTOHEAL_DEBOUNCE`
consecutive checks (default 3) — beyond its load-grace window and out of
cooldown — is **force-recreated** by re-applying the currently applied template
(`template apply <applied> --confirm --force-recreate`), which heals a unit
whose container is "Up" in docker but whose vLLM never answers (a plain relaunch
no-ops on it). Debounced and cooldown-guarded so it never fights a slow load.
The orchestrator is not auto-restarted — see the intro.

## Safety Guarantees

1. **Max 1 restart per cycle** — prevents restart loops.
2. **Timeout per handler** — no blocking on slow checks.
3. **Debounced auto-heal** — a unit must stay down across
   `HSCC_WORKER_AUTOHEAL_DEBOUNCE` consecutive checks before force-recreate.
4. **Cooldown-guarded** — `HSCC_WORKER_AUTOHEAL_COOLDOWN_MINUTES` (default 10)
   between force-recreates.
5. **Handler exceptions caught** — one check crash doesn't kill the daemon.
6. **Graceful shutdown** — current cycle finishes before exit.
7. **Ad-hoc `hscc check` never writes shared state** — a manual failure must
   not masquerade as a fleet failure in `hscc status`.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `hscc status` shows STOPPED | Run `hscc start` (or `hscc install` for launchd) |
| `hscc` says unknown command | Use the real verbs: `start stop status check watch triggers notify install uninstall plist log verify stats throughput autoscale escalate` + groups `cluster template profiles project api autodown kanban` |
| `hscc` not on PATH | It is installed in the Hermes venv (`~/.hermes/hermes-agent/venv/bin/hscc`) |
| Daemon log missing | `hscc status` uses `~/.hscc/state/*.json`; `hscc log` tails `~/.hscc/daemon.log` |
| `deps not met` error | The daemon needs **Rich** in its Python env (`pip install rich` in the Hermes venv) |
