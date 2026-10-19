# Why HSCC exists

Hermes is an excellent *single* agent: one brain, one chat, one model, with a
built-in kanban dispatcher and git-worktree execution. But the moment you have
a **GPU cluster and want a team of agents**, you hit gaps Hermes doesn't fill
on its own — it knows how to *think* and dispatch work, not how to run the
physical cluster underneath it or behave like a specialized organization.

HSCC exists to close that gap without forking or fighting Hermes. It leans on
Hermes' engine and wraps it in the operational shell a real fleet needs.

## What Hermes was missing (and HSCC adds)

| Gap in stock Hermes | What HSCC adds |
|---|---|
| No physical cluster control — can't provision / stop / heal vLLM models, no NAS or node-health awareness | A sparkrun-backed cluster toolset (orchestrator-only) + a self-heal daemon that keeps worker models alive and relaunches crashed ones |
| Profiles exist, but no way to define a *roster* of specialists — every worker is generic | A role framework: each role is a spec file, generated into a Hermes profile with a layered SOUL (shared base + role disposition); 25 role specs ship, new ones minted on demand |
| The review-status dispatch path is present in core but inert — no producer, no reviewer skill | A `kanban_submit_review` producer + an `sdlc-review` skill, so code is gated (diff + tests + spec) and merged to an integration branch before it counts as done |
| No "run hands-off" control | An autonomy flag (`~/.hscc/autonomy`) + a "do it autonomously" trigger that lets the orchestrator run idea→shipped without pausing for approval |
| No cluster-aware setup | One bootstrap command that detects the cluster from sparkrun and installs everything — no hardcoded topology |

The result: Hermes goes from *one smart agent* to a *self-running, specialized
fleet* spread across your GPU nodes — with quality gates and an off switch.

## How it looks in practice

You type in chat: *"Build a REST API for the project tracker in ~/dev/tracker."*

1. The **orchestrator** brainstorms a spec with the **architect**, then
   decomposes it into a dependency-ordered task graph on the kanban board.
2. Tasks dispatch to role-specialized workers — **backend-engineer** scaffolds
   the API, **qa** writes tests, **coder** fills in the routes — each in its
   own git worktree on a GPU worker node.
3. When the coder finishes, it submits its work to **review**. The **reviewer**
   agent checks the diff, runs the tests, confirms the work matches the spec,
   and merges to the integration branch.
4. The whole chain runs hands-off. If a worker model crashes, the daemon
   relaunches it. If a task fails repeatedly, the escalation watcher reassigns
   it — and notifies you (desktop) if it gets stuck.

This runs on a DGX Spark cluster with Hermes agents and sparkrun — not a
turnkey product for arbitrary hardware, but a proven pattern if you already
have (or are building) that stack.
