# Specialized Autonomous Agent Fleet — Design

**Date:** 2026-06-09
**Status:** Approved design → ready for phased implementation planning
**Scope:** Full system design. Build is phased; Phase 1 (role framework) is specced in detail first.

## Context

Today the Hermes fleet is three identical general-purpose worker profiles (`worker-246/247/248`, toolset `[hermes-cli]`, stock boilerplate SOULs) dispatched by native kanban. The orchestrator's SOUL is purely mechanical (tool-routing, no character). There are no specialized roles, no autonomous review (the `review-required` block mechanism exists but nobody acts on it), and the brainstorm→plan skill chain terminates in a markdown plan file — it never fans work into kanban tasks for the fleet.

This design turns the fleet into a **self-extending, specialized, autonomous system**: you give an idea (or say "do it autonomously"), Hermes brainstorms it into a spec, decomposes it into a dependency-ordered kanban task graph with auto-paired review tasks, and role-specialized workers build + review it, landing approved work on an integration branch while main stays human-gated.

Future-compatibility note: an external trigger system (n8n-like) is explicitly OUT of scope now, but the pipeline entry point stays clean so such a trigger could feed it later. Nothing is built for it now.

## Goals

1. **Autonomous quality** — a reviewer role closes the loop; work is vetted before it reaches you.
2. **Idea→shipped throughput** — chat idea becomes running fleet work with minimal manual steps.
3. **Smart specialization** — right role per task (architect plans, coder builds, reviewer checks, QA verifies).
4. **Richer identity** — layered SOULs give the fleet consistent character + per-role disposition.
5. **Self-extension** — the system authors its own new roles as work demands.

## Key decisions (locked during brainstorming)

- **Autonomy level:** full-auto — user sets direction, fleet runs. Master switch `~/.hscc/autonomy on|off` (revived pattern). Phrase trigger: saying "do it autonomously" (or similar) flips it on and runs hands-off.
- **Pipeline brain:** the orchestrator (Hermes-main on .244) runs brainstorm→spec→decompose **inline** (one brain, no extra dispatch hop). Interactive brainstorm stays in chat with the user; full-auto skips the back-and-forth with best-judgment spec.
- **Merge model:** reviewer-approved work auto-merges to an **integration branch**; main stays human-gated (user promotes integration→main).
- **Reject loop:** tiered — auto-retry with the same coder up to N times (default 3), then escalate to the user with full review history.
- **Review bar (strict):** approve only if (1) diff read for correctness/quality, (2) task tests run + green, (3) work matches the task spec. Any failure → reject.
- **Role model:** each role = one spec file → generator builds the Hermes profile. Roster is data, not hand-built dirs.
- **Self-extension:** `hscc-roles create` authors new role specs (from a name+description). In full-auto, the pipeline auto-creates a missing role, uses it, and notifies Operations — with a light "reuse an existing close match first" hygiene check to avoid role sprawl.
- **Identity:** layered — base character + role disposition + thin operational facts, composed per profile.
- **Capability:** every role gets the **full Hermes toolset + skill library** (capability is uniform). The **only** hard boundary: `hscc-cluster` (provision/stop/heal models) is **orchestrator-only** — no worker role can change cluster shape. `preload_skills` is an in-context optimization, not a permission gate (a role can `skill_view` anything on demand).
- **Node mapping:** roles are node-agnostic and carry **no base_url**; the free GPU node's endpoint is injected into the worker env at spawn time (a Hermes-core change, on the `local-custom` branch).
- **Safety ceiling:** "retry till success" is relentless, not infinite — per-stage max-attempts (default 3) + a global per-project give-up that escalates rather than loops forever (protects GPU/cost).

## Architecture — five components

### 1. Role framework (`hscc-roles` plugin, in `pom11/hscc`)
```
plugins/hscc-roles/
  hscc.py            # CLI: create, generate, list, validate
  base-identity.md   # Layer-1 shared character (the "who we are")
  generator.py       # spec -> profile dir (SOUL compose + config + skills)
  roles/
    orchestrator.yaml
    architect.yaml
    coder.yaml
    reviewer.yaml
    qa.yaml
```
Role spec shape:
```yaml
name: reviewer
identity: |            # Layer-2 disposition (role SOUL text)
  You are a code reviewer. Skeptical but fair; read diffs adversarially;
  trust nothing until tests prove it...
preload_skills: [code-review, verification-before-completion, test-driven-development]
# toolset: implicit — full Hermes capability MINUS hscc-cluster (enforced by generator)
# node: none — base_url injected at spawn
```
- `create <name> --from-description "..."` authors a new spec (drafts disposition, picks preload skills) → writes `roles/<name>.yaml`.
- `generate` reads specs → builds `~/.hermes/profiles/<role>/` (idempotent, hash-diff like `hscc-skills`). Specs are truth; profile dirs are regenerable build artifacts.
- Two clean stages: **author** (description→spec) and **build** (spec→profile). A human can review/edit a generated spec before it goes live.

### 2. Layered identity
SOUL composed by the generator from three layers:
- **Base** (`base-identity.md`, shared): character, values, judgment, taste — correctness over speed, admit uncertainty, simple over clever, surface problems, no fabrication, frequent commits.
- **Role** (spec `identity:`): per-role disposition.
- **Operational** (generated, thin): cluster topology + "you are a kanban worker in a worktree." Kept minimal — the lifecycle contract already ships at runtime via `KANBAN_GUIDANCE`.

`SOUL.md = base + role + operational`. Orchestrator gets `base + orchestrator-role + cluster-ops` (fixes its all-mechanical SOUL). Edit base once → regenerate → whole fleet's character shifts consistently.

### 3. Pipeline (orchestrator, inline)
- **Stage 1 — idea→spec:** Hermes runs `brainstorming` in chat with the user (interactive). Full-auto: skip back-and-forth, write best-judgment spec, flip autonomy on.
- **Stage 2 — spec→task graph:** decompose → emit kanban graph via `kanban_create`: design/architect tasks first, coder tasks with dependency links (`parents=`), and an **auto-paired `review:` child** per coder task (parent=impl, assignee=reviewer). Review is structural, not optional. Light "reuse existing role if it fits, else mint" check here.

### 4. Reviewer / quality loop
A driver (gateway dispatcher extension or daemon stream — decided in plan) picks up `review-required` blocks, dispatches to the `reviewer` role:
- **Approve** (diff + tests green + spec match) → merge worktree → integration branch → mark done.
- **Reject** → change-requests as comments → unblock to same coder → retry up to N → escalate to user with history.

### 5. Autonomy governor
- `~/.hscc/autonomy on|off` + phrase trigger in the orchestrator prompt.
- Per-project config: `review_retry_limit` (default 3), `merge_target` (default integration branch).
- Safety ceiling: per-stage max-attempts + global per-project give-up→escalate.
- Notifications (Operations topic): spec ready (if not full-auto), milestones, escalations, integration merges, auto-created roles.

## Data flow
```
You: "build X"  (or "build X autonomously")
  -> Hermes (orchestrator, inline): brainstorm -> spec -> kanban task graph
       (architect/design tasks, coder tasks w/ deps, + auto-paired review tasks)
  -> Dispatcher: fans ready tasks to any free GPU node; worker loads the assigned
       ROLE identity; node base_url injected at spawn
  -> Coder: builds in worktree -> kanban_block("review-required: ...") + diff in comment
  -> Reviewer: diff read + tests green + spec match
       approve -> merge worktree -> INTEGRATION branch -> done
       reject  -> change-requests -> retry same coder (<= N) -> escalate
  -> You: notified at milestones / escalations / integration merges; promote
       integration -> main when you choose
```

## Hermes-core changes (local-custom branch)
1. **Spawn base_url injection** — dispatcher fills the chosen free node's endpoint into the worker env at spawn (roles carry no base_url). Hook at the existing `dispatch_once`→spawn point that already sets `HERMES_PROFILE` / `HERMES_KANBAN_DB`.
2. **Pipeline terminal step** — orchestrator brainstorm/decompose ends in kanban graph emission with auto-paired review tasks (skill or prompt addition).
3. **Reviewer loop driver** — review-required pickup, dispatch to reviewer, approve→integration-merge / reject→tiered-retry.

All on `local-custom` (where heartbeat/TTS/kanban-assignee fixes already live); never pushed to NousResearch upstream.

## Build phases (each independently shippable, own spec→plan→build)
- **Phase 1 — Role framework:** `hscc-roles` plugin + generator + `create` + 4 role specs + base-identity. Generate profiles, verify they load. No behavior change yet — identities exist. **Specced in detail first.**
- **Phase 2 — Pipeline:** orchestrator brainstorm→decompose→kanban graph + auto-review tasks. Manual trigger first.
- **Phase 3 — Reviewer loop:** autonomous review + tiered retry + integration-branch merge.
- **Phase 4 — Autonomy governor:** autonomy switch, phrase trigger, safety ceilings, notifications, auto-role-creation.

## Out of scope (now)
- n8n / external trigger system (keep pipeline entry clean for future hook; build nothing).
- Model-tier-by-role (different models per role) — future; all nodes serve one model.
- Role-to-node pinning — rejected (wastes GPU on 3 nodes); roles are node-agnostic.
- The full 20+ role roster — emerges via `create`/auto-creation over time; not a build task.

## Testing strategy
- **Phase 1:** generate the 4 starter profiles; assert each profile dir loads (`hermes -p <role>` starts), SOUL contains base+role layers, toolset excludes `hscc-cluster`. `create` round-trip: description→spec→generate→loadable profile.
- **Phase 2:** give a small idea; assert a kanban graph appears with correct deps + paired review tasks; assignees are real roles.
- **Phase 3:** force a known-bad task; assert reviewer rejects, retries, then escalates; assert a good task merges to integration branch only.
- **Phase 4:** "do it autonomously" end-to-end on a toy project; assert hands-off run, safety ceiling triggers on an impossible task, Operations notifications fire.
