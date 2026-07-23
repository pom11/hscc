# HSCC cron watchdogs

Pure shell scripts (no LLM) that monitor the HSCC cluster + Mac dispatcher host. Each follows the watchdog pattern: silent when healthy, output only when action is taken or a problem is detected. Registered through Hermes' built-in cron with `--no-agent` so they cost zero tokens.

## Scripts

| Script | Purpose |
|---|---|
| `hscc_proxy_watchdog.sh` | Probe `localhost:4000` (sparkrun LiteLLM proxy). If unreachable, restart via `sparkrun proxy start --cluster hscc --port 4000`. Covers the stale-PID regression where the proxy dies and sparkrun doesn't auto-restart it. |
| `hscc_worker_health.sh` | Probe all 4 vLLM endpoints (`.244/.246/.247/.248:8000`). Verifies each serves the expected model id; reports unreachable hosts and model-id mismatches. |
| `hscc_cluster_digest.sh` | Periodic summary: container count per host, endpoint health, proxy state, per-job uptime. Designed to be delivered to a chat (e.g. the HSCC Telegram channel). |
| `hscc_nas_watchdog.sh` | NAS health: ping QNAP `.249`, check Mac `/Volumes/NAS` mount listability. Falls back to project docs for remediation. |

## Install

Scripts go under `~/.hermes/scripts/` so Hermes can find them via `--script`.

### Via bootstrap (recommended)

Bootstrap copies these automatically as part of the install flow:

```bash
~/dev/hscc/hscc-bootstrap/bootstrap.sh
```

Look for the *Install: operator watchdog scripts* stage. User-added scripts in
the runtime dir are preserved (only `hscc_*.sh` names are touched), and the
previous version of each script is backed up to `<name>.bak-<timestamp>` before
overwrite. Pass `--no-backup` to skip the backup step.

### Manual install (no bootstrap)

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

## Runtime-dependency update loop

`dep_pr_watcher.py` closes an automated loop that keeps the cluster's runtime
dependencies (hermes-agent, sparkrun) current, gated by human review:

1. **`.github/workflows/check-runtime-deps.yml`** (GitHub Actions, daily) checks
   each upstream repo's latest release against `hscc-bootstrap/runtime-versions.json`.
   On a bump it opens/updates a PR — label `needs-cluster-check`, repo owner as
   reviewer — carrying a cluster-verification checklist.
2. **`dep_pr_watcher.py`** (Hermes cron, daily) polls for those PRs and creates
   one idempotent kanban card per PR (`idempotency_key = dep-check-pr-<n>`), so a
   worker verifies the bump end-to-end. Silent when nothing is pending.
3. A worker upgrades the runtime, re-bootstraps, runs the suites, and reports on
   the card. The human reviewer merges the PR (bumping the lock) once satisfied.

Install the cluster side (installs the poller to `~/.hermes/scripts/` and
registers the Hermes cron job, idempotently):

```
scripts/install_dep_watcher.sh            # daily 08:00 by default
scripts/install_dep_watcher.sh --uninstall
```

The GitHub Action runs from the default branch once merged; the poller/cron is
independent and needs no network path from GitHub to the cluster.
