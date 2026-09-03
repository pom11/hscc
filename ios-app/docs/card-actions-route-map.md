# Card-action UI route map (t_89f693ac)

This documents which kanban-card mutating routes exist in the HSCC API and
which are MISSING, so the next agent (or the operator) can add the missing
routes without guessing.

## Implemented in this change (existing routes only)

| UI surface | Action | Route | Client method |
|------------|--------|-------|---------------|
| ProjectBoardView "+ New Card" → CreateCardSheet | Create a card (title, description/body, assignee) | `POST /v1/cards` | `client.dispatchCard(board:title:assignee:body:)` |
| CardDetailView Actions (blocked cards) | Unblock / recover a blocked card | `POST /v1/kanban/blocked/{id}/recover` | `client.recoverBlockedCard(_:)` |

Both follow the B4 mutation contract: confirm-gated (the control only ARMS a
confirmation dialog; the request fires only on confirm and always sends
`"confirm": true`), in-flight disabled + spinner, and honest result/error
alerts.

## Missing routes — recorded, NOT invented

These card actions were requested but have NO HTTP route in the API. Per the
task constraint ("if a needed route is missing, record exactly which one on
this card instead of inventing it"), they are listed here and on the board
card for an API follow-up. In the app they are deliberately NOT surfaceable:

| Action | Missing route | Notes |
|--------|---------------|-------|
| Comment on a card | POST `/v1/cards/{id}/comment` (or similar) | `hermes kanban comment` exists only as a CLI verb; there is no HTTP endpoint. |
| Block a card | POST `/v1/cards/{id}/block` (or similar) | No route. Only unblock (recover) exists. |
| Complete/close a card | POST `/v1/cards/{id}/close` (or similar) | No standalone route; closing only happens via merge (`POST /v1/review/{card_id}/merge`), which is a git-merge semantic and needs a real branch/review context, not a plain status move. |
| Edit a card | PATCH `/v1/cards/{id}` (or similar) | No route. Title/body/assignee cannot be changed after creation. |

## Full card-route inventory (authoritative)

From `hscc-api/routes_*.py` `ROUTES.append(...)` registration:

- `POST /v1/cards` — create
- `GET /v1/cards/{id}` — detail
- `GET /v1/cards/{id}/events` — event stream
- `POST /v1/kanban/blocked/{id}/recover` — unblock
- `POST /v1/kanban/task/{id}/kill` — kill a running task
- `POST /v1/review/{card_id}/merge` — merge + close

The board list/hygiene read routes (`GET /v1/kanban/stale`, `running`,
`blocked`) are read-only and were not part of this task's write surface.
