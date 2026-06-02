# HSCC — Hermes Spark Cluster Control

**Private installation package for managing a DGX Spark GPU cluster with AI agents.**

## What is HSCC?

HSCC is the operational backbone for a cluster of DGX Spark (GB10 / Grace-Blackwell,
`sm_121a`) nodes running LLMs via vLLM and orchestrated by Hermes agents. It is a set
of pure-stdlib Python plugins that live in `~/.hermes/plugins/` (this repo), with
runtime state in `~/.hscc/`.

## Cluster Topology

| Node             | Role          | Notes                                                      |
|------------------|---------------|------------------------------------------------------------|
| `192.0.2.10` | **Gateway / orchestrator** | Always-on vLLM serving Telegram + every Hermes agent. Runs the Hermes gateway. **Never reaped** by the idle monitor. |
| `192.0.2.11` | Worker        | vLLM spun up on-demand for dispatched agent work.          |
| `192.0.2.12` | Worker        | vLLM spun up on-demand.                                    |
| `192.0.2.13` | Worker        | vLLM spun up on-demand.                                    |
| `192.0.2.20` | **NAS** (QNAP)| HF model cache, NFS-mounted to every node at `/mnt/nas` (`/share/CACHEDEV1_DATA/models`). Containers serve from this offline cache (`HF_HUB_OFFLINE=1`). |

Workers are **not kept running**. They are started from the NAS cache via the
serving recipe when a task is dispatched, and stopped by the idle monitor when no
agent references them — so idle GPUs are reclaimed automatically.

## Model Serving

### Current model — NVFP4

The cluster serves **`nvidia/Qwen3.6-35B-A3B-NVFP4`** (NVIDIA ModelOpt FP4: mixed
FP8 attention + W4A16-NVFP4 MoE, ~21.82 GB weights, 3 safetensors shards).

**The working recipe is the only one that loads on the current vLLM image:**

```
~/.sparkrun-local/recipes/local-fixed/qwen3.6-35b-a3b-nvfp4-vllm.yaml
```

Launch it with sparkrun (port 8000, NAS-backed cache via `--cluster hscc`):

```bash
sparkrun run ~/.sparkrun-local/recipes/local-fixed/qwen3.6-35b-a3b-nvfp4-vllm.yaml \
  --cluster hscc --hosts 192.0.2.10 --port 8000 --no-follow --ensure
```

#### Why only `local-fixed` works

The NVFP4 checkpoint quantizes `lm_head` (`W4A16_NVFP4`, see the model's
`hf_quant_config.json`), but vLLM's `Qwen3_5MoeForCausalLM` keeps `lm_head`
unquantized. A minimal `vllm serve <model>` therefore auto-detects
`quantization=modelopt_mixed` and dies at load:

```
ValueError: There is no module or parameter named 'lm_head.input_scale'
            in Qwen3_5MoeForCausalLM
```

`local-fixed` avoids this by carrying:
- `mods/exp-w4a16` — patches the loader to accept the quantized `lm_head` scales
- `--quantization modelopt` + `--moe-backend marlin`
- GB10 / `sm_121a` env: `CUTE_DSL_ARCH=sm_121a`,
  `VLLM_MARLIN_USE_ATOMIC_ADD=1`, `VLLM_USE_FLASHINFER_MOE_FP4=0`,
  `VLLM_FP8_MOE_BACKEND=flashinfer_cutlass`, `FLASHINFER_DISABLE_VERSION_CHECK=1`
- `--kv-cache-dtype fp8 --attention-backend flashinfer --enable-prefix-caching`

> ⚠️ Do **not** use the top-level `~/.sparkrun-local/recipes/qwen3.6-35b-a3b-nvfp4-vllm.yaml`.
> Its flags `--quantization nvfp4` and `--nvfp4-group-size 16` are not recognized
> by the current vLLM build (`0.21.1rc1.dev292`, image `sparkrun-eugr-vllm:latest`,
> rebuilt 2026-05-27) and the serve command exits immediately.

#### Load time

Bringing the model up takes **~7 minutes**: ~2.5 min shard load (3 × ~21.8 GB
total) + ~4 min `fp8_gemm` AutoTuner / CUDA-graph capture. `GET /v1/models`
returns an **empty list the entire time** — this is normal; do not mistake the
loading window for a failure. Verify readiness with:

```bash
curl -s http://192.0.2.10:8000/v1/models    # lists the model id once ready
```

### Model name must agree in every config

vLLM answers only for the exact loaded model string, so a stale name = `404`
(dead Telegram, erroring crons) even when the server is healthy. The name lives in
several places that must all match what `/v1/models` reports:

| Location                                   | Consumed by                                              |
|--------------------------------------------|---------------------------------------------------------|
| `~/.hermes/config.yaml` `model.default`    | **The big one** — every Hermes agent + every cron with `model:null` inherits it (drives Telegram + crons). `hermes status` shows the effective value, read live. |
| `~/.hscc/models.json` `primary_model` + `models[].name` | Telegram bridge model resolution.            |
| `~/.hscc/serving.json` unit `model` + `recipe` | Desired-state serving topology.                    |
| `cluster-config/orchestrator-config.yaml`, `cluster-config/profiles/worker-2{46,47,48}/config.yaml` | Per-node profile defaults (orchestrator + worker base_urls). |
| `hscc-daemon/hscc.py` `VLLM_RECIPE`        | Daemon watchdog recovery recipe (see below).            |

`hermes model` is interactive-OAuth only — set `model.default` by editing
`config.yaml` directly.

## Daemon

`hscc-daemon` is the only continuous process — a launchd service.

- **launchd label:** `com.nousresearch.hscc-daemon` (kickstart with
  `launchctl kickstart -k gui/$(id -u)/com.nousresearch.hscc-daemon`).
- **Watchers:** kqueue watcher + in-process FallbackPoller. Periodic streams —
  dgx 5s, gateway 10s, local 30s, heartbeat 60s, nas 30s, idle 300s.
- **Watchdog recovery recipe:** `hscc-daemon/hscc.py` `VLLM_RECIPE` points at the
  **NVFP4 `local-fixed` recipe**. If the gateway vLLM on `.244` goes unhealthy the
  watchdog relaunches it from this recipe via `sparkrun run … --cluster hscc`.
- **Load grace window:** `HSCC_VLLM_LOAD_GRACE_MINUTES` (default 6). After a
  (re)start the watchdog waits out this grace before counting a failed health
  check, so it does not latch BLOCKED while the model is still loading.

> Editing the daemon: this repo carries **two layouts** that must stay in sync —
> the active `hscc-daemon/hscc.py` and the template copy
> `install/hscc-plugins/hscc-daemon/hscc.py`. Restart the daemon after any change.

## Idle Monitor

`install/hscc-plugins/hscc-agent-coordinator/scripts/model-idle-monitor.py`
(run every 5 min by the Hermes cron `HSCC Model Idle Monitor`, id `937310319dfc`)
reclaims idle GPUs:

1. List running sparkrun containers (`sparkrun status`).
2. Match each container to agents by host IP (`agents.json` + `lifecycle.json`).
3. **Gateway node `192.0.2.10` is always protected** — it runs the always-on
   orchestrator vLLM which has no agent row and would otherwise be reaped as an
   "orphan", thrashing against the daemon's restart loop. Override the protected IP
   with `HSCC_GATEWAY_NODE`.
4. A container with **no** referencing agent → stopped (orphan).
5. A container whose agents are all idle longer than `HSCC_IDLE_TIMEOUT_MINUTES`
   (default 30) → stopped. Actively-running agents keep it alive.

```bash
# Dry-run a scan (shows keep/stop decisions, changes nothing)
python3 install/hscc-plugins/hscc-agent-coordinator/scripts/model-idle-monitor.py --dry-run

# Pause/resume the cron (e.g. during manual model testing so it stops reaping)
hermes cron pause  937310319dfc
hermes cron resume 937310319dfc
```

> The monitor is name-agnostic (keys off `sparkrun status` + host IP), so model
> swaps need no edit. It only ever failed historically because its executing cron
> agent inherited a broken `model:null` default — fix the model name, not the script.

## Crons

Cron store is `~/.hermes/cron/jobs.json` (not the session scheduler). Three jobs:

| id             | name                    | schedule   | purpose                                   |
|----------------|-------------------------|------------|-------------------------------------------|
| `138f8b56d790` | `hscc-orch-tick`        | `* * * * *`| Orchestrator report-back / reconcile loop (`hscc_orch_tick.py`). Honors the autonomy gate `~/.hscc/autonomy`. |
| `937310319dfc` | `HSCC Model Idle Monitor` | every 5m | Runs `model-idle-monitor.py` (above).     |
| `c67ce27a1f36` | `X feed — timeline + news`| every 360m | Personal digest (unrelated to cluster).  |

## Components

### HSCC Plugins
- `hscc-daemon` — monitoring daemon + watchdog (above)
- `hscc-agent-coordinator` — agent lifecycle, worktrees, dispatch/release gate, autonomy gate
- `hscc-orchestrator` — agent dispatch + fleet status
- `hscc-provision` — model container management
- `hscc-cluster` — cluster operations (status, hosts, jobs)
- `hscc-projects` — projects + kanban boards
- `hscc-events` — event bus / lifecycle log
- `hscc-governance` — policy engine + permissions
- `hscc-skills` — skills & templates installer
- `hscc-chat` — gateway client
- `hscc-bootstrap` — bootstrap command
- `hscc-mcp` — MCP server exposing HSCC tools to the model
- `hscc-optimizations` — performance tuning

### Dual-layout rule

Several plugins exist twice: the **active** copy under `<plugin>/` and the
**template** copy under `install/hscc-plugins/<plugin>/`. When editing a plugin,
update both copies so a fresh install matches the running system.

## Quick Start

```bash
hscc init           # detect setup, wire components, deploy model
hscc status         # health
hscc cluster status # running workloads, idle hosts
```

## Troubleshooting

```bash
hscc status                              # overall health
tail -f ~/.hscc/daemon.log               # daemon watchdog log
curl http://192.0.2.10:8000/v1/models # what the orchestrator actually serves
hermes status | grep Model               # effective model Hermes will use
```

Common: **Telegram dead / crons 404** → the served model name and the config name
disagree. Confirm `/v1/models`, then align the config locations in the table above.

## Security

- SSH keys managed locally (not in repo); StrictHostKeyChecking on
- Secrets are not stored in config; runtime state in `~/.hscc/` is git-ignored
- Model sync via the NAS HF cache over NFS

## License

Private — do not distribute.
