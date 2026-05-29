---
name: hscc-cluster
description: Manages DGX Spark GPU cluster — status, hosts, monitor, jobs, stop
tags: [hsc, cluster, gpu, spark, hosts, monitor, jobs]
---

# HSCC Cluster Control

Manage the DGX Spark GPU cluster via the `hscc-cluster` Python plugin.

## When to use

- User asks about cluster status, idle hosts, or running workloads
- User wants to see GPU/CPU/RAM metrics across nodes
- User wants to list or stop a sparkrun job
- User needs cluster configuration details

## Commands

```bash
python3 ~/.hermes/plugins/hscc-cluster/hscc.py <command> [args]
```

### `cluster-status`
Show running workloads and idle hosts. Equivalent to `sparkrun status`.

```bash
python3 ~/.hermes/plugins/hscc-cluster/hscc.py cluster-status
```

### `hosts`
List all cluster hosts, saved clusters, and live status. Returns structured JSON with host IPs, roles, and current workload distribution.

```bash
python3 ~/.hermes/plugins/hscc-cluster/hscc.py hosts
```

### `monitor`
Single snapshot of CPU/RAM/GPU metrics across all nodes (non-interactive).

```bash
python3 ~/.hermes/plugins/hscc-cluster/hscc.py monitor
```

### `jobs`
List all sparkrun jobs currently running on the cluster.

```bash
python3 ~/.hermes/plugins/hscc-cluster/hscc.py jobs
```

### `stop <container_id>`
Stop a running sparkrun workload by container ID.

```bash
python3 ~/.hermes/plugins/hscc-cluster/hscc.py stop 1b6e77192e59
```

### `info`
Show detailed cluster configuration: hosts, gateway, NAS, saved cluster files.

```bash
python3 ~/.hermes/plugins/hscc-cluster/hscc.py info
```

## HSCC CLI Commands (modern)

The `hscc` CLI (at `~/.hermes/hscc/bin/hscc`) wraps sparkrun commands directly:

### `hscc cluster cluster-status`
One-shot combined view of workloads + per-host metrics + cluster config.

```bash
hscc cluster cluster-status
```

Outputs three sections:
1. **WORKLOADS** — from `sparkrun status`
2. **SYSTEM METRICS** — from `sparkrun cluster monitor --simple --json` (pulsed once via `timeout 4`, NDJSON parsed)
3. **CLUSTER INFO** — from `sparkrun cluster list`

Raw output, no AI postprocessing.

### `hscc status`
System health dashboard (nodes, daemon, model, agents).

## Pitfalls

**sparkrun cluster monitor --json outputs NDJSON, not a single JSON object.**

The `sparkrun cluster monitor --simple --json` command streams NDJSON (newline-delimited JSON) — one JSON object per sample. For a one-shot snapshot, always take only the first line:

```python
first_line = output.strip().split("\n")[0]
data = json.loads(first_line)
```

**`timeout` returns exit code 124 when it kills a process — data is already captured.**

When using `timeout N command` in subprocess calls, the exit code is **124** (not 0) when timeout kills the process. But stdout/stderr are already buffered by the time timeout fires. Always accept both rc == 0 and rc == 124:

```python
rc, output, _ = run_cmd("timeout 4 sparkrun cluster monitor --simple --json", check=False)
if (rc == 0 or rc == 124) and output:
    data = json.loads(output.strip().split("\n")[0])
```

**NEVER hardcode cluster node IPs in HSCC CLI code.**

HSCC's `detect_cluster_nodes()` used to have hardcoded IPs (`.244, .245, .246, .247`) which did not match the actual cluster (`.244, .246, .247, .248`). This caused false "unreachable" reports for real nodes and false positives for missing ones.

When writing or modifying node discovery code, always read from the live cluster config via one of these methods:
1. `sparkrun cluster list --json` — preferred (returns `{"name": "hscc", "hosts": [...], "default": true}`)
2. `sparkrun cluster list` — parse tabular output for IPs matching `192.0.2.*`
3. `~/.hermes/plugins/cluster.json` — legacy cluster config (gateway + workers)
4. Fallback hardcoded list only as last resort

The correct node list for the 4-node DGX Spark cluster is:
- `.244` — gateway/primary node
- `.246`, `.247`, `.248` — worker nodes
- `.249` — NAS (QNAP, NFS `/share/CACHEDEV1_DATA/models`)

## Tips

- The `hosts` command returns structured JSON with worker IPs, gateway IP, and NAS details
- All outputs are JSON by default — parse them for programmatic use
- For human-readable output, echo the `output` field from the JSON response

## Design Patterns

### Modular Health-Check Daemon (for building monitoring plugins)

When building a Hermes plugin that needs periodic health checks or daemon-like behavior, use this architecture:

1. **Modular handlers** — each handler checks one subsystem independently via SSH. Timeout each handler (10s), don't let one failure block others.
2. **Categorized statuses** — always use `healthy` / `unhealthy` / `unknown`. `unknown` means the handler couldn't connect (SSH timeout, network issue) — never auto-restart on `unknown`, only alert.
3. **Single JSON report per cycle** — after all handlers complete, build one HealthReport JSON object. Save to `~/.hscc/daemon/status.json`. Append alerts to `~/.hscc/daemon/alerts.jsonl` (one JSON object per line — append-only, never overwrite).
4. **Escalator logic** — reads the report and decides actions. Auto-restart ONLY for the orchestrator container (max 1 restart per cycle). All other `unhealthy` + `unknown` (if all handlers are unknown) route via Telegram alert.
5. **File-based state** — avoid WebSocket dependency. CLI reads/writes `status.json` and `alerts.jsonl`. Telegram delivery is done via subprocess calling the orchestrator's bot.

See `references/health-check-daemon-pattern.md` for the full design.

## Diagnostics

### Quick Checks

```bash
# One-shot workload + metrics + config
hscc cluster cluster-status

# Daemon health
launchctl list com.hermes.hscc-daemon

# Agent counts
cat ~/.hermes/plugins/plugin-state/hermes-lifecycle.json | python3 -c "import json,sys; d=json.load(sys.stdin); a=d['agents']; print(len(a), 'agents'); print(set(v.get('state') for v in a.values()))"
```

### Troubleshooting: Node Won't Serve vLLM

When a node has a running sparkrun container but vLLM isn't responding (port unreachable, no HTTP response):

**1. Check container is actually running vLLM, not just `sleep infinity`:**
```bash
ssh spark@<IP> "docker ps --format '{{.ID}} {{.Names}} {{.Status}}'"
ssh spark@<IP> "docker exec <container> ps aux | grep -i vllm"
```
If `ps aux` shows only `sleep infinity` — the vLLM command failed to launch and sparkrun fell back to the health-check loop.

**2. Check for `libtorch_cuda.so` / CPU-only PyTorch (the #1 cause):**
```bash
# Quick check — if it says CPU, the image is wrong
ssh spark@<IP> "docker run --rm --gpus all <image> python3 -c \"import torch; print(torch.__version__); print(torch.cuda.is_available())\""
```
Correct: `2.11.0+cu130` + `True`
Wrong: `2.10.0+cpu` + `False` — this means the node has a CPU-only PyTorch install and will crash on `ImportError: libtorch_cuda.so: cannot open shared object file`

**3. Verify image ID matches a working node:**
```bash
ssh spark@<problem-node> "docker inspect <image> --format '{{.Id}}'"
ssh spark@<working-node> "docker inspect <image> --format '{{.Id}}'"
```
If image IDs differ, the problem node has a stale/broken local image. The fix is to copy the correct image from a working node over the LAN:
```bash
# From any working node (e.g., .246), copy to .248 (~40 min for 19GB)
ssh spark@192.0.2.11 "docker save sparkrun-eugr-vllm:latest | gzip -9" \
  | ssh spark@192.0.2.13 "gzip -dc | docker load"
```
This is much faster than pulling from GHCR over the slow SSH link.

**4. Check vLLM process logs for the actual crash:**
```bash
ssh spark@<IP> "docker logs <container> 2>&1 | tail -40"
ssh spark@<IP> "docker exec <container> cat /tmp/sparkrun_serve.log 2>&1 | tail -40"
```

**5. Manual vLLM launch inside container (for diagnosis):**
```bash
ssh spark@<IP> "docker exec <container> python3 /usr/local/bin/vllm serve Qwen/Qwen3.6-35B-A3B-FP8 \
  --host 0.0.0.0 --port 8000 --max-model-len 262144 --max-num-batched-tokens 32768 \
  --trust-remote-code --gpu-memory-utilization 0.8 --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder --kv-cache-dtype fp8 --load-format instanttensor \
  --attention-backend flashinfer --enable-prefix-caching 2>&1"
```
This prints the actual error instead of failing silently.

**6. Verify model weights exist and are readable:**
```bash
ssh spark@<IP> "docker exec <container> ls -la /cache/huggingface/hub/models--Qwen--Qwen3.6-35B-A3B-FP8/snapshots/"
```
