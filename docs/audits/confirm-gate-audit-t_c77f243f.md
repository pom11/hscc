# Confirm-gate audit: iOS client <-> hscc-api

**Task:** t_c77f243f — "does EVERY mutating call from the iOS client carry `confirm`?"
**Auditor:** backend-engineer
**Date:** 2026-08-30
**Result:** **NO MISMATCHES FOUND** — all 19 mutating client calls carry `confirm: true`, and all 19 server handlers actually gate on it.

## 1. Client side — every POST method sends `confirm: true`

All 19 mutating methods in `ios-app/Sources/HSCC/HSCCClient.swift` include
`"confirm": true` in the request body, and the single shared `post(_:body:as:timeout:)`
helper (L285-322) is the *only* way the client issues a mutating POST. There is no
code path that can send a mutating request without confirmation.

| # | Client method | Line | POST path | Sends confirm? | Cite |
|---|---|---|---|---|---|
| 1 | `triggersRun()` | 504 | /v1/triggers/run | yes | L505 body `["confirm": true]` |
| 2 | `escalateRun()` | 512 | /v1/escalate | yes | L513 body `["confirm": true]` |
| 3 | `updateProfile(_:)` | 541 | /v1/profile/editor/{profile} | yes | L550 `["confirm": true]` seeded |
| 4 | `retireSession(id:profile:)` | 585 | /v1/sessions/{id}/retire | yes | L588 body confirms |
| 5 | `compactSession(id:profile:)` | 598 | /v1/sessions/{id}/compact | yes | L601 body confirms |
| 6 | `deleteMemory(nodeID:profile:)` | 626 | /v1/memory/{node_id}/delete | yes | L629 body confirms |
| 7 | `editMemory(nodeID:profile:content:)` | 637 | /v1/memory/{node_id}/edit | yes | L643 body confirms |
| 8 | `dispatchCard(...)` | 730 | /v1/cards | yes | L734-738 payload confirms |
| 9 | `mergeCard(_:)` | 753 | /v1/review/{card_id}/merge | yes | L757 body `["confirm": true]` |
| 10 | `applyTemplate(name:forceRecreate:)` | 767 | /v1/template/apply | yes | L769 confirms |
| 11 | `stopCluster(containerID:)` | 780 | /v1/cluster/stop | yes | L782 body confirms |
| 12 | `orchestratorChatStart(project:prompt:)` | 813 | /v1/orchestrator/chat | yes | L815 payload confirms |
| 13 | `autodownEnable(idleMinutes:force:)` | 847 | /v1/autodown/enable | yes | L848 confirms |
| 14 | `autodownDisable()` | 855 | /v1/autodown/disable | yes | L856 body confirms |
| 15 | `autodownWake()` | 864 | /v1/autodown/wake | yes | L865 body confirms |
| 16 | `autodownCancel()` | 870 | /v1/autodown/cancel | yes | L871 body confirms |
| 17 | `recoverBlockedCard(_:reason:)` | 878 | /v1/kanban/blocked/{id}/recover | yes | L880 confirms |
| 18 | `clusterUp()` | 890 | /v1/cluster/up | yes | L891 body confirms |
| 19 | `clusterDown()` | 897 | /v1/cluster/down | yes | L898 body confirms |

## 2. Server side — every handler the client POSTs to actually requires confirm

Each client POST route was resolved to its registered handler via `api_server.ROUTES`
and inspected (docstring stripped, call-site check) for a call to a confirm-gating
helper — `_require_confirm(...)` or `_action_fields(...)` (the actions preamble
that wraps `_require_confirm`).

| Client POST path | Server handler | Module:line (def) | Confirm gate | Cite |
|---|---|---|---|---|
| /v1/triggers/run | `handle_triggers_run` | routes_ops.py:288 | yes | routes_ops.py:296 |
| /v1/escalate | `handle_escalate_run` | routes_ops.py:311 | yes | routes_ops.py:319 |
| /v1/profile/editor/{profile} | `handle_profile_editor_put` | routes_profile_editor.py:328 | yes | routes_profile_editor.py:337 |
| /v1/sessions/{id}/retire | `handle_sessions_retire` | routes_sessions.py:241 | yes | routes_sessions.py:250 |
| /v1/sessions/{id}/compact | `handle_sessions_compact` | routes_sessions.py:278 | yes | routes_sessions.py:289 |
| /v1/memory/{node_id}/delete | `handle_memory_delete` | routes_memory.py:274 | yes | routes_memory.py:278 |
| /v1/memory/{node_id}/edit | `handle_memory_edit` | routes_memory.py:301 | yes | routes_memory.py:305 |
| /v1/cards | `handle_create_card` | routes_actions.py:197 | yes | via `_action_fields` routes_actions.py:181 |
| /v1/review/{card_id}/merge | `handle_merge_card` | routes_actions.py:210 | yes | routes_actions.py:220 |
| /v1/template/apply | `handle_template_apply` | routes_actions.py:279 | yes | via `_action_fields` routes_actions.py:181 |
| /v1/cluster/stop | `handle_cluster_stop` | routes_actions.py:301 | yes | via `_action_fields` routes_actions.py:181 |
| /v1/orchestrator/chat | `handle_orchestrator_chat` | routes_orchestrator.py:1390 | yes | routes_orchestrator.py:1409 |
| /v1/autodown/enable | `handle_autodown_enable` | routes_autodown.py:245 | yes | routes_autodown.py:254 |
| /v1/autodown/disable | `handle_autodown_disable` | routes_autodown.py:309 | yes | routes_autodown.py:312 |
| /v1/autodown/wake | `handle_autodown_wake` | routes_autodown.py:326 | yes | routes_autodown.py:338 |
| /v1/autodown/cancel | `handle_autodown_cancel` | routes_autodown.py:360 | yes | routes_autodown.py:363 |
| /v1/kanban/blocked/{id}/recover | `handle_kanban_recover` | routes_kanban.py:166 | yes | routes_kanban.py:169 |
| /v1/cluster/up | `handle_cluster_up` | routes_ops.py:348 | yes | routes_ops.py:351 |
| /v1/cluster/down | `handle_cluster_down` | routes_ops.py:370 | yes | routes_ops.py:373 |

## 3. Matrix / verdict

Every client POST sends confirm; every corresponding server handler requires it.
**No mismatch in either direction.**
- Client omits confirm but server requires it → **0 findings** (all 19 send it).
- Client sends confirm but server never checks → **0 findings** (all 19 handlers gate).

(For completeness: the remaining server-only POST routes the iOS app does NOT
call — /v1/projects/new, /v1/kanban/task/{id}/kill, /v1/profile/install,
/v1/profile/export, /v1/profiles/create|delete|rename|describe — are each
confirm-gated too, so there is no ungated mutation anywhere on the server.)

## 4. Regression guard

`hscc-api/tests/test_contract_swift_routes.py` was extended with
`test_every_client_post_server_handler_is_confirm_gated`, which resolves each
derived client POST route to its `api_server.ROUTES` handler and asserts the
handler body (docstring stripped) calls `_require_confirm(` or `_action_fields(`.

- Existing `test_every_mutating_post_carries_confirm` guards the client→confirm
  direction (client forgets confirm → 409-on-the-phone → fails).
- New test guards the server→gate direction (handlers stops gating → ungated
  mutation → fails).

Both directions are **mutation-proven**: temporarily removing the
`_require_confirm` call from `handle_triggers_run` makes the new test FAIL with
`POST /v1/triggers/run ... UNGATED`, then restoring it returns the suite to green.

## Evidence / commands

- `git log -1` on `wt/t_c77f243f`: `fdc456e` (rebased onto dev b570906).
- `HSCC_TEST_PY=.../p313/bin/python  python -m pytest hscc-api/tests/test_contract_swift_routes.py -q` → `8 passed`.
- Cross-check script (enumerate client POSTs, resolve server handlers, strip
  docstrings, check for `_require_confirm`/`_action_fields` call sites) → 0 ungated.

## Limits (no iOS runtime on this host)

- **Proven:** source-level contract, server route registration (fixture-decoding
  & dispatch), regression guard logic. All via the Python suite.
- **NOT provable here:** actual runtime behaviour on an iOS device/simulator.
  The Swift code is not compiled or executed on this host. Claims are limited to
  what the source shows and what the server does.
