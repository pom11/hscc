# API contract audit — client params vs server handler requirements

Card `t_3e152ac7`: "audit: client sends the params each server handler requires."

Scope: every route the iOS Swift client (`ios-app/Sources/HSCC/HSCCClient.swift`)
calls, checked bidirectionally against the server handler it resolves to:

1. **Client omits required** — a parameter the handler will 400 on if missing
   but the client does not transmit. A call that always fails.
2. **Client sends ignored** — a parameter the client transmits that the
   handler never reads. Dead code / misleading.

Method: the route set is derived FROM the Swift client at import time (the
anti-drift source of truth in `tests/test_contract_swift_routes.py`), matched
to registered server routes exactly as the real dispatcher does (first matching
method + path regex). Then each handler's source was read to extract the
parameters it REQUIRES (raising 400 "missing required") vs OPTIONAL (forsaken
on absence). Path params are inherently satisfied — they are embedded in the
URL the client itself builds — and the dispatcher merges path groups into the
handler's `query` dict, so `query.get("card_id")` etc. is always populated.

## Result: no mismatches in either direction

Every client route is registered server-side, and for every handler-required
parameter (query OR body field) the client actually transmits it. No client-sent
parameter is ignored by its handler. The one real defect this card was spun up
to catch — the escaped-backslash `?profile=\(encoded)` literal on
`/v1/sessions` and `/v1/memory`, which URLComponents percent-encoded into the
path so the handler never saw the query — was already fixed by routing those
params through real `URLQueryItem`s. That fix is locked in by the regression
gates below.

## Required-query-param matrix (GET routes)

| client route | required query param | server handler |
|---|---|---|
| GET /v1/sessions | profile | routes_sessions.py:200-202 |
| GET /v1/memory | profile | routes_memory.py:252-255 |

Every other GET the client calls — ping, cluster/status|hosts|monitor|jobs|info,
health, fleet/throughput|stats|streams, autoscale, standup, cards, card-detail,
review/queue, review-detail, qa/queue, projects, project-detail, verify,
daemon/status, triggers, escalate, profiles, profile/editor, kanban/blocked,
template/list|status|preview, activity/feed, project session/events — takes no
required query param (only optionals: fleet/stats `days`=7, kanban/stale
`older_than`, activity/feed `limit`, session/events `before`/`limit`). All are
satisfied or correctly optional-defaulted.

## Required-body-field matrix (POST routes)

| client route | required body fields | server handler |
|---|---|---|
| POST /v1/cards | board, title | routes_actions.py:199 |
| POST /v1/template/apply | name | routes_actions.py:281 |
| POST /v1/cluster/stop | container_id | routes_actions.py:303 |
| POST /v1/sessions/{id}/retire | profile | routes_sessions.py:252-254 |
| POST /v1/sessions/{id}/compact | profile | routes_sessions.py:291-293 |
| POST /v1/memory/{node}/delete | profile | routes_memory.py:280-282 |
| POST /v1/memory/{node}/edit | profile, content | routes_memory.py:307-314 |
| POST /v1/orchestrator/chat | prompt | routes_orchestrator.py:1304-1310 |

Posts that require only `confirm` (merge, autodown enable/disable/wake/cancel,
kanban recover, cluster up/down, triggers run, escalate) are covered by the
confirm gates. Profile-editor PUT requires "at least one of many" — excluded.

## Client-sends-ignored (dead param) check

For every POST, the client only transmits keys the handler reads
(`board`,`title`,`assignee`,`body` on cards; `name`,`force_recreate` on
template/apply; `container_id` on cluster/stop; `profile`,`content` on
memory edit; `prompt`,`project` on orchestrator/chat — all read). No ignored
params found.

## Locking it in (the durable artifact)

The gate lives in `hscc-api/tests/test_contract_swift_routes.py`:

* `test_every_swift_route_is_registered` — every client route is registered.
* `test_every_mutating_post_carries_confirm` — client always sends confirm.
* `test_every_client_post_server_handler_is_confirm_gated` — server always checks it.
* `test_every_required_query_param_is_sent` — the query-param matrix (sessions/memory profile).
* `test_every_required_body_field_is_sent` — the body-field matrix (this run).

Any drift in either direction — client drops a required param, or the server
starts requiring one the client doesn't send — fails the suite. Verified
non-vacuous by inspecting the exact payload keys each client function produces.
