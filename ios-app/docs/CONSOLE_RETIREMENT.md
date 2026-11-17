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

### 2.1 Tier A — deletions named by the card body (35 files)

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
| `Sources/HSCC/Views/ProjectUnreadCenter.swift` | 102 | `reply_watcher_check.sh`; **coupling:** `Notify/NotificationCoordinator.swift` and `Notify/ObservedState+LastSeenState.swift` read it — see §2.5 |
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

### 2.2 Tier B — read-only console screens (D2; 14 files)

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
| `Sources/HSCC/Views/CardsView.swift` | 151 | `widget_kanban_decode_check.swift` **stays** (widget decodes cards) |
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

### 2.4 Tier D — KEEP, untouched (34 files)

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

### 2.5 Tier E — KEEP/PRUNE (5 files; the silent-red ones)

These files survive, so no `rm` reminder appears next to them — and that is
exactly how a gate goes red unnoticed: the check still compiles the file, but
a type or endpoint inside it disappears.

| File | LOC | Prune | Breaks if the prune is not paired with |
| --- | --- | --- | --- |
| `Sources/HSCC/Models.swift` | 1664 | response structs for dead screens (autodown, memory, template, diff, cards-kanban, ops, logs, cron, activity feed, sessions, profiles) | `scripts/model_decode_check/main.swift` **names** each type and `fixtures/*.json` (55 entries) each payload; `live_decode_check`, `diff_model_check`, `ws_decode_check`, `fleet_stats_render_check`, `connection_probe_check`, `offline_cache_fix_check` compile the same file. Rule: **a struct, its fixture and its `main.swift` check leave in one commit** |
| `Sources/HSCC/HSCCClient.swift` | 926 | `EndpointPath` cases for dead screens; **all** mutation verbs (`post` at `:222`, `deleteMemory` at `:652`) | `offline_cache_fix_check/main.swift` asserts cache keys `/v1/sessions`, `/v1/fleet/stats`, `/v1/activity/feed`; `capture_live.sh` + `compare_fixtures_live.py` iterate a live endpoint list that must be trimmed to survivors |
| `Sources/HSCC/DeepLink.swift` | 226 | destination cases pointing at deleted screens; keep `hscc://settings` (+ whatever D1a's session picker needs) | `deeplink_check.sh` + `deeplink_check/Stubs.swift:8,18-20` which stub `ContentView.Tab` — the stub type dies with `ContentView.swift`, so the harness needs a new stub, not a deletion |
| `Sources/HSCC/HSCCApp.swift` | 113 | `#if DEBUG` DebugHarness branches (10 sites in `init()`), `unread`/`replyWatcher`/`router` objects, `AppShell()` root | `shared_store_check.sh` (asserts on the root's app-group read), `build_check.sh` (root file set) |
| `Sources/HSCC/Notify/ObservedState+LastSeenState.swift` | 130 | the observed signals that only the deleted screens produced (project-unread via `ProjectUnreadCenter`, approvals, kanban hygiene) | `notify_check.sh` + `notify_check/main.swift` — the decision-table harness asserts rows per signal; each pruned signal drops a row |

**Net:** ~54 files deleted (35 Tier A + 14 Tier B + `ContentView`/`AppShell`/
`Sidebar`/`HomeView` + `DisplayJSON`), 1 collapse, 5 keep/prune, ~34 untouched.
`wc -l` over the Tier A+B deletions is **~11,300 of ~22,600 lines** of
`Sources/`.

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
