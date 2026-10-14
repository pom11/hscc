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
- [x] C3 remaining screens on the list recipe + shared components — commits
      `182b8266`..`0ac20295` (9 commits, one/two screens each per the
      operator's "one/two per commit" instruction)
- [x] C3b the rest of §5/§2 — commits `45645201`..`9d51e473` (6 commits: Fleet,
      app-wide list style, Activity's ticking time, displayJSON/statusColor,
      AppliedTemplateStatus, Approvals' last bespoke badge + count-in-title)
- [x] C3c the last two screens — `79be2dd5` (Autodown) + `644286f6` (Serving
      Control): **no screen in the app builds a ScrollView of HSSectionCard
      boxes any more** (10 screens converted in total)
- [x] reviewer screenshots committed (`docs/design/screens/phase2-home*.png`)
- [ ] §8 screen MERGES filed as cards, NOT done here (each retires a screen —
      a different decision class from restyling): Autodown+self-heal
      `t_1a9bd7f8` · BoardHygiene→Approvals `t_31d037ac` · Serving+topology
      "Nodes & serving" `t_65d791da` · Models→Fleet (needs the destination
      identified first — see below)

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

## Remaining screens — commits 182b8266..0ac20295 (spec §5, one/two per commit)

Nine commits, each a screen or a named §5 defect. The hand-rolled dialect was
seven screens (ScrollView + `HSSectionCard`/`surfaceRaised` boxes + manual
`Divider()`s + in-content `Label` headers); all seven are now the recipe
dialect (`List` + `.listStyle(.plain)` + `Section` + `HSEmpty`/`HSError`).

| commit | screen / defect |
|---|---|
| `182b8266` | Cron roster (was one `HSSectionCard` + bare `ProgressView`); chip moves to the trailing slot |
| `4a84123c` | Memory (two `surfaceRaised` boxes → one List; picker becomes the first Section) |
| `32132ba2` | Templates (applied-status box + per-template cards → group `Section`s) |
| `c8df2177` | Logs (segmented picker above a boxed `LazyVStack` → one List, footer note becomes `footer:`) |
| `b8602da3` | Sessions: `ProfilePickerSheet` replaces TextField + "Load" (§5 named) |
| `282bbfeb` | Ops: five boxes → five Sections; Run Triggers / Perform Escalations become sticky bottom controls (§5 named) |
| `0516cef0` | Settings: sticky Save bar, named delete confirm, Toggle-out-of-NavigationLink (§5 named) |
| `dca7f25c` | per-target a11y labels on Stop/Restart, Recover, Unblock (§5 named) |
| `0ac20295` | `HealthCheckList` — the `verifyBody`/`healthBody` byte-twins collapse (§5 named) |

Two patterns worth noting for the reviewer:

* **Load states ride INSIDE the list** on screens whose controls you need in
  order to recover (Memory/Sessions profile picker, Logs source picker, Ops'
  five independent sections). A full-pane `HSLoading`/`HSError` there is a
  dead end — it hides the one control that could fix the failure. Screens with
  no such control use the centred `HSLoading`/`HSError` pair instead.
* **Loading rows are inline `ProgressView` + caption**, not `HSLoading`,
  whenever any part of the screen has content to show — a section that failed
  must not blank the four that loaded.

`statBadge` (Fleet) deliberately stays: it is a value-over-label **stat tile**,
not a status chip, and that decision (unchanged from `8e299472`) is recorded in
its commit message rather than left for a reviewer to rediscover.

## C3b — commits 45645201..df396a20 (the rest of §5 and the §2 duplicates)

| commit | what |
|---|---|
| `45645201` | **Fleet** — the last big hand-rolled screen. Its five `HSSectionCard` boxes became one `List` with one `Section` per feed (Health · Throughput · Stats · Streams · Autoscale). Each feed keeps its own `LoadState` and renders loading/failed as an INLINE row, so one dead endpoint cannot blank the four that answered. Its check rows now go through `HealthCheckList` (so Fleet and Ops cannot drift again); the by-day bars and stream rows became single VoiceOver elements. `statBadge` kept (documented) but moved to a 3-up `Grid` — inside a `List` row, an `HStack` of `maxWidth: .infinity` tiles sizes to content and starts a horizontal scroll. |
| `971bfffa` | **`.listStyle(.plain)` app-wide** — §5's recipe names it, and only nine screens had it: sixteen more `List`s (Projects ×5, Search ×4, Approvals, BoardHygiene ×2, Cards detail, CardsRoot, ChatsRoot, ProfileEditor's picker) rendered inset-grouped next to their plain siblings. Same commit: Approvals' empty state stops being a green `Label` in a `Section` and becomes `HSEmpty` (§5 names this one as the inconsistent state). |
| `80529362` | **Activity's relative time ticks** — §5 "relative-time ticked by a timer". The row rendered `Text(timeLabel(at))`: a String computed once, so an open feed kept saying "3 minutes ago" for the whole session. `TimelineView(.periodic(by: 30))` + an injectable `now`. 30 s because the finest bucket `Offline.agePhrase` prints is a minute. |
| `b28cf1f7` | **§2 duplicate renderers** — `DisplayJSON.render` (Ops rendered object fields; FleetControl returned `"<complex>"` for the same payload, so the applied-unit count was readable on one screen and hidden on the other) and `Theme.cardStatusColor` (the status→colour switch was copied THREE times, not twice as §2 counted). `StreamingChatView.statusColor` deliberately stays separate — it maps `blocked`→bad on purpose where the board screens map it→warn. |
| `df396a20` | **`AppliedTemplateStatus`** — §5's "two renders of `/v1/template/status` collapse". They had drifted: Templates rendered the structured `applied` fields and never printed `speak` (the live `speak` is a Python dict repr); Fleet Control printed that dict repr FIRST as an italic subheadline, as if it were prose. One structured render now; `speak` only when there is nothing structured to say. Fleet Control's boxes also became `Section`s of one `List` and its two fleet-wide mutations got scope-naming `accessibilityLabel`s. |
| `9d51e473` | **§6 badge inventory closed + Approvals count** — of §6's six bespoke badges, five already used `HSStatusChip` and `statBadge` is the documented keep (stat tile, not a chip). The last un-converted one was the block-kind chip on each approval row — still a warn `Label`, so Approvals was the one screen where a badge could drift from the shared chip. Now the chip. Approvals also gets §5's "count in the title" ("Approvals (3)"), derived from the same response + same `isPendingApproval` filter the list renders — not from the shell's separately-polled badge count, which can be a different moment. |
| `79be2dd5` | **Autodown** — its three `HSSectionCard` boxes (Status · Controls · Active cron jobs) became Sections of one `List`. The four `statusRow` label/value lines became `LabeledContent` (the manual `HStack`+`Spacer` was re-implementing a control that already does label-leading/value-trailing, and gets VoiceOver pairing free). Controls still appear only once the real `enabled` state is known, so an armed cluster never flashes "Enable Autodown". The four mutations keep their confirm prompts and gained scope-naming `accessibilityLabel`s. `wakingBanner` kept — §2 calls it a `StaleBanner` *sibling*, not a twin (it says "wake in progress", and folding it into StaleBanner would make it claim "Offline" during a wake). |
| `644286f6` | **Serving Control** — the worst case of the pattern: an `HSSectionCard` that contained, per unit, ANOTHER hand-rolled `surfaceRaised` card. A card inside a card inside a scroll view, for a list of things that should be rows. Now one `List`, one row per workload (name · mono tp/pp + container id · Stop/Restart). The screen's two prose blocks (a `Text` above the cards plus a titled "fleet-wide actions" card below) became one section footer. **The unit rows deliberately do NOT combine into one VoiceOver element** — every other converted row does, but these carry two buttons that must stay individually reachable, and §9 asks for those buttons to be *named* (done in `dca7f25c`), not absorbed. Documented in the file so nobody "fixes" the inconsistency later. |

## Screens §5 wanted merged that are NOT merged (follow-ups)

Deliberately left as separate cards rather than folded in here — each is a
navigation decision with a screen disappearing, which is a different risk class
from restyling, and this card is already 14 commits deep. All TEN screens are on
the recipe now; what remains is only the merges:

1. `AutodownView` + `SelfHealHistoryView` → one "Autodown & self-heal"
   destination (history as a section) — card `t_1a9bd7f8`. Autodown's own
   restyle landed in `79be2dd5`; `wakingBanner` was left alone and the reason is
   recorded there (it means "wake in progress", not "offline" — folding it into
   `StaleBanner` would make it lie).
2. `ServingControlView` + the NodeTopology strip → "Nodes & serving" (topology
   as header, per-row Stop/Restart) — card `t_65d791da`. The restyle landed in
   `644286f6`; the Stop/Restart colour fix and per-unit a11y labels landed
   earlier (`dca7f25c`), so only the topology merge is outstanding.
3. `BoardHygieneView` → **merged into Approvals** (same `/v1/kanban/blocked`
   endpoint, two row dialects) — card `t_31d037ac`.
4. `ModelsView` → **merged into Fleet** as a serving section. No card filed:
   **there is no `ModelsView` in `Sources/HSCC/Views/` at base `23fcfb15` or at
   HEAD** — the spec's migration map names a screen that is not in the tree.
   The serving reads live in `ServingControlView` today, so either the map's
   name is stale or the screen was retired earlier. Someone with the daemon's
   model inventory should say which before this can be scoped.
5. `FleetControlView` is on the recipe but is still its own drawer row; §8 wants
   it as a "Control section" of Fleet. Small, but it changes the drawer, so it
   belongs with the merge cards rather than with a restyle.


## Screenshots (what each one actually shows)

`docs/design/screens/phase2-home-first-run-notifications-prompt.png` and
`phase2-home.png` are Home in the unconfigured state — "No cluster selected",
"Not connected / Add your cluster in Settings to begin.", the four Work tiles
(Projects / Approvals / Activity / Cards), and the five-verb floating bar
(Chat · Fleet · Cluster · Approvals · Settings). Both were taken with the iOS
notification-permission alert up (the app asks on first run,
`NotificationsAppDelegate`), so the tile labels are partly obscured and the
content is dimmed — they show the empty state and the shell, NOT a connected
fleet or any list recipe. The old `check.png` was removed: it was a third
capture of the same dimmed Home with the same alert, adding nothing.

A clean (dialog-free, connected) screen set is still owed to the reviewer — the
first-run alert cannot be dismissed headlessly (`simctl privacy grant` is
refused on this simulator: "Operation not permitted"), so it needs one tap in
the Simulator UI.

## Gates (all re-run at `9d51e473`)

build_check 97 files / 0 err / 0 warn · check_theme CLEAN · sources in sync
97 files · ws_decode ok · reconnect 42/42 · deeplink 41/41 · turn_state ok ·
shared_store ok · first_run ok · actool icon ok · xcodegen re-run after each
file addition (stale-project pitfall) · **xcodebuild device-target (iPhone 17
Pro simulator) BUILD SUCCEEDED** at `df396a20`.

Earlier runs: gates green at every commit in C3 and C3b; the device build was
also run at `c8df2177`.

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
