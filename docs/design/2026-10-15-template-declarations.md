# Template declarations: endpoints, proxy routing, and caps

**Date:** 2026-10-16
**Status:** Proposed (spec only — no code in this card)
**Extends:** `docs/DESIGN-template-explicit-placement.md` (schema v3 `routing:`),
`docs/ROADMAP.md` milestones `template-routing` and `template-engine`.

## This doc is the roadmap item

The operator's "bootstrap/template migration item — caps, heal tuning, etc. all
live in the template" does not exist as one document today. Its pieces are
scattered:

| Declared thing | Lives in code today | Where it is recorded |
|---|---|---|
| Placement + consumer routing | `routing:` block, schema v3 | `DESIGN-template-explicit-placement.md`; writer `_apply_routing` (`cluster_template.py:2020+`) |
| Kanban caps | hardcoded `MAX_IN_PROGRESS=30` / `MAX_IN_PROGRESS_PER_PROFILE=10` env-defaults, fill-if-absent (`hscc-bootstrap/enable_plugins.py:81-82,276-287`) | nowhere — live fleet runs 6/2 by hand-edit (`config.yaml.bak-cap6-20261015`) |
| Heal tuning | env-only constants: `HSCC_WORKER_AUTOHEAL_DEBOUNCE` / `_COOLDOWN_MINUTES` / `_LOAD_GRACE` (`hscc_daemon/health.py:80-95`), `HSCC_PROXY_RELAUNCH_COOLDOWN_S` (`health.py:642`) | rationale in `docs/audits/HEAL_CASCADE_2026-10-14.md` |
| Proxy balancer / router settings | nothing in hscc; hand-edited `~/.config/sparkrun/proxy.yaml` | this section |
| Hermes endpoints (where hermes talks) | computed ad hoc in the apply writer | `t_ac795424` (branch `wt/t_ac795424` @ `c450d966`) |

The converging decision: **the template is the declaration, apply is the
writer.** This spec adds the routing sections (`endpoints:`, `proxy:`, caps) to
that umbrella. Heal tuning is deliberately deferred (see Non-goals) — the
heal-cascade audit is still landing fixes and freezing its knobs now would
spec values that are still moving.

## Problem

Under a `uniform_pool` template, apply still *computes* routing in code instead
of reading a declaration: `chat_url` is inferred at `cluster_template.py:1613-
1617`, `custom`/`family-*` provider entries are crafted in one stretch
(`:1645-1659`), delegation/fallback re-aiming lives in
`_set_worker_model_in_config` (`:1846-1874`). The `t_ac795424` fix made the
result correct (nothing names a fleet node under uniform_pool); it did not make
it declarative. Meanwhile the LiteLLM balancer — the thing that actually
decides which node serves each request — is configured by hand-editing
`~/.config/sparkrun/proxy.yaml`, outside every template, so nothing validates
or doctor-checks it, and `monitor --live` shows its absence directly (one node
sits at 0 running while another queues).

## Goals

1. `endpoints:` — the template declares where hermes talks. Under
   `uniform_pool` everything is the family proxy **by alias**, never
   `node:port`. Apply writes exactly what is declared.
2. `proxy:` — the template declares the load-balancer settings. Apply writes
   them to `~/.config/sparkrun/proxy.yaml` **through sparkrun**, never into the
   regenerated `litellm_config.yaml`.
3. Caps — kanban `max_in_progress` / `max_in_progress_per_profile` become
   declared template fields with a derivation from declared per-backend
   parallelism.
4. Validate rejects incoherent declarations; doctor proves the live state
   agrees with them.
5. Templates without the new blocks behave **byte-identically** to today.

## Non-goals

- Heal tuning in the template (deferred — see the roadmap-item table).
- Any change to sparkrun, `~/.config/sparkrun/proxy.yaml` handling beyond the
  declared write path, or the live hermes configs. The sparkrun-side hooks
  (§4.3) are the operator's own patches, spec'd here as contracts.
- Per-family balancer policies. The shipped balancer toggle is global (§2.2);
  the spec does not invent a per-family mechanism.

## Live state this spec is written against (2026-10-15/16, operator-verified)

- Live sparkrun = fork branch `feat/proxy-least-in-flight` (`609351e0` on
  `5b0f9b5e`): top-level `router_settings:` from proxy.yaml is emitted
  verbatim; `gateways.litellm.balancing: least-in-flight` installs the
  `CustomLogger` balancer (`src/sparkrun/proxy/balancer.py`), copied to
  `~/.cache/sparkrun/proxy/sparkrun_balancer.py` and wired via
  `litellm_settings.callbacks: [sparkrun_balancer.handler]`.
- Live `~/.config/sparkrun/proxy.yaml` carries exactly
  `aliases: {}` + `router_settings: {routing_strategy: simple-shuffle}` +
  `gateways: {litellm: {balancing: least-in-flight}}`. Balancer proof: 4 fake
  backends → 2/2/2/2 per wave; 1-slow-backend soak 224 completed vs 52 under
  shuffle; post-switch cluster 2/3/2/2 including waiting.
- LiteLLM is **1.82.6**, pinned by sparkrun's launch argv
  (`uvx --from 'litellm[proxy]==1.82.6'`, `proxy/engine.py:359-361`). All
  litellm behavior claims below were verified read-only against 1.82.6.
- `t_ac795424` (branch `wt/t_ac795424`): uniform_pool apply writes
  `chat_url` = the family proxy URL for `model.base_url` and the `custom`
  provider, with the probe-resolved alias as `model.default`;
  `apply` records `uniform_pool` in `~/.hscc/applied_template.json`; verify
  check `provider_routing` fails if any fleet node appears in any hermes
  config while the flag is live.

---

## 1. `endpoints:` — where hermes talks

### 1.1 Schema

New optional top-level block, schema `version: 4` (see §1.6):

```yaml
endpoints:
  mode: proxy-alias          # proxy-alias | explicit
  chat: family-coding        # target for model.base_url + custom provider
  delegation: family-coding  # target for delegation.* + fallback_providers
  profiles:                  # role-profile → target
    default: family-coding   # any profile whose role is not listed
    "*-orch": family-coding  # orchestrator-role profiles
```

Targets are the same symbolic vocabulary `routing:` already uses
(`orchestrator`, `family-<name>`; resolution `_routing_target_endpoint`,
`cluster_template.py:2060+`). **A target is never an address.** A template
value containing `:` or a host-shaped string is a structural error; node
addresses appear only in apply's output, never in the declaration.

### 1.2 Semantics under uniform_pool

`endpoints:` subsumes the ad-hoc logic `t_ac795424` left in the writer. Under
`uniform_pool` with `mode: proxy-alias`, **every** hermes-facing consumer
resolves to the family proxy by alias — `model.base_url`, the `custom`
provider and its `model.default`, `family-*` providers,
`fallback_providers`, auxiliary/compression models, and every role profile:

| Consumer | Written value | Replaces |
|---|---|---|
| `model.base_url` | `http://localhost:<proxy_port>/v1` of the family named by `endpoints.chat` | `chat_url` inference (`:1613-1617`) |
| `providers.custom.base_url` | same string (literally the same value, not a second crafted URL) | `:1645-1649` |
| `providers.custom.model.default` | the probe-resolved alias of that family | `:1647` (`f"{o.node}:{o.port}"`) |
| `providers.family-<name>` | alias-valued `model.default` (per t_ac795424) | `localhost:<port>` default (`:1656`) |
| `delegation.model` + `.base_url`, every `fallback_providers[].*` | target's proxy URL + probe-resolved alias | `_set_worker_model_in_config` defaults (`:1861-1874`) |
| auxiliary/compression entries | target's proxy URL + alias | `_apply_routing` targets (unchanged path) |
| role profiles (`profiles/*/config.yaml` `model.*`) | per `endpoints.profiles` selector | `_set_worker_model_in_profile` / `_set_orchestrator_profile_to_pool` heuristics (`:1877-1927`) |

The invariants:

- **One computation point.** `chat_url` and the probe-resolved alias are
  decided once from `endpoints:` at the top of `_update_hermes_config`; both
  consumers (model block, `custom` provider) read that single value — the
  layering t_ac795424 left (`structured_for_spec` note 1-2) is exactly the
  seam. After this change **no routing decision is computed outside the
  declared block.**
- **Probe-before-write is unchanged.** An endpoint that does not advertise the
  alias keeps the concrete served id; an unreachable probe writes nothing
  (carries previous good id, drops host:port-shaped stale values) — all as
  t_ac795424 pinned.
- **Idempotence unchanged.** `by_name` rebuild + stale `family-*` drop stay.

### 1.3 The non-uniform exemption, stated precisely

A template is exempt from `proxy-alias` enforcement when it is **not**
`uniform_pool` — i.e. `ClusterTemplate.uniform_pool` is false
(`template_intent.py:189`), which includes every v2 template (uniform_pool
requires v3, structural gate `:1333-1334`) and every v3/v4 template that
omits the flag.
Exempt templates keep explicit pins: `orchestrator: <ip>:<port>` targets are
legal, and `endpoints:` there is an *optional* override only — absent, apply
writes exactly what it writes today (byte-identical, §6). The reason is the
v1.7 design's: under a role split the orchestrator really is one pinned node,
and omitting an endpoint means "leave it as tuned", not "route it".

**Default when the block is absent under uniform_pool:** the effective
declaration is the full table in §1.2 (all consumers → the worker-proxy-owning
family, `_owned_worker_family`, `:1676`). This is behavior-preserving: it is
precisely what t_ac795424 made apply compute. So old uniform_pool templates
need no edit; adding `endpoints:` only *names* what apply already did, and
`endpoints:` with a *different* target for a consumer is a new capability
(e.g. chat on `family-orch2`, delegation on `family-coding`).

### 1.4 `endpoints:` vs `routing:`

`routing:` (v3) keeps its meaning: per-consumer symbolic target with strict
omission. `endpoints:` is the superset that also covers the model block,
providers, and profiles — the consumers `routing:` was documented as unable
to express ("routing has no profiles consumer", `:1958-1959`). Precedence:

- A key in both: `endpoints:` wins (it is the newer, broader declaration);
  validate emits a warning naming the shadowed key so the redundancy is
  visible.
- `routing:` present, `endpoints:` absent: today's behavior exactly.

The two should converge in a later card (one vocabulary, one writer); this
spec deliberately does not break v3 templates.

### 1.5 Apply writes it all

`apply_template` step 3 (`config.yaml`, `:1073-1077`) and step 5
(`worker-model-ids`, `:1100-1119`) consume the declared block instead of
deciding. Step 5's `skip_delegation` heuristic (`:1108`, derived from
"routing block present") is replaced: the declared block says per-consumer
whether it is written — omission means untouched, same doctrine as `routing:`.
The step report gains the resolved targets so `hscc template apply` output
shows which family each consumer was pointed at (and the probe status that
gated each write).

### 1.6 Version bump

`endpoints:` and `proxy:` are v4 keys (`RECOGNISED_VERSIONS`, currently
`(2, 3)` at `cluster_template.py:1222`, becomes `(2, 3, 4)`). v2/v3 templates
carrying them are refused with the existing legacy-key error
(`template_intent.py:206-218`), matching how
`routing`/`uniform_pool` were introduced. v3 parses unchanged and stays
byte-identical.

## 2. `proxy:` — LiteLLM balancer + per-deployment knobs

### 2.1 Schema

```yaml
proxy:
  families:
    coding:
      routing: least-in-flight     # balancer policy, §2.2 (the only live knob)
      max_parallel_requests: 2     # per backend — INERT, §8
      weight: 1                    # optional, uneven nodes — INERT, §8
      timeout: 600                 # INERT until the §4.3 hook, §8
      num_retries: 2               # INERT until the §4.3 hook, §8
```

The block is a **generic dict**: apply passes whatever is under it into the
proxy.yaml fragments it owns, so adding a knob later is a schema-free change.
v1 recognizes only the keys above and rejects unknown ones (validate, §5) —
generic pass-through is the wire format, not a license to ignore typos.

The field shape is deliberately independent of the proxy.yaml *key path*,
which has already moved twice (top-level `router_settings:` → upstream
`gateways.<gw>.*` scoping via spark-arena/sparkrun#317 → the live fork still
reading top-level). The template declares intent; §4 owns the mapping.

### 2.2 Balancing: `least-in-flight`, and what `routing_strategy` must stay

The balancer is a LiteLLM `CustomLogger` overlay (`async_filter_deployments`),
**not** a LiteLLM `routing_strategy`. Consequences for the contract:

- `proxy.families.<fam>.routing: least-in-flight` → apply writes
  `gateways.litellm.balancing: least-in-flight` in proxy.yaml (the upstream
  scoped shape; forward-compatible with upstream where
  `ProxyConfig.gateway_settings("litellm")` replaced `router_settings`).
- Under it, `router_settings.routing_strategy` stays `simple-shuffle` —
  LiteLLM's base that the balancer overlays. **Never write
  `routing_strategy: least-in-flight`**: the 1.82.6 Literal
  (`types/router.py:62-68`) admits only `simple-shuffle`, `least-busy`,
  `usage-based-routing`, `latency-based-routing`; it would fail at proxy
  startup.
- `least-busy` stays rejected on its own merits: in 1.82.6 its in-flight
  counter is incremented only from a sync hook `Router.acompletion` never
  calls, so every deployment reads 0 and the tie goes to the first
  `model_list` entry (verdict in `sparkrun/proxy/balancer.py` module
  docstring; operator's cluster measurement 2026-10-15 20:20).
- The shipped toggle is **global** — one value per template is enough for v1.
  If the block carries different `routing:` values for two families,
  validate rejects it: two policies are not expressible today, and the spec
  refuses to invent a mechanism.

**Default (operator decision):** a `uniform_pool` apply writes
`gateways.litellm.balancing: least-in-flight` unless the template explicitly
overrides `proxy.*.routing`. Rationale on record: today's base is
simple-shuffle (random per request, ignores in-flight), which is why
`monitor --live` shows one node at 0 running while another queues;
prefix-cache hit measured 65-71% per node already under random, so
in-flight-aware placement costs nothing measurable on cache locality.

### 2.3 What apply writes, and through whom

Apply owns exactly the regions of `~/.config/sparkrun/proxy.yaml` its
template declares and merges them (read-modify-write; sparkrun's
`ProxyConfig.save` already has the merge-into-newest-document pattern at
`proxy/config.py:144` with `set_proxy`, and hscc reuses it through the
setter contract #4 below):

1. `router_settings:` — from declared knobs that map to LiteLLM
   `router_settings` (v1: none by default; the generic-dict shape lets
   future keys land here without schema change).
2. `gateways.litellm.balancing:` — from `proxy.*.routing` (§2.2).
3. `model_list` per-deployment extras — from the §2.1 knobs, **pending the
   sparkrun hook in §4.3**: today sparkrun builds `model_list` entries itself
   from autodiscovery (`proxy/engine.py:100-152`); hscc writing per-deployment
   keys into proxy.yaml has no effect until that hook exists. Apply must
   therefore *record and validate* the knobs now and pass them through in a
   shape §4.3 consumes later.

Write path is sparkrun only (a `ProxyConfig`-level API or `sparkrun proxy`
CLI); hscc never opens the file for write and never edits the regenerated
`~/.cache/sparkrun/proxy/litellm_config.yaml`. **Restart semantics:** LiteLLM
reads router settings only at startup, so a policy change requires a proxy
restart. The live fork already rewrites + restarts on policy change
(`5b0f9b5e`); hscc's apply follows the same rule — write, and restart the
proxy only when the region actually changed (mirroring the gateway-restart
discipline at `cluster_template.py:1149-1161`). The operator hand-fix that
established the live config (`config.yaml.bak-direct244-20261015`,
`proxy.yaml.bak-*`) is never restored or rewritten by hscc code.

## 3. Caps — kanban concurrency as a declaration

Today: `enable_plugins._ensure_kanban_routing` fills
`kanban.max_in_progress=30` / `max_in_progress_per_profile=10` **only when
absent or non-int** (`hscc-bootstrap/enable_plugins.py:276-287`), env-defaults
at `:81-82`; the live fleet runs 6/2 by hand-edit
(backup `config.yaml.bak-cap6-20261015`). Neither value is in any template.

Declaration (under the existing caps story; key placement is the writer's
detail):

```yaml
caps:
  max_in_progress: 6
  max_in_progress_per_profile: 2
  expected_requests_per_card: 1   # divisor for the derivation, §3.1
```

### 3.1 Derivation from declared parallelism

When `caps.max_in_progress` is **absent** and any
`proxy.families.<fam>.max_parallel_requests: N` is declared:

```
max_in_progress := floor( sum(N over declared backends)
                          / expected_requests_per_card )   # default 1
```

`expected_requests_per_card` is a template field (one card holds ~1
consequential in-flight request in the measured regime: a worker session
serialises its model calls). It is a template field, not a hidden constant,
because the honest divisor is workload shape — the derivation is a
*starting point the operator can see and override*, never a guess applied
silently. **The derivation is advisory until its inputs are enforced**:
`max_parallel_requests` is INERT today (§8), so the sum is a plan value,
not a physical ceiling. The first implementation card therefore DERIVES AND
REPORTS (apply prints "derived cap 6; declared 4; declared wins") before it
ever derives-and-writes without an explicit value.

Precedence when both sides speak: **explicit `caps.*` wins over derived**;
validate warns that the derived value was shadowed. When neither is present
and no `proxy:` block exists: **migration default = today's static values,
unchanged** — 30/10 fill-if-absent, exactly `enable_plugins.py` today.

### 3.2 Measurement on record (2026-10-15)

- 4→6 global held ~1h under a 30-min `monitor --live` capture (899 frames);
  the one anomaly in it was explained by the balancer misconfiguration the
  20:20 correction fixed.
- Revert-to-4 watch condition: `Wait>0` on several nodes, or KV% >90%
  sustained ~30 min, as read by `hscc cluster monitor --live` (per-node,
  per-model running/waiting/KV%/tok-s — commit `983b10ab`). That is the
  operator's instrument for cap judgment; the doctor's remediation text (§5)
  and every caps error message point at it, not at board-guessing.

## 4. sparkrun contracts (operator-owned; hscc specs, never patches)

hscc cards never touch `pom11/sparkrun` / `sparksq/sparkroute`, never write
`~/.config/sparkrun/proxy.yaml` directly, never write the cache dir. What
hscc depends on, as contracts:

1. **Satisfied — router_settings verbatim.** Whatever top-level
   `router_settings:` mapping is in proxy.yaml is emitted verbatim into
   `litellm_config.yaml`; a changed policy alone triggers rewrite + restart
   (`5b0f9b5e`, 4 tests, proxy suites 121 green).
2. **Satisfied — balancing toggle.** `gateways.litellm.balancing:
   least-in-flight` installs the balancer (copy `sparkrun_balancer.py` next
   to the generated config + `litellm_settings.callbacks:
   [sparkrun_balancer.handler]`). The generated-config pair is the ground
   truth the doctor reads (§5).
3. **Open — per-deployment pass-through** (only if the §2.1 knobs go live).
   Contract: a proxy.yaml `model_list` entry may carry extra per-deployment
   keys (the §2.1 names) that sparkrun merges into that entry's
   `litellm_params`, verbatim doctrine as in 1; restart-on-change applies;
   tests per the `5b0f9b5e` precedent (build + rewrite + policy-change-
   restart). Today `build_litellm_config` crafts `litellm_params` with only
   `model`/`api_base`/`api_key` + `model_info.max_input_tokens`
   (`proxy/engine.py:129-152`), so any other key is silently dropped —
   which is why §8 marks the knobs INERT rather than broken.
4. **Open — config write API surface.** A sparkrun-callable entry point
   (`ProxyConfig` merge-save or `sparkrun proxy config set`) so hscc's apply
   writes its declared regions without owning the file format.

## 5. Validate

Additions to `_structural_validate` (errors follow the existing voice: name
the key and the fix, `cluster_template.py:1256+`):

| Rejection | Message shape |
|---|---|
| `uniform_pool` + any `endpoints`/`routing` target naming a host or `host:port` | `endpoints.chat -> '10.0.0.1:8000': uniform_pool endpoints must name a family (family-<name>) or 'orchestrator', never an address` |
| `proxy:` not a mapping / `proxy.families` not a mapping of mappings | `proxy: expected a mapping (generic pass-through dict), got list` |
| unknown key under `proxy.families.<name>` | `family 'coding' proxy knob: unknown key 'routning' (known: routing, max_parallel_requests, weight, timeout, num_retries)` |
| `routing_strategy` or any spelling of the balancer as a LiteLLM strategy | `proxy knob 'routing_strategy': the balancer is not a LiteLLM routing strategy — LiteLLM 1.82.6 admits only simple-shuffle / least-busy / usage-based-routing / latency-based-routing, and least-busy is broken there (its in-flight counter is never incremented on the async path); use 'routing: least-in-flight'` |
| `routing: least-busy` | same error, least-busy-specific |
| two families with different `routing:` values | `proxy: balancing is global today — families disagree ('a': least-in-flight, 'b': simple-shuffle); declare one value` |
| `endpoints`/`routing` target not a family/unit the template defines | reuse `routing.<consumer> -> '<target>': no such family` (`:1409-1420`) |
| `caps.*` present without int values; `expected_requests_per_card` ≤ 0 | `caps.max_in_progress: expected an integer, got 'many'` |
| `proxy`/`endpoints`/`caps` on version < 4 | legacy-key refusal (`template_intent.py:206-218` voice) |

`validate_resolved_plan` (`:950`) gains the plan-level pair: under
uniform_pool, every declared endpoint target must resolve to a family that
**has a proxy** (`fam.proxy_port is not None`) — a target family without
`proxy: true` has no stable alias endpoint and would silently re-introduce a
node pin.

## 6. Doctor — live agreement, pure-read

Extend the `provider_routing` check from t_ac795424 with a declaration↔live
agreement check (same pure-read rules: never repairs, never restarts; node
addresses only via sparkrun's own `resolve_hosts` under sparkrun's venv
python — the health.py pattern t_ac795424 uses; hand-parsing cluster state
is banned):

1. **hermes ↔ endpoints:** existing fleet-node scan (unchanged) plus: each
   consumer the applied template's `endpoints:` declares must carry the
   declared target's URL/alias in the root config and each matched profile.
2. **proxy ↔ declaration:** read `~/.config/sparkrun/proxy.yaml`
   (`router_settings`, `gateways.litellm.balancing`, model_list extras) and
   compare against the applied template's `proxy:` block.
3. **balancer pair (cross-ref t_39d7c915 — ONE implementation):** when
   proxy.yaml asks for `balancing`, the live
   `~/.cache/sparkrun/proxy/litellm_config.yaml` must contain
   `litellm_settings.callbacks` including `sparkrun_balancer.handler` AND
   `~/.cache/sparkrun/proxy/sparkrun_balancer.py` must exist next to it;
   otherwise **WARN** (never auto-repair, never restart).
4. **live model info:** the proxy's `/v1/model/info` model set must cover the
   aliases the applied template's `endpoints:` routes to.

Any leg that cannot be read (no applied state, no proxy.yaml, unreachable
proxy) is **unverified (`ok=None`)**, never a silent pass — the
provider_routing fail-closed doctrine. Remediation text names
`hscc cluster monitor --live` as the load-instrument for the caps/watch
conditions (§3.2).

## 7. Migration

- **Old templates (no new blocks):** parse unchanged; behavior
  byte-identical — defaults are §1.3 (uniform_pool: full proxy-alias) + §2.2
  (balancing default-on under uniform_pool only) + §3 (static caps fill
  unchanged). A v3 non-uniform template's rendered output must not change a
  single byte; pin with an overreach guard test as t_ac795424 pinned its
  non-uniform branch.
- **The balancing default is the one visible behavior change** on the first
  apply of an untouched uniform_pool template: proxy.yaml gains
  `gateways.litellm.balancing: least-in-flight` and (if it changed) the proxy
  restarts — matching the live fleet's current state, so in practice a
  no-op there.
- `hscc template validate 4node-...` output example (structural layer):
  `proxy: 2 families, balancing=least-in-flight (global); endpoints: chat
  family-coding, delegation family-coding, profiles default→family-coding;
  caps: max_in_progress=6 (declared; derived 6 from 4 backends × 2)` vs a
  rejected `proxy knob: unknown key 'routning'`.
- `applied_template.json` records the resolved declarations (targets + knob
  values) alongside `uniform_pool`, so doctor's "applied template's declared
  X" always reads one file.

## 8. Knob enforcement table (measured against litellm 1.82.6, 2026-10-16)

Read-only verification against the pinned build (`litellm[proxy]==1.82.6`,
the version sparkrun launches via uvx). **No knob may be documented as
working unless it appears ENFORCED here.**

| Knob | Status | Evidence / mechanism |
|---|---|---|
| `routing: least-in-flight` | **ENFORCED** (end-to-end, shipped) | balancer CustomLogger; 4-fake-backend 2/2/2/2 proof + slow-backend soak 224 vs 52 |
| `routing_strategy` passthrough (router_settings) | **ENFORCED** as verbatim emit (1.82.6 Literal limits values, §2.2) | `5b0f9b5e`; `types/router.py:62-68` |
| `num_retries` (per-deployment) | **INERT-PENDING-SPARKRUN** — litellm honors it, sparkrun drops it | 1.82.6 reads it from the deployment's `litellm_params` into the retry loop (`router.py:2257`); but sparkrun crafts `litellm_params` with only `model`/`api_base`/`api_key` (`proxy/engine.py:129-140`), so it can only arrive via hook §4.3 |
| `timeout` (per-deployment) | **INERT-PENDING-SPARKRUN** — litellm honors it, sparkrun drops it | 1.82.6 splats the whole `litellm_params` into the call kwargs (`router.py:2125` `input_kwargs = {**litellm_params, ...}`) and the Timeout path reads it back (`router.py:2200-2206`); same sparkrun drop as above |
| `max_parallel_requests` (per-deployment) | **INERT-PENDING-SPARKRUN even as a queue cap** | 1.82.6 *does* honour it (semaphore built from `litellm_params`, `client_initalization_utils.py:16-32`, acquired around the awaited call at `router.py:2143-2148`) — **but the acquisition happens after deployment selection**, so even once hook §4.3 lands it queues requests on a picked backend; it cannot influence which backend the balancer picks. |
| `weight` | **INERT even after §4.3, under the balancer** | honored by simple-shuffle's weighted pick only (`simple_shuffle.py:43-49`); the balancer's `pick()` is fewest-in-flight with random ties and ignores `weight`. Uneven-node weighting has no live mechanism under `least-in-flight` — declare it, validate it, do not promise it. |

## 9. Boundaries and gates (restated for the implementation cards)

- Read-only on sparkrun + proxy.yaml + cache dir: checks read, apply writes
  only through sparkrun's own write path (§4.4). Never restore
  `config.yaml.bak-direct244-20261015`, `proxy.yaml.bak-*`.
- No touch: `hscc_daemon/cluster_live.py`, live hermes configs, sparkrun
  repos (`pom11/sparkrun`, `sparksq/sparkroute`).
- **Sequencing (operator, explicit):** this spec's implementation cards sit
  AFTER drawer `t_5ed5d281`, the CLI-help `t_7b0f65c8` lane, and
  proxy-only-providers `t_ac795424`; every implementation card's `parents`
  must cover all three. The balancer-pair check exists once — shared with
  `t_39d7c915`, referenced not duplicated.
- Tests: fakes only (injectable `_http_get` / `run` / paths as t_ac795424
  established); placeholders `10.0.0.x` / `100.64.0.1` only
  (`scripts/address_guard.py --repo .` exit 0 before every commit); full
  suite per commit; changelog fragment per card per `changelog.d/README.md`
  (docs-only cards like this one: no fragment required — the release
  assembler consumes `Added/Changed/…` kinds only and this diff changes no
  shipped behavior).
