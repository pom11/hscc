# HSCC cron watchdogs

Pure shell scripts (no LLM) that monitor the HSCC cluster + Mac dispatcher host. Each follows the watchdog pattern: silent when healthy, output only when action is taken or a problem is detected. Registered through Hermes' built-in cron with `--no-agent` so they cost zero tokens.

## Scripts

| Script | Purpose |
|---|---|
| `hscc_proxy_watchdog.sh` | Probe `localhost:4000` (sparkrun LiteLLM proxy). If unreachable, restart via `sparkrun proxy start --cluster hscc --port 4000`. Covers the stale-PID regression where the proxy dies and sparkrun doesn't auto-restart it. |
| `hscc_worker_health.sh` | Probe all 4 vLLM endpoints (`.244/.246/.247/.248:8000`). Verifies each serves the expected model id; reports unreachable hosts and model-id mismatches. |
| `hscc_cluster_digest.sh` | Periodic summary: container count per host, endpoint health, proxy state, per-job uptime. Designed to be delivered to a chat (e.g. the HSCC Telegram channel). |
| `hscc_nas_watchdog.sh` | NAS health: ping QNAP `.249`, check Mac `/Volumes/NAS` mount listability. Falls back to project docs for remediation. |

## Install (one-time per host)

Scripts go under `~/.hermes/scripts/` so Hermes can find them via `--script`:

```bash
mkdir -p ~/.hermes/scripts
cp scripts/hscc_*.sh ~/.hermes/scripts/
chmod +x ~/.hermes/scripts/hscc_*.sh
```

Then register the cron jobs (Hermes gateway must be running so the cron ticker picks them up):

```bash
hermes cron create 'every 5m'   --name 'hscc-proxy-watchdog'  --no-agent --script hscc_proxy_watchdog.sh
hermes cron create 'every 10m'  --name 'hscc-worker-health'   --no-agent --script hscc_worker_health.sh
hermes cron create 'every 4h'   --name 'hscc-nas-watchdog'    --no-agent --script hscc_nas_watchdog.sh

# Cluster digest — replace <chat_id> with your delivery target (e.g. telegram:-100...).
hermes cron create 'every 2h'   --name 'hscc-cluster-digest'  --no-agent --script hscc_cluster_digest.sh --deliver 'telegram:<chat_id>'
```

Verify with `hermes cron list`.

## Customization

Each script is self-contained shell + hardcoded host IPs / model ids. Cluster topology assumptions live at the top of each script — edit there if your cluster differs.

## Why `--no-agent`

These are pure probes; no semantic interpretation needed. Routing through the LLM would burn orchestrator tokens (currently Qwen3.6-35B-A3B-NVFP4 on `.244`) every tick for no added value. Operator slash commands like `/cluster` and `/orch-restart` remain agent-mediated since they're interactive and need confirmation flows.
