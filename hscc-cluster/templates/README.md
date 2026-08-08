# Cluster templates

**Topology-free intent templates** (schema v2, extended by v3). A template
describes *what* to run; by default it says nothing about *where*. At apply time
the engine resolves it against the live sparkrun cluster (orchestrator →
gateway node, families → worker nodes) and auto-assigns vLLM ports (8000, 8001,
…) + proxy ports (4000, 4001, …). The same template works on a 2-node or a
40-node cluster.

Since **v1.7.0** (schema **v3**) a template may also state placement and routing
explicitly — `nodes:`, `allow_colocation:`, and `routing:`. All new keys are
optional, so v2 templates parse and apply **byte-identically**. Full spec:
[`docs/DESIGN-template-explicit-placement.md`](../../docs/DESIGN-template-explicit-placement.md).

## Format

```yaml
name: my-cluster
version: 2
description: "one line"
orchestrator:
  recipe: "~/.sparkrun-local/recipes/local-fixed/qwen3.6-35b-a3b-nvfp4-vllm.yaml"
families:
  - name: coding              # a named group of workers + its own proxy
    models:
      - recipe: "…/qwen3.6-27b-fp8-vllm.yaml"   # 1 model = same model on every worker
    workers: all              # all | <N> | remaining
    proxy: true               # port auto-assigned (4000, 4001, …)
```

- **`workers`**: `all` (every worker), `<N>` (first N), `remaining` (those not
  claimed by an earlier family).
- **Two models in one family's `models:`** → co-located on EACH worker, on
  distinct ports (8000/8001). Their combined per-GPU VRAM must fit one Spark —
  checked via `sparkrun show` at resolve (refused otherwise).
- **`tp > 1`** models occupy their node(s) exclusively (can't co-locate).
- Topology-free default: no `orchestrator_node`, `cluster_size`, or `proxy.port`
  — those are legacy v1 keys and are rejected.

## v3: explicit placement and routing

### `nodes:` (per unit — optional)

A list of node IPs. When present the resolver's placement inference is
**bypassed** for that unit and the list is used verbatim. When absent, inference
applies (`workers: remaining`).

For a multi-node span, order is significant: `nodes[0]` is the span **primary**
(it exposes the endpoint); the rest are tp peers — consistent with
`serving_unit_scoreboard()` and `ops.pick_node`.

**The trade-off, honestly:** explicit `nodes:` make placement deterministic, but
they hardcode a specific cluster — a template that names nodes fails structural
validation the moment those nodes are repurposed. The **shipped templates
deliberately omit `nodes:`** and stay portable, resolved purely from intent.

### `allow_colocation:` (per unit — optional, default `false`)

When two units name the same node, apply **blocks** (naming both units) unless
*both* units set `allow_colocation: true`. When permitted, apply warns about
VRAM contention. This is the guard that would have stopped a solo container
being provisioned onto a node already serving as a tp peer.

### `routing:` (whole block — optional)

Maps a **consumer** to a **symbolic unit name** (`orchestrator`, or
`family-<name>` for any family the template defines). The target resolves to
that unit's live endpoint at apply time — the same indirection principle as the
model aliases: name the role, resolve the address at apply. Moving a family to
different nodes needs no template edit.

| consumer      | config keys written |
|---------------|---------------------|
| `delegation`  | `delegation.base_url`, `delegation.model` |
| `compaction`  | `auxiliary.compression.{base_url,model}` |
| `auxiliaries` | `auxiliary.<task>.{base_url,model}` for the **8 text** auxiliaries (`kanban_decomposer`, `triage_specifier`, `profile_describer`, `curator`, `title_generation`, `skills_hub`, `approval`, `mcp`) — deliberately **not** `vision`/`web_extract`, which need capability-specific providers |

**Probe-before-write:** an id is never written to an endpoint that does not
advertise it (reuses `doctor._check_models_served` / `_models_url`); the proxy
advertises the model aliases alongside the concrete id.

**KEY SEMANTIC — omission means do-not-touch.** An absent `routing` key (or an
absent block) means apply does **not write that config key at all** — the live
value in `~/.hermes/config.yaml` survives untouched. This is stricter than the
old fill-empty behaviour (fill-empty wrote when a value was blank; an omitted
routing key is never written even if blank). Apply never silently re-routes
something tuned by hand — `hscc doctor` is where drift should be surfaced.

```yaml
name: 4node-dual-dsv4
version: 3

orchestrator:
  recipe: "~/.sparkrun-local/recipes/local-fixed/deepseek-v4-fp8-scitrera-hscc.yaml"
  tp: 2
  nodes: [10.0.0.244, 10.0.0.246]     # optional explicit span

families:
  - name: reasoning
    nodes: [10.0.0.247, 10.0.0.248]   # optional explicit span
    allow_colocation: false                    # optional, default false
    proxy: true
    models:
      - recipe: "~/.sparkrun-local/recipes/local-fixed/deepseek-v4-fp8-scitrera-hscc.yaml"
        tp: 2

routing:                                       # optional, whole block
  delegation: family-reasoning
  compaction: orchestrator
  auxiliaries: orchestrator
```

## Validation — two layers

`validate` answers two different questions and reports them separately:

- **Layer 1 · structural** — *offline*. No cluster state, no resolver. Checks
  the template file alone: `version` recognised, unknown keys rejected (typo
  protection), every `nodes:` entry exists in `cluster.json`, `len(nodes) == tp`,
  no unflagged colocation, `routing` targets resolve to a unit the template
  defines, recipe paths exist.
- **Layer 2 · placement** — *live*. Uses the resolver: capacity, occupancy,
  availability ("can these units be placed right now"). With explicit `nodes:`
  this becomes near-trivial, so a v3 template validates deterministically
  instead of depending on resolver health.

```
hscc-cluster cluster-template validate 4node-dual-dsv4
hscc-cluster cluster-template validate 4node-dual-dsv4 --structural-only  # offline / CI
hscc-cluster cluster-template validate 4node-dual-dsv4 --json             # machine-readable
```

`--structural-only` skips layer 2 — usable when the cluster is down or in CI.
The result is already returned in a machine-readable JSON shape (reported under
two keys, `structural` and `placement`, plus a top-level `ok`). A **non-zero
exit** means either layer failed, so `validate` scripts cleanly.

`apply` runs the *same* validation as its pre-flight gate — one implementation,
not two — and **blocks before stopping or starting anything** when it fails.
Every structural violation is a hard block naming the offending unit, e.g.:

```
node '10.0.0.250' in family 'reasoning' is not defined in cluster.json
family 'reasoning': 2 nodes listed but model tp=3
node '10.0.0.248' claimed by both 'reasoning' and 'coding'
  (set allow_colocation: true on both to permit)
routing.delegation -> 'family-coding': no such family in this template
```

Colocation permitted via the flag emits a **warning**, not an error. Structural
✅ / placement ❌ is a legible result that distinguishes "malformed template"
from "broken resolver".

## Shipped library — verified against real `sparkrun show` VRAM

Per-GPU cost of the local-fixed recipes (DGX Spark ≈110 GB usable):
A3B-NVFP4 orch **31.82 GB** · A3B-FP8 **44.89 GB** · 27B-FP8 **60.75 GB** ·
nemotron-550b **64 GB (tp=4, needs 4 nodes)**.

| Dir | Template | Layout |
|-----|----------|--------|
| `1node/` | orchestrator-only | orch only, no workers |
| `2node/` | coding | orch + 1× 27B-FP8 |
| `3node/` | coding | orch + 2× 27B-FP8 |
| `4node/` | **coding** | orch + 3× 27B-FP8 — **the live setup** |
| `4node/` | coding-plus-fast | coding (2× 27B) + fast (1× A3B-FP8), separate proxies |
| `4node/` | colocated-dual | 3 workers each running **2× A3B-FP8** (89.8 GB/GPU) |
| `8node/` | coding · coding-plus-fast | orch + 7 workers (single- or two-family) |

Plus the flat top-level: `single-family`, `colocated-two-models`, `hscc-live`.

A regression test (`tests/test_template_intent.py::test_all_shipped_templates_resolve_and_fit`)
resolves every node-count template against its N-node cluster with real recipe
costs — so a template that can't fit fails the suite.

## Use

```
hscc-cluster cluster-template list
hscc-cluster cluster-template validate 4node-coding
hscc-cluster cluster-template preview  4node-coding
hscc-cluster cluster-template apply    4node-coding --confirm
```

or the `/template` slash command. Templates resolve by their `name:` field or
filename stem (subdirs are searched).

## Compatibility

`version: 3` is **additive** — every new key (`nodes:`, `allow_colocation:`,
`routing:`) is optional, and an existing v2 template parses and applies
byte-identically to its v2 behaviour. Nothing is forced onto an old template.
