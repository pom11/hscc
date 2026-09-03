# Search across sessions, cards and memories — gap analysis (t_86cdea3a)

## Verdict

The HSCC HTTP API has **NO server-side search endpoint** for sessions, cards, or
memories. The existing `SearchView` searches projects + cards via **client-side
filtering of an already-loaded page**, which the card explicitly defines as NOT
search ("Client-side filtering of an already-loaded page is NOT search — if the
server has no search endpoint for these, record what is missing rather than
faking it with a local filter").

Per the card's own instruction, this is a **record-what-is-missing** card. No
client search feature was faked. This document records exactly what is missing,
what the current screen actually does, and what a real server-side search
endpoint would need to provide.

## Current SearchView semantics (what exists)

`ios-app/Sources/HSCC/Views/SearchView.swift` loads TWO full list endpoints
concurrently in `load()` (SearchView.swift:261-266):

- `loadProjects` → `GET /v1/projects` (SearchView.swift:268-274)
- `loadCards`     → `GET /v1/cards`    (SearchView.swift:275-281)

Both are plain GETs with NO query items. Matching is done **entirely in Swift**:
`matched(_:)` (SearchView.swift:114-129) filters the already-fetched in-memory
arrays with a case-insensitive `contains` substring check over a few fields:
- Project: name, repo, board, displayTopic
- Card: displayTitle, id, displayStatus, board

So the current screen is a **local filter over a snapshot**, not search. It only
covers projects and cards — **sessions and memories are not in SearchView at
all.**

## What "the operator cannot search" means (per card)

- **Past chat sessions** — finding "what did we decide about X" means scrolling
  `SessionHistoryView` / `/resume`.
- **Card history** — finding a past card by title/body content means scrolling
  the card list.
- **Memories** — finding a memory by its content.

## Server-side search: what the HSCC HTTP API provides (or lacks)

Full route sweep of every `ROUTES.append(...)` in `hscc-api/routes_*.py`
(43 routes total, enumerated in the evidence below). **No route contains
`search`.** No handler reads a `q` / `search` query parameter for text search.
The only query parameters on the list endpoints are *filters over the already
returned set*, not search:

| Domain | Route | Query params | Server-side text search? |
|---|---|---|---|
| Cards | `GET /v1/cards` | `board`, `status` only | **No** |
| Cards | `GET /v1/cards/{card_id}` | — | by exact id only |
| Cards | `GET /v1/kanban/blocked` | — | No |
| Cards | `GET /v1/kanban/stale` | `older_than` (days) | No |
| Cards | `GET /v1/kanban/running` | — | No |
| Sessions | `GET /v1/sessions` | `profile` only | **No** |
| Sessions | `GET /v1/orchestrator/chat/{id}` | — | by exact job id only |
| Memories | `GET /v1/memory` | `profile` only | **No** |

`hscc-api/routes_actions.py` configures `GET /v1/cards` (`routes_actions.py`
handler `handle_cards`) to filter ONLY on `board` and `status` query params —
there is no free-text search parameter.

`hscc-api/routes_sessions.py` (`handle_sessions_list`, routes_sessions.py:198)
returns a profile's full listable session set. No search filter.

`hscc-api/routes_memory.py` (`handle_memory_list`, routes_memory.py:283)
returns a profile's full memory card set. No search filter.

## The word "search" in the codebase means ONE of three things, none of them an HTTP search endpoint

Across `hscc-api/`, the word "search" appears only as:

1. **A Hermes agent tool allowlist** — `routes_profile_editor.py:61,63` lists
   tool names (`search`, `x_search`, `session_search`) that a profile can
   declare. These are Hermes-side tools, invoked by an agent inside a chat run —
   NOT exposed as HTTP routes. The API cannot call them directly; the only way
   to reach them is `POST /v1/orchestrator/chat` (send a prompt mentioning the
   tool) which is a heavyweight, slow, confirm-gated mutation — not a search
   primitive.
2. **A docstring mentioning the Hermes `session-search` tool**
   (`routes_orchestrator.py:499`) — again a Hermes-side tool, not an HTTP route.
3. **Unrelated `re.search` regex calls** (e.g. `api_server.py:345`,
   `routes_profile.py:264`).

There is no `/v1/search`, no `?q=` text search supported by any read route.

## The one "search-adjacent" Hermes capability that exists but is NOT in the API

Hermes has a first-class `session_search` tool (FTS5 over the local session DB —
the same "what did we decide about X" search the card wants). It lives in the
agent's toolset (`~/.hermes/profiles/<p>/...`), reachable only by an agent, not
by the HSCC HTTP API. The HTTP API's conversational path to it is
`POST /v1/orchestrator/chat`, which:
- is confirm-gated (409 without `confirm: true`),
- is async/job-based (202 + poll), returns in 30-600 s,
- dispatches through Hermes (spawns a chat run) — nowhere near a lightweight
  read/autocomplete search.

So even though the *data the operator wants to search exists* (Hermes sessions
DB, HSCC card store, memory files), the HSCC HTTP API gives the iOS app **no
cheap, synchronous, read-only search surface** for it.

## What a real fix needs (server-side endpoints that do not exist)

To satisfy the card honestly, the HSCC API would need NEW read-only endpoints:

1. **Session search** — `GET /v1/sessions/search?q=<text>&profile=<p>` (or a
   `q` param on `/v1/sessions`) that FTS-searches the profile's `state.db`
   message store (`hermes_state.SessionDB`) and returns matching sessions/titles.
2. **Card search** — `GET /v1/cards/search?q=<text>` (or a `q` param on
   `/v1/cards`) that searches card title **and body** across boards.
3. **Memory search** — `GET /v1/memory/search?q=<text>&profile=<p>` (or a `q`
   param on `/v1/memory`) that searches memory card **content**, not just the
   80-char title prefix.

Each would return a synchronous, read-only result (with `speak`, per the §B
convention) that the iOS app could call with a debounced query. None exist today.

## Decision: nothing shipped to the client

Because none of the three server-side search endpoints exist, extending the
SearchView to "cover sessions/cards/memories" via local filtering would violate
the card's own rule. The correct, honest outcome for THIS card is this gap
record + a follow-on card to build the search endpoints on the API side (then a
client card to wire SearchView to them).

## Evidence

- Full route enumeration from `hscc-api/routes_*.py` (`ROUTES.append`
  registry), 43 routes, none named/does search:
  ```
  GET  /v1/cards$                         GET  /v1/cards/{card_id}$
  GET  /v1/kanban/blocked$                GET  /v1/kanban/stale$
  GET  /v1/kanban/running$                POST /v1/kanban/blocked/{card_id}/recover$
  POST /v1/kanban/task/{task_id}/kill$
  GET  /v1/sessions$                      POST /v1/sessions/{id}/retire$
  POST /v1/sessions/{id}/compact$
  GET  /v1/memory$                        POST /v1/memory/{node_id}/delete$
  POST /v1/memory/{node_id}/edit$
  GET  /v1/projects$                      GET  /v1/projects/{name}$
  ... (no /search anywhere)
  ```
- `grep` for `search` across `hscc-api/` → only the three non-HTTP meanings above.
- `HSCCClient` (ios-app/Sources/HSCC/HSCCClient.swift): `cards()` has no query
  args; `sessions(profile:)` and `memories(profile:)` take only `profile` — no
  search param wired on the client either.

## Open follow-on (not done here — API-side work, not iOS)

Card for the API team: **add server-side text-search endpoints** for sessions,
cards, and memories (the three routes above). Then an iOS card can wire
SearchView to them with a debounced query. This card only records the gap.
