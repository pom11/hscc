# Serving Control (per-unit stop/restart) — t_e13c8c2b

`ios-app/Sources/HSCC/Views/ServingControlView.swift` — start/stop/restart a
serving unit from the phone, confirm-gated, with in-flight state, an honest
result, and "which unit serves what" shown BEFORE acting.

## Surface

New "Serving Control" hub row in `ClusterView.swift` (right under "Fleet
Control"). Each serving unit from `GET /v1/cluster/status` is listed as a card
showing: the **served model name** (what it serves), **tp/pp** parallelism, and
the **container id**. Per-run unit Stop + Restart buttons hang off the card.

## Server reality (verified against source)

| Operation | Endpoint | Scope |
|---|---|---|
| Stop ONE unit | `POST /v1/cluster/stop` `{container_id, confirm:true}` (`hscc-api/routes_actions.py`, `hscc-cluster/hscc.py:210 cmd_stop` → `sparkrun stop <id>`) | **per-unit** ✓ |
| Bring fleet up | `POST /v1/cluster/up` `{confirm:true}` (`hscc-api/routes_ops.py:348`, `hscc-cluster/hscc.py:301` → `serving.fleet_up_plan()` starts EVERY serving.json unit) | **fleet-wide** — no per-unit start |

There is **no per-unit start/restart endpoint** in the HSCC HTTP API. `up` is
all-or-nothing (orchestrator + all keepalive/non-keepalive workers).

## Honest semantics — no fabricated per-unit start

- **Stop** = truly per-unit (`sparkrun stop <container_id>`). Confirm dialog
  names the unit and its container.
- **Restart** = stop that unit, then `POST /v1/cluster/up`. The confirm dialog
  explicitly states the up re-asserts EVERY unit in serving.json — it never
  pretends a single unit can be started alone. This mirrors exactly how an
  operator recovers a wedged unit (`hscc cluster stop/up`).
- A unit with no `container_id` (not running) shows "Not running" instead of
  dead Stop/Restart buttons, and points to Fleet Control for the fleet-up.

## Confirm gate / in-flight / result

Everything goes through `MutationButton` (`Views/MutationSupport.swift`):
single tap only arms a confirm dialog; the request fires only after the explicit
confirm step, with `confirm: true` always sent. During flight the button is
disabled + spinners; a non-2xx throws and is surfaced as a FAILURE alert (never
a success). After each mutation the view reloads `cluster/status` so the
operator sees the real post-action list.

`status` list loads via `Offline.load(..., cacheKey: EndpointPath.clusterStatus)`
— a transient network failure shows last-known (`.stale`) rather than blanking
which units serve what.

## Files

- `ios-app/Sources/HSCC/Views/ServingControlView.swift` (new)
- `ios-app/Sources/HSCC/Views/ClusterView.swift` (hub row)
- `ios-app/project.yml` (source list)

## Verification

- `scripts/check_sources.sh` — sources in sync.
- `scripts/build_check.sh` — HSCC target: **71 files, 0 errors, 0 warnings**.
- `scripts/check_theme.sh` — CLEAN (no raw colour outside Theme.swift;
  destructive tint uses `Theme.Semantic.bad`).
