# hscc-commands

Operator slash commands for cluster incident response. Handlers run **directly
in the gateway** (not through the LLM), so they work even when the orchestrator
model is wedged. Registered via `register(ctx)`.

## When you touch it

A new incident-response command, or a change to confirm-first semantics.
Model lifecycle stays tool-only + confirm-gated on purpose — there is no
`/provision` or `/stop` slash, so the dangerous ops are never one keystroke
away.

## Commands

Mutations are **confirm-first**: a bare command shows a preview; re-run with
the word `confirm` (or `yes`/`y`) in the args to execute. Read-only commands
(`/cluster`, `/status`, `/template list`) never gate.

| Command | What |
|---------|------|
| `/cluster` | Live status: orchestrator + workers, per-node health. Read-only. |
| `/status` | Rich dashboard: topology + free-VRAM + power/idle + proxy + applied template + autonomy flag. Read-only. |
| `/orch-restart [confirm]` | Restart the orchestrator vLLM. |
| `/cluster-restart [confirm]` | **Re-apply the active template** (`~/.hscc/applied_template.json`) — the recovery contract; falls back to restarting serving.json units if no template recorded. |
| `/heal [confirm]` | Report unhealthy workers + restart them; on an orchestrator wedge, advise `/cluster-restart`. |
| `/template [list\|status\|preview <n>\|validate <n>\|apply <n> [confirm]]` | Manage cluster templates from chat. |
| `/workers-up` | Bring up only down keepalive workers — never touches the orchestrator. Non-destructive, no confirm. |
| `/cluster-down [confirm] [dry-run]` | Stop the whole serving layer — hosts stay up. Delegates to the ONE fleet-down (`hscc-cluster/hscc.py::cmd_cluster_down` → `sparkrun stop --all --cluster <cluster>`); `dry-run` prints the plan and executes nothing. |
| `/cluster-docker-prune [confirm]` | `docker system prune -af` on every node (volumes preserved). |
| `/cluster-apt-upgrade [confirm]` | `apt update + upgrade` across the cluster; chains to `/cluster-reboot` if a reboot is needed. |
| `/cluster-reboot [confirm]` | Reboot all nodes — workers in parallel, orchestrator last. |
| `/cluster-prune [confirm]` | Macro: `/cluster-down` → `/cluster-docker-prune` → `/cluster-apt-upgrade` → `/cluster-restart`. |

`cmdlib.py` holds the gateway-side logic (kept import-light for
wedge-resilience).

## Which verbs are the engine, and which bypass it

These handlers shell out to `sparkrun` with literal argv by design — the whole
point of running them in the gateway is that they work when the orchestrator
model is wedged (the plugin docstring). The cost of that design is that a guard
added to the cluster engine does **not** automatically protect a slash verb.

Since t_bf8577f9 the destructive fleet-down is no longer one of those
exceptions: `/cluster-down` delegates to `hscc-cluster/hscc.py::cmd_cluster_down`
(the one `sparkrun stop --all --cluster <cluster>` built by
`serving.fleet_down_cmd()`), so it inherits the engine's host scope, its
benign-`already down` classification and the post-boot stay-down marker — the
three things it used to re-implement badly, and the reason the two surfaces
could give an operator different answers to the same destructive command
(audit S2/F5). It still does not need the LLM.

The remaining slash verbs that do build their own argv — `/cluster-restart`,
`/cluster-reboot`, `/cluster-docker-prune`, `/cluster-apt-upgrade`,
`/orch-restart`, `/heal`, `/workers-up` — are still outside any future guard
added to `cmd_cluster_*`. If a verb gains a guard that matters for safety
(node-pause overlay, dry-run contract, marker bookkeeping), check whether a
slash verb needs the same call rather than assuming it inherits it.

## Node enumeration

`/cluster` and `/status` enumerate **every** `cluster.json` compute node and
label each with a distinct state (`✅ serving <model>` / `🔗 tp peer` / `○ idle` /
`❌ unreachable`). A tensor-parallel **peer** is never rendered as down — its
model lives on the span's primary, so its own `:8000` being idle is normal, not
a failure. The mutation commands (`/orch-restart`, `/cluster-restart`, `/heal`,
`/cluster-down`, `/cluster-*-prune|upgrade|reboot`) operate on **serving.json
units** and target each unit's primary node — with `tp>1` the primary's restart
covers the whole unit (span), so no compute node is skipped. No command
hardcodes node addresses; topology always comes from `cluster.json` /
serving.json / live discovery.

## How to test / Links

`tests/` — from this dir: `python -m pytest tests/ -q`.
Links: [hscc-cluster (the tools these wrap)](../hscc-cluster/README.md) ·
[hscc_daemon (what heals between incidents)](../hscc_daemon/README.md)
