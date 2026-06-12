# hscc-commands

Operator slash commands for cluster incident response. Handlers run **directly
in the gateway** (not through the LLM), so they work even when the orchestrator
model is wedged. Registered via `register(ctx)`.

| Command | What |
|---------|------|
| `/cluster` | Live status: orchestrator + workers, per-node health. Read-only. |
| `/status` | Rich dashboard: topology + free-VRAM + power/idle + proxy + daemon + applied template + autonomy flag. |
| `/orch-restart [confirm]` | Restart the orchestrator vLLM (confirm-first). |
| `/cluster-restart [confirm]` | **Re-apply the active template** (`~/.hscc/applied_template.json`) — the recovery contract; falls back to restarting serving.json units if no template recorded. |
| `/heal [confirm]` | Report unhealthy workers + restart them; on an orchestrator wedge, advise `/cluster-restart`. |
| `/template [list\|preview <n>\|validate <n>\|apply <n> [confirm]]` | Manage cluster templates from chat. |

No `/provision` or `/stop` slash — model lifecycle stays tool-only + confirm-gated
(the dangerous ops aren't one keystroke away).

All commands read topology from live discovery — no hardcoded nodes. `cmdlib.py`
holds the gateway-side logic (kept import-light for wedge-resilience).

Tests: `tests/` — `python -m pytest tests/ -q`.
