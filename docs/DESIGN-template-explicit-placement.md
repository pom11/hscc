# Explicit placement and routing in cluster templates

**Date:** 2026-08-07
**Status:** Approved, pending implementation

## Problem

A template describes a fleet layout, but says nothing about *where* anything runs.
Placement is inferred entirely by the resolver:

```yaml
orchestrator:
  recipe: "~/.sparkrun-local/recipes/local-fixed/deepseek-v4-fp8-scitrera-hscc.yaml"
  tp: 2                    # no node named
families:
  - name: reasoning
    workers: remaining     # no nodes named
    proxy: true
```

Two consequences, both observed in production on 2026-08-06/07:

1. **The inference is broken and there is no way around it.** `4node-dual-dsv4`
   fails resolution with `family 'reasoning': no available worker nodes
   (pool=[], claimed=['10.0.0.247'])` — and produces the identical error
   whether the target nodes are running or completely free. `apply
   --force-recreate` is therefore unusable, and activating the v1.6.0 logical
   aliases required bypassing `hscc` entirely and driving `sparkrun` by hand.
   Tracked separately as `t_16dcceb4`; this design routes *around* it rather
   than fixing it.
2. **Routing is invisible and drifts silently.** `delegation.model` and
   `fallback_providers[0]` currently point at the orchestrator
   (`10.0.0.244:8000`) rather than the worker proxy, contradicting HSCC's
   own documented intent that "orchestrator subagents run on the WORKER pool,
   not the gateway GPU". Every delegated subagent burns orchestrator GPU. No
   template expresses this, so nothing detects or corrects it.

A related failure of trust: `hscc template validate` is coupled to the resolver,
so the template *currently running the live fleet* fails its own validation. The
command cannot distinguish "this template is malformed" from "the resolver is
broken".

## Goals

- A template can state explicitly which nodes each unit occupies.
- A template can state explicitly which unit each consumer routes to.
- Anything a template does not state is left exactly as it is — apply never
  silently re-routes something tuned by hand.
- Conflicting or impossible placement is rejected *before* the fleet is touched.
- `validate` gives a trustworthy answer about the template itself, independent
  of live cluster state.

## Non-goals

- Fixing the `pool=[]` resolver bug (`t_16dcceb4`). Explicit placement bypasses
  the resolver; templates that omit `nodes:` still depend on it.
- Changing how aliases are advertised (shipped in v1.6.0/v1.6.1).
- Exposing `worker-model` on the sparkrun proxy. Required before routing
  `delegation` at the proxy with an alias-valued model; called out in Risks.

## Schema

All new keys are optional. `version: 3`; v2 templates parse unchanged and
produce byte-identical output.

```yaml
name: 4node-dual-dsv4
version: 3

orchestrator:
  recipe: "~/.sparkrun-local/recipes/local-fixed/deepseek-v4-fp8-scitrera-hscc.yaml"
  tp: 2
  nodes: [10.0.0.244, 10.0.0.246]     # NEW, optional

families:
  - name: reasoning
    nodes: [10.0.0.247, 10.0.0.248]   # NEW, optional
    allow_colocation: false                    # NEW, optional, default false
    proxy: true
    models:
      - recipe: "~/.sparkrun-local/recipes/local-fixed/deepseek-v4-fp8-scitrera-hscc.yaml"
        tp: 2

routing:                                       # NEW, optional, whole block
  delegation: family-reasoning
  compaction: orchestrator
  auxiliaries: orchestrator
```

### `nodes:`

A list of node IPs. When present the resolver's placement inference is
**bypassed** for that unit and the list is used verbatim. When absent, current
inference applies (`workers: remaining`).

For a multi-node span, order is significant: `nodes[0]` is the span **primary**
(it exposes the endpoint); the rest are tp peers. This matches
`serving_unit_scoreboard()` and keeps `/cluster`, self-heal and `ops.pick_node`
consistent with the template.

### `allow_colocation:`

Default `false`. When two units name the same node, apply blocks unless **both**
units set `allow_colocation: true`. When permitted, apply warns about VRAM
contention. This is the guard that would have prevented a solo container being
provisioned onto `.248` while `.248` was already a tp peer of the `.247` span.

### `routing:`

Maps a consumer to a **unit name**, not a URL. Valid targets: `orchestrator`, or
`family-<name>` for any family the template defines.

Recognised consumers, and the config keys each writes:

| consumer      | config keys written |
|---------------|---------------------|
| `delegation`  | `delegation.base_url`, `delegation.model` |
| `compaction`  | `auxiliary.compression.{base_url,model}` |
| `auxiliaries` | `auxiliary.<task>.{base_url,model}` for the text auxiliaries (`kanban_decomposer`, `triage_specifier`, `profile_describer`, `curator`, `title_generation`, `skills_hub`, `approval`, `mcp`) — deliberately **not** `vision`/`web_extract`, which need capability-specific providers |

Symbolic targets rather than URLs: the same indirection principle as the model
aliases — name the role, resolve the address at apply time. Moving a family to
different nodes then needs no template edit.

## Resolution and precedence

**Placement.** `nodes:` present → used verbatim, inference bypassed. Absent →
current resolver inference.

**Routing.** A target resolves to that unit's live endpoint: `orchestrator` → the
orchestrator's `host:port`; `family-<name>` → that family's proxy port when
`proxy: true`, else its primary node's endpoint. The model value written is the
unit's configured model id — which, post-v1.6.0, is normally the logical alias
(`orchestrator-model` / `worker-model`).

**Omission means "do not touch".** A `routing` key that is absent — or the whole
block being absent — means apply does **not write that config key at all**. The
live value in `~/.hermes/config.yaml` survives untouched. This is stricter than
the existing fill-empty behaviour: fill-empty writes when a value is blank,
whereas an omitted routing key is never written even if blank.

Rationale: apply must never silently re-route something an operator tuned by
hand. The cost is that drift persists (today's `delegation` regression would
survive an apply that omits `routing.delegation`) — accepted deliberately;
`hscc doctor` is the place to surface drift, not `apply`.

## Validation

Two independent layers, because they answer different questions and have
different dependencies.

**Layer 1 — structural.** Offline. No cluster state, no resolver.

- `version` recognised; unknown keys rejected (typo protection)
- every node in `nodes:` exists in `cluster.json`
- `len(nodes) == tp` for each unit that names nodes
- no node claimed by two units unless **both** set `allow_colocation: true`
- `routing` targets resolve to a unit the template defines
- recipe paths exist on disk

**Layer 2 — placement.** Requires live cluster state; uses the resolver.
Capacity, occupancy, availability — "can these units be placed right now".

With explicit `nodes:`, layer 2 becomes close to trivial: placement is declared
rather than inferred, so a v3 template validates deterministically instead of
depending on resolver health.

### CLI

`hscc template validate <name>` reports the layers separately:

```json
{
  "template": "4node-dual-dsv4",
  "structural": { "ok": true,  "errors": [], "warnings": [] },
  "placement":  { "ok": false, "errors": ["family 'reasoning': no available worker nodes (pool=[], …)"] },
  "ok": false
}
```

- `--structural-only` — skip layer 2. Usable in CI and when the cluster is down.
- `--json` — machine-readable (the shape above).
- Exit non-zero if either layer fails, so it scripts cleanly.

This makes the current confusing state legible: structural ✅ / placement ❌
points at the resolver bug instead of casting doubt on the template.

`apply` calls this same validation as its pre-flight gate — one implementation,
not two — and blocks before stopping or starting anything, as it correctly did
on 2026-08-07.

## Error handling

Every structural violation is a hard block naming the offending unit and value,
e.g.:

```
node '10.0.0.250' in family 'reasoning' is not defined in cluster.json
family 'reasoning': 3 nodes listed but tp=2
node '10.0.0.248' claimed by both 'orchestrator' and 'reasoning'
  (set allow_colocation: true on both to permit)
routing.delegation -> 'family-coding': no such family in this template
```

Colocation permitted via the flag emits a warning, not an error. Placement
failures are reported as-is from the resolver. Nothing is stopped or started
when validation fails.

## Testing

- v2 template produces byte-identical apply output to today (no regression)
- v3 with explicit `nodes:` resolves to exactly those spans — asserted both when
  the target nodes are **running** and when they are **free** (this is the case
  the current resolver gets wrong)
- `nodes[0]` is treated as span primary; the rest are tp peers, consistent with
  `serving_unit_scoreboard()`
- omitted `routing` key → the config key is **absent from the write set**
  (assert not-written, not merely unchanged — a value that happens to match
  must not count as a pass)
- present `routing` key → correct endpoint and model written for that consumer
- each structural error fires: unknown node, tp mismatch, unflagged collision,
  dangling routing target, unknown key
- `allow_colocation: true` on both units permits sharing and warns
- `--structural-only` returns layer 1 only and succeeds with the cluster
  unreachable
- fixture: `4node-dual-dsv4` with explicit nodes passes structural validation
  where the inferring version returns `pool=[]`

## Risks

- **Proxy does not advertise `worker-model`.** `localhost:4000` currently serves
  only the concrete id. Routing `delegation` at `family-reasoning` while the
  family's model value is the alias would write an id the proxy cannot resolve.
  Either expose the alias on the proxy first, or have routing write the concrete
  id when the target endpoint does not advertise the alias. Implementation must
  probe before writing, reusing `doctor._check_models_served` / `_models_url`.
- **Explicit placement can encode a stale layout.** A template naming nodes that
  have been repurposed fails structural validation — which is the intended
  outcome, but means templates need maintaining alongside the cluster.
- **Bypassing the resolver hides `t_16dcceb4`.** Templates that adopt explicit
  `nodes:` stop exercising the broken path. The resolver bug must still be fixed
  on its own merit.
