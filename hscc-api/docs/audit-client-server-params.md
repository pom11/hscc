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
| GET /v1/sessions | profile | routes_sessions.py:202 (`handle_sessions_list`) |

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
| POST /v1/cards | board, title | routes_actions.py:230 (`handle_create_card`) |
| POST /v1/cluster/stop | container_id | routes_actions.py:334 (`handle_cluster_stop`) |
| POST /v1/sessions/{id}/retire | profile | routes_sessions.py:254 (`handle_sessions_retire`) |
| POST /v1/sessions/{id}/compact | profile | routes_sessions.py:293 (`handle_sessions_compact`) |
| POST /v1/orchestrator/chat | prompt | routes_orchestrator.py:1974-1979 (`_validate_prompt`) |

Posts that require only `confirm` (merge, autodown enable/disable/wake/cancel,
kanban recover, cluster up/down, triggers run, escalate) are covered by the
confirm gates. Profile-editor PUT requires "at least one of many" — excluded.

### Rows retired with their client call site (t_80e185d2, 2026-11-18)

`be154aec` (#318, the iOS operator-console retirement) deleted the memory
viewer and the template detail screen together with their client methods
(`memories()`, `deleteMemory`, `editMemory`, `applyTemplate`). The card-mutation
rows below retired earlier or by another route and this card does not establish
when — what is measured here is simply that they are gone: grep of
`HSCCClient.swift` finds no `post("/v1/cards/(`, no `put(`/`patch(` (the client's
only verbs are `get`/`post`/`read`, which is what the derivation regexes match),
and no `/v1/memory` or `/v1/template/apply` call site outside the two
explanatory comments at `HSCCClient.swift:630` and `:715`:

| retired client route | was required | server requirement (STILL ENFORCED) |
|---|---|---|
| GET /v1/memory | profile | routes_memory.py:288 |
| POST /v1/memory/{node}/delete | profile | routes_memory.py:315 |
| POST /v1/memory/{node}/edit | profile, content | routes_memory.py:342, :346 |
| POST /v1/template/apply | name | routes_actions.py:312 (`_action_fields`) |
| POST /v1/cards/{id}/comment | body | routes_actions.py:368 `handle_card_comment`, 400 sites at :382/:385 |
| POST /v1/cards/{id}/block | reason | routes_actions.py:400 `handle_card_block`, 400 site at :413 |
| POST /v1/cards/{id}/close, PATCH /v1/cards/{id} | confirm / assignee | routes_actions.py:434 `handle_card_close`, :464 `handle_card_edit` |

A retired row is not lost coverage: the server still 400s on the missing field
for every caller, and the routes keep their own server-side tests (e.g.
`test_routes_memory.py::test_list_missing_profile_400`). It only stops being a
*client* contract, because there is no client call left to make it one.


## Client-sends-ignored (dead param) check

For every POST, the client only transmits keys the handler reads
(`board`,`title`,`assignee`,`body` on cards; `name`,`force_recreate` on
template/apply; `container_id` on cluster/stop; `profile`,`content` on
memory edit; `prompt`,`project` on orchestrator/chat — all read; the
template/apply and memory-edit pairs are retired call sites, see the retirement
note above). No ignored params found.

## Locking it in (the durable artifact)

The gate lives in `hscc-api/tests/test_contract_swift_routes.py`:

* `test_every_swift_route_is_registered` — every client route is registered.
* `test_every_mutating_post_carries_confirm` — client always sends confirm.
* `test_every_client_post_server_handler_is_confirm_gated` — server always checks it.
* `test_every_required_query_param_is_sent` — the query-param matrix (sessions `profile`).
* `test_every_required_body_field_is_sent` — the body-field matrix (this run).
* `test_matrices_name_routes_the_client_still_calls` — every matrix row still
  names a route the client calls, so a retired screen fails as a STALE ROW
  instead of posing as "the client omits a required param" (t_f09436f1, PR #351).
* `test_audit_doc_live_tables_mirror_the_matrices` — **this document's two live
  tables equal the matrices**, in both directions: every matrix row present with
  the same required set, and no row here naming a route absent from a matrix. The
  retired-rows section above is out of scope (the parser stops at the next
  heading), so it stays the intentional superset it is (t_5f33be1d).
* `test_audit_doc_citations_still_point_inside_their_handler` — every
  `file.py:<line> (`handler`)` in those two tables still resolves: the file
  exists, the line is inside it, the named handler still exists, and the cited
  range falls inside that handler's `def`..`end` span (t_5f33be1d).

Any drift in either direction — client drops a required param, or the server
starts requiring one the client doesn't send — fails the suite. Verified
non-vacuous by inspecting the exact payload keys each client function produces,
and (t_80e185d2) by mutation: adding a field the client does not send still fails
`test_every_required_body_field_is_sent`, and adding a row for a route the client
does not call fails the liveness gate. The two doc gates above were likewise
mutation-probed (t_5f33be1d, 9/9 as expected), including the negative case that a
row added to the retired-rows section must NOT fail anything.
