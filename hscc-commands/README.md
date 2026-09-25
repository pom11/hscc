# hscc-commands

Operator slash commands for cluster incident response. Handlers run **directly
in the gateway** (not through the LLM), so they work even when the orchestrator
model is wedged. Registered via `register(ctx)`.

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
| `/cluster-down [confirm]` | Stop all vLLM units cluster-wide — hosts stay up (parallel `sparkrun stop --all` per node). |
| `/cluster-docker-prune [confirm]` | `docker system prune -af` on every node (volumes preserved). |
| `/cluster-apt-upgrade [confirm]` | `apt update + upgrade` across the cluster; chains to `/cluster-reboot` if a reboot is needed. |
| `/cluster-reboot [confirm]` | Reboot all nodes — workers in parallel, orchestrator last. |
| `/cluster-prune [confirm]` | Macro: `/cluster-down` → `/cluster-docker-prune` → `/cluster-apt-upgrade` → `/cluster-restart`. |

No `/provision` or `/stop` slash — model lifecycle stays tool-only + confirm-gated
(the dangerous ops aren't one keystroke away).

`cmdlib.py` holds the gateway-side logic (kept import-light for
wedge-resilience).

Tests: `tests/` — `python -m pytest tests/ -q`.

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
