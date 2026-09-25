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
hscc template validate 4node-dual-dsv4
hscc template validate 4node-dual-dsv4 --structural-only  # offline / CI
hscc template validate 4node-dual-dsv4 --json             # machine-readable
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
| `4node/` | coding | orch + 3× 27B-FP8 |
| `4node/` | coding-plus-fast | coding (2× 27B) + fast (1× A3B-FP8), separate proxies |
| `4node/` | colocated-dual | 3 workers each running **2× A3B-FP8** (89.8 GB/GPU) |
| `4node/` | deepseek-v4-orchestrator · dsv4-plus-coding · dual-dsv4 | DSV4 spans — see the speed warning below |
| `8node/` | coding · coding-plus-fast | orch + 7 workers (single- or two-family) |

Plus the flat top-level: `single-family`, `colocated-two-models`, `hscc-live`.

### 2026-09 generation — registry-sourced, single-node strong tier

The DSV4 templates above decode at **9–14 tok/s**, and that is a property of
the layout, not of tuning: `@atlas/deepseek-v4-flash-nvfp4-ep2` records
"~15.5 tok/s (network/all-reduce bound over RoCE)" in its own metadata. A
cross-node EP/TP span pays an all-reduce on **every decoded token**. The
templates below keep the strong tier on a single node instead.

The recipes behind these came from the sparkrun **registries** (`@community/…`,
`@official/…`, `@eugr/…`) but are **pinned into `local-fixed/`**, and the
templates reference the pinned paths. A `sparkrun registry update` can change or
remove an upstream recipe underneath a running fleet, silently altering serve
flags; a pin means what the fleet runs changes only when someone edits it here on
purpose. Each pinned file carries its origin, source path and pin date in a
header — diff against the origin to re-sync deliberately, and put local fixes in
the pin, never in the registry copy.

`recipe_cost.recipe_exists()` still matters: it makes `@reg/name` tokens
resolvable at preflight (a plain `Path.is_file()` reports every one as missing),
so a template *can* reference a registry recipe directly when that is what you
want — it is just not how these seven are wired.

| Dir | Template | Orchestrator | Workers | Notes |
|-----|----------|--------------|---------|-------|
| `4node/` | **balanced** | Nemotron-3-Super 120B-A12B NVFP4+MTP, 23.6 tok/s | 2× Qwen3.8-27B-FP8 + 1× North-Mini-Code NVFP4 | **recommended default** |
| `4node/` | throughput | A3B-NVFP4, **116 tok/s** | 3× North-Mini-Code NVFP4 (~530 tok/s aggregate) | burst mode for atomic cards |
| `4node/` | quality | Qwen3.8-Flash-Next NVFP4, SWE-bench Pro **62.5** | 2× Qwen3.8-27B-FP8 + 1× North-Mini-Code NVFP4 | tightest VRAM of the seven |
| `4node/` | dual-orch | 2× Nemotron-3-Super 120B-A12B | 2× North-Mini-Code NVFP4 | one brain per project board |
| `4node/` | review-heavy | Nemotron-3-Super 120B-A12B | 1 coder + 2× Nemotron-3-Nano (88–100 tok/s) | sized for `auto_review` |
| `3node/` | balanced | Nemotron-3-Super 120B-A12B | 1 coder + 1 fast | degraded fleet / idle-autodown |
| `2node/` | fast | A3B-NVFP4, 116 tok/s | 1× North-Mini-Code NVFP4 | minimum viable pair |

**Container images are per-node local disk, not NAS — and none of these images
are on the fleet yet.** All seven templates run on
`ghcr.io/spark-arena/dgx-vllm-eugr-nightly:latest`, so they cost one pull per
node; `4node/quality` additionally builds `vllm-node-b12x` locally for its
orchestrator via the eugr recipe's `build_args: --exp-b12x`. No template uses
the atlas runtime. Watch disk: the gateway node was at **95% (44 GB free)** when
these were written.

**Check `max_nodes` and `max_batch_size` before putting a recipe in a family.**
A recipe declaring `Nodes: 1 - 1` cannot take `tp: 2` at all, and one declaring
`max_batch_size: 1` serializes behind a load-balanced proxy — `ModelIntent` has
no field to raise either. Both ruled out the otherwise-fastest coder recipe for
`4node/quality`; its header records what that cost.

**A recipe whose `container:` has no registry prefix and no `build_args` cannot
be used at all** — sparkrun can neither pull nor build it. That is what ruled
out the faster Qwen3.5-122B-A10B int4+MTP orchestrator (~50 tok/s, BFCL-V4 72.2,
the best open-weight tool-caller measured); its only correctly-tuned recipe
names `vllm-qwen35-v2:latest`, built by an external pipeline. Its weights are
staged on the NAS, so if that image is ever built it is a one-line upgrade in
four templates. Check `sparkrun show --no-vram -- <recipe> | grep Container:`
before adopting any recipe.

**Two constraints these encode, worth knowing before editing them:**

1. **The orchestrator must batch.** The live `~/.hermes/config.yaml` points
   *nine* consumers at `orchestrator-model` — the main chat plus
   `kanban_decomposer`, `triage_specifier`, `curator`, `profile_describer`,
   `title_generation`, `skills_hub`, `approval` and `mcp`. A recipe pinned to
   `max_num_seqs: 1` (several atlas ones are, deliberately) serializes that
   whole set and, on some, errors mid-decode at C>1.
2. **A template cannot force `tp: 1`.** `_render_serve_cmd` emits `--tp` only
   when `tp > 1`, so a recipe whose own default is `tensor_parallel: 2` will
   still try to span two nodes no matter what the template says. Every
   one-node-per-worker slot above uses a recipe whose own default is already 1.
   Check with `sparkrun recipe show <name>` before substituting.

A regression test (`tests/test_template_intent.py::test_all_shipped_templates_resolve_and_fit`)
resolves every node-count template against its N-node cluster with real recipe
costs — so a template that can't fit fails the suite.

## Use

```
hscc template list
hscc template validate 4node-coding
hscc template preview  4node-coding
hscc template apply    4node-coding --confirm
```

or the `/template` slash command. Templates resolve by their `name:` field or
filename stem (subdirs are searched).

## Compatibility

`version: 3` is **additive** — every new key (`nodes:`, `allow_colocation:`,
`routing:`) is optional, and an existing v2 template parses and applies
byte-identically to its v2 behaviour. Nothing is forced onto an old template.
