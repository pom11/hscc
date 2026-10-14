# iOS redesign Phase 2 — work log (t_25aed701)

Branch: `feat/ios-redesign-2026-10-14`. The operator's scope update
(2026-10-14) landed the dormant drawer components at `23fcfb15` (AppShell,
Sidebar, HomeView, HSCCMark, elevation tokens) — this card did NOT rebuild
them; it consumed them and did the cutover on top.

## Scope (card + operator scope update)

1. THE CUTOVER — shell wired, 17 screen stacks removed, destinations hoisted
2. Full-bleed chat (spec §4) — protocol files untouched
3. Remaining screens one/two per commit

## Status

- [x] worktree based on the redesign branch; operator's 23fcfb15 pulled in
- [x] C1 shell cutover — commit `ad215658`
- [x] C2 chat full-bleed — commit `f6c92a87`
- [x] gates re-run green + xcodebuild device build (see Gates)

## Cutover — commit ad215658

`HSCCApp` roots `AppShell()` now (same five environmentObjects; the single
ProjectUnreadCenter built in `HSCCApp.init` is passed to both `unread` and
`replyWatcher` — no second center). ContentView's TabView is gone; the file
keeps only `ContentView.Tab` (the public vocabulary `DeepLink.swift` names in
`requestedTab`) and `ConnectionBanner`.

17 screen roots unwrapped (mechanical brace-match; sheet-owned stacks KEPT —
QRScannerView and MultiSelectSheet keep theirs because they are presented):
SettingsView, SearchView, LogsView, OpsView, SelfHealHistoryView,
FleetControlView, BoardHygieneView, ServingControlView, AutodownView,
ApprovalsView, ClusterView, ProjectsView, ProfileEditorView, MemoryView,
TemplateDetailView, ContentView. Their toolbar/destination content hoisted
into `AppShell.projectRouteView`.

Router shape preserved: `DeepLinkRouter.projectsPath` drives the shell's ONE
stack (AppShell binds `NavigationStack(path:)` to it), `requestedTab` maps to
`AppDestination`, `pendingSegment` is consumed by ProjectDetailView only when
it is the stack top (a card link has already pushed above; yanking would
fight the target). deeplink_check 41/41 unchanged.

New destination vocabulary (AppDestination + ProjectRoute) — closes the two
deviations the shell commit documented:
- `.chats` → `ChatsRootView`: the dock's Chat verb lands on a real Chats
  screen (project rows → tap pushes chat directly). No more Chat→Projects
  proxy.
- `.cards` → `CardsRootView`: the drawer's Cards row; cross-board list that
  pushes `ProjectRoute.card` (the id-less route that was missing).
- `ProjectRoute.chat/.board/.projectSettings`: the dissolved 4-segment
  sandwich — ProjectDetailView IS the Overview; Chat/Board/Settings push.
- TemplateDetail converts sheet→push (`NavigationLink(value: template)`,
  `ClusterTemplate: Hashable`); ApplyConfirmSheet → `confirmationDialog`
  with the force-recreate toggle inline (spec §3.1).
- SearchView results push by value (`ProjectRoute`) so they ride the same
  typed path.

ClusterView retired as a hub: its 14 nav rows became the drawer (spec §8
migration map is the contract); the topology strip stays — that is the
screen now.

Two latent bugs fixed in the dormant components while wiring them:
Sidebar's header dot read `ConnectionMonitor.shared` — a singleton nothing
ever observes (the real monitors are per-view `@ObservedObject`) — and
printed "connected" under a grey dot. Home's hero had both bugs. Both now
take the observed monitor via environment; the word matches the dot.

## Full-bleed chat — commit f6c92a87

`StreamingChatView` rewritten against spec §4 with the protocol files
untouched (StreamingChatStore / SessionEvent / WSFrameCodec / fixtures
byte-identical — see `git show f6c92a87 --stat`).

- No cards: rows are transcript lines separated by inset hairlines, 16pt
  gutters, background surface; the operator bubble is the app's ONLY filled
  bubble (radius 18, squared bottom-trailing corner, capped measure).
- reasoning rows render collapsed-by-default as muted disclosure rows — the
  old view silently DROPPED them even though the store carried them (the
  ws_turn_roundtrip fixture has three).
- tool rows: one mono "name — state" line, tap expands the payload; pending
  shows a spinner. No boxes.
- nav-bar live dot (9pt, phase-coloured, honest label) + one-line status
  sheet replace the stacked banner; turn pill ("Thinking…" /
  "Running {tool}…" / "Working…") from `store.turnState` rides the composer;
  composer 12pt inset in `safeAreaInset` — keyboard reflows the transcript.
- stick-to-bottom: near the tail → follow; scrolled away → rows count into
  the floating "N new ↓" pill (tap scrolls + clears). The per-token
  animated scrollTo is deleted (spec §4 + §9). Reduce-motion respected.
- each row = ONE combine a11y element, role-prefixed ("Orchestrator: …",
  "Tool, {name}, {status}"); rotor order = reading order because rows never
  re-render position on token deltas.

## Gates (all re-run at f6c92a87)

build_check 87 files / 0 err / 0 warn · check_theme CLEAN · sources in sync
95 files · ws_decode ok · reconnect 42/42 · deeplink 41/41 · shared_store ok
· xcodegen re-run after file additions (stale-project pitfall) ·
xcodebuild generic iOS Simulator build (see run log) · actool icon ok.

Pre-existing failures NOT touched (verified failing identically at base
23fcfb15): chat_state_check.sh and offline_queue_check.sh (both grep the
RETIRED OrchestratorChatView for ChatEntry), fleet_stats_render_check.sh and
live_decode_check.sh (need capture_live.sh fixtures). These are candidates
for a small follow-up card, not Phase 2 scope.

## Reviewer notes / deliberate choices

1. Home tiles: Projects / Activity / Approvals / Cards (spec §3.3's set);
   Search is a toolbar ⌘ on Home AND a drawer row.
2. Sidebar splits Fleet (rollups) and Cluster (topology) into two rows —
   spec §3.1 lists them as one line, but they were two screens before and
   merging them would have deleted a screen.
3. Home hero taps reach the Cluster topology (spec: "tapping the hero should
   reach the Cluster screen").
4. `ConnectionBanner` kept — still used by 8 non-chat screens; only the
   chat's stacked banner was removed.
5. `AppDestination.clusterHub` is what the old "Cluster tab" hub became —
   deliberately not called `.cluster` to keep the drawer row honest about
   it being topology-only now.
