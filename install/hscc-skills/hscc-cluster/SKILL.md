---
name: hscc-cluster
description: Manages DGX Spark GPU cluster — the 28 registered agent tools, each tool's state source, the CLI verbs, and node/NAS diagnostics
tags: [hsc, cluster, gpu, spark, hosts, monitor, jobs, agent-tools, serving]
---

# HSCC Cluster Control

The DGX Spark GPU cluster is reached through the `hscc-cluster` plugin. There are
two ways in, and they are not interchangeable:

- **Agent tools** — 28 tools registered by `hscc-cluster/__init__.py`. This is what
  a model calls. Most of them are read-only; the rest are confirm-gated.
- **The `hscc` CLI** — what an operator (or a shell) runs. Fleet-wide and
  power-mutating verbs live only here and on confirm-gated API routes.

**The registry is the source of truth, not this file.** `hscc-cluster/__init__.py`
defines the `_*_TOOLS` tables, `hscc-cluster/plugin.yaml` mirrors them, and
`hscc_daemon/tests/test_plugin_manifest_drift.py` pins registry ↔ manifest in both
directions. If this document ever disagrees with the registry, the registry wins and
this document is the bug — fix the document.

The tool-by-tool surface (which tool covers which CLI verb, and which verbs are
deliberately *not* covered) is measured in
`docs/audits/2026-11-23-agent-surface.md` — that matrix is the sign-off record for
the decisions quoted below.

## When to use

- Asked about cluster status, idle hosts, running serving units, or throughput
- Asked whether a node is paused, or whether the serving layer is armed to idle down
- Asked what is in the local merge queue
- Asked to provision / stop / restart one model, or to park / resume one node
- Diagnosing a node (GPU/disk/OOM/docker), a vLLM container's logs, or the NAS mount
- Asked what the fleet would look like scaled (advice, not action)

## The registered agent tools (28)

`confirm=true` is the plugin's spelling of the CLI's confirmation: **absent means
"dry-run and report", never "do it".** Every tool returns a JSON string.

### Read (8) — no confirmation, no side effects

| tool | answers | state source |
|---|---|---|
| `cluster_status` | running models, idle nodes, serving units | live sparkrun query; `~/.hscc/cluster.json` for names/ids only (`ops.py`) |
| `list_recipes` | which sparkrun recipes exist | sparkrun recipes, live |
| `pick_node` | which worker is free to provision on | live status + the pause overlay (`node_pauses.covering`) |
| `discovery_status` | topology map (orchestrator/workers/NAS); `probe=true` adds VRAM, GPU model, power-based idle, vLLM health | precedence: live `sparkrun cluster list --json` → `~/.hscc/cluster.json` cache → `DiscoveryError`. **There is no silent fake-IP fallback** — a misconfigured host fails loudly (`discovery.py`) |
| `nas_status` | NAS node + one mount probe (no fan-out) | discovery + a single mount probe |
| `node_paused` | which nodes are paused, which units were stopped, reason, since, until | `~/.hscc/node_pauses.json` through `hscc_daemon/node_pauses.py` |
| `autodown_status` | is idle down armed, the idle window, recorded state reconciled against the live serving probe, last activity, and which interlock (kanban work, PR/CI, watchdog block) is holding a teardown off | calls the CLI's own verb `autodown_cli._cmd_status` with `--json`; payload built by `autodown.status_snapshot()` from `~/.hscc/autodown.json` + `_interlock.json` |
| `merge_queue_status` | per project: enabled (or why off — no repo / no GitHub remote / no gates), paused, queue depth and entries, waiting head, in-flight PR, last outcome | calls the CLI's own verb `merge_queue_cli.cmd_merge_queue(["status", "--json"])`; projection is `merge_queue._status_doc` over `~/.hscc/merge-queue-state.json` (+ the flightdeck registry, to explain a why-off) |

`autodown_status` and `merge_queue_status` take **no arguments at all**
(`additionalProperties: false`) — the schema itself refuses an invented `host` or
`confirm`, because neither has a meaning on a surface that is read-only by contract.

### Ops (5) — per-unit, two of them guarded

| tool | answers | state source / guard |
|---|---|---|
| `provision_model` | start a model on a node via sparkrun | serving topology from `~/.hscc/serving.json` + applied template. GUARDED: `confirm=true` |
| `stop_model` | stop one model on one node | live + `serving.json`. GUARDED: `confirm=true` |
| `model_health` | is this node's vLLM endpoint answering | live endpoint probe. read-only |
| `node_pause` | park one node: stop its sparkrun units, record a durable pause the heal paths skip | **delegates to the unified CLI's own verb** (`node_pause_tools.py`). GUARDED: `confirm=true`; without it returns the plan (every unit + every node it would touch). Refuses a node serving the orchestrator model unless `force=true`, and a multi-node unit unless `span=true` |
| `node_resume` | restart the units a pause recorded (`sparkrun --ensure`, same recipe and flags), then clear the pause and that node's heal strikes | same delegation. GUARDED: `confirm=true`; without it returns the commands |

### Debug (3) — live, read-only

| tool | answers | state source |
|---|---|---|
| `vllm_logs` | a node's vLLM serve log + docker logs | live ssh/docker through sparkrun |
| `node_diagnostics` | dmesg/OOM, fd count, disk, docker, GPU temp/util/ECC | live through sparkrun |
| `nas_diagnose` | mount healthy / stale / unreachable + the QNAP exports | live |

### Heal (4) — all guarded, one is high-risk

| tool | answers | state source / guard |
|---|---|---|
| `restart_model` | stop + run one model | live + the pause guard `lifecycle.py` (`node_pauses.covering()` — a parked node is not "broken") |
| `remount_nas` | lazy-umount + remount `/mnt/nas` on a node | live. GUARDED: `confirm=true` |
| `repair_nas_export` | re-publish QNAP NFS exports (backs up first) | live. HIGH-RISK. GUARDED: `confirm=true` |
| `reap_orphans` | stop sparkrun containers `serving.json` does not account for | `serving.json` **is** the definition of "orphan", and it has ≥3 writers. GUARDED: `confirm=true`, plus the F13 staleness guard `_serving_verdict()`: an absent, unparseable, unit-less or stale `serving.json` — or an orphan set whose nodes it does not name — **refuses** (`executed: false`, with the reason). `force=true` overrides the guard only. It never writes or repairs `serving.json` |

A stale `serving.json` is the one way this tool can delete live work, which is why
the guard reads the file and refuses rather than repairing it.

Naming (audit F7): these four are the `heal` **tools** — individual repairs, each
yours to call one at a time. They are not `/heal` (the operator's chat-surface
sweep that decides which workers are unhealthy) and not `hscc heal-status` /
`heal-clear` (the daemon's breaker strike ledger, CLI-only until D4 lands a read
tool). The whole `heal*` family is tabulated once in `install/hscc-skills/hscc/SKILL.md`
under "Naming traps" — that table is the single source; do not restate it here.

### Fleet (4) — reads and advice, no scaling

| tool | answers | state source |
|---|---|---|
| `cluster_verify` | full compatibility/health smoke-test (plugins, multiplex, streams, proxy, config) | `hscc_daemon/verify.py` reads plugins, streams, proxy, `~/.hscc/applied_template.json` |
| `cluster_throughput` | vLLM token throughput + per-node queue depth | live vLLM metrics |
| `fleet_stats` | task completions + tool usage over N days (default 7) | `hscc_daemon/stats.compute_stats` → the Hermes kanban DB, **through Hermes**. Hermes owns that store; never open `state.db` yourself |
| `autoscale_advice` | advisory up/down/none from current queue depth | live throughput + queue depth (`throughput.compute_throughput`, `autoscale.decide_scale`). **It advises; it does not scale** |

### Template (3) — one family, one of them fleet-wide

| tool | answers | state source / guard |
|---|---|---|
| `list_templates` | declared fleet layouts | the template dir (repo/bundled) |
| `preview_template` | what applying one would change vs the live cluster | templates + live diff. No writes |
| `apply_template` | apply it — provisions orchestrator + workers, i.e. reshapes the **entire** fleet | writes `~/.hscc/applied_template.json`. HIGH-RISK. GUARDED: `confirm=true`; without it returns a preview |

### Orchestrator messaging (1)

| tool | answers | state source |
|---|---|---|
| `orch_message` | nudge another orchestrator profile — queue-only, signed `[from <you>]`, audited, rate-limited per pair per hour | rate-limit config from `~/.hscc/cluster.json` (env override `HSCC_ORCH_MSG_MAX_PER_HOUR`), audit to append-only `~/.hscc/state/orch_messages.jsonl`, and the main-orchestrator identity **resolved** through `hscc-roles/main_orch.py` — the same module `hscc main status` reads, so a renamed main needs no edit here |

Both ends must be `*-orch` (the one non-`*-orch` name it accepts is the configured
main orchestrator, from that resolver). It refuses worker profiles and `default`, and
it **never interrupts a running turn** — it queues.

Not to be confused with the other capabilities that share the `orch` prefix and do
different things: `hscc orch inject` (types into a live tmux pane — CLI-only),
`hscc orch waker status|enable|disable` (the daemon-resident waker's on/off
switch — it sends nothing), `/orch-restart` (restarts the orchestrator **vLLM**),
`POST /v1/orchestrator/chat` (one chat turn, by job id). All of them are tabulated
once in `install/hscc-skills/hscc/SKILL.md` under "Naming traps"; that table is
the single source.

## Tools that deliberately do NOT exist

When a verb has no tool, that is usually a decision, not an omission. Do not go
looking for these, and do not "helpfully" add one — each row is pinned by a test.

| verb | tool? | why | audit |
|---|---|---|---|
| `hscc cluster monitor` | **no** | `cluster_status` + `discovery_status(probe=true)` already answer a worker, and `GET /v1/cluster/monitor` covers dashboards | D2 |
| `hscc cluster up` / `down` | **no** | fleet-wide power stays operator + confirm-gated route; per-unit is `provision_model` / `stop_model` | D3 |
| `hscc kanban pause` / `resume` / `paused` / `enforce` | **no** (`kanban_paused` does not exist) | a board brake. A worker that can pause its own board can pause the fleet's work | D6 |
| `hscc merge-queue add` / `remove` / `pause` / `resume` / `enable` / `disable` | **no** | the queue force-pushes and squash-merges — an agent-writable merge door is a self-merge path. The daemon tick is the only thing that runs git/gh for it | D8 |
| `hscc autodown enable` / `disable` / `wake` / `cancel` | **no** | arming or disarming fleet power is daemon/operator scope, not a per-turn agent call | §2.4 |
| `hscc notify` | **no** | an agent that can page the operator can train the operator to ignore pages | D7 |
| `hscc doctor` | **no** (and no route) | `hscc project doctor` reads *this host's* trees — meaningless off the host | D11 |
| `hscc main status`, `orch waker status` | **no tool** | pure reads wanted by the dashboards → read **routes**, not agent tools | D9 |
| `hscc heal-status` / `heal-clear` | **no tool yet** | breaker strikes. D4 recommends a read-only `heal_status` tool and `clear` staying CLI-only; until that lands these are CLI verbs. Note `/heal` is a *different* subsystem (healing unhealthy workers) | D4, F7 |
| `hscc template validate` | **no tool** (and no route yet) | D5 recommends `GET /v1/template/validate/{name}` + the `/template` hint. `validate` is the safe half of a family whose destructive half (`apply`) has every surface | D5, F10 |
| `hscc start` / `stop` / `install` / `uninstall` / `api *` | **no** | service-manager scope: the API cannot start or stop the process that serves it; launchd units are never agent-reachable | §2.1, §2.7 |

## The one-door rule (read this before adding a tool)

`node_pause` / `node_resume` / `node_paused`, `autodown_status` and
`merge_queue_status` all do the same thing: **they call the unified CLI's own verb
functions in-process and report what the verb printed.** No second implementation.

Why it matters, and it is the whole point of the module docstrings:

- Whatever gate, guard or enrichment the CLI adds to that verb is automatically the
  tool's too. A tool that imported the builder or the state file directly would be a
  second door — one that can be widened without anyone touching the CLI.
- The agent sees the operator's own report, so a chat screenshot and a terminal
  transcript describe the same decision.
- Read-only is the *shape of the call*, not a promise: `autodown_status` hands the
  dispatcher the verb's `--json` and that verb is the one branch in its group that
  never calls `record_activity` / `save_config`; `merge_queue_status` hands over a
  fixed `["status", "--json"]` argv that no caller input reaches.
- `confirm=true` maps to the CLI's `--yes`, and **absent `confirm` maps to
  `--dry-run`** — never to an unguarded mutation. The CLI itself refuses a verb with
  no terminal and no `--yes`, which is the posture a cron entry must land in too.
- Import daemon modules **lazily, inside the handler**. Importing `hscc_daemon.*` at
  plugin import time would make the plugin's load depend on the daemon's import graph.
- Schemas live with their tool module (the `template_tools` / `orch_message`
  precedent), so a tool and its contract cannot drift apart across files.

## State files and who owns them

One owner per file is the invariant. `~/.hscc` paths below; readers are who else
consults the file.

| file | owner (writer) | readers |
|---|---|---|
| `serving.json` | `hscc_daemon/serving.py` + bootstrap `serving_gen.py` | discovery, clusterlib, cluster_template, doctor, enable_plugins, cluster-guard hook, onboard.py |
| `applied_template.json` | `hscc-cluster/cluster_template.py` | cluster-guard hook, `node_pause_exec`, `verify`, `declaration_agreement`, `hscc-commands/cmdlib` |
| `node_pauses.json` | `hscc_daemon/node_pauses.py` (via `node_pause_exec`) | cluster_render, autodown, lifecycle, hscc.py, hscc-cluster, doctor_node_pauses, routes_cluster |
| `cluster.json` | bootstrap / operator-authored, merged with the discovery cache | discovery, ops, clusterlib, doctor_*, install_* |
| `discovery_cache.json` | `hscc-cluster/discovery.py` | discovery only |
| `autodown.json` (+ `_interlock.json`) | `hscc_daemon/autodown.py` | routes_autodown, autodown_cli, `autodown_status` |
| `merge-queue-state.json` | `hscc_daemon/merge_queue.py` | merge_queue_cli, routes_merge_queue, `merge_queue_status` |
| `state/kanban_paused.json` | `hscc_daemon/kanban_pause.py` | orch_waker, kanban_cli (CLI-only surface, D6) |
| `state/orch_waker.json` (`.jsonl`) | `hscc_daemon/orch_waker.py` | routes / CLI status |
| `main_wakeups.json` + `state/main_wakeups.json` | `hscc_daemon/main_wakeups.py` | main_cli |
| `state/<stream>.json` | daemon `state.py` (`STATE_DIR`) | doctor rows, `hscc status`, `orch_waker.py` |
| `history.db` | `hscc_daemon/metrics_history.py` | `hscc cluster history` + `GET /v1/cluster/history` — one store, two readers, which is correct |
| `alerts.json` | `hscc_daemon/alerts.py` | notify_cli, routes_alerts |
| `update-check.json` | `hscc_daemon/update_check.py` | `hscc status`, `hscc update` |

## CLI verbs (the operator surface)

`TOP_LEVEL_COMMANDS` in `hscc_daemon/hscc.py` is the list; `hscc nosuchverb` prints
it, so it cannot silently lie. The groups that matter here:

```bash
hscc cluster status | hosts | monitor | jobs | info | stop <cid> | up | down | history
hscc template list | status | preview <name> | validate <name> | apply <name> --confirm
hscc node pause <host> [--force] [--yes] [--dry-run] | resume <host> | paused [--json]
hscc autodown status | enable | disable | wake | cancel
hscc merge-queue status | list | add | remove | pause | resume | enable | disable
hscc kanban blocked | stale | monitor | pause | resume | paused | enforce
hscc verify [--json] | stats [days] | throughput | autoscale | escalate
hscc heal-status | heal-clear <node>        # breaker strikes, NOT /heal
hscc main status | start | wakeups …
```

`cluster up` / `down` accept `--dry-run`. `history` is the only place the retention
tiers are explained.

**`hscc cluster cluster-status` is not a verb.** `cluster-status` belongs to the
standalone plugin entry below; the unified group takes `status`.

## Standalone plugin entry

The same handlers, callable without a model in the loop
(`hscc-cluster/hscc.py`, `COMMANDS`):

```bash
python3 ~/.hermes/plugins/hscc-cluster/hscc.py <command> [args] [--json]

  cluster-status      running workloads and idle hosts
  hosts               all cluster hosts and saved clusters
  monitor             one CPU/RAM/GPU snapshot
  jobs                sparkrun jobs running
  stop <id>           stop a workload by container ID
  down [--dry-run]    stop ALL sparkrun workloads fleet-wide
  up   [--dry-run]    start every unit in serving.json (orch + keepalive)
  info                detailed cluster configuration
  cluster-template    list | preview | apply
  profile-status      running kanban task counts per profile
  orch inject <p> "<msg>" [--mode auto|plain|queue|steer] [--dry-run] [--json]
  orch waker status|enable|disable [--json]
```

`--json` is the machine path and is byte-identical to the pre-themed output (daemons,
scripts and the iOS console parse it); without it you get the themed human view.
Prefer the unified `hscc` CLI for anything new — this entry exists for back-compat.

## Pitfalls

**`sparkrun cluster monitor --json` streams NDJSON, not one JSON object.**

One JSON object per sample. For a one-shot snapshot, take only the first line:

```python
first_line = output.strip().split("\n")[0]
data = json.loads(first_line)
```

**`timeout` exits 124 when it kills a process — the data is already captured.**

stdout/stderr are buffered by the time timeout fires, so accept both rc 0 and rc 124:

```python
rc, output, _ = run_cmd("timeout 4 sparkrun cluster monitor --simple --json", check=False)
if (rc == 0 or rc == 124) and output:
    data = json.loads(output.strip().split("\n")[0])
```

**Never hardcode cluster node IPs.**

`detect_cluster_nodes()` once carried a hardcoded list (`.244, .245, .246, .247`) that
did not match the real cluster (it skipped `.245` and ended at `.248`), so it reported
live nodes as unreachable and missing ones as present. Node identity is a *live
resource map*, not a constant — read it:

1. `sparkrun cluster list --json` — preferred (`{"name": "hscc", "hosts": [...], "default": true}`)
2. the `discovery_status` tool (same precedence, already implemented)
3. `~/.hscc/cluster.json` — the documented cache, and the *last* thing `discovery.py`
   trusts before raising

There is deliberately no fake-IP fallback: a misconfigured machine must fail loudly
rather than SSH documentation addresses. Idle is classified by **power draw**
(~11–15 W idle vs 35–80 W serving on a GB10), because `util%` reads ~96% even when
idle.

**A slash command bypasses every guard the cluster engine adds.**

`hscc-commands/cmdlib.py` shells out to `sparkrun` directly and builds its own argv —
deliberately, so the commands still work when the orchestrator LLM is wedged. The cost:
a guard added to `cmd_cluster_up` / `cmd_cluster_down` does **not** protect
`/cluster-down`, `/cluster-reboot`, `/cluster-restart`. When you add a guard, say so
in `hscc-commands/README.md`.

## Design Patterns

### Modular Health-Check Daemon (for building monitoring plugins)

For a Hermes plugin with periodic health checks or daemon-like behaviour:

1. **Modular handlers** — one subsystem per handler, each over SSH, each timed out
   (10s). One failure must not block the others.
2. **Categorized statuses** — `healthy` / `unhealthy` / `unknown`. `unknown` means it
   could not connect; never auto-restart on `unknown`, only alert.
3. **Single JSON report per cycle** — one HealthReport object to
   `~/.hscc/daemon/status.json`; alerts appended to `~/.hscc/daemon/alerts.jsonl`
   (one JSON per line, append-only, never overwritten).
4. **Escalator logic** — reads the report and decides. Auto-restart ONLY the
   orchestrator container, max 1 per cycle; everything else routes to an alert.
5. **File-based state** — avoid a WebSocket dependency. CLI reads/writes
   `status.json` and `alerts.jsonl`; delivery is desktop notifications +
   `events.jsonl`, not chat.

See `references/health-check-daemon-pattern.md` for the full design, and
`references/image-corruption-case-2026-05-28.md` for a worked image-corruption case.

## Diagnostics

### Quick Checks

```bash
hscc cluster status          # workloads + idle hosts (pause-overlay aware)
hscc status                  # daemon/nodes/models/agents dashboard (different subject!)
launchctl list com.hermes.hscc_daemon
```

### Troubleshooting: Node Won't Serve vLLM

A node with a running sparkrun container whose vLLM is not answering (port
unreachable, no HTTP response). **Prefer the agent tools first** — `node_diagnostics`,
`vllm_logs` and `nas_diagnose` resolve hosts through sparkrun and are the sanctioned
path. The raw `ssh`/`docker` commands below are the manual fallback for when sparkrun
itself cannot reach the node.

**1. Is the container actually running vLLM, or just `sleep infinity`?**
```bash
ssh spark@<IP> "docker ps --format '{{.ID}} {{.Names}} {{.Status}}'"
ssh spark@<IP> "docker exec <container> ps aux | grep -i vllm"
```
Only `sleep infinity` → the vLLM command failed to launch and sparkrun fell back to
its health-check loop.

**2. `libtorch_cuda.so` / CPU-only PyTorch (the #1 cause):**
```bash
ssh spark@<IP> "docker run --rm --gpus all <image> python3 -c \"import torch; print(torch.__version__); print(torch.cuda.is_available())\""
```
Correct: `2.11.0+cu130` + `True`.
Wrong: `2.10.0+cpu` + `False` → this node has a CPU-only PyTorch and will crash on
`ImportError: libtorch_cuda.so: cannot open shared object file`.

**3. Does the image ID match a working node?**
```bash
ssh spark@<problem-node> "docker inspect <image> --format '{{.Id}}'"
ssh spark@<working-node> "docker inspect <image> --format '{{.Id}}'"
```
Different IDs → the problem node has a stale/broken local image. Copy the correct one
over the LAN (far faster than pulling from GHCR over the slow SSH link, ~40 min for 19GB):
```bash
ssh spark@192.0.2.11 "docker save sparkrun-eugr-vllm:latest | gzip -9" \
  | ssh spark@192.0.2.13 "gzip -dc | docker load"
```

**4. Get the actual crash from the logs:**
```bash
ssh spark@<IP> "docker logs <container> 2>&1 | tail -40"
ssh spark@<IP> "docker exec <container> cat /tmp/sparkrun_serve.log 2>&1 | tail -40"
```

**5. Launch vLLM by hand inside the container (for diagnosis only):**
```bash
ssh spark@<IP> "docker exec <container> python3 /usr/local/bin/vllm serve Qwen/Qwen3.6-35B-A3B-FP8 \
  --host 0.0.0.0 --port 8000 --max-model-len 262144 --max-num-batched-tokens 32768 \
  --trust-remote-code --gpu-memory-utilization 0.8 --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder --kv-cache-dtype fp8 --load-format instanttensor \
  --attention-backend flashinfer --enable-prefix-caching 2>&1"
```
This prints the real error instead of failing silently.

**6. Are the model weights present and readable?**
```bash
ssh spark@<IP> "docker exec <container> ls -la /cache/huggingface/hub/models--Qwen--Qwen3.6-35B-A3B-FP8/snapshots/"
```
