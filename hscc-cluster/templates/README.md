# Cluster templates

**Topology-free intent templates** (schema v2). A template describes *what* to
run, never *where* — no IPs, no ports. At apply time the engine resolves it
against the live sparkrun cluster (orchestrator → gateway node, families →
worker nodes) and auto-assigns vLLM ports (8000, 8001, …) + proxy ports
(4000, 4001, …). The same template works on a 2-node or a 40-node cluster.

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
- No `orchestrator_node`, `cluster_size`, `nodes:`, or `proxy.port` — those are
  legacy v1 keys and are rejected.

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
| `5node/` | coding | orch + 4× 27B-FP8 |
| `6node/` | coding · coding-plus-fast | orch + 5 workers (single- or two-family) |
| `7node/` | coding | orch + 6× 27B-FP8 |
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
