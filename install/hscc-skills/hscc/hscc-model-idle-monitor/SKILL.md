---
name: hscc-model-idle-monitor
description: Background process (Hermes cron job) that scans running sparkrun containers and shuts down models idle for > 30 min
category: hscc
domain: cluster resource management, automatic cleanup
version: 1.0.0
license: MIT
metadata.hermes.tags: []
---

# HSCC Model Idle Monitor

> **STALE / PAUSED (historical).** The idle monitor lived in the `hscc-agent-coordinator` plugin, archived 2026-06-08. The cron job is **PAUSED 2026-06-08** (`381ef65e40f5`): it keys off the now-stale `~/.hscc/agents.json` + absent `lifecycle.json` and would reap native-provisioned worker vLLMs as "orphan". Do not resume it without porting the daemon's BRIDGE_FILE guard first. Current teardown is owned by the gateway dispatcher + `hscc_daemon` keep-alive. This doc is kept for history only.

Background **Hermes cron job** that periodically scans running sparkrun containers and automatically shuts down model instances that have been idle (no active agent) for a configured timeout period.

## Critical: Cron job, NOT daemon

The `hscc_daemon` process does health checks only — it does **NOT** stop containers. The idle monitor was a **separate Hermes cron job**. It calls the Python script on a schedule.

If you see containers being killed, check `hermes cron list` — not the daemon.

## How it works

1. Cron job runs every 5 minutes (configurable via `HSCC_SCAN_INTERVAL`)
2. Lists all running sparkrun containers via `sparkrun status`
3. For each container, maps it to agents via host IP matching
4. Shuts down containers that are:
   - **Orphans** — no agent references the host at all
   - **Stale** — agent references it but has been idle > 30 min (configurable via `HSCC_IDLE_TIMEOUT_MINUTES`)
5. Never auto-stops MTP container on gateway (192.0.2.10)

## How it works

1. Runs every 5 minutes (configurable via `HSCC_SCAN_INTERVAL`)
2. Lists all running sparkrun containers via `sparkrun status`
3. For each container, maps it to agents via host IP matching
4. Shuts down containers that are:
   - **Orphans** — no agent references the host at all
   - **Stale** — agent references it but has been idle > 30 min (configurable via `HSCC_IDLE_TIMEOUT_MINUTES`)
5. Never auto-stops MTP container on gateway (192.0.2.10)

## Usage

### One-shot scan
```bash
python3 ~/.hermes/plugins/install/hscc-plugins/hscc-agent-coordinator/scripts/model-idle-monitor.py
```

### Dry run (no stops)
```bash
python3 ~/.hermes/plugins/install/hscc-plugins/hscc-agent-coordinator/scripts/model-idle-monitor.py --dry-run
```

### Continuous daemon mode
```bash
python3 ~/.hermes/plugins/install/hscc-plugins/hscc-agent-coordinator/scripts/model-idle-monitor.py --daemon
```

## Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `HSCC_IDLE_TIMEOUT_MINUTES` | `30` | How long an agent can be idle before its model is stopped |
| `HSCC_SCAN_INTERVAL` | `5` | Daemon scan interval in minutes |

## Output

JSON summary with:
- `containers_scanned` — number of containers checked
- `kept` — containers kept alive and why
- `stopped` — containers that were shut down
- `errors` — any failures during stop attempts

## Files

- **Script**: `~/.hermes/plugins/install/hscc-plugins/hscc-agent-coordinator/scripts/model-idle-monitor.py`
