# Idle Monitor Integration (May 2026)

> **STALE (historical).** The `hscc-agent-coordinator` (ACC) plugin referenced below was archived 2026-06-08, and the legacy idle monitor was PAUSED 2026-06-08 (it reaps native-provisioned worker vLLMs as "orphan"). Dispatch is now native Hermes kanban; provisioning is the hscc-cluster `provision_model` tool. This doc is kept for history only — see the `hscc` skill for current mechanics.

## Background
The idle monitor was originally a standalone cron job (`model-idle-monitor.py`) running every 5 minutes. It conflicted with the daemon's heartbeat check (which only read agent state, never killed containers). This caused kill loops — agents were assigned tasks but containers were preemptively killed by the cron job.

## Solution
Integrated idle monitoring directly into the HSCC daemon in two places:

### 1. ACC Plugin (`hscc-agent-coordinator/hscc.py`)
- Added full `run_idle_monitor_scan()` function for manual/standalone use
- Added `check_idle_monitor_containers()`, `match_agents_to_container()`, `check_container_idle()`, `stop_container()`
- Added `cmd_idle_monitor()` CLI command with `--dry-run` support
- Reads `HSCC_IDLE_TIMEOUT_MINUTES` env var (default 30)

### 2. Daemon (`hscc_daemon/hscc.py` + `event_driven.py`)
- Added `check_idle_monitor()` as a new periodic check (every 5 min = 300s)
- Added to `STREAMS` dict in `hscc.py`
- Added to `check_map` in `_run_event_driven_daemon()`
- Added to `PERIODIC_STREAMS` and `STATE_STREAMS` in `event_driven.py`

## CRITICAL: Adding New Streams
When adding ANY new periodic check, update THREE places:

1. `hscc_daemon/hscc.py` — `STREAMS` dict
2. `hscc_daemon/hscc.py` — `check_map` in `_run_event_driven_daemon()`
3. `hscc_daemon/event_driven.py` — `PERIODIC_STREAMS` dict AND `STATE_STREAMS` set

If you forget #3, the launchd plist won't be generated and the check won't fire.

## How It Works
1. Scans running containers via `sparkrun status`
2. Loads agent definitions from `~/.hscc/agents.json` and lifecycle from `~/.hscc/lifecycle.json`
3. Maps containers to agents by matching host IP in model strings (`vllm-192.0.2.XXX`)
4. Decision logic:
   - MTP gateway (244) → always protected
   - Orphaned container (no agents) → stop immediately
   - Agent in `spawning`/`ready` state → keep (provisioning in progress)
   - Agent `running` → keep
   - Agent `idle` > 30 min → stop
   - Agent `idle` < 30 min → keep with remaining time

## Removed
- Cron job `9508e87f9729` (HSCC Model Idle Monitor) — removed May 29 2026
- Standalone script `model-idle-monitor.py` is no longer the primary mechanism (code remains in ACC plugin for manual use)
