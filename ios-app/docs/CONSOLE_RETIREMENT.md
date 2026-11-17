# CONSOLE RETIREMENT — what `ios-app/` deletes, and which checks die with it

Card: `t_100920c3` · written on `origin/main` @ `a972ee5d` · **this document deletes
nothing.** It is the list the deletion commits are cut from.

The pivot: `ios-app/` stops being a SwiftUI operator console and becomes a
**widget + Live Activity client**. The app keeps connection settings and a small
widget/Live Activity config screen. Everything else in the console goes.

A deletion that strands a check is a red gate, and a red gate blocks the next
card. So every row in §2 names the checks that must die **in the same commit**
as its subject (§4 is the same map read from the script side).

---

## 0. Scope of this document

| In | Out |
| --- | --- |
| File-by-file disposition of all **96** Swift files under `ios-app/Sources/` | Any actual deletion |
| The `project.yml` per-target `sources:` sets that change (§3) | Test authoring for the new root screen |
| Which of the scripts in `ios-app/scripts/` lose their subject (§4) | Server-side endpoint retirement (the API keeps serving what the widgets read) |
| The commit order that never leaves the gate red (§5) | A decision on whether Tier-B screens survive — §1.4 asks it |

`ios-app/scripts/` holds **44 `*.sh`**, **4 standalone `*.swift`** harnesses
(`shared_model_check.swift`, `slash_palette_check.swift`, `slash_preview_check.swift`,
`widget_kanban_decode_check.swift`), **7 `*.py`** (4 top-level + 3 `run.py` live
probes) and one `<name>_check/` directory per harness (`main.swift`, sometimes
`fixtures/`). Every verdict in §4 covers the `.sh` **and** its harness directory:
deleting the script but leaving `scripts/<name>_check/main.swift` keeps
`check_sources.sh`-style drift invisible and leaves a dangling compile target.

## 1. The rule that decides every row

### 1.1 End state

The app process exists to (a) hold the connection, (b) configure the surfaces,
and (c) feed the surfaces that render outside the app:

- **Home Screen widgets** — `HSCCWidgets` (cluster + monitor).
- **Live Activities** — wake (`HSCCLiveActivity`), monitor (`HSCCLiveActivityMonitor`),
  session (`HSCCLiveActivitySession`, see D1).
- **Drivers that push into them** — `LiveActivityManager`, `MonitorActivityDriver`,
  `SessionActivityDriver`, plus the notify engine (`Notify/`).
- **Settings** — the connection (host/token/QR) and the per-surface widget/LA
  settings (`WidgetSettingsStore` + the `WidgetSurfaceEditor` that lives inside
  today's `Views/SettingsView.swift:511-700`).

### 1.2 The two-question tie-break

For each file, in order:

1. **Does a surviving surface (widget, Live Activity, driver, notify engine,
   settings) read it — directly or through a shared model?** → KEEP.
2. **Is it a *mutation* (POST/PATCH/DELETE, send, approve, merge, dispatch,
   apply, wake, stand down)?** → DELETE, unconditionally. "No actions" is not
   negotiable in this pivot; the console's write paths are the reason it is
   being retired.

Everything that is neither — a read-only *screen* — is **Tier B** and goes
unless §1.4 says otherwise. "Read-only" is not sufficient grounds to keep a
screen; the console already proved that read-only screens keep growing buttons.

### 1.3 Verdicts used in §2

| Verdict | Meaning |
| --- | --- |
| **KEEP** | Untouched. Compiles as today. |
| **KEEP/PRUNE** | File stays, contents shrink (endpoint paths, model structs, enum cases). A named edit, not a delete. |
| **COLLAPSE** | File is replaced by a smaller one; some of its code moves into the survivor. |
| **DELETE** | `git rm`, plus every check named beside it, same commit. |

### 1.4 Decisions this card cannot make (blocking, one line each)

**D1 — does the session Live Activity survive?**
`HSCCLiveActivitySession` renders `SessionActivityAttributes`, and the only
thing that ever fills it is `SessionActivityDriver`, fed by
`StreamingTranscript` + `SessionEvent` + `SessionStreamCursor` folding the
session event stream — the same stream today's streaming *chat* folds. Two
coherent answers:
- **D1a (recommended): keep the stream layer, delete the chat UI.** Keep
  `SessionEvent`, `SessionStreamCursor`, `StreamHello`, `StreamConnectionError`,
  `StreamingTranscript`, `SessionActivitySummary`, `SessionActivityDriver`
  (all non-UI), delete every `Views/*Chat*`. Cost: the WS plumbing and its
  four checks (`streaming_check`, `reconnect_check`, `ws_decode_check`,
  `session_activity_check`) stay alive with no screen attached, and something
  has to *choose* which session to mirror — a picker on the config screen.
- **D1b: retire the session Live Activity too.** Then `Shared/SessionActivity.swift`,
  the whole `HSCCLiveActivitySession` target, the stream layer and those four
  checks all go, and `model_decode_check` loses the `v1_session_events*`
  fixtures. Cleanest end state; loses a Live Activity the operator built.

**D2 — do the read-only console screens (Tier B) survive as read-only, or die?**
Fleet, Nodes & serving, Ops & health, Logs, Cron, Cards, Projects, Activity
feed, Search, Memory-read. The card body's "keep only connection settings and
a small widget/LA config screen" reads as **die**, and the widget + monitor LA
already carry that data outside the app. §2 lists them as **DELETE (Tier B)**
and §4 costs each one. If the operator wants any of them kept, keep the whole
screen *and* its check and cross that row out — do not keep a screen by
grafting it onto the config view.

**D3 — does anything still need fixtures?** `DebugHarness.swift` is the
`HSCC_DEBUG_FIXTURES` / `HSCC_DEBUG_SCREEN` seam, and it is the only reason the
`HSCCUITests` target, `nav_graph_check.sh`, `nav_graph_screens.py`,
`capture_screens.sh`, `seed_fixtures.sh` and `nav_probe_drawer.sh` exist. With
AppShell and Sidebar gone there is no drawer to walk and no screen table to
derive, so **the whole tap-free navigation lane dies with DebugHarness**
(§4.1). Recommendation: yes, retire it; the surviving two screens need their
own small UI test, which is a separate card, not a repair of this lane.

**D4 — `Views/DisplayJSON.swift`, `Views/HSCCMark.swift`,
`Views/HealthCheckIndicator.swift`.** Three 37-71 line helpers used only by
deleted screens today. Default DELETE with their consumers; keep `HSCCMark` if
the new root wants the brand mark, keep `HealthCheckIndicator` if the config
screen keeps a live health row (recommended: keep both those two, delete
`DisplayJSON`).

## 2. Swift file disposition (all 96)

Line counts are from `wc -l` on `origin/main` @ `a972ee5d`. "Kills" = checks
that must be deleted or repointed **in the same commit** (full map: §4).

### 2.1 Tier A — deletions named by the card body (29 files, 8,253 LOC)

| File | LOC | Kills with it |
| --- | --- | --- |
| **Streaming chat — UI + store** | | |
| `Sources/HSCC/Views/StreamingChatView.swift` | 1094 | `no_op_handler_check` (subject set shrinks), nav lane (D3) |
| `Sources/HSCC/Views/StreamingChatStore.swift` | 561 | `turn_state_check.sh` (compiles it, line 41) |
| `Sources/HSCC/Views/ChatStore.swift` | 276 | `chat_state_check.sh`, `offline_queue_check.sh` (both **already red**, §6) |
| `Sources/HSCC/Views/ChatEntry.swift` | 110 | same two — they slice `ChatEntry` from the **renamed-away** `Views/OrchestratorChatView.swift` (`chat_state_check.sh:40`, `offline_queue_check.sh:40`) |
| `Sources/HSCC/Views/ComposerText.swift` | 49 | `voice_draft_check.sh` (its only subject) |
| `Sources/HSCC/Views/SlashCommandPalette.swift` | 178 | `slash_palette_check.swift` |
| `Sources/HSCC/Views/SlashCommandPreview.swift` | 240 | `slash_preview_check.swift` |
| `Sources/HSCC/Views/StreamReplyWatcher.swift` | 127 | `reply_watcher_check.sh` |
| `Sources/HSCC/Views/ChatsRootView.swift` | 137 | nav lane (`.chats` drawer row) |
| `Sources/HSCC/Views/ProjectUnreadCenter.swift` | 102 | `reply_watcher_check.sh`. Consumers are only `HSCCApp` (env object), `DebugHarness` and deleted views — `Notify/NotificationCoordinator.swift` names it at `:18`/`:62` in **comments only**, so no Notify edit is forced |
| `Sources/HSCC/OfflineSendQueue.swift` | 254 | `offline_queue_check.sh` (mutation path — rule 2) |
| **Sessions / history / diff** | | |
| `Sources/HSCC/Views/SessionsView.swift` | 313 | `offline_cache_fix_check` asserts `/v1/sessions` + `SessionsListResponse` (`main.swift:113`) |
| `Sources/HSCC/Views/SessionHistoryView.swift` | 454 | — |
| `Sources/HSCC/Views/DiffDetailView.swift` | 307 | `diff_model_check.sh` + `scripts/diff_model_check/fixtures/v1_review_diff.json` |
| **Approvals** | | |
| `Sources/HSCC/Views/ApprovalsView.swift` | 428 | `live_activity_rehydration_check` unaffected (wake LA only) |
| `Sources/HSCC/Views/ApprovalBadge.swift` | 51 | — |
| **Memory** | | |
| `Sources/HSCC/Views/MemoryView.swift` | 494 | `model_decode_check` fixtures `memory_list/​memory_delete`; `HSCCClient.deleteMemory` (`HSCCClient.swift:652`) is a mutation → rule 2 |
| **Ops / health** | | |
| `Sources/HSCC/Views/OpsView.swift` | 369 | `live_decode_check` fixture `v1_escalate` |
| **Autodown / self-heal** | | |
| `Sources/HSCC/Views/AutodownView.swift` | 645 | `model_decode_check` fixtures `autodown_{status,enable,disable,cancel,wake}` |
| **Templates** | | |
| `Sources/HSCC/Views/TemplatesView.swift` | 262 | `model_decode_check` fixtures `template_list/template_status` |
| `Sources/HSCC/Views/TemplateDetailView.swift` | 561 | `model_decode_check` fixtures `template_apply/template_preview*` |
| `Sources/HSCC/Views/TemplateTopologyView.swift` | 138 | — |
| `Sources/HSCC/Views/AppliedTemplateStatus.swift` | 99 | — |
| **DebugHarness (D3)** | | |
| `Sources/HSCC/DebugHarness.swift` | 402 | `capture_screens.sh`, `seed_fixtures.sh`, `nav_probe_drawer.sh`, `nav_graph_check.sh`, `nav_graph_screens.py`, the **whole `HSCCUITests` target**, and 10 `#if DEBUG` branches in `HSCCApp.swift` |
| **ProjectIntents + the action intents** | | |
| `Sources/HSCC/Intents/ProjectIntents.swift` | 304 | `AppShortcuts.swift` entries for `AskOrchestratorIntent`; `model_decode_check` fixture `orchestrator_chat` |
| `Sources/HSCC/Intents/ApprovalsIntent.swift` | 48 | `AppShortcuts.swift` |
| `Sources/HSCC/Intents/ReviewQueueIntent.swift` | 36 | `AppShortcuts.swift` |
| `Sources/HSCC/Intents/CannedCard.swift` | 120 | `AppShortcuts.swift`; fixture `dispatch_card` |
| `Sources/HSCC/Intents/AppShortcuts.swift` | 94 | nothing (it only registers the four above) — delete it with them, or keep it as the one-shortcut provider for `ClusterStatusIntent` |

### 2.2 Tier B — read-only console screens (D2; 16 files, 4,584 LOC)

Each is read-only, so each survives rule 2 and dies only on "keep only
settings". Every one has a check attached: killing the screen silently is what
turns the gate red.

| File | LOC | Kills with it |
| --- | --- | --- |
| `Sources/HSCC/Views/FleetView.swift` | 738 | `fleet_offline_check` (survives — subject is `LoadState`), `fleet_stats_render_check`, `offline_cache_fix_check` (`/v1/fleet/stats`) |
| `Sources/HSCC/FleetServing.swift` | 169 | `fleet_models_check.sh` (its only subject, `fleet_models_check.sh:49`) |
| `Sources/HSCC/Views/NodesServingView.swift` | 470 | `serving_placement_check.sh`, `model_decode_check` fixture `cluster_hosts` render claims |
| `Sources/HSCC/Views/NodeTopologyView.swift` | 97 | — |
| `Sources/HSCC/ServingPlacement.swift` | 263 | `serving_placement_check.sh` (compiles it directly) |
| `Sources/HSCC/Views/LogsView.swift` | 210 | `logs_redactor_check.sh` |
| `Sources/HSCC/Views/LogRedactor.swift` | 95 | `logs_redactor_check.sh` (its only subject). **Security note:** this is the app's second redaction line; if any surviving surface ever prints server log text, it must stay |
| `Sources/HSCC/Views/CronView.swift` | 202 | fixture `cron_list`, `seed_fixtures.sh` MAIN_LIST |
| `Sources/HSCC/Views/CardsView.swift` | 151 | *(harness gone — `widget_kanban_decode_check.swift` was deleted by t_595a5fee; the widget card decoders are covered by `model_decode_check` against the real types)* |
| `Sources/HSCC/Views/CardsRootView.swift` | 130 | nav lane (`.cards`) |
| `Sources/HSCC/Views/ProjectsView.swift` | 976 | `live_decode_check` fixture `v1_projects`; `ProjectStatusIntent` if D1b |
| `Sources/HSCC/Views/ActivityFeedView.swift` | 277 | `offline_cache_fix_check` (`/v1/activity/feed`, `main.swift:98-114`), `model_decode_check` fixture `v1_activity_feed` |
| `Sources/HSCC/Views/SearchView.swift` | 269 | fixtures `commands`, `v1_qa` |
| `Sources/HSCC/Views/ProfileEditorView.swift` | 261 | fixtures `v1_profile_editor`, `profiles_list` — it is also a **mutation** screen (writes profiles), so rule 2 deletes it regardless of D2 |
| `Sources/HSCC/Views/CreateCardSheet.swift` | 166 | mutation (POST card) → rule 2, regardless of D2 |
| `Sources/HSCC/Views/MutationSupport.swift` | 110 | all mutation buttons → rule 2, regardless of D2 |

### 2.3 Tier C — the AppShell/HomeView/SettingsView collapse (5 files)

| File | LOC | Verdict | Notes |
| --- | --- | --- | --- |
| `Sources/HSCC/Views/AppShell.swift` | 630 | **DELETE** | `AppDestination` (22-26 + the four legacy aliases 36-59), its `title`/`symbol` switches, `normalized`, `projectRouteView` and the drawer-over-NavigationStack shell. `nav_graph_screens.py` parses exactly these anchors (`:47-48,68,99,122`) — with them gone the generator `die()`s by design |
| `Sources/HSCC/Views/Sidebar.swift` | 211 | **DELETE** | the drawer rows (`row(.x`) the walker taps |
| `Sources/HSCC/Views/HomeView.swift` | 311 | **DELETE** | the dock (`DockItem.symbol` at `:50`, the second `var symbol: String` extractor target in `sf_symbol_check.sh`) |
| `Sources/HSCC/ContentView.swift` | 113 | **DELETE** | already legacy (header lines 5-6: "The three-tab TabView root was REPLACED by AppShell"). `shared_store_check.sh:46,61` compiles it and asserts "nothing in ContentView reads appGroupUnavailable" → must be re-pointed at the new root, not deleted |
| `Sources/HSCC/Views/SettingsView.swift` | 746 | **COLLAPSE → `Sources/HSCC/Views/ConfigView.swift`** | Keep: the connection Sections (host/token/test-connection/QR entry, ~`63-186`), the App-Group warning row (~`194-202`), `widgetsAndLiveActivitiesRows` (`:511-557`) and `WidgetSurfaceEditor` (`:611-700`). Drop: everything that routed into a console screen |

New root file (one addition, must be added to `project.yml` **and** to
`build_check.sh`'s per-target sets automatically via `project.yml`):
`Sources/HSCC/Views/ConfigView.swift` — the only screen. Suggested shape:
connection section → per-surface widget/LA section → (D1a) session-picker
section. `HSCCApp.swift` roots it and drops the `unread` / `replyWatcher` /
`router` environment objects it no longer needs.

### 2.4 Tier D — KEEP, untouched (41 files)

`Sources/Shared/`: `SharedModels.swift`, `ExtensionClient.swift`,
`MonitorActivity.swift`, `WakingActivity.swift`, and `SessionActivity.swift`
(unless D1b).

`Sources/HSCCWidgets/`: `HSCCWidgetsBundle.swift`, `ClusterWidget.swift`,
`ClusterWidgetViews.swift`, `MonitorWidget.swift`, `MonitorViews.swift`.

`Sources/HSCCLiveActivity/`+`…Session/`+`…Monitor/`: `HSCCLiveActivity.swift`,
`HSCCLiveActivitySession.swift` (unless D1b), `MonitorLiveActivity.swift`.

`Sources/HSCC/`: `APIError.swift`, `ConnectionMonitor.swift`,
`KeychainStore.swift`, `SettingsStore.swift`, `WidgetSettingsStore.swift`,
`SetupQRCode.swift`, `StateCache.swift`, `LiveActivityManager.swift`,
`MonitorActivityDriver.swift`, `SessionActivityDriver.swift`,
`SessionActivitySummary.swift`, `SessionEvent.swift`, `SessionStreamCursor.swift`,
`StreamHello.swift`, `StreamConnectionError.swift`, `StreamingTranscript.swift`
(last six: KEEP only under D1a), `Intents/ClusterStatusIntent.swift`,
`Intents/IntentClient.swift`, `Views/Theme.swift`, `Views/LoadState.swift`,
`Views/QRScannerView.swift`, `Views/HSCCMark.swift` (D4),
`Views/HealthCheckIndicator.swift` (D4), `Notify/OperatorAlert.swift`,
`Notify/NeedsOperatorNotifier.swift`, `Notify/NotificationsAppDelegate.swift`,
`Notify/NotificationCoordinator.swift`, `Notify/ObservedState+LastSeenState.swift`.

### 2.5 Tier E — KEEP/PRUNE (4 files; the silent-red ones)

These files survive, so no `rm` reminder appears next to them — and that is
exactly how a gate goes red unnoticed: the check still compiles the file, but
a type or endpoint inside it disappears.

| File | LOC | Prune | Breaks if the prune is not paired with |
| --- | --- | --- | --- |
| `Sources/HSCC/Models.swift` | 1664 | response structs for dead screens (autodown, memory, template, diff, cards-kanban, ops, logs, cron, activity feed, sessions, profiles) | `scripts/model_decode_check/main.swift` **names** each type and `fixtures/*.json` (55 entries) each payload; `live_decode_check`, `diff_model_check`, `ws_decode_check`, `fleet_stats_render_check`, `connection_probe_check`, `offline_cache_fix_check` compile the same file. Rule: **a struct, its fixture and its `main.swift` check leave in one commit** |
| `Sources/HSCC/HSCCClient.swift` | 926 | `EndpointPath` cases for dead screens; **all** mutation verbs (`post` at `:222`, `deleteMemory` at `:652`) | `offline_cache_fix_check/main.swift` asserts cache keys `/v1/sessions`, `/v1/fleet/stats`, `/v1/activity/feed`; `capture_live.sh` + `compare_fixtures_live.py` iterate a live endpoint list that must be trimmed to survivors |
| `Sources/HSCC/DeepLink.swift` | 226 | destination cases pointing at deleted screens; keep `hscc://settings` (+ whatever D1a's session picker needs) | `deeplink_check.sh` + `deeplink_check/Stubs.swift:8,18-20` which stub `ContentView.Tab` — the stub type dies with `ContentView.swift`, so the harness needs a new stub, not a deletion |
| `Sources/HSCC/HSCCApp.swift` | 113 | `#if DEBUG` DebugHarness branches (10 sites in `init()`), `unread`/`replyWatcher`/`router` objects, `AppShell()` root | `shared_store_check.sh` (asserts on the root's app-group read), `build_check.sh` (root file set) |

**Notify is NOT pruned.** `Notify/ObservedState+LastSeenState.swift` (5
signals: `reviewQueue`, `blocked`, `escalationsCount`, `apiReachable`,
`daemonRunning` — `:32-43`) looks like it belongs to the deleted approvals /
kanban / ops screens, but `NeedsOperatorNotifier` polls those endpoints
**itself**; it never reads a view's state. The whole `Notify/` family and its
`notify_check.sh` harness (compiles `OperatorAlert`, `ObservedState+LastSeenState`,
`NeedsOperatorNotifier` + a slice of `AppGroup` — `notify_check.sh:34,49-51`)
survive unchanged, and pruning their endpoints "because ApprovalsView is gone"
silently kills the operator alert. Same shape of trap as §4.4 trap 1.

**Net:** **50 files deleted** (29 Tier A + 16 Tier B + `ContentView`/`AppShell`/
`Sidebar`/`HomeView` + `DisplayJSON`), **1 collapse** (`SettingsView` →
`ConfigView`), **4 keep/prune**, **41 untouched** — 96 accounted for.
`wc -l` over the 50 deletions is **14,139 of 25,755 lines** of `Sources/`
(all four app-side targets counted).

## 3. `project.yml` — the per-target sets that change

`build_check.sh` reads each target's `sources:` list out of `project.yml`
(`build_check.sh:31-45`) and compiles **exactly** that subset, so `project.yml`
is the single place a deletion becomes visible to the gate. `check_sources.sh`
then fails both directions — a file on disk that is not listed, and a listed
file that is gone ("GHOST … the build will fail"). One `git rm` without its
`- path:` line removed = red; one `- path:` removed without the `git rm` = red.

| Target | Change |
| --- | --- |
| `HSCC` (105-246) | **~54 `- path:` lines removed**, incl. `DebugHarness.swift:119`, `Views/AppShell.swift:123`, `Views/Sidebar.swift:124`, `Views/HomeView.swift:126`, `ContentView.swift:134`, `OfflineSendQueue.swift:141`, the 5 chat/stream view lines `187-193`, `SessionHistoryView.swift:193`, `AutodownView.swift:194`, `ApprovalsView.swift:205`, `AppliedTemplateStatus.swift:208`, `MemoryView.swift:227`, `CronView.swift:229`, `CreateCardSheet.swift:230`, `DiffDetailView.swift:233`, `Intents/ProjectIntents.swift:240`. **1 line added**: `Sources/HSCC/Views/ConfigView.swift`. Re-generate with `scripts/generate.sh`, never bare `xcodegen generate` |
| `HSCC` `info.properties` (290-339) | `NSUserActivityTypes` / `CFBundleURLSchemes` stay (settings deep link). `NSCameraUsageDescription` stays (QR first-run). `BGTaskSchedulerPermittedIdentifiers` stays (monitor LA background push). Portrait-landscape list can drop the two landscape entries once no transcript/log view exists — cosmetic, gate-neutral |
| `HSCCWidgets` (343-392) | unchanged |
| `HSCCLiveActivity` (396-440) | unchanged |
| `HSCCLiveActivitySession` (451-495) | **whole target removed under D1b**; unchanged under D1a |
| `HSCCLiveActivityMonitor` (506-545) | unchanged |
| `HSCCUITests` (547-575) | **target removed (D3)** together with `UITests/DrawerNavigationUITests.swift`, `UITests/NavGraphUITests.swift`, `UITests/NavGraphScreenTable.swift`. The `HSCCUITests` *scheme* (93-101) goes with it. Note `UITests/NavGraphScreenTable.swift` is **generated** by `scripts/nav_graph_screens.py`; deleting the generated file without deleting the generator leaves a `check` step that `die()`s |
| `schemes.HSCC.build.targets` (70-77) | drop `HSCCLiveActivitySession` under D1b |
| `Sources/*.entitlements` + `Sources/*/Info.plist` | the D1b target's `HSCCLiveActivitySession.entitlements` and `Sources/HSCCLiveActivitySession/Info.plist` leave with the target |

**Regeneration trap:** after a rebase or a file-set change that ADDs files,
`xcodegen generate` must be re-run (`scripts/generate.sh`) or Xcode compiles a
stale project and reports 0 tests / missing types while `build_check.sh` stays
green — `build_check.sh` reads `project.yml`, Xcode reads the generated `.xcodeproj`.

---

## 4. Script cross-reference — which gate loses its subject

Verdicts: **RETIRE** = delete the `.sh` *and* its `scripts/<name>_check/`
directory in the same commit as the subject file. **REPOINT** = the subject
survives in a smaller form; the check must be edited (its harness names a type,
a path or a file that is going away, so it fails to compile or asserts a dead
claim). **SURVIVE** = subject fully intact, nothing to do.

### 4.1 RETIRE — 19 scripts + the nav lane

| Script (+harness dir) | Subject, with evidence | Dies with |
| --- | --- | --- |
| `chat_state_check.sh` + `chat_state_check/` | compiles `Views/ChatStore.swift`; slices `ChatEntry` from `Views/OrchestratorChatView.swift` (`:40`) — **already red, §6** | chat |
| `offline_queue_check.sh` + `offline_queue_check/` | `OfflineSendQueue.swift` + `ChatStore.swift`, same dead slice (`:40`) — **already red, §6** | chat |
| `turn_state_check.sh` + `turn_state_check/` (+`SpeakableStub.swift`) | compiles `Views/StreamingChatStore.swift` | chat |
| `reply_watcher_check.sh` + `reply_watcher_check/` (+`Runner.swift`) | `Views/StreamReplyWatcher.swift`, `Views/ProjectUnreadCenter.swift` | chat |
| `voice_draft_check.sh` + `voice_draft_check/` | `Views/ComposerText.swift` — its only subject | chat |
| `slash_palette_check.swift` (standalone) | `Views/SlashCommandPalette.swift` | chat |
| `slash_preview_check.swift` (standalone) | `Views/SlashCommandPreview.swift` | chat |
| `diff_model_check.sh` + `diff_model_check/` (+`fixtures/v1_review_diff.json`) | `DiffDetailResponse` + the diff view helpers | diff |
| `fleet_models_check.sh` + `fleet_models_check/` | `Sources/HSCC/FleetServing.swift` — its only subject (`real_derivation=`, `fleet_models_check.sh:45`) | Fleet (D2) |
| `fleet_stats_render_check.sh` + `fleet_stats_render_check/` | by_day/activity render replica of FleetView's stats section | Fleet (D2) |
| `serving_placement_check.sh` + `serving_placement_check/` | `Sources/HSCC/ServingPlacement.swift` (`real_parse=`, `serving_placement_check.sh:40`) | Nodes & serving (D2) |
| `logs_redactor_check.sh` + `logs_redactor_check/` | `Views/LogRedactor.swift` — its only subject | Logs (D2) |
| `sessions_row_check/` (**no `.sh`** — run ad hoc) | prints the computed fields `SessionsView` renders | Sessions (D2) |
| `nav_graph_check.sh` | the whole XCUITest lane: step 1 fails if `NavGraphScreenTable.swift` drifts from **AppShell/Sidebar** (`:76`), step 2 seeds fixtures via `seed_fixtures.sh` | shell collapse (D3) |
| `nav_graph_screens.py` | parses `Sources/HSCC/Views/AppShell.swift` + `Sidebar.swift` (`:47-48`), `die()`s if `enum AppDestination` (`:68`), `AppShell.normalized` (`:99`) or `AppShell.projectRouteView` (`:122`) is missing — i.e. it is designed to fail the moment AppShell goes | shell collapse (D3) |
| `capture_screens.sh` | drives `HSCC_DEBUG_SCREEN` / `HSCC_DEBUG_FIXTURES` (`:7`), both DebugHarness seams | shell collapse (D3) |
| `seed_fixtures.sh` | `MAIN_LIST` of 24 fixture names (`:12-16`), most for deleted screens; degrades silently (`[ -f ] … \|\| true`) rather than failing | shell collapse (D3) |
| `make_empty_fixtures.py` | called only by `seed_fixtures.sh` and `nav_graph_check.sh` | shell collapse (D3) |
| `nav_probe_drawer.sh` | dev probe, drawer-only | shell collapse (D3) |
| `UITests/DrawerNavigationUITests.swift`, `UITests/NavGraphUITests.swift`, `UITests/NavGraphScreenTable.swift` | the `HSCCUITests` target; `NavGraphScreenTable.swift` is **generated** by `nav_graph_screens.py` | shell collapse (D3) |

`docs/design/screens/` (the `capture_screens.sh` output) and `ios-app/README.md:400-403`
— which advertises `nav_graph_check.sh`, `capture_screens.sh` and "the 54
committed live fixtures" as the reproducible gate — go stale in the same commit
and must be edited there, not later.

### 4.2 REPOINT — 7 scripts (they go red if left alone; the last two rows need no edit)

| Script | What breaks | Fix |
| --- | --- | --- |
| `model_decode_check.sh` + `model_decode_check/main.swift` | compiles `Models.swift` + `SessionEvent.swift` and **names** each response type against `fixtures/` (55 entries). Prune a struct without its `main.swift` block → **compile error**; prune a fixture the block still decodes → runtime failure | struct + fixture + `main.swift` block leave together (S8) |
| `live_decode_check.sh` + `live_decode_check/` | same models, fed from `scripts/live_captures/` captured by `capture_live.sh` | trim its capture list with the endpoint prune |
| `capture_live.sh` | iterates a live endpoint list including every dead screen's route | trim to survivors (§4.4 trap 1) |
| `compare_fixtures_live.py` | diffs fixtures vs live captures for the same list | trim with it |
| `offline_cache_fix_check.sh` + `main.swift` | asserts cache keys `/v1/sessions` (`:54-56`), `/v1/fleet/stats` (`:79-80`), `/v1/activity/feed` (`:88-114`) and decodes `SessionsListResponse` / `ActivityFeedResponse` (`:98,:113`) — all three screens are Tier A/B | repoint at surviving endpoints (`/v1/cluster/monitor`, `/v1/daemon/host`) or retire with those screens |
| `deeplink_check.sh` + `deeplink_check/` (+`Stubs.swift`) | `Stubs.swift:8,18-20` stub `ContentView.Tab` "real: `Sources/HSCC/ContentView.swift`" — that file is deleted; the router's destination cases shrink too | re-stub against the new root / surviving `DeepLink` cases |
| `shared_store_check.sh` | `:46` compiles `Sources/HSCC/ContentView.swift`, `:61` fails if "nothing in ContentView reads `appGroupUnavailable`" | re-point at `ConfigView.swift` — the assertion is about the root screen reading the App-Group warning, which survives |
| `build_check.sh` | needs no edit (it parses `project.yml`), but it is the proof for every slice — and it fails on a listed-but-missing file, so `git rm` and `- path:` removal are one commit | no edit; run it per slice |
| `check_theme.sh` | survives untouched **because** its allowlist is content-anchored (`// theme-allow:` markers, header `:25-29`, after a line-number allowlist went stale). Deleting a file takes its markers with it | no edit |

### 4.3 SURVIVE — the remaining 21 scripts (subject intact; no action)

`actool_icon_check.sh` + `check_appicon.py` + `make_appicon.sh` (Assets.xcassets
— the pivot does not touch the icon); `generate.sh`; `check_sources.sh`
(mechanical — it is what makes a half-done deletion visible); `no_op_handler_check.sh`
(generic grep over `Sources/`; its subject set shrinks, never fails from a
deletion); `sf_symbol_check.sh` (see trap 3); `connection_banner_check.sh`,
`connection_probe_check.sh` (`ConnectionMonitor` + `Models` — the pill/banner
move to `ConfigView`; `connection_probe_check/main.swift:152` mentions
"AppShell's `reset()`" in a comment only, so update prose, not code);
`first_run_check.sh`, `first_run_check_settings.sh`, `qr_classify_check.sh`
(`SetupQRCode` + `SettingsStore` + `KeychainStore` — the surviving first-run
path); `fleet_offline_check.sh` (subject is `Views/LoadState.swift`, the
`Offline` enum, which the config screen still uses); `snapshot_store_check.sh`,
`widget_settings_check.sh`, `widget_view_dispatch_check.sh`
(`shared_model_check.swift` and `widget_kanban_decode_check.swift` were on this
list until t_595a5fee: both were mirrors that re-declared the shared structs and
neither was wired to any `.sh`. They are deleted, and `model_decode_check` covers
the same payloads against the real types — the counts in §1 above predate that.)
(`SharedModels` + `ClusterWidgetViews` — widget side, untouched);
`live_activity_rehydration_check.sh`, `monitor_activity_check.sh` + `run.py` +
`template.swift`, `monitor_datapath_live/`, `widget_datapath_live/` (monitor +
wake LA data paths); `notify_check.sh` + `notify_check/` (the operator-alert
engine — see the "Notify is NOT pruned" note in §2.5);
`fix_model_decode_chars.py` (fixture hygiene, survives
with the fixtures); `streaming_check.sh`, `reconnect_check.sh`,
`ws_decode_check.sh`, `session_activity_check.sh` — **SURVIVE under D1a,
RETIRE under D1b** (subjects: `StreamingTranscript`, `SessionStreamCursor`,
`StreamHello`, `StreamConnectionError`, `SessionEvent`,
`SessionActivitySummary`).

### 4.4 Traps — the failures that are not compile errors

1. **Pruning an `EndpointPath` the *widget* reads.** `Sources/HSCCWidgets/ClusterWidget.swift:138-142`
   fetches `/v1/autodown/status`, `/v1/cluster/status`, `/v1/kanban/running`,
   `/v1/kanban/blocked`, `/v1/kanban/stale`, and `MonitorWidget.swift:159-160`
   fetches `/v1/cluster/monitor` + `/v1/daemon/host` — i.e. the endpoints whose
   **screens** AutodownView and CardsView are being deleted. Deleting those
   paths "because AutodownView is gone" empties the widget with no compile
   error, and `build_check.sh` cannot see it. Pre-flight before any path prune:
   `grep -rn '"/v1/' Sources/` and keep every path a widget, Live Activity,
   driver or notifier literal still references.
2. **`model_decode_check` fixture vs struct.** A struct without its fixture and
   a fixture without its struct fail differently (runtime vs compile). Both are
   red. Delete the pair.
3. **`sf_symbol_check.sh` fails on ZERO extractions** (`:46-49`: "the grep
   patterns have rotted"). `AppShell.swift:93` and `HomeView.swift:50` hold the
   only two `var symbol: String` properties its second extractor reads (the
   brace-matching block at `:26-42`). After the shell collapse that extractor
   contributes nothing, so the surviving `Image(systemName:)` / `systemImage:`
   literals in the widget files must keep the total above zero — check the count
   line before trusting a green.
4. **A dangling harness directory.** `scripts/<name>_check/main.swift` compiles
   nothing once its `.sh` is gone, and nothing in the gate notices. Delete both
   halves; `git status --short` after `git rm -r scripts/<name>{.sh,}` must be
   clean.
5. **README and `docs/` go stale silently.** `ios-app/README.md:400-403` names
   `nav_graph_check.sh` and `capture_screens.sh` as the reproducible gate, and
   `ios-app/docs/` holds ~30 audits of screens this list deletes
   (`approvals-audit.md`, `autodownview-audit-t_9b678f46.md`,
   `fleetview-audit.md`, `memoryview-audit.md`, `opsview-audit.md`,
   `templatesview-audit.md`, `sessionsview-audit.md`, `logs-view-report.md`,
   `cron-view-gap.md`, `searchview_audit_t_51bad645.md`, …). They are not
   subjects of any check, so nothing goes red — they just start lying. Sweep
   them in the last slice (S9 below), tagged as a separate card.

## 5. Commit slices (ordered so the gate is never red between commits)

Ordering rule discovered from the dependency map: **collapse the root first.**
`AppShell.swift` holds four switch sites over `AppDestination` (`:22-26` cases,
`:65-89` titles, `:95-115` symbols, `:337-349` destination views) and
`Sidebar.swift` holds the drawer rows — so if the shell survives, **every**
screen deletion has to edit those two files, which is a guaranteed collision
between sibling cards. Deleting the shell first removes the shared file and
makes the family slices disjoint (and parallelisable across cards).

| Slice | Contents | Gate after it |
| --- | --- | --- |
| **S0** | this document (card `t_100920c3`) | unchanged, green |
| **S1 — shell collapse** (D3) | `git rm` `AppShell`, `Sidebar`, `HomeView`, `ContentView`, `DebugHarness` + `UITests/` (3 files); ADD `Views/ConfigView.swift` (connection Sections + `widgetsAndLiveActivitiesRows` + `WidgetSurfaceEditor`, extracted from `SettingsView.swift`); `HSCCApp.swift` roots it and drops the 10 `#if DEBUG` branches; `project.yml`: −`HSCCUITests` target + −`HSCCUITests` scheme + −5 `sources:` lines + 1 added; RETIRE `nav_graph_check.sh`, `nav_graph_screens.py`, `capture_screens.sh`, `seed_fixtures.sh`, `make_empty_fixtures.py`, `nav_probe_drawer.sh`; REPOINT `shared_store_check.sh`; edit `README.md:400-403` | `build_check` + `check_sources` + `check_theme` + `actool_icon` + `model_decode` green; the XCUITest lane is gone, so nothing else to run |
| **S2 — chat** | the 11 Tier-A chat files; RETIRE `chat_state_check`, `offline_queue_check`, `turn_state_check`, `reply_watcher_check`, `voice_draft_check`, `slash_palette_check`, `slash_preview_check`; drop the `unread` / `replyWatcher` env objects from `HSCCApp.swift` — `Notify/` references both only in comments (`NotificationCoordinator.swift:18,62`), so no Notify edit | same five |
| **S3 — sessions / history / diff** | `SessionsView`, `SessionHistoryView`, `DiffDetailView`; RETIRE `diff_model_check`, `sessions_row_check/`; REPOINT `offline_cache_fix_check` | same five |
| **S4 — approvals, memory, ops** | 4 views + `Intents/ApprovalsIntent`, `ReviewQueueIntent`; fixture+struct pairs leave with them | same five |
| **S5 — autodown + templates** | 5 views; keep the widget's `/v1/autodown/status` literal (trap 1) | same five |
| **S6a-S6h — Tier B (D2)** | one commit per screen (Fleet, Nodes & serving, Logs, Cron, Cards, Projects, Activity feed, Search) with the checks in §4.1 rows 9-13 | same five |
| **S7 — intents** | `Intents/ProjectIntents.swift`, `CannedCard.swift`, `AppShortcuts.swift` (or its one-shortcut successor), `IntentClient.swift` if `ClusterStatusIntent` goes too | same five |
| **S8 — the prune** | `Models.swift` structs + `fixtures/*.json` + `model_decode_check/main.swift` blocks (together, per struct), `HSCCClient` `EndpointPath` + mutation verbs (widget paths survive, trap 1), `DeepLink` cases + `deeplink_check/Stubs.swift`, `capture_live.sh` + `compare_fixtures_live.py` lists | same five, and `model_decode_check` fixture count drops — update the count in `README.md` |
| **S9 — docs sweep** | `README.md` rewrite for the two-screen app; retire/archive the dead `docs/*audit*` files; delete `docs/design/screens/` | docs-only; gate unchanged |

Under **D1b** add **S10**: `git rm` `Shared/SessionActivity.swift`,
`Sources/HSCCLiveActivitySession/` + `.entitlements`, `SessionActivityDriver`,
`SessionActivitySummary`, `SessionEvent`, `SessionStreamCursor`, `StreamHello`,
`StreamConnectionError`, `StreamingTranscript`, the `HSCCLiveActivitySession`
target + scheme entry, and RETIRE `streaming_check`, `reconnect_check`,
`ws_decode_check`, `session_activity_check`.

## 6. Already red on `main` before the pivot (so nobody blames a slice)

Two gates fail on `origin/main` @ `a972ee5d` today, for an unrelated reason:
`ChatEntry` was moved out of `Views/OrchestratorChatView.swift` into
`Views/ChatEntry.swift` and the two harnesses that slice it were never
re-pointed. Verbatim, from this card's run:

```
$ bash ios-app/scripts/chat_state_check.sh
grep: Sources/HSCC/Views/OrchestratorChatView.swift: No such file or directory
error: could not locate the real ChatEntry enum (markers moved?)

$ bash ios-app/scripts/offline_queue_check.sh
grep: Sources/HSCC/Views/OrchestratorChatView.swift: No such file or directory
error: could not locate the real ChatEntry enum (markers moved?)
```

Both subjects (`ChatStore.swift`, `OfflineSendQueue.swift`) die in S2 anyway, so
**do not fix them** — retire them with the chat family. The lesson for the
retirement itself: a harness that greps for a marker by *file path* rots on the
first rename, which is exactly the failure mode §4.2 lists. `check_theme.sh:25-29`
documents the same lesson and switched to content-anchored markers; every
remaining path-anchored harness (`chat_state_check`, `offline_queue_check`,
`nav_graph_screens.py`, `shared_store_check`) is on this list.

Baseline for comparison: `bash ios-app/scripts/build_check.sh` on `a972ee5d` is
green (final line: `full compile clean, 0 warnings (compile only — never built
or run on a device)`, RC=0).

## 7. Left / explicitly out of scope

- **No deletions.** This card ships a list; S1-S10 are separate cards.
- **A replacement UI test for `ConfigView`.** Killing the nav lane (D3) leaves
  the app with zero automated UI coverage; the new root is two sections and
  needs its own small lane, authored fresh rather than repaired from the drawer
  walker. Card it after S1 lands.
- **Server side.** Every endpoint this list stops calling keeps serving (the
  widgets call several of them). Endpoint retirement is not implied.
- **`docs/design/screens/` and the ~30 dead `docs/*audit*` files** (S9) —
  listed, not touched.
- **The `HSCCWidgets`/Live Activity content model** (`WidgetSurfaceKind`,
  `showSections`) is assumed correct as-is; if the widget set shrinks with the
  console, that is a separate design decision, not part of this retirement.
