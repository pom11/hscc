# hscc-cluster

The cluster-control plugin: the **orchestrator-only** toolset Hermes uses to see
and shape the DGX Spark cluster, plus the topology-free template engine and the
agentic work-flow helpers.

> Loaded by Hermes via `register(ctx)` in `__init__.py`. Worker roles never get
> this toolset — only the orchestrator can change cluster shape.

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

## Discovery — `discovery.py`
The keystone. `discover()` resolves topology **live → cache → fail-loud** (no
silent fake-IP fallback). Idle is classified by **power draw** (≈<15 W on GB10),
not util% (which reads ~96% idle). New sparkrun nodes are **auto-adopted**.

## Templates — topology-free intent (`template_intent.py`, `templates/`)
Templates describe **intent only** — no IPs, no ports. At apply, `resolve()` maps
the template onto the live cluster (orchestrator→gateway, `workers: all|N|remaining`
→ real nodes) and auto-assigns ports. See [`templates/README.md`](templates/README.md)
for the format and the shipped node-count library.

`recipe_cost.py` parses `sparkrun show`'s VRAM Estimation so placement only
proposes layouts that **actually fit** (incl. 2 models co-located on one node).

## Apply pipeline — `cluster_template.py`
`load v2 yaml → resolve(discover()) → validate_resolved_plan → write
serving.json/models.json/config.yaml + proxies + provision`. Transactional:
snapshots state to `~/.hscc/rollback/<ts>/` and **auto-rolls-back** on a failed
apply. Records the active template in `~/.hscc/applied_template.json`.

CLI: `hscc.py cluster-template <list|status|validate|preview|apply> [name] [--confirm]`.

## Work-flows — `workflow.py`
The idempotent-resume probe (`probe_task_state`) + the `kanban_task_claimed` hook
handler that posts a "resume, don't redo" note to a re-dispatched worker, built
from its task branch's committed state.

## Delegation routing — `_update_worker_model_ids`
Delegated subagents and the first fallback are supposed to run on the WORKER
pool, not the orchestrator GPU (`delegation.base_url`/`fallback_providers[0]`
→ the `:4000` worker proxy; `model` → the served worker id). Once a worker
family exists in the plan, `_update_worker_model_ids` keeps `model` **and
`base_url`** in lockstep, so a worker-tier apply re-aims any drifted
`delegation.base_url` / `fallback_providers[].base_url` from the orchestrator
(`:8000`) back to the worker proxy.

**Probe-before-write (the 404 guard).** The `:4000` worker proxy is the sparkrun
LiteLLM load-balancer that fronts the worker span. It advertises **only the
CONCRETE id** (e.g. `deepseek-ai/DeepSeek-V4-Flash-0731`) — it does NOT publish
the stable `worker-model` alias. (The alias IS advertised, but by a DIFFERENT
endpoint: the worker span's primary vLLM on the `.247` node, which registers
both the concrete id and `worker-model` via multi-name `--served-model-name`.
Those are two endpoints with two different model registries; do not conflate
them.) A worker-tier apply therefore derives `delegation.model` /
`fallback_providers[].model` from what the **proxy actually serves**: it probes
the proxy's `/v1/models` and writes the declared model id when the proxy serves
it, else the CONCRETE id the proxy serves. If a template ever declares the
alias `worker-model` as the worker unit model, an apply still writes the
concrete id the proxy resolves — never an alias the endpoint would 404 on. If
the probe fails or finds no resolvable id, nothing is written (blinding an
apply mid-flight is safer than writing an id every delegated call 404s on).

The probe machinery (correct `/v1/models` path + Bearer auth) mirrors
`hscc-bootstrap/doctor.py`'s `_probe_served_models` / `_models_url` so the
fleet shares one probe contract across the standalone plugins.

**Tests**
`tests/` — run: `python -m pytest tests/ -q`. Integration tests assert
generated files / real git, not mocks.
