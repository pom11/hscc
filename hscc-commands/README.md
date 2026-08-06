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

## Known node-visibility behavior

`/cluster` and `/status` enumerate **every** cluster.json compute node and label
each with a distinct state (`✅ serving <model>` / `🔗 tp peer` / `○ idle` /
`❌ unreachable`). A tensor-parallel **peer** is never rendered as down — its
model lives on the span's primary, so its own `:8000` being idle is normal, not
a failure. This note documents how the other commands treat tp-peer nodes for
anyone reading the code.

- **`/workers-up`** (`cmd_workers_up`) — operates on keepalive **units** and
  addresses each unit's primary node. With `tp=2` the peer is part of the same
  sparkrun span, so restarting the unit's primary restarts the peer too — it
  does **not** hide a node for action purposes. The output only names the
  primary; the peer is intentionally not listed because it is not an
  independently actionable unit.
- **`/heal`** (`cmd_heal`) — checks unit **primaries** only. Because a restart
  targets the whole unit (primary + peer together via the recipe's span), no
  compute node is silently skipped; the peer is covered by the primary's unit.
  Output names primaries.
- **`/orch-restart`** — restarts the orchestrator unit, which covers its tp
  peer since the whole unit (span) restarts. Fine.
- **`/cluster-restart`** — re-applies the active template, which re-provisions
  the whole declared cluster. Covers every node. Fine.
- **`hscc cluster status`** (ops.py) — owned by a separate card; not assessed
  here.
- **`hscc verify`** (hscc_daemon/verify.py) and **`hscc daemon fleet stats`**
  (hscc_daemon/stats.py) — do **not** per-node enumerate and do not report
  per-node states; they are install-health / usage-statistics checks, not
  node-status views. Same defect does not apply. Out of scope.
- **`cluster_throughput`** (hscc_daemon/throughput.py) — aggregates by
  serving.json unit (primary's `/metrics`). For a tp span the vLLM metrics
  endpoint on the primary rank already aggregates the whole span, so summing
  peer metrics would double-count. It does not report a per-peer throughput
  line, but that is semantically correct, not a node omission. No change needed.
