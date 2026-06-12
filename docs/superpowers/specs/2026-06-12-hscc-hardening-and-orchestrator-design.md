# HSCC Hardening + HSCC-Orchestrator — Master Design

**Date:** 2026-06-12
**Status:** Draft — awaiting user sign-off, then phased execution
**Owner:** HSCC orchestrator (autonomous build by Claude, user gates per phase)
**Scope:** Full-system design covering (a) the 2026-06-12 audit punch-list (tasks #106–#117) and (b) the user's 18-item feature list, collapsed into 8 workstreams (#118–#125). Each workstream gets its own implementation plan in `docs/superpowers/plans/` before its build. This doc is the contract; the plans are the build steps; the work follows both.

---

## 1. Context

Two inputs converge.

**The audit.** An in-depth 3-agent audit (every finding verified against live state — 2 false criticals were caught and discarded) found real defects:
- daemon plist hardcodes `/usr/local/bin/python3` — runs on this machine only because python.org's framework Python provides that symlink; a Homebrew-only Mac or a Spark node has no such path, and bootstrap swallows the failure (`bootstrap.sh:155`).
- `auxiliary.compression.base_url = http://192.168.88.244:8000/v1` — summarization runs **on the orchestrator**, re-arming the context-compaction freeze we already fixed.
- `ops.provision_model` (`ops.py:71`) calls `sparkrun run <recipe> --hosts <node>` — no `--cluster`, `--ensure`, `--port`, no `expanduser` → bypasses NAS-cached staging, `~/...` paths don't expand.
- ~150 unbounded `<file>.bak.<epoch>` in `~/.hscc` + a 165 MB `state.db.bak`; `cluster_template.write_json` writes a backup every call and never prunes.
- `config.yaml` is `644` (world-readable) with a cleartext `sk-sparkrun` key; `hf_token` sits in `~/.hermes/hf_token` **and** loose in the `~/dev/hscc` working tree.
- SOUL.md line 1 + `personalities.ops` hardcode `.244/.246/.247/.248/.249`; 5 committed templates hardcode the same — in a **public** repo, despite the README claiming "topology-free."
- zero end-to-end tests: all 48 pass while mocking the subprocess boundary, the exact reason every past bug shipped green.

**The vision.** The system should graduate from "runs commands" to a self-running **HSCC orchestrator**: live topology discovery, topology-free identity, doc-driven autonomous work-flows with idempotent resume + strict review, newbie-proof install, templates that only propose what can actually run (incl. 2 models on one Spark), real healing, NAS integration, and a clean overlay relationship with upstream hermes/sparkrun so updates stop being painful.

**Already done (don't redo):** the dev-env split — one work repo at `~/dev/hscc`, bootstrap copies plugins into `~/.hermes/plugins` (`project_hscc_dev_env`); the kanban review-flow already merged into the hermes fork (`feat/kanban-submit-review`, 8 commits); the role/fleet design (`2026-06-09-specialized-autonomous-fleet-design.md`).

---

## 2. Goals & non-goals

**Goals**
1. **Topology-free everything** — one live discovery source; SOUL, commands, **templates (intent-only, D16)**, NAS read it. No committed IP or port, anywhere — they're resolved from the live sparkrun cluster at apply.
2. **Newbie-proof, reproducible install** — clean clone → `bootstrap.sh` → working machine on a fresh Mac/Spark, with *loud* failures.
3. **Correct templates** — a template proposes only what can run, including co-located models, validated against the actual sparkrun recipes.
4. **Real autonomy** — doc-driven work-flows; workers/subagents resume idempotently (never redo finished work) behind a strict native-kanban review gate.
5. **Clean upstream relationship** — run official hermes/sparkrun; local edits = rebase-able patch branch + reapply script.
6. **Self-healing** — correct provision, worker restart, orchestrator fallback.

**Non-goals (now)**
- External trigger system (n8n-like) — keep the entry point clean, build nothing.
- Migrating off the forks before WS8's reapply script is proven on a real update.
- New worker roles beyond the existing roster (self-extension already designed).

---

## 3. Locked decisions (with rationale)

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | **Fork strategy: rebase-able patch branch + reapply script** | Run official releases; reapply the curated delta on update. Avoids PR-merge dependency and the current "painful updates" pain. |
| D2 | **Multi-model-per-node: v1 must-have** | User explicitly wants 2 models on one Spark; current validation *rejects* it (`node:8000` collision). Real schema work, done now. |
| D3 | **Review on hermes-native kanban** | `kanban_submit_review` + review-pairing already in the fork; one system, aligns with upstreaming, no duplicate review layer. |
| D4 | **Doc-first** | This design → per-workstream plans → build. Plans are the acceptance contract. |
| D5 | **Identity named HSCC, delivered as overlay** | Sentinel blocks in SOUL.md + `personalities.ops` (the `install_soul.py` pattern), topology-free. Not a fork. |
| D6 | **Working-dir discipline: `~/dev/<repo>`, one repo each** | Encoded as a Hermes instruction so the agent stops spawning duplicate clones. |
| D7 | **Autonomous build, gated** | Claude drives build/test/review/commit in `~/dev/hscc`; pauses for: design sign-off, live-cluster mutation, destructive ops, public push, GPU-only validation. |
| D8 | **Discovery = live resource map** | WS2 tracks per-node capability (VRAM, GPU model, **power-draw idle detection** — the real GB10 idle signal, not util%), free-VRAM, per-node health, and **auto-adopts** any node added to the sparkrun cluster. Not just IPs/roles. |
| D9 | **Identity = balanced** | HSCC named, operationally focused, light personality, topology-free. |
| D10 | **Slash commands** | Keep `/cluster /orch-restart /cluster-restart`; add `/heal /status /template`. **No** `/provision` or `/stop` slash — model lifecycle stays tool-only + confirm-gated (the dangerous ops are not one keystroke away). |
| D11 | **Work-flows = full-auto + reviewer gate** | Fleet builds; reviewer role approves against the plan; approved work → integration branch; main human-gated; tiered retry → escalate. |
| D12 | **Placement = sparkrun-driven auto-fit** | Parse `sparkrun show`'s **VRAM Estimation** block (per-GPU total, "DGX Spark fit: YES/NO", weights, KV, usable mem) + live free-VRAM (D8) to compose co-location layouts from real recipe costs. Replaces hand-set mem-fractions as the primary path (manual override still allowed). |
| D13 | **Install = guided + preflight doctor** | Interactive prompts + a `doctor` that checks every prereq (python, sparkrun, NAS, GPU reachability) and explains failures in plain language. `--yes` expert path retained. |
| D14 | **Healing split: workers auto, orchestrator human-gated** | Daemon auto-heals crashed/wedged **workers**. An orchestrator wedge → **alert human + activate fallback** (gateway keeps answering via `:4000` worker LB), but does **not** auto-restart. The human runs **`/cluster-restart`**, which **re-applies the active template** (`~/.hscc/applied_template.json`). The template is the recovery contract — recovery = "make reality match the template." |
| D15 | **Fork: patch branch now, upstream later** | Build the rebase-able patch branch + reapply script now; upstream the 8 kanban + 2 sparkrun commits once proven. |
| D16 | **Templates = pure intent, zero topology** | Templates carry **no IPs and no ports**. They describe shape (recipes + family structure); nodes and ports are resolved from the live sparkrun cluster at apply time. Dissolves the M2/G5 leak at the root (nothing to leak) and makes re-IP / add-node need zero template edits. The current 6 templates are deleted + replaced. |
| D17 | **Worker selection = counts/roles** | A family says `workers: all \| N \| remaining` — never an IP list. Discovery maps the count to actual nodes at apply (auto-adopting new ones). No capability selectors in v1 (deferred). |

---

## 4. Current-state reference (what the code actually looks like today)

So the workstreams below can be read against reality.

**Discovery is split across two sources:**
- `hscc-bootstrap/detect.py` → parses `sparkrun cluster list --json` → `{name, hosts:[ip,...], user, nas}`. Used by bootstrap.
- `hscc-cluster/clusterlib.py:_resolve_topology()` → reads `~/.hscc/cluster.json` shape `{gateway:{ip}, workers:[{ip}], nasDevices:[{ip}]}` → module globals `HEAD, NODES, NAS_HOST`. Falls back to RFC-5737 `192.0.2.x` if the file is missing. Used by all cluster ops/heal. **This is correct today** (the audit's "wrong shape" claim was the false critical) — but it's a *second, independent* source that can drift from `detect.py`.

**Template schema (`cluster_template_schema.py`), today:**
```python
class ModelSpec(BaseModel):
    recipe: str            # sparkrun recipe path
    tp: int = 1            # tensor parallel
    pp: int = 1            # pipeline parallel
    gpu_memory_util: float = 0.8   # EXISTS but unused by validation/serving

class FamilyProxyConfig(BaseModel):
    port: int = 8000       # the LiteLLM proxy port (NOT the vLLM port)
    ...

class WorkerFamily(BaseModel):
    name: str; models: List[ModelSpec]; nodes: List[str]; proxy: FamilyProxyConfig

class ClusterTemplate(BaseModel):
    name; version; cluster_size; orchestrator: ModelSpec; orchestrator_node: str
    families: List[WorkerFamily]
    # to_serving_json(): emits ONE keepalive unit per (model, node) on vLLM :8000
```

**The blocker for D2 (multi-model/node)** lives in `cluster_template.py:validate_template_deployable()`:
```python
# rule 2 today: ">1 model on the same node = collision on :8000" → REJECTS co-location
# rule 4 today: a family node == orchestrator_node → REJECTS
```
And `to_serving_json` hardwires every unit to `:8000` and `"port": 8000`. So two models on one node is impossible: both want `:8000`.

**A real sparkrun recipe already declares the knobs we need** (`local-fixed/qwen3.6-27b-fp8-vllm.yaml`):
```yaml
model: Qwen/Qwen3.6-27B-FP8
defaults:
  port: 8000
  gpu_memory_utilization: 0.8
  tensor_parallel: 1
  max_model_len: 262144
```
So multi-model-per-node = give each model a **distinct vLLM port** + a **gpu_memory_utilization** that sums ≤ 1.0 across co-located models, sourced from / validated against the recipe defaults. The data already exists; the schema + validation + serving-gen just need to use it.

**Other relevant code:** `ops.provision_model` (`ops.py:60-72`), the daemon supervised loop (`daemon_ops.run_daemon_loop`) vs the dead `daemon.py` polling class (with `docker restart hscc-orchestrator:379`), `gateway_restart.py` (`kickstart -k`, dead `status_result`), `serving_gen.build_serving` (octet-derived unit ids), `install_soul.py` (sentinel-block overlay writer), `install_payload.py` (`DEFAULT_PAYLOAD`).

---

## 5. The 8 workstreams (detailed)

> **Dependency graph.** Roots: **WS2 (discovery)** and **WS8 (fork)**.
> `WS2 → {WS1, WS3, WS5, WS7}` · `WS5 → WS6` · `WS8` parallel (shapes how WS1/WS5/WS7 deliver edits) · `WS4` builds on the kanban fork, independent.

---

### WS2 — Dynamic cluster discovery  *(the keystone — build first)*
**Tasks:** #119. **Dissolves:** M2 (#112), M4 (#114). **Blocks:** WS1, WS3, WS5, WS7.

**Problem.** Two discovery sources (`detect.py` `{hosts}` vs `clusterlib` `{gateway,workers,nasDevices}`) that can disagree; topology is hardcoded in SOUL, commands, templates.

**Design.** One discovery module — `hscc-cluster/discovery.py` (new) — the single source of truth:
```python
def discover() -> ClusterTopology:
    """Live → cached → fail-loud. Never returns RFC-5737 placeholders silently."""
# precedence:
#   1. live: `sparkrun cluster list --json` (authoritative when reachable)
#   2. cache: ~/.hscc/cluster.json (last known good; written on every successful live read)
#   3. fail-loud: raise DiscoveryError (NOT silent 192.0.2.x fallback)
```
`ClusterTopology` is a **live resource map** (D8), not just IPs:
```python
@dataclass
class Node:
    ip: str; name: str; ssh_user: str; role: str; id: str
    # capability (D8) — probed live, cached with TTL:
    gpu_model: str | None         # e.g. "GB10"
    vram_total_gb: float | None
    vram_free_gb: float | None    # feeds WS5 auto-fit placement
    power_draw_w: float | None    # REAL idle signal on GB10 (util% misleads ~96% when idle)
    idle: bool | None             # derived from power draw, not util%
    reachable: bool               # ssh/ping
    vllm_healthy: bool | None     # endpoint /v1/models 200

@dataclass
class ClusterTopology:
    orchestrator: Node
    workers: list[Node]
    nas: Node | None
    proxy_port: int = 4000        # sparkrun LiteLLM LB
    source: str                   # "live" | "cache"
```
- **Reconcile** the two shapes inside `discover()`: parse sparkrun's `{hosts}` into `Node`s, enrich from `cluster.json` (names/ssh_user/ids), write merged result back to `cluster.json` as cache.
- **Auto-adopt (D8):** any node present in `sparkrun cluster list` but absent from cache joins the fleet on next `discover()` — no manual edit. (Removed nodes drop out the same way.)
- **Capability probe (D8):** per-node VRAM (`nvidia-smi --query-gpu=memory.total,memory.free`), GPU model, and **power draw** (`--query-gpu=power.draw`) over ssh; `idle = power_draw_w < IDLE_WATTS` (≈15 W on GB10) — NOT util%, which reads ~96% even when idle. Cached with a short TTL so `discover()` stays cheap; a `refresh=True` forces a live probe.
- `clusterlib.HEAD/NODES/NAS_HOST` become thin shims over `discover()` — the silent `192.0.2.x` fallback is **removed** (fail-loud, see §8).
- New read-only tools: `discovery_status` (full map + `source`) and free-VRAM surfaced in `cluster_status` + the `/status` dashboard (WS3).

**Tests.** Live-parse from a captured `sparkrun cluster list --json` fixture (the boundary `test_detect` never exercised); cache fallback; fail-loud on both-absent; reconcile when sparkrun & cluster.json disagree (live wins, cache enriched); auto-adopt a new host; power-draw idle classification (idle at 12 W, busy at 60 W) — **not** util-based; VRAM parse from captured `nvidia-smi` output. **No** test may assert a `192.0.2.x` result.

**Acceptance.** Every consumer (SOUL render, commands, templates, NAS, ops/heal, WS5 placement) reads `discover()`; a node added to sparkrun appears with VRAM+power without code change; grep shows no hardcoded `192.168.88.*` in committed `.py`/`.md` (templates handled in WS5).

---

### WS1 — HSCC orchestrator identity  *(SOUL / config / agents)*
**Tasks:** #118 (absorbs M4 #114). **Depends:** WS2.

**Problem.** Identity is mechanical + hardcodes IPs in two drift-prone places (SOUL.md line 1, `personalities.ops`).

**Design.**
- **Name + character:** the orchestrator is **HSCC**. A layered SOUL (base character "HSCC, operator of a DGX Spark fleet" + operational facts that are *fetched live*, not baked). The managed `HSCC:BEGIN/END` block (written by `install_soul.py`) carries the operational guidance; the preamble carries character. **No IPs in either** — guidance says "read live topology via `cluster_status` / `discovery_status`."
- **Working-dir discipline (D6):** a SOUL instruction — "all development happens in `~/dev/<repo>`; one repo per project; never create duplicate clones; if a repo exists, work in it." Directly addresses the duplicate-repo pain.
- **Config:** `personalities.ops` managed block regenerated from the same single source as SOUL (so they can't drift — one `HSCC_GUIDANCE` constant in `install_soul.py` feeds both).
- **Agents:** confirm the role roster (`hscc-roles`) aligns; orchestrator-only `hscc-cluster` boundary preserved.

**Tests.** `install_soul` idempotency already covered; add: rendered SOUL/ops contain no `\d+\.\d+\.\d+\.\d+`; the working-dir instruction is present; SOUL block == ops block (shared source).

**Acceptance.** Fresh `install_soul.py` run yields a topology-free, HSCC-named identity with working-dir discipline; re-run is a no-op.

---

### WS3 — Dynamic slash commands
**Tasks:** #120 (D10). **Depends:** WS2.

**Design.** All commands call `discover()` for nodes — no hardcoding.
- **`/cluster`** — live topology + per-node health + `source`.
- **`/orch-restart`** — targets `discover().orchestrator`.
- **`/cluster-restart`** — **re-applies the active template** (`~/.hscc/applied_template.json` → `apply_template(confirm=True)`). This is the **template-driven recovery contract** (D14): recovery = make reality match the declared template, not an ad-hoc per-node restart. It iterates whatever the template declares (orchestrator + worker units), so it also recovers a wedged orchestrator when the human triggers it.
- **`/status`** (new) — rich one-glance dashboard: nodes, running models, **free VRAM per node** (D8), proxy `:4000` health, daemon health, applied template name, autonomy flag.
- **`/heal`** (new) — trigger a healing pass on demand (workers / NAS / orchestrator-alert) — the manual entry point to WS6.
- **`/template`** (new) — list / preview / apply / validate cluster templates from chat (thin wrapper over the existing `cluster_template_cli`).
- **No** `/provision` or `/stop` slash (D10) — model lifecycle stays tool-only + confirm-gated; the destructive ops are not one keystroke away.

**Tests.** Command handlers given a stub `discover()` + a stub applied-template produce node-correct actions; `/cluster-restart` re-applies the recorded template (not a hardcoded node list); `/status` renders free-VRAM from the discovery map; no literal IPs in `hscc-commands/`.

**Acceptance.** Commands work on a re-IP'd cluster with zero code change; `/cluster-restart` recovers the cluster from the template alone.

---

### WS5 — Install + templates + sparkrun-recipe integration  *(the multi-model lift)*
**Tasks:** #122 (absorbs C1 #106, H3 #109, H4 #110). **Depends:** WS2. **Blocks:** WS6.

Three sub-parts.

**5a — Newbie-proof install (D13: guided + preflight doctor).**
- **`doctor`** (new, `hscc-bootstrap/doctor.py`) — runs first; checks every prereq and explains failures in **plain language**: python present + version, PyYAML importable, sparkrun on PATH + a cluster configured, Hermes present, GPU reachability per node (via discovery), NAS mount/export health, disk space. Output is a checklist (✓/✗ + a one-line "how to fix"). Bootstrap calls `doctor` in Stage 1 and **hard-stops** on a ✗ that can't be auto-resolved.
- **Guided mode (default):** interactive prompts (orchestrator node, recipe, template) with detected defaults shown; `--yes` retains the expert one-shot path.
- Fix daemon plist: `launchd-setup.sh` substitutes `$PYBIN` (Hermes venv python) into `ProgramArguments[0]`, not the hardcoded `/usr/local/bin/python3` (C1).
- Loud failures: bootstrap stages stop swallowing stderr; a failed **copy** stage hard-stops (it cascades) (H3). (PyYAML check moves into `doctor`.)
- End-to-end test: drive the bootstrap stage sequence against a temp `HOME` with a stub `hermes-agent/` + captured sparkrun fixtures; assert `doctor` runs, plugins land, config wired, plist has a real python, serving.json written (H4).

**5b — Topology-free template schema + multi-model-per-node (D2 + D16).** The core change.

**Templates describe INTENT only — no IPs, no ports, ever (D16).** Everything physical is resolved at apply time from the live sparkrun cluster. The current 6 IP/port-bearing templates are **deleted** and replaced.
```python
# NEW schema — intent, not placement:
class ModelIntent(BaseModel):
    recipe: str                     # sparkrun recipe (the only required field)
    tp: int = 1; pp: int = 1
    # NO port, NO node — resolved at apply

class FamilyIntent(BaseModel):
    name: str
    models: list[ModelIntent]
    workers: str | int = "all"      # "all" | N | "remaining" (D17 counts/roles, NOT IPs)
    proxy: bool = True              # proxy wanted? port auto-assigned at apply

class ClusterTemplate(BaseModel):
    name: str; version: int = 2; description: str = ""
    orchestrator: ModelIntent       # runs on the gateway node (from discovery)
    families: list[FamilyIntent] = []
    # NO orchestrator_node, NO cluster_size, NO ports — all derived
```
**Resolution at apply (`resolve(template, topology) -> ResolvedPlan`)** — upon sparkrun-cluster discovery (WS2):
- orchestrator → `discover().orchestrator` (the gateway node).
- each family's `workers: all|N|remaining` → mapped to actual `discover().workers` (auto-adopting new nodes; "remaining" = workers not yet claimed by an earlier family).
- **vLLM ports auto-assigned** per (node) sequentially from 8000 (co-located models on a node get 8000, 8001, …); **proxy ports** auto-assigned from 4000 per family. No port ever written in a template.
- `plan_placement` (5c) checks the resolved layout fits via `sparkrun show` cost + live free-VRAM; refuses with a clear reason otherwise.
- `to_serving_json` emits units from the **ResolvedPlan** (concrete node + assigned port), one keepalive unit per (model, node); a node may carry N units on distinct ports.

This **dissolves M2/G5 at the root** — a template with no topology has nothing to leak, and re-IP'ing / adding a node needs zero template edits.

**5d — Daemon supervision rewrite (G1/G2 — folded into WS5).** *The multi-model feature is incomplete without this.* The keepalive/health loop is currently **node-keyed** and hardwires one port:
- `health.py:22` `VLLM_PORT = 8000`; `health.py:411` iterates `keepalive_nodes(serving_data)` (a node set); `health.py:422` health-checks `http://{node}:{VLLM_PORT}/health`; `health.py:440` relaunches with a single `--port 8000`.
- So today the daemon would only ever supervise `:8000` on a node and never see a co-located model's second port.
- **Rewrite to be (node, port)-unit-keyed:** iterate serving.json **units** (each carries its own `nodes:[ip]` + the new `port`), health-check `http://{node}:{unit.port}/health` per unit, and relaunch the specific crashed unit with its own `--port`. The idle-reaper + load-grace guard become per-unit too (the persisted grace from WS6 keys on (node,port), not node). `serving.VLLM_PORT` / `KEEPALIVE_NODES` / `keepalive_nodes()` helpers in `hscc_daemon/serving.py` get unit-aware replacements.
- This lives in `hscc_daemon/{health.py,serving.py}` but is **owned by WS5** (the feature it enables); WS6 consumes the same unit-keyed primitives for healing.

**Constraint — multi-node `tp>1` vs co-location (G3).** A model with `tp>1` spans multiple nodes' GPUs; co-location assumes a model fits within one node's budget. **v1 rule:** co-location (≥2 models on one node) requires every co-located model to be `tp=1`; a `tp>1` model occupies its node(s) exclusively. `plan_placement` (5c) enforces this — it will not co-locate onto a node already claimed by a `tp>1` model, and refuses to co-locate a `tp>1` model itself. Documented as a v1 limitation; multi-node-TP + co-location interplay is out of scope now.
- **Validation runs on the RESOLVED plan** (`validate_resolved_plan`), not the template (the template has no ports/nodes to validate). After `resolve()`:
  - co-location OK **iff** the auto-assigned ports on a node are distinct (guaranteed by assignment) **and** Σ per-GPU-total (from `sparkrun show`) ≤ node free-VRAM. Port collisions are structurally impossible now, so the real gate is the VRAM fit.
  - one proxy per port (auto-assigned, so also structural).
  - recipe-must-exist (still validated against the referenced recipe).
  - `tp>1` exclusivity (G3) enforced during `resolve()` placement.
- Placement is **entirely sparkrun-`show` auto-fit (5c/D12)** — there is no manual port/mem-fraction in a template anymore (D16). The recipe's own `defaults` (port baseline, gpu_memory_utilization) inform the cost model; HSCC owns the actual port assignment.

**5c — Recipe integration + sparkrun-driven auto-fit (D12).** This is what makes templates *correct by construction*. sparkrun already computes real resource cost: `sparkrun show <recipe>` emits a **VRAM Estimation** block —
```
VRAM Estimation:
  Model weights:    28.75 GB
  KV cache:         32.00 GB (max_model_len=262,144)
  Per-GPU total:    60.75 GB
  DGX Spark fit:    YES
  GPU Memory Budget: usable 96.8 GB (121 GB x 80%), available-for-KV 68.1 GB
```
- **Parser** `recipe_cost(recipe) -> {weights_gb, kv_gb, per_gpu_total_gb, fits: bool, usable_gb, ...}` — parses that block (no `--json` flag exists, so text-parse, like `detect.recipe_model` already does). Cache by recipe path + mtime.
- **Auto-fit placement** `plan_placement(models, topology) -> layout | errors`: for a set of models, use each recipe's `per_gpu_total_gb` + live **free VRAM** per node (WS2/D8) to assign models to nodes such that Σ per-GPU-total ≤ node free-VRAM, assigning distinct ports automatically. Refuse (with a clear reason) if nothing fits — never propose an OOM layout. sparkrun's own "DGX Spark fit: YES/NO" is the first gate.
- Template validation (5b) and `/template` preview both call this, so a template can't propose a co-location sparkrun says won't fit. Manual `gpu_memory_fraction`/`port` in a template still override the auto-fit (D12).
- Ship a **working** co-location example template — two recipes whose combined `per_gpu_total_gb` fits one Spark (≤ ~110 GB usable), distinct ports — that passes validation and applies. (Which two recipes = open sub-decision §8.)

**5e — Apply snapshot + auto-rollback (G4).** *We corrupted the live cluster once this session via a half-completed apply; pre-apply validation alone isn't a safety net.* `apply_template` becomes transactional:
- Before writing, **snapshot** the current `serving.json`, `models.json`, `config.yaml`, and `applied_template.json` to a single timestamped rollback bundle (`~/.hscc/rollback/<ts>/`), distinct from the per-write `.bak` churn (which WS Phase-0/M1 caps).
- Apply proceeds; if any stage fails (provision error, gateway restart non-zero, post-apply health check fails), **auto-restore** the bundle (rewrite the 4 files back) and report the failure + that rollback happened. The cluster is left in its pre-apply state, not a half-state.
- A post-apply health gate: after provisioning, verify each declared unit's endpoint is reachable within a timeout; failure triggers rollback. (GPU-gated in tests → the gate logic is unit-tested with a stubbed health check; the live verify is flagged.)
- Keep the last N rollback bundles; older ones pruned (consistent with M1).

**Tests.** `recipe_cost` parses a captured `sparkrun show` fixture (weights/kv/per-gpu-total/fit). `plan_placement`: two-models-fit-one-node → valid layout w/ distinct ports; over-budget → refused with reason; respects live free-VRAM from a stub topology; honors manual override. Validation: co-location passes when sparkrun-fit + ports distinct; fails on shared port; fails when Σ per-GPU-total > node VRAM. serving.json: N units/node on distinct ports, all keepalive. Integration: apply the co-location template against a temp HOME → daemon-supervisable serving.json. E2E install (5a).

**Acceptance.** A committed co-location template (chosen via `recipe_cost` so it genuinely fits) applies cleanly and its serving.json declares 2 keepalive units on one node at distinct ports; **the daemon health-checks + would relaunch each unit on its own port (5d)**; **a deliberately-failing apply auto-rolls-back to the prior state (5e)**; `/template preview` shows the sparkrun-derived fit; clean-clone bootstrap (with `doctor`) produces a working machine with a real-python daemon.

---

### WS6 — Cluster + worker healing
**Tasks:** #123 (absorbs H2 #108, M5 #115, M6 #116). **Depends:** WS2, WS5.

**Design (D14: workers auto, orchestrator human-gated via template re-apply).**
- **Workers — full auto-heal.** The daemon (`daemon_ops.run_daemon_loop`) detects a crashed/wedged worker (endpoint unhealthy / power-draw says idle-but-should-be-serving) and auto-restarts it: `stop → run --ensure`, with the load-grace guard **persisted** across invocations (so it can't thrash a mid-loading vLLM — fixes the latent event-driven thrash). No human.
- **Orchestrator — alert + fallback, NOT auto-restart.** On an orchestrator wedge (the documented 200-but-hangs mode), the daemon: (1) **activates the fallback** so the gateway keeps answering via the `:4000` worker LB, and (2) **alerts the human** (notification). It does **not** restart the orchestrator. The human runs **`/cluster-restart`**, which **re-applies the active template** (D14, WS3) — bringing the orchestrator (and any drifted worker) back to the declared state. The template is the recovery contract.
- **Fallback target = worker LB `:4000`** (resolved, was §8 open): a wedged orchestrator degrades to the worker model tier via `fallback_providers` rather than a dedicated tiny model — keeps capability close during the wedge, no extra always-on model. (M5)
- Fix `ops.provision_model`: `sparkrun run <expanduser(recipe)> --cluster <name> --hosts <node> --port <port> --ensure` — matching the canonical form in `cluster_template._provision_models` + `health.check_workers` (H2). Source `<name>`/`<port>` from `discover()` + the model spec.
- Remove dead `daemon.py` polling class + `docker restart hscc-orchestrator` escalator (the installed path is `daemon_ops.run_daemon_loop`); archive, don't delete (M6).

**Tests.** provision invocation shape (argv assertion incl. expanduser + `--ensure` + `--cluster`); worker auto-restart idempotency + persisted load-grace (no thrash across simulated per-process invocations); orchestrator-wedge path → asserts fallback activated + alert emitted + **no** restart command issued; `fallback_providers` wiring loads. Live provision/restart is GPU-gated → flagged, not auto-run.

**Acceptance.** Workers auto-recover without human; an orchestrator wedge yields a fallback + alert (no auto-restart); `/cluster-restart` re-applies the template to recover; `provision_model` argv matches canonical form; dead code archived; review agent passes the diff.

---

### WS7 — NAS integration
**Tasks:** #124. **Depends:** WS2.

**Design.**
- Bootstrap: detect NAS from `discover().nas`; verify mount/export health; write fstab entry; on a vanished QNAP export (known fragility) diagnose + offer restore (the `repair_nas_export` heal path, now targeting the *real* NAS via discovery).
- Recipes use NAS `cache_dir` for weight staging (the `--cluster` cache the provision fix in WS6 restores).
- Honor staging constraints (memory: NAS 10G/SATA caps parallel pulls; sequential or 100G node-to-node fanout beats parallel NAS pulls) — bootstrap/provision shouldn't fan out parallel NAS pulls.

**Tests.** NAS detection from discovery; export-health probe parsing; fstab idempotency. Live mount is host-gated → flagged.

**Acceptance.** Bootstrap on a NAS-equipped cluster mounts + health-checks it; provision uses the NAS cache.

---

### WS4 — Agentic work-flows: resume / review / docs  *(the prize)*
**Tasks:** #121. **Depends:** kanban fork (present). Mostly independent.

**Design.**
- **Idempotent resume.** Before (re)dispatching a worker/subagent task, check "is this already satisfied?" — completed kanban subtask state + committed diffs on the task branch + passing task tests. If satisfied → mark done, don't redo. If partially done → resume from the first unsatisfied step (the plan's checklist is the unit of progress).
  - **⚠ Highest-uncertainty item (G6).** The *mechanism* of the completion probe is the hard 80%, not the goal. Open design questions for the WS4 plan: what's the unit of "a step" (kanban subtask? plan checklist item? a commit per step?); how is "satisfied" decided (branch HEAD touches the expected files + named tests green + subtask marked done — and what if tests don't exist yet); how to distinguish "done" from "started but abandoned mid-edit." This gets the **most detailed standalone plan before any build**, and likely a spike/prototype first. Do not hand-wave it in the build.
- **Review flow (D3).** Extend native `kanban_submit_review` / review-pairing (in the fork). Strict bar (from the fleet spec): approve only if (1) diff read for correctness, (2) task tests run + green, (3) work matches the task/plan spec. Else reject → tiered retry (same coder ≤N, default 3) → escalate to user with full review history.
- **Doc-driven execution.** design doc → impl plan (per-WS, in `docs/superpowers/plans/`) → kanban task graph; workers execute against the plan; the plan's checklist is the acceptance contract; review checks work-vs-plan.

**Tests.** Completion-probe unit tests (satisfied / partial / unsatisfied); review-gate transitions (approve/reject/escalate); resume picks up at the right step. Pipeline dry-run.

**Acceptance.** A killed-and-restarted worker continues instead of redoing; a deliberately-broken diff is rejected and retried; an approved diff lands on the integration branch.

---

### WS8 — Upstream/fork strategy  *(rebase-able patch branch + reapply script)*
**Tasks:** #125. **Depends:** none (parallel). **Shapes:** how WS1/WS5/WS7 deliver edits.

**Current deltas (enumerated):**
- hermes-agent: `feat/kanban-submit-review`, **8 commits** ahead of `origin/main` (kanban review-flow features + a plugin removal + an autostash recovery).
- sparkrun (`~/sparkrun`): **2 commits** ahead (openclaw compat, restart-policy default).
- recipes: `local-fixed/` overlay (the A3B/27B fixes) — already the sanctioned pattern per the no-edit-official rule.

**Design.**
- Document the patch set (what each commit changes + why) in `docs/`.
- A reapply script: `update = fetch upstream release → checkout the patch branch → rebase onto the release → run the patched test subset → report conflicts`. For sparkrun, the same against `spark-arena`.
- Wire an `hscc-update` flow into bootstrap/commands so updating is one command, not a manual rebase.
- Recipes stay as the `local-fixed/` overlay (no change needed; document it as the model).

**Tests.** Script dry-run against a synthetic upstream advance (a temp clone with an added commit) → asserts clean rebase + conflict reporting. No live upstream mutation without user OK.

**Acceptance.** `hscc-update` rebases the patch branch onto a (simulated) new upstream and reports status; the local edits are reproducible from official + the script.

---

## 6. Phasing (each phase independently shippable)

| Phase | Contents | Why this order |
|------|----------|----------------|
| **0 — Quick wins** | H1 (#107 compression→:4000), **M2 (#112 interim placeholder-scrub of committed template IPs → `192.0.2.x`)**, M3 (#113 perms + move hf_token), M1 (#111 prune .bak), L (#117) | Low-risk hygiene; H1 un-arms a live freeze. **M2 is now only an interim placeholder-scrub** (no real IPs at HEAD); the *real* fix is D16 — WS5 deletes these templates for the topology-free schema, dissolving the leak entirely. Public git *history* still holds the old IPs (RFC1918, LAN-only) — history rewrite not done unless user requests (public force-push). |
| **1 — Roots** | WS2 discovery (#119) ∥ WS8 fork strategy (#125) | Keystone + fork decision unblock the rest. |
| **2 — Surface** | WS1 identity (#118) + WS3 commands (#120) | Both consume discovery; make the system topology-free + HSCC-named. |
| **3 — Capability** | WS5 install/templates + **daemon unit-keying (5d/G1) + apply rollback (5e/G4)** (#122) → WS6 healing (#123) | Multi-model is the big lift (schema + serving + daemon supervision + rollback); healing reuses the unit-keyed primitives + correct provision. |
| **4 — NAS** | WS7 (#124) | Depends on discovery + correct provision. |
| **5 — The prize** | WS4 work-flows (#121) | Highest leverage, on a solid base; **resume completion-probe (G6) is the highest-uncertainty item — gets the most detailed plan before build.** |

Each phase: per-WS plan (sign-off) → build → unit+integration tests → **review-agent pass on the diff** → commit to `~/dev/hscc`. Report at each phase boundary.

> **Note on M2/G5 vs WS2:** Phase 0 scrubs the *committed* template IPs (the public exposure). WS2 later makes templates *runtime*-topology-free (apply fills IPs from `discover()`). The committed example templates use placeholder `192.0.2.x`; the real `hscc-live.yaml` is gitignored and generated locally.

---

## 7. Testing & review strategy (applies everywhere)

- **Mock-vs-real is the enemy.** Integration tests assert *generated files / real I/O*, not mocked subprocess returns — every past bug passed while mocking that boundary. Each WS adds at least one test that would have caught its own class of bug.
- **Boundary tests** for the two untested integrations: `detect.detect_cluster()` (sparkrun JSON) and `bootstrap.sh` end-to-end.
- **Faulty-logic review.** A `code-reviewer` agent reads each WS diff before commit; I treat its output trust-but-verify (this audit itself produced 2 false criticals).
- **Live-GPU caveat.** vLLM-on-GPU behavior is not exercisable from the dev host. Those paths are logic-tested and **explicitly flagged** for a real-cluster run before being called "done."

---

## 8. Open sub-decisions (will ask when reached, not now)

- **WS5 co-location example:** which two real recipes to ship as the canonical "2-on-one-node" template — must be two whose combined `per_gpu_total_gb` (from `sparkrun show`) genuinely fits one Spark. Decided at WS5 build time using `recipe_cost` against the live recipe set.
- **WS2 fail-loud blast radius:** removing the silent `192.0.2.x` fallback means a misconfigured machine errors instead of limping. *Leaning yes* (silent fake-IP is worse — it SSHes documentation addresses); confirm at WS2 build.

**Resolved during shaping:** WS6 fallback target = worker LB `:4000` (D14); WS6 orchestrator recovery = human-gated `/cluster-restart` template re-apply (D14); WS5 placement = sparkrun-`show`-driven auto-fit (D12); WS1 = balanced identity (D9); WS3 command set (D10); WS4 = full-auto + reviewer gate (D11).

---

## 9. Pause points (where autonomy stops for the user)
1. Sign-off on this design + each per-WS plan.
2. Live-cluster mutations (provision, gateway/daemon restart, NAS mount) — staged + confirmed.
3. Destructive ops (rm, force-push) and **public-repo pushes** / any upstreaming.
4. The §8 sub-decisions, when their phase arrives.
5. Anything requiring real-GPU validation — surfaced, not guessed.
