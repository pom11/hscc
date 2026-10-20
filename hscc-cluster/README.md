# hscc-cluster

The cluster-control plugin: the **orchestrator-only** toolset Hermes uses to see
and shape the DGX Spark cluster, plus the topology-free template engine and the
agentic work-flow helpers.

> Loaded by Hermes via `register(ctx)` in `__init__.py`. Worker roles never get
> this toolset — only the orchestrator can change cluster shape.

## When you touch it

New cluster/health tools, the template intent format, or the apply pipeline.
The template **format reference** is
[`templates/README.md`](templates/README.md), not this file.

## Tools (registered into the `hscc-cluster` toolset)

| Tool | What |
|------|------|
| `discovery_status` | Single source of truth for live topology (orchestrator/workers/NAS, `source: live\|cache`); `probe=true` adds per-node VRAM / power-draw idle / vLLM health. |
| `nas_status` | NAS node + a single mount probe (no fan-out — honors the NAS staging limit). |
| `cluster_status` | Running models, idle nodes, serving units (+ free-VRAM when probed). |
| `list_recipes` / `pick_node` | Browse sparkrun recipes / choose an idle worker. |
| `provision_model` / `stop_model` / `model_health` | Model lifecycle (guarded: `confirm=true`). Correct sparkrun invocation (`--cluster/--port/--ensure`). |
| `vllm_logs` / `node_diagnostics` / `nas_diagnose` | Debug a node / NAS. |
| `restart_model` / `remount_nas` / `repair_nas_export` / `reap_orphans` | Self-heal (guarded). |

## Modules

- `discovery.py` — the keystone. `discover()` resolves topology **live → cache →
  fail-loud** (no silent fake-IP fallback). Idle is classified by **power draw**
  (≈<15 W on GB10), not util% (which reads ~96% idle). New sparkrun nodes are
  **auto-adopted**.
- `template_intent.py` + `templates/` — templates describe **intent only** (no
  IPs, no ports); at apply, `resolve()` maps them onto the live cluster
  (orchestrator→gateway, `workers: all|N|remaining` → real nodes) and
  auto-assigns ports. `recipe_cost.py` parses `sparkrun show`'s VRAM Estimation
  so placement only proposes layouts that **actually fit** (incl. 2 models
  co-located on one node).
- `cluster_template.py` — the apply pipeline: load yaml → `resolve(discover())`
  → `validate_resolved_plan` → write `serving.json`/`models.json`/`config.yaml`
  + proxies + provision. Transactional: snapshots to `~/.hscc/rollback/<ts>/`
  and **auto-rolls-back** on a failed apply; records the active template in
  `~/.hscc/applied_template.json`. Worker-facing model ids are
  **probe-before-write** — see
  [`docs/DESIGN-probe-before-write.md`](../docs/DESIGN-probe-before-write.md).
- `workflow.py` — the idempotent-resume probe (`probe_task_state`) + the
  `kanban_task_claimed` hook that posts a "resume, don't redo" note to a
  re-dispatched worker, built from its task branch's committed state.

## CLI

Merged into the main `hscc` CLI (this plugin also exposes the same engine as a
library the merged CLI imports):
- `hscc template <list|status|validate|preview|apply> [name] [--confirm]`
- `hscc cluster <status|hosts|monitor|jobs|info|stop|down|up>`

## Tests

`tests/` — run: `python -m pytest tests/ -q`. Integration tests assert
generated files / real git, not mocks.
