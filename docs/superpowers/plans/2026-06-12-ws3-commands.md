# WS3 — Dynamic Slash Commands — Implementation Plan

**Spec:** §WS3, D10, D14. **Depends:** WS2 (done).

## Goal
Keep `/cluster /orch-restart /cluster-restart`; add `/heal /status /template`.
All topology comes from discovery / serving.json — no hardcoded nodes. NO
`/provision` or `/stop` slash (lifecycle stays tool-only, confirm-gated).

## Existing (keep, lightly evolve)
- `/cluster` — reads serving.json units + live curl health. Keep; already
  topology-free (units carry their nodes).
- `/orch-restart` — restart orchestrator unit. Keep.
- `/cluster-restart` — **change per D14:** re-apply the ACTIVE TEMPLATE
  (`~/.hscc/applied_template.json` → cluster_template.apply_template(confirm)).
  The template is the recovery contract: recovery = make reality match the
  declared template. Fall back to the current serving.json unit-restart when no
  template is recorded (back-compat). Confirm-first.

## New commands
- **`/status`** — rich dashboard: discovery topology (orchestrator/workers/NAS,
  source), per-worker free-VRAM + power/idle (discovery probe), running models,
  proxy :4000 health, daemon health, applied-template name, autonomy flag. One
  glance. Read-only.
- **`/heal`** — manual healing pass entry point (WS6): report unhealthy workers
  and (confirm-first) restart them; for an orchestrator wedge, report + advise
  `/cluster-restart`. Thin now; deepens when WS6 lands.
- **`/template`** — list / preview / apply / validate templates from chat, a thin
  wrapper over cluster_template_cli. `apply` is confirm-first.

## cmdlib additions
`cmdlib.py` stays import-light for wedge-resilience, but may import discovery
(which has its own live→cache→error fallback). Add:
- `applied_template() -> dict|None` (read ~/.hscc/applied_template.json).
- `reapply_template(confirm) -> dict` (call cluster_template.apply_template).
- `discovery_snapshot()` (best-effort discover(probe=True) for /status; never raise).
- `proxy_health()` / `daemon_health()` readers.

## Tests (`tests/test_commands.py` — extend)
- /cluster-restart re-applies the recorded template (stub apply); falls back to
  unit-restart when none recorded.
- /status renders topology + free-VRAM from a stubbed discovery.
- /template list/preview/apply routing (stub cli).
- /heal reports unhealthy + confirm-gates restart.
- no hardcoded IPs in hscc-commands/.
- register() exposes all 6 commands.

## Acceptance
6 commands registered; /cluster-restart recovers from the template; /status
shows live free-VRAM; works on a re-IP'd cluster with no code change.
