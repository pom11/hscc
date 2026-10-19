# The fleet — roles, review, autonomy, escalation

Agent work flows through native Hermes kanban — HSCC adds a pre-dispatch hook
(cluster-aware host routing) and the review gate, but the dispatch loop is
Hermes'. An idea is brainstormed into a spec, decomposed into a
dependency-ordered task graph, and each task dispatches to a
**role-specialized worker** running in its own git worktree.

The full "why" behind this shape (what stock Hermes lacks) is in
[WHY.md](WHY.md).

## Roles

A role is a single spec file (`hscc-roles/roles/<name>.yaml`). A generator
builds it into a Hermes profile with a **layered SOUL** (shared base character
+ role disposition + thin operational facts) and the full Hermes toolset
**minus cluster control** — only the orchestrator can change the cluster.

```
hscc.py generate            # build all profiles from specs → ~/.hermes/profiles/
hscc.py create <name> "…"   # author a new role on demand
hscc.py list                # roles + whether a profile exists
```

(`hscc.py` is `hscc-roles/hscc.py`, installed as
`~/.hermes/plugins/hscc-roles/hscc.py`.)

The roster is **25 spec files** as of 2026-10-19 (`ls hscc-roles/roles/*.yaml`):
orchestrator, architect, coder, reviewer, qa; backend/frontend/devops/security/
data engineers; ml-engineer, ml-researcher, data-scientist; product-manager,
technical-writer, ux-designer; financial-analyst, business-analyst,
market-researcher; content-writer, social-media-manager, project-manager;
ios-engineer, ecofire-bc-engineer; plus `worker` (the generic fallback). New
roles are minted (by a human or the orchestrator) with `create`, so the roster
is data, not code. Details: [hscc-roles/README.md](../hscc-roles/README.md).

## Review loop

Code tasks are gated. When a coder finishes, it submits its task to `review`
status; the dispatcher spawns a review agent loading the `sdlc-review` skill,
which checks the diff, runs the tests, and confirms the work matches the spec.
Approved work merges to an **integration branch** (main stays human-gated);
rejected work is sent back with change requests.

## Autonomy

A master flag at `~/.hscc/autonomy` (default off) governs whether the
orchestrator pauses for approval:

```
hscc.py autonomy        # show
hscc.py autonomy on     # hands-off
hscc.py autonomy off    # ask-first
```

Saying *"do it autonomously"* flips it on: the orchestrator writes a
best-judgment spec without back-and-forth and lets the fleet run. Autonomy
never bypasses the reviewer gate or merges to main.

## Failure escalation

A Hermes-cron watcher (`scripts/escalate_watcher_run.py`) runs every 15 minutes
and reassigns repeatedly-failing tasks to the strong model tier. When even the
strong tier fails, it posts a human-attention alert as a **native desktop
notification** — sent directly by the script via
`hscc_daemon.desktop.send_desktop_notification()` (Hermes' `--deliver desktop`
is not a valid delivery target and is silently dropped, so the script notifies
on its own) — deduped so a stuck task is not re-announced every tick, and
silent when nothing is stuck. This is an opt-in *acting* automation; the
`hscc escalate` CLI command provides a dry-run view of what would be escalated.

A daily, human-gated dependency-update loop keeps the cluster's Hermes and
sparkrun runtime dependencies current via automated PRs and kanban
verification cards.

## Ops lore

- Restart the gateway after prompt/plugin/config edits:
  `launchctl kickstart -k gui/$UID/ai.hermes.gateway`
