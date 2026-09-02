# Screen audit: MemoryView -- prove every element works

Task: t_1869d327 | Branch: audit/memoryview-t_1869d327 | Base: dev @ 81d6de5
Auditor: ios-engineer | Date: 2026-09-02

Audit target: `ios-app/Sources/HSCC/Views/MemoryView.swift` (314 lines)
Backed by: `routes_memory.py` (GET /v1/memory, POST /v1/memory/{node_id}/delete,
                                            POST /v1/memory/{node_id}/edit)

## Verdict

**MemoryView.swift itself is correct and proven end-to-end.** No clearly-broken
bug in the iOS surface; no code changes made to it.

**The whole screen is defeated by a BACKEND bug** (routes_memory.py), not an iOS
bug: the memory endpoint resolves a profile's memories dir through
`routes_profile._hermes_profiles_dir()`, which honors the `HERMES_HOME` env var.
The live API process runs with `HERMES_HOME=/Users/desac/.hermes/profiles/hscc-orch`
(the orchestrator's own profile dir), so the memory dir resolves to a
NON-EXISTENT path and every `GET /v1/memory` returns "no memory store" (empty)
for EVERY profile -- while `/v1/sessions` for the same profiles returns full
data (119 rows). The operator's real device therefore sees an empty Memories
screen even though every profile holds real memories.

This is a backend fix (routes_memory.py profile resolution), outside the
ios-engineer lane; filed as a follow-up task.

---

## 1. DATA IN

Endpoint: `GET /v1/memory?profile=<name>` -- HSCCClient.swift:613-619
(`memories(profile:)` -> `get(path:"/v1/memory", queryItems:[profile])`).
Read-only, no confirm.

Model: `MemoryListResponse` (Models.swift ~1101) with `memories:[MemoryItem]?`,
`count:Int?`, `speak:String`. `MemoryItem` (Models.swift ~1104) carries
`id`, `nodeID`, `source`, `kind`, `timestamp:Int?`, `title:String?`,
`body:String?`, `sourceLabel` (computed). Backend shape matches
(routes_memory.py:152-159 emits `id / node_id / source / kind / timestamp /
title / body`; count + speak at handler).

**Live response (read-only).** Derived address from `hscc api status`, token
`~/.hscc/api-token`; host redacted below as `100.64.0.1` (public-repo guard):

    GET /v1/memory?profile=hscc-orch  ->  200
    {"profile":"hscc-orch","memories":[],"count":0,"memory_count":0,
     "profile_count":0,"speak":"Profile 'hscc-orch' has no memory store."}

    GET /v1/memory?profile=ios-engineer -> 200, same empty "no memory store"
        (even though /Users/desac/.hermes/profiles/ios-engineer/memories/MEMORY.md
        exists with 7 entries on this host)

    GET /v1/memory?profile=backend-engineer -> 200, same empty

**Every field the view needs arrives** (all decode), but the DATA ITSELF is
empty because of the backend resolution bug. Not an iOS data-in problem.

Cross-check (api_route_sweep.py): `/v1/memory` is swept (needs `?profile`,
supplied) and answers 200. The interpolated `/v1/memory/<id>/delete` and
`/v1/memory/<id>/edit` are dynamic and not swept (they need a live node id and
would fire a mutation); verified directly below.

Executed-proof decode of the POPULATED shape (committed fixtures):
`bash ios-app/scripts/model_decode_check.sh` -> `OK memory_list.json ->
MemoryListResponse`, `OK memory_delete.json -> MemoryMutationResponse (delete)`,
`ALL DECODE CHECKS PASSED -- 48/48`. So the iOS models match the server's
populated shape.

## 2. RENDER

For a loaded profile the operator sees (MemoryView.swift):
- `state.speak` (line 145) -- server-authored line, e.g. "N memories for
  <profile> (X notes, Y profile)." Rendered `.italic().secondary`.
- One card per memory (lines 153-158): title (line 176, `?? "(untitled)"`),
  source badge (line 179 via `sourceBadge`, shows `memory`/`profile`), FULL body
  (line 183, unabridged -- contract line 181-182), then Correct + Delete (192-208).

Dropped fields:
- `timestamp` (Models.swift ~1106) is present in the model but NEVER rendered.
  LOW severity, arguably intentional (cards don't need a timestamp; the operator
  edits by content). Noting, not fixing.
- Client does NOT render its own count -- it renders the server's `speak`.
  Line 149 takes `items = state.memories ?? []` and `ForEach` (line 153) over
  exactly those items. `memories` decodes all-or-nothing (a decode failure
  throws -> `.failed`), so rows ALWAYS == the server's count. No truncation,
  no client/server count disagreement is possible on this screen.

## 3. STATES

switch on `LoadState` (line 137):
- loading    -> `ProgressView()` (line 139). Spinner. Distinct.
- failed     -> `errorLabel(message)` (line 142) = `Label(message,
               systemImage:"exclamationmark.triangle.fill")` in
               `Theme.Semantic.bad` (line 231-235). Red triangle. Distinct.
- loaded     -> speak line + rows, or `emptyLabel("This profile holds no
               memories.")` (line 151) = tray icon in onSurfaceMuted
               (line 237-241).

empty ≠ error, confirmed: empty shows tray + "This profile holds no memories."
(text differs from `speak` line above it); error shows red triangle + message.
NEVER visually identical. **Proven by source (lines 137-163).**

**stale/offline: NONE.** `load()` (lines 112-121) calls `client.memories`
directly -> on failure sets `.failed`. There is NO `Offline.load` fallback and
the client never caches this call (HSCCClient.swift:260-262 caches only when
`queryItems.isEmpty`; `memories(profile:)` always sends a query item), so there
is nothing to fall back to. A single failed request blanks the screen. This is a
SYSTEMIC limitation (affects every query-param GET: sessions, activity/feed,
memory, fleet/stats, kanban stale), not MemoryView-specific. Fixing it properly
requires a profile-scoped cache key in HSCCClient -- cross-cutting, out of scope
for this screen audit. Reported, deliberately not fixed.

## 4. CONTROLS

1. Profile TextField `.onSubmit` -> `load(client)` (line 91). Feedback: list ->
   `.loading` spinner then rows/error.
2. "Load" Button -> `load(client)` (lines 92-96). Feedback: same as above.
3. "Correct" Button -> `editingItem = item` (line 192) -> `.sheet(item:)`
   (line 53) presents MemoryEditSheet. Opening is NON-destructive (contract
   lines 189-191); nothing mutates till the in-sheet Save. Feedback: sheet.
4. "Delete" MutationButton (lines 197-208) -> confirm dialog ->
   `client.deleteMemory(nodeID:profile:)` (always `confirm:true`,
   HSCCClient.swift:626-631) -> `reloadAfterMutation` -> returns `r.speak`.
   Feedback: confirm dialog + post-action reload + MutationButton success alert
   ("Done: Deleted the memory \"<title>\".").
   **Confirm text renders the item name**: line 201-202 prompt =
   `Delete the memory "<title>"? This removes it permanently from <sourceLabel>.`
   `titleDisplay` = `item.title ?? "(untitled)"` (line 223-225). PROVEN by source.
5. In sheet, "Save" MutationButton (lines 283-294) -> confirm ->
   `client.editMemory(nodeID:profile:content:)` (always `confirm:true`,
   HSCCClient.swift:637-645) -> `onSaved()` (reload) -> `dismiss()`. Disabled
   when unchanged or blank (line 295). Feedback: sheet closes + list reloads on
   success; on failure the sheet STAYS and MutationButton shows the error alert
   (dismiss() is after the throwing await). Slight quirk: on success the
   returned `r.speak` is discarded (sheet dismisses before the success alert can
   present) -- feedback is the row update, which is adequate. Minor.

Route answers, proven live (read-only; delete/edit on an empty profile is a
no-op 404 since the store is unresolved, which still proves the route + gate):
- DELETE without confirm -> 409 `confirm_required`  (gate wired, correct)
- DELETE with confirm:true  -> 404 `profile_unreachable`
  (route reached, confirm accepted, then env bug makes the store unresolvable)
- EDIT  with confirm:true  -> 404 `profile_unreachable` (same)

Backend proof the mutations themselves work when pointed at a real store:
`pytest tests/test_routes_memory.py` -> **17 passed** (hermetic, real files,
real HTTP loopback; covers list + delete + edit including confirm gate).

## 5. OBSERVATION

`client` is declared `let client: HSCCClient?` (line 28). `HSCCClient` is a
**struct** (HSCCClient.swift:114), NOT an ObservableObject -- so a plain `let`
is correct; there is no re-render bug here. (Contrast: the "switch tabs" bug
only bites with ObservableObjects held as `let`.)

All reactive state is `@State`: `profile`, `list`, `editingItem` (lines 30-32).
No `@StateObject` anywhere, hence nothing keyed by a changing value, hence no
stale first-instance-after-navigation bug. MemoryView is created fresh as a
NavigationLink destination (ClusterView.swift:218) each time it's pushed, so
`@State` reinits and `.task` (line 48) reloads on every visit. CLEAN.

## 6. LAYOUT

ScrollView (line 35) + VStack with `.frame(maxWidth:.infinity, alignment:
.leading)` (lines 37, 165). Rows `VStack(alignment:.leading)` (line 174) with
title HStack (line 175) that wraps under `titleDisplay`. Body Text wraps
naturally (line 183). TextEditor `minHeight:200` + `.large` detent (272, 312).
No fixed widths, no hardcoded point constraints -> survives Dynamic Type headline
sizes and iPhone SE width. `HStack(spacing:12)` at line 188: Correct + Delete +
Spacer -- on the narrowest SE width the two labels still fit (short titles).
No truncation-risk layout. Clean.

## 7. ACCESSIBILITY

Every control uses `Label(_, systemImage:)` WITH text: Profile (line 85), Load
(line 95), Correct (line 195), Delete (line 198), Save (line 284). None are
icon-only. Colour is never the only signal -- the source badge has text (line
216), the error label has icon+text (line 232), the empty label has icon+text
(line 238). `Text(state.speak)` dimmed `.secondary` still carries text.
`.textInputAutocapitalization(.never)` + `.autocorrectionDisabled()` on the
profile field (lines 88-89) prevent it mangling profile names. CLEAN.

---

## Findings, ranked by likelihood the operator hits them

1. **HIGH -- Backend: memory endpoint resolves the wrong profile dir in the
   live deployment, so the Memories screen is EMPTY for every profile.**
   routes_memory.py:73-81 uses `routes_profile._hermes_profiles_dir()`
   (routes_profile.py:58-60) which returns `$HERMES_HOME/profiles`. The live API
   process runs with `HERMES_HOME=/Users/desac/.hermes/profiles/hscc-orch`
   (verified: `ps eww -p <api_pid>`), so the memory dir is
   `/Users/desac/.hermes/profiles/hscc-orch/profiles/<p>/memories` == NON-EXISTENT
   for every profile -> `_memory_dir` returns None -> empty list + "no memory
   store". In CONTRAST, the sessions endpoint resolves via
   `hermes_cli.profiles` (routes_orchestrator.py:509-520) and returns FULL data
   (119 sessions live). Proof: simulated in a subshell -- with the leaked
   HERMES_HOME `_memory_dir` is None; without it, it resolves to the real dir
   and populates. Routed to backend-engineer as a follow-up task.

2. **MEDIUM -- Systemic: no offline/stale fallback for query-param GETs.**
   MemoryView `load()` (112-121) has no Offline.load; the client only caches
   empty-`queryItems` GETs (HSCCClient.swift:260-262), so `memories(profile:)`
   (always has a query item) is never cached and a single failure shows "failed
   to load" with no last-known data. Affects all query-param screens, not just
   MemoryView. Requires a profile-scoped cache key in the client. Deliberately
   NOT fixed here (cross-cutting).

3. **LOW -- `timestamp` never rendered** (Models.swift ~1106 present;
   MemoryView.swift shows title/body/source only). Arguably intentional;
   noted, not fixed.

4. **LOW -- committed fixture `node_id` shape drift.** model_decode_check
   fixture `memory_list.json` uses node ids like
   `memory:memory:backend-engineer:0` (profile embedded), but the real backend
   emits `memory:memory:<index>` (routes_memory.py:70 `_NODE_RE` =
   `^memory:(memory|profile):(\d+)$`; ids built `f"memory:{source}:{idx}"`).
   The iOS client treats `nodeID` as OPAQUE (round-trips it into the URL,
   HSCCClient.swift:627, 639), so this causes NO runtime bug -- only the
   fixture is testing an unrealistic id. Not fixed (zero impact; would be
   fixture churn).

## What was fixed

**Nothing in MemoryView.swift.** The iOS surface is correct as written. Making
changes with no clear defect would violate "do not invent problems."

## What was deliberately NOT fixed, and why

- **Backend profile-resolution bug (finding 1):** backend code + deployment
  env, not the iOS lane; fixing `routes_memory.py` needs the hscc-api engineer.
  Filed as follow-up task.
- **Offline/stale gap (finding 2):** systemic across many screens; correct fix
  is a cross-cutting profile-scoped cache key in HSCCClient, out of scope for a
  single-screen audit.
- **Fixture node_id drift (finding 4):** zero runtime impact.

## Evidence trail (executed)

- `bash ios-app/scripts/build_check.sh`                -> 57 files, 0 err/0 warn
- `bash ios-app/scripts/model_decode_check.sh`         -> 48/48 pass (memory OK)
- `pytest tests/test_routes_memory.py`                 -> 17 passed
- `scripts/api_route_sweep.py`                         -> /v1/memory 200
- live GET /v1/memory?profile=<p> (curl + token)       -> 200 empty "no store"
- live GET /v1/sessions?profile=hscc-orch              -> 200 POPULATED 119 rows
- live DELETE /edit (curl, confirm gate)               -> 409 (no confirm) /
                                                          404 profile_unreachable
- profile-resolution simulation (python, with/without
  HERMES_HOME)                                          -> None vs. real dir

All real addresses redacted (tailnet host -> 100.64.0.1); no secrets in this
report. No code changes in this audit.
