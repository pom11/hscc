# End-to-end API review: view → endpoint → model (2026-08-27)

Dated evidence, relocated out of `ios-app/README.md` per
`docs/audits/README_REVIEW_2026-10-15.md` §2.4 ("move to a dated doc — still
useful"). The table below is the audit's original capture, **verbatim**, with
only two updates marked inline: the decode-fixture count (the harness has grown
since August) and a `HISTORICAL` note on the rows whose screens no longer exist.
Everything under "The two fixes this pass made" is the 2026-08-27 pass as it was
written.

Verified **2026-08-27** against the live API (READS only).
Every row was confirmed with a real `curl`-equivalent GET, and every app model
was **actually decoded** against the captured live JSON (see
`scripts/model_decode_check.sh` — compiles the real model sources, decodes the
committed fixtures — plus `scripts/shared_model_check.swift`).

> **Fixture-count update (2026-10-19).** At capture time this read "26 committed
> fixtures 26/26". The harness has grown since: **54 JSON fixtures / 62 decode
> checks**, all green as of this relocation
> (`bash scripts/model_decode_check.sh` → `ALL DECODE CHECKS PASSED — 62/62
> fixtures decoded against the real models`).

| View | Endpoint | Model | Verdict |
| --- | --- | --- | --- |
| Settings → Test connection / banner | `GET /v1/ping` | `PingResponse` | OK |
| Projects (list) + Search | `GET /v1/projects` | `ProjectsResponse` / `Project` | OK |
| Project → Overview | `GET /v1/projects/{name}` | `ProjectDetailResponse` / `ProjectGit` | OK |
| Project → Chat | `POST /v1/orchestrator/chat` | `OrchestratorChatJobResponse` | OK |
| Project → Chat (poll) | `GET /v1/orchestrator/chat/{id}` | `OrchestratorChatJobStatus` | OK |
| Project → Board (cards) | `GET /v1/cards` | `CardsResponse` / `Card` | OK |
| Card detail | `GET /v1/cards/{id}` | `CardDetailResponse` | OK |
| Project → Board (hygiene) | `GET /v1/kanban/blocked` | `KanbanBlockedResponse` / `BlockedCard` | OK |
| Project → Board (hygiene) | `GET /v1/kanban/stale` | `KanbanStaleResponse` / `StaleCard` | **FIXED** — `boards` was `Int?`, server sends a `[String]` of board names |
| Cluster → topology strip | `GET /v1/cluster/status` | `ClusterStatusResponse` / `ClusterWorkload` | OK |
| Cluster → hosts load | `GET /v1/cluster/hosts` | `ClusterHostsResponse` | **FIXED** — `hosts` was `[String]`, server sends array of `{id,name,ip,role,ssh_user}` |
| Cluster → Health & Ops | `GET /v1/verify` | `VerifyResponse` (=`HealthResponse`) | OK |
| Health & Ops → daemon | `GET /v1/daemon/status` | `DaemonStatusResponse` | OK |
| Health & Ops → triggers | `GET /v1/triggers` | `TriggersResponse` | OK |
| Health & Ops → escalations | `GET /v1/escalate` | `EscalationsResponse` | OK |
| Health & Ops → profiles | `GET /v1/profiles` | `ProfilesResponse` | OK |
| Fleet view → health | `GET /v1/health` | `HealthResponse` | OK |
| Fleet view → stats | `GET /v1/fleet/stats` | `FleetStatsResponse` | OK |
| Fleet view → throughput | `GET /v1/fleet/throughput` | `FleetThroughputResponse` | OK |
| Fleet view → streams | `GET /v1/fleet/streams` | `FleetStreamsResponse` / `StreamStatus` | OK |
| Fleet view → autoscale | `GET /v1/autoscale` | `AutoscaleResponse` | OK |
| Autodown | `GET /v1/autodown/status` | `AutodownStatusResponse` | OK |
| Autodown (enable/disable/wake/cancel) | `POST /v1/autodown/*` | `AutodownEnable/Disable/Wake/CancelResponse` | OK |
| Approvals (merged hygiene) → blocked | `GET /v1/kanban/blocked` | `KanbanBlockedResponse` | OK |
| Approvals (merged hygiene) → stale | `GET /v1/kanban/stale` | `KanbanStaleResponse` | **FIXED** (same as above) |
| Templates → applied | `GET /v1/template/status` | `TemplateStatusResponse` / `TemplateApplied` | OK |
| Templates → library | `GET /v1/template/list` | `TemplateListResponse` / `ClusterTemplate` | OK |
| Templates → preview | `GET /v1/template/preview/{name}` | `TemplatePreviewResponse` | OK |
| Standup / review / QA models | `GET /v1/standup`, `/v1/review/queue`, `/v1/qa/queue` | `StandupResponse` / `ReviewQueueResponse` / `QAQueueResponse` | OK (client methods exist + decode; not wired to a current screen — kept for the API surface) |
| Widget + Live Activity reads | `GET /v1/autodown/status`, `/v1/cluster/status` | shared models | OK |
| Fleet Control (up/down) | `POST /v1/cluster/up`, `/v1/cluster/down` | `ClusterUpResponse` / `ClusterDownResponse` | OK (confirm-gated) |

## What is HISTORICAL in this table (read as of 2026-10-19)

Two dispositions have moved since the 2026-08-27 capture. Keep the rows for the
record, but know they no longer describe a live screen:

- **`ClusterHostsResponse` / `KanbanStaleResponse` — the two `FIXED` rows are now
  historical.** Both decode bugs (below) were fixed on 2026-08-27 and the fixes
  still stand in `Sources/HSCC/Models.swift` (`ClusterHostsResponse.hosts:
  [JSONValue]`, `KanbanStaleResponse.boards: [String]?`); the rows are pinned by
  committed fixtures (`cluster_hosts.json`, `kanban_stale.json`) so a regression
  re-surfaces in the decode gate. They are "historical" only in the sense that
  they document a one-time fix, not a current open verdict.
- **`Project → Chat` (both rows) — `OrchestratorChatView` has since been retired.**
  The view was deleted in `ec6a94f0` (2026-10-14, "retire dead
  OrchestratorChatView"). The **endpoints themselves are still live and still
  correct**: `POST /v1/orchestrator/chat` + `GET /v1/orchestrator/chat/{id}` are
  driven through `HSCCClient.orchestratorChatStart`/`orchestratorChatPoll` from
  `AppShell.swift` (the drawer send path) and the Siri `ProjectIntents`, not from
  the retired view. The models (`OrchestratorChatJobResponse`,
  `OrchestratorChatJobStatus`) still decode green against their fixtures. So the
  *View* column for these two rows is a 2026-08-27 screen name; the endpoint and
  model columns are current.

## The two fixes this pass made

Decode mismatches are exactly what the task warned about — a single wrong key
blanks a screen silently. Two were found and fixed (both in
`Sources/HSCC/Models.swift`):

1. **`ClusterHostsResponse.hosts` was `[String]`; the server sends an array of
   node dicts** `{id, name, ip, role, ssh_user}`. The old type made the whole
   response throw on decode, so the Cluster hub's `clusterHosts()` read always
   failed. Fixed to `[JSONValue]` (not rendered — the topology strip derives
   from `/v1/cluster/status` — but now the read is honest). `Models.swift:55`.
2. **`KanbanStaleResponse.boards` was `Int?`; the server sends an array of
   board-name STRINGS** (while `/v1/kanban/blocked` sends a board COUNT int —
   the two envelopes differ!). This made `kanbanStale()` always throw, silently
   blanking the Board Hygiene "Stale" pane and every project board's stale
   section. Fixed to `[String]?`. `Models.swift:774`.

Both are verified by the actual decode check run against live JSON: 23/23 app
models + the shared models all decode cleanly now.

---

*Relocated 2026-10-19 (card t_872120ce) from `ios-app/README.md` history so the
audit table stays available as dated evidence while the README keeps only
current-IA prose. The `Models.swift:55` / `:774` line refs are the capture-day
line numbers and have since drifted — grep the type names instead.*
