# Delegation routing and the probe-before-write guard

**Audience:** whoever changes worker-tier apply, the delegation block, or the
worker proxy contract. Extracted from `hscc-cluster/README.md` (R4, audit
§2.2) — the README carries a pointer here; the implementation lore lives here
and in the module docstrings.

Canonical code: `hscc-cluster/cluster_template.py` —
`_update_worker_model_ids` (rewrite), `_resolve_worker_model_id` and
`_routing_model_to_write` (the probe resolution), `_probe_served_models` +
`_models_probe_url` (the probe itself). Related spec:
`docs/DESIGN-template-explicit-placement.md` (v3 `routing:`) and
`docs/design/2026-10-15-template-declarations.md`.

## What the routing is for

Delegated subagents and the first fallback are supposed to run on the WORKER
pool, not the orchestrator GPU (`delegation.base_url` /
`fallback_providers[0]` → the `:4000` worker proxy; `model` → the served worker
id). Once a worker family exists in the plan, `_update_worker_model_ids` keeps
`model` **and** `base_url` in lockstep, so a worker-tier apply re-aims any
drifted `delegation.base_url` / `fallback_providers[].base_url` from the
orchestrator (`:8000`) back to the worker proxy. Without the base_url half, an
apply fixes the model id and leaves subagents silently still on the
orchestrator — the `:8000` routing regression this rule was written for.

## Two endpoints, two model registries (the 404 trap)

The `:4000` worker proxy is the sparkrun LiteLLM load-balancer that fronts the
worker span. It advertises **only the concrete model id** it was configured
with — it does NOT necessarily publish the stable `worker-model` alias. (The
alias *is* advertised, but by a DIFFERENT endpoint: a worker span's primary
vLLM — e.g. `10.0.0.x` in the placeholder convention — which registers both
the concrete id and `worker-model` via multi-name `--served-model-name`; see
`hscc-cluster/tests/test_apply.py`, `TestServedModelNameAliases`.) Those are
two endpoints with two different registries — do not conflate them. Writing the
alias where only the concrete id is served makes **every** delegated call 404.

## The rule: probe before you write

A worker-tier apply therefore derives `delegation.model` /
`fallback_providers[].model` from what the **proxy actually serves**:

1. probe the endpoint's `/v1/models` (`_probe_served_models`);
2. the probe candidate is the unit's stable logical alias (`worker-model`,
   via `_unit_alias`);
3. if the endpoint serves the candidate → write the candidate (alias or
   concrete — post-config-refresh the proxy does advertise the alias);
4. else if it serves exactly one id → write that concrete id;
5. else (unreachable, error, empty, or an ambiguous multi-id list) → **write
   nothing**. Blinding an apply mid-flight is safer than writing an id every
   delegated call 404s on; `apply` reports the refusal and leaves
   delegation/fallback untouched.

The same posture governs the v3 `routing:` block (`_routing_model_to_write`):
a present routing key is the sole authority for its consumer, and it is still
probe-resolved before any write. When a template carries `routing:`, the
delegation/fallback rewrite is skipped (`skip_delegation`) because routing owns
that consumer — but the worker *role profile* rewrite still runs, since
routing has no `profiles` consumer and cannot express that concern.

## One probe contract for the whole fleet

The probe machinery (correct `/v1/models` path + Bearer auth) mirrors
`hscc-bootstrap/doctor.py`'s `_probe_served_models` / `_models_url`. It is
deliberately NOT a cross-plugin import: each HSCC plugin deploys standalone
into `~/.hermes/plugins` and hyphenated dirs are not importable package names,
so `hscc-cluster` cannot `import doctor` from `hscc-bootstrap`. Keep the two
byte-compatible so both stay in lockstep.

## Tests that pin this

`hscc-cluster/tests/test_apply.py` — the probe-before-write cases (alias
served / only-concrete served / ambiguous / unreachable → refuse), the
`skip_delegation` + `routing:` interaction, and the argv-level
`--served-model-name` alias tests. Run: `python -m pytest tests/ -q`.
