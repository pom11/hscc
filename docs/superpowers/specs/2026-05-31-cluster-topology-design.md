# Cluster Serving-Topology Design (`serving.json`)

**Date:** 2026-05-31
**Status:** Approved design — ready for implementation plan

## Problem

The DGX Spark cluster runs a model split: orchestrator vLLM on gateway `.244`,
worker GPUs on `.246/.247/.248`. The intended split is defeated in two ways, and
the topology is encoded in scattered hardcoded constants rather than one
declarative source.

1. **Routing gap (fixed interim, formalized here):** dispatched tasks had no
   `assignedAgent` → `resolve_profile` returned `default` → worker inference ran
   on the orchestrator `.244`, not a worker node. An interim round-robin
   (`pick_worker_agent`) routes via the `agents.json` roster, but topology still
   lives in code constants (`PRIMARY_NODE`, `WORKER_VLLM_RECIPE`).

2. **No declarative topology:** "1 orchestrator + 3 same-model workers" is
   implicit. Re-mapping (e.g. orchestrator across 2 nodes + workers on 2) means
   editing code. Concurrent agent capacity is implicit (one agent ≈ one node).

**Goal:** a single declarative serving-topology file that (a) locks the current
config, (b) lets the layout be re-mapped by editing data not code, and (c) lets
arbitrary N concurrent agents run on available worker models with explicit
per-unit capacity.

## Approach

**Approach A — dedicated `~/.hscc/serving.json`** (chosen). A new file is the
single source of truth for *serving topology* (what model runs where, how many
concurrent workers each can sustain), distinct from `cluster.json` (hardware
roles) and `models.json` (model registry). The daemon and the agent-coordinator
both read it; both fall back to today's hardcoded behavior if it is absent.

Rejected: overloading `cluster.json`/`models.json` (mixes hardware/registry
concerns with serving state); inferring topology from live `provision.json`
(describes what *is* running, not desired state — can't express the lock).

## Section 1 — `serving.json` schema

`~/.hscc/serving.json`:

```json
{
  "version": 1,
  "port": 8000,
  "units": [
    {
      "id": "orch-244",
      "role": "orchestrator",
      "model": "Qwen/Qwen3.6-35B-A3B-FP8",
      "recipe": "~/.sparkrun-local/recipes/local-fixed/qwen3.6-35b-a3b-fp8-vllm.yaml",
      "nodes": ["192.0.2.10"]
    },
    {
      "id": "worker-246",
      "role": "worker",
      "model": "Qwen/Qwen3.6-35B-A3B-FP8",
      "recipe": "~/.sparkrun-local/recipes/local-fixed/qwen3.6-35b-a3b-fp8-vllm.yaml",
      "nodes": ["192.0.2.11"],
      "max_workers": 4
    },
    {
      "id": "worker-247",
      "role": "worker",
      "model": "Qwen/Qwen3.6-35B-A3B-FP8",
      "recipe": "~/.sparkrun-local/recipes/local-fixed/qwen3.6-35b-a3b-fp8-vllm.yaml",
      "nodes": ["192.0.2.12"],
      "max_workers": 4
    },
    {
      "id": "worker-248",
      "role": "worker",
      "model": "Qwen/Qwen3.6-35B-A3B-FP8",
      "recipe": "~/.sparkrun-local/recipes/local-fixed/qwen3.6-35b-a3b-fp8-vllm.yaml",
      "nodes": ["192.0.2.13"],
      "max_workers": 4
    }
  ]
}
```

**Unit fields:**
- `id` — stable identifier. Convention: octet matches head node (`worker-246`
  → head `.246`). Cleans the earlier id/node mismatch.
- `role` — `orchestrator` | `worker`.
- `model` — HF model id served by the unit.
- `recipe` — sparkrun recipe path used to launch the unit's vLLM.
- `nodes[]` — one node = solo serve; **two or more = sparkrun multi-node serving
  of one model as a single endpoint** (distributed tp/pp). Head node = `nodes[0]`.
- `max_workers` — **worker units only.** Concurrent task cap the node can sustain.

**Derived, not stored:**
- `endpoint` = `http://<nodes[0]>:<port>/v1`.
- worker `profile` = `worker-<head-octet>` (reuses existing on-disk Hermes
  profiles `worker-246/247/248`).

## Section 2 — Reconcile policy (daemon reads `serving.json`)

The daemon reconciles toward desired state each tick (every 300s, existing
cadence):

- **Orchestrator units always up:** `sparkrun --ensure`; restart if unhealthy.
- **Reaper exempt set** = union of all nodes in `role==orchestrator` units.
  Worker-unit nodes remain reapable (cold/on-demand; reaped when idle — keeps
  current "never run idle containers" policy).
- **Daemon never proactively starts worker units.** Workers come up on demand at
  dispatch (Section 3), not on the daemon tick.
- **base_url auto-update (chosen over explicit apply):** when the orchestrator
  unit's head endpoint changes, the daemon **auto-updates** each worker profile's
  `config.yaml` `model.base_url`. Validate the new endpoint (`/v1/models` → 200)
  before writing; skip + loud log on failure (keep last good value).
- **Fallback:** `serving.json` missing/invalid → daemon falls back to current
  hardcoded behavior (`PRIMARY_NODE`, orchestrator recipe) + loud WARN log.

## Section 3 — Routing + capacity (coordinator reads `serving.json`)

Replaces interim `pick_worker_agent()` (roster-based) with topology-based,
capacity-greedy scheduling. Drops the `agents.json` roster dependence for routing
(agents remain as identities; node routing comes from topology).

**`pick_worker_unit()` — capacity-greedy (least-loaded):**
- Read `role==worker` units from `serving.json`.
- Per-unit free slots = `max_workers - _unit_load[unit_id]` (live load tracked in
  `bridge.json` `_unit_load` map).
- Pick the unit with the **most free slots** (ties → lowest octet, deterministic).
  Self-balances: 3 units cap-4, 5 tasks → 2/2/1, never 4/1/0.
- **Cold units are eligible.** A unit with capacity whose vLLM is down is still
  picked; dispatch triggers provisioning and **waits for spin** — the task sits
  BLOCKED while the 35B loads (~minutes), then releases onto it.
- All units `free == 0` → `None` → task queued BLOCKED; retried when a slot frees.

**Routing chain (per dispatched task):**
1. `pick_worker_unit()` → unit (e.g. `worker-247`, head `.247`).
2. profile = `worker-<head-octet>` (existing on-disk profile, reused).
3. `ensure_worker_vllm(head_host, recipe, wait=True)` using the **unit's own
   recipe** (not a hardcoded constant). Waits for the node's vLLM to be healthy.
4. Increment `_unit_load[unit_id]`.

**Capacity decrement:** on green-check / cancel / reap, `_unit_load[unit_id] -= 1`
so freed slots reopen for queued tasks.

**Dynamic capacity:** capacity is `max_workers` per unit vs live load. Add a node
or bump `max_workers` in `serving.json` → more slots immediately, no code change.

## Section 4 — Lock + migration

**Step 1 — Lock:** write the current live state to `serving.json` (the schema in
Section 1: orch-244 + worker-246/247/248, `max_workers: 4` default, tune per node).

**Step 2 — Daemon (`hscc-daemon/hscc.py`):**
- `resolve_cluster_config()` also loads `serving.json`.
- Orchestrator node set = union of `nodes` from `role==orchestrator` units →
  replaces hardcoded `PRIMARY_NODE`.
- Reaper exempt set = orchestrator nodes.
- Per tick: orchestrator units `--ensure`; auto-update worker base_url on change
  (Section 2).
- Fallback to hardcoded behavior if `serving.json` absent/invalid + loud log.

**Step 3 — Coordinator (`hscc-agent-coordinator/hscc.py`):**
- `pick_worker_unit()` + per-unit recipe replace `WORKER_VLLM_RECIPE` constant
  and `pick_worker_agent()`.
- `ensure_worker_vllm` takes a `recipe` arg sourced from the unit.
- Fallback: no `serving.json` → keep current round-robin-on-agents behavior.

**Step 4 — Re-map later (data-only):** to put the orchestrator on 2 nodes, edit
`serving.json`: set the orch unit `nodes: ["192.0.2.10","192.0.2.11"]`
(sparkrun serves one model across both, single endpoint) and drop those nodes
from worker units. Daemon reconciles next tick. No code change.

**Dual-layout rule:** every edit to an active plugin
(`~/.hermes/plugins/<p>/`) is mirrored to its template
(`~/.hermes/plugins/install/hscc-plugins/<p>/`).

## Section 5 — Error handling + testing

**Error handling:**

| Failure | Behavior |
|---|---|
| `serving.json` missing/malformed | Fallback to current hardcoded behavior + loud WARN. Never crash. |
| Unit `recipe` bad / sparkrun launch fails | `ensure_worker_vllm` returns false → task stays BLOCKED, logged, slot not consumed. No worker into dead endpoint. |
| Cold spin times out (>180s) | `release-task` aborts unblock, logs, task stays BLOCKED for retry; decrement `_unit_load`. |
| All worker units at cap | `pick_worker_unit` → None → task queued BLOCKED, picked up when a slot frees. |
| `_unit_load` drift (worker died uncounted) | Reaper reconciles: recount live kanban cards per unit, rewrite `_unit_load` from truth. Self-heals. |
| Orchestrator node down | Daemon `--ensure` restarts per tick (topology-driven). |
| base_url auto-update bad value | Validate endpoint (`/v1/models` 200) before writing config.yaml; skip + log on fail. |

**Testing plan:**

1. Unit — `pick_worker_unit()`: most-free-capacity pick, tie→lowest octet,
   all-full→None.
2. Unit — `serving.json` parse: valid→units; missing→fallback flag;
   malformed→fallback + WARN.
3. Unit — capacity accounting: dispatch increments; green-check/cancel/reap
   decrements; reaper reconcile rewrites from live cards.
4. Integration — cold spin: worker node down, dispatch → `ensure_worker_vllm`
   brings vLLM up, `/v1/models` 200, task releases onto it; idempotent 2nd call.
5. Integration — base_url auto-update: change orch head in `serving.json` →
   daemon rewrites worker profiles' base_url; bad endpoint → skip+log.
6. E2E — the lock: write `serving.json` (current state), dispatch 5 tasks across
   3 cap-4 units → 2/2/1 spread; each worker inference hits its node
   (.246/.247/.248), NOT orchestrator .244 (verify via worker container logs).
7. E2E — re-map: edit `serving.json` to orchestrator on 2 nodes → daemon
   reconciles, sparkrun serves one model across both, single endpoint, no code
   change.
8. Fallback safety: delete `serving.json` → daemon + coordinator revert to
   today's behavior, no crash.

## Constraints honored

- No sparkrun recipe edits; no Hermes-core patches (`~/.hermes/hermes-agent/`).
- No gateway restart until explicitly authorized.
- Secrets untouched; no commits unless asked.
- `serving.json` fallback makes the whole feature a reversible toggle (write to
  enable, delete to revert).
- Risky action = provisioning worker containers; additive, idempotent, gated
  behind explicit dispatch/release.
