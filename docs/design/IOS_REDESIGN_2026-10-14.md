# HSCC iOS redesign — design spec (Phase 1, 2026-10-14)

Status: **awaiting operator approval.** No implementation code in this PR.
Branch `feat/ios-redesign-2026-10-14`, from main at app 2.8.0.
Companion: `ios_redesign_mockup.html` (open on phone or Mac — 9 frames,
light + dark, 3 app-icon candidates at home-screen size).

Scope discipline carried into every section:

- Chat protocol code (`StreamingChatStore`, `StreamHello`, `SessionEvent`,
  `SessionStreamCursor`, `StreamConnectionError`, the `StreamingTranscript`
  data path) stays **behaviourally unchanged**; `ws_decode_check.sh` and
  `reconnect_check.sh` stay green. This is a UI layer over the same seams.
- Every screen reachable today stays reachable (migration map §8 — nothing
  merged or moved without a row there).
- New Swift files get registered in `project.yml` `sources:` (an unlisted file
  silently does not build — the StreamHello incident, 2026-10-13).
- The operator's phone is never the test target: compile + simulator only.

---

## 1. What is wrong today

Basis: all 42 files under `ios-app/Sources/HSCC/Views/` (13,033 lines) read
in full by two parallel audit passes; every claim below re-verified by
independent grep; line numbers cite the 2.8.0 sources.

### 1.1 Navigation

- Three-tab `TabView` (Projects / Cluster / Settings) puts 2–3 pushes between
  the user and most content. The Cluster tab is already a drawer in disguise:
  14 hand-rolled hub rows in one scroll (`ClusterView.swift:185-243`).
- **10 screens re-wrap their own `NavigationStack`** — Approvals, Logs,
  BoardHygiene, FleetControl, ServingControl, Autodown, SelfHealHistory,
  Search, TemplateDetail, MemoryView sheets — so there are parallel stacks
  with independent paths, and the toolbar composition changes per screen.
- Deepest chains: Projects → ProjectDetail → Board → Card → DiffDetailView
  (**L4**); Templates → TemplateDetail (sheet) → ApplyConfirmSheet
  (**sheet-in-sheet, L4**).
- Search is a **sheet from the Projects toolbar** (`ProjectsView.swift:47-67`),
  invisible from Cluster and Settings, and its internal pushes bypass
  `DeepLinkRouter.projectsPath` — two route tables into the same screens.
- `ConnectionBanner` is the first element of every tab root
  (`ContentView.swift:237-309`) — a permanent full-width strip eating
  transcript height even in the healthy state.
- Deep links (`hscc://project|session|card`) assume `selectedTab = .projects`
  plus a `NavigationPath`; the new shell must preserve them exactly.

### 1.2 Chat — the screen the operator lives in

- Every non-message row is a **boxed card inside a padded column**: ToolChip
  (`StreamingChatView.swift:559-562`), CardChip (674-677), AgentRow (721-724),
  SystemRow (768-771), ErrorRow (814-821), UnknownRow (851-854), on top of the
  list's own card background. Only `NoticeRow` (862-877) is un-boxed — that is
  the shape every row should have.
- **Three stacked notification zones**: `statusBanner` (163-177, shown even
  when healthy), `turnStateBanner` (203-221, ~30 pt), `sendError` (101-108).
  Plus `ConnectionBanner` above them.
- **Composer is a height bomb**: `SlashCommandPalette` (up to 8 rows) and
  `SlashPreviewCard` (~110 pt panel) render *inside* the composer block above
  the field (314-323) — typing one `/` evicts most of the transcript. The
  composer is VStack-pinned (111-113), not `safeAreaInset`.
- **Correction to the first draft of this spec — reasoning rows do not exist
  today.** Nothing in iOS renders model reasoning; the closest thing is a
  `system kind=working` frame. The reasoning row in §4 is therefore
  **greenfield** (new view over data the store already receives), not a
  restyle.
- **Tool calls are the one pattern already at bar**: `ToolChip` (507-629) is a
  collapsible row whose expansion state lives store-side (`expandedToolIDs`,
  `StreamingChatStore.swift:111, 534-539`) so it survives scroll. Phase 2
  **flattens the card to a hairline row and keeps the behaviour**.
- `via=serve` / `via=tmux` has **zero special handling**: it lands in
  SystemRow's `details` dict as a muted `caption2`, `lineLimit(2)` dump
  (756-761), visually identical to every other detail and truncated.
- Auto-scroll fires `proxy.scrollTo` animated on **every token delta**
  (92-98) — it fights manual scroll-back and spams VoiceOver announcements.
- **Honesty bug**: `failedState` says "pull to reconnect" (300) but the
  ScrollView has no `.refreshable` — the failed state has no retry affordance.
- `ProjectDetailView` pins a 4-segment picker (Overview · Chat · Board ·
  Settings) above the chat (`ProjectsView.swift:236-243`): the chat is a pane
  in a chrome sandwich, which is the direct opposite of requirement 1.
- `OrchestratorChatView.swift` (874 lines) is **dead code** — zero call sites
  (verified by grep over all of `Sources`). Its `ChatEntry`/`ChatStore` model
  *is* still used by `ProjectsView.swift:616-620` and `OfflineSendQueue`, so
  the model migrates and the view retires.

### 1.3 Theme adoption

Theme.swift already defines a real token set (Palette / Semantic / Spacing /
Corner + `HS*` components) — this is an adoption problem, not a from-scratch
one. Its own header says "There is NO other scale" (line 91), and the code
disobeys it:

- ~25 raw spacing values in ProjectsView alone; `spacing: 6` recurs in
  Approvals (108, 112), Cron (112-116), Sessions (159), Memory (235), Activity
  (123, 142); raw `cornerRadius: 4` in FleetView's chart (258).
- **`Color.accentColor` is used in 6+ places while Theme has no accent role at
  all** — the user-bubble fill/outline (`StreamingChatView:491,497`),
  MemoryView (280-281, 408), ProjectsView avatar (630, 634), OrchestratorChat
  bubble (783). Every accent usage is off-system by construction.
- The tint-overlay recipe `color.opacity(0.12/0.15)` is hand-rolled in at
  least 4 places (`StreamingChatView:175,219`; `ContentView:85,262`) — a
  missing `Semantic.tintOverlay` token.
- **Bespoke chip/badge recipes ×6**: `statBadge` (FleetView 393-408),
  comment chips (Approvals 140-143), `sourceBadge` (Memory 275-282),
  `AppliedBadge` (Templates 353-365), `bloatBadge` (Sessions 229-236),
  `kindBadge` (Activity 167-175) — all six reimplement `HSStatusChip`.
- `HSSectionCard` has no tap variant, so `ClusterView.hubRow` (298-333) and
  `approvalsRow` (244-274) inline the card recipe; Memory (140-143, 208-211),
  Sessions (79-82, 130-133), Activity (93-96), Templates (125-130) do the same.
- Duplicated renderers: `verifyBody` (Ops 129-156) ≈ `healthBody` (Fleet
  121-148); two divergent `displayJSON` copies (Ops 366-386 vs FleetControl
  171-180, one returns `"<complex>"`, the other renders keys); `statusColor`
  copied between SearchView (249-257) and ProjectsView (838-841);
  `wakingBanner` (Autodown 165-180) is a sibling of `StaleBanner` written
  separately; Logs (118-125) and SelfHealHistory (148-155) share a card+stroke
  recipe verbatim.
- Component-set violations: bare `ProgressView()` (Cron 67) instead of
  `HSLoading`; error views hand-built with `.font(.system(size: 36))`
  (Memory 344) instead of `HSError`.
- Light mode exists but was designed dark-first: `lightElevated #E6E8EE` gives
  no elevation signal on `lightRaised #F1F2F6` without a shadow token.

### 1.4 App icon

**There is no AppIcon and no asset catalog at all** — no `*.xcassets` anywhere,
and `project.yml` wires no `ASSETCATALOG_COMPILER_APPICON_NAME`. iOS therefore
renders the placeholder "H". A real gap, not a style gap (§7).

---

## 2. Design tokens

The token *set* stays in `Theme.swift` and remains the single source; the
change is **add the missing roles, then enforce adoption** (a lint-style audit
script, not a new system).

### 2.1 Colour

Hex values are the existing palette; `L=` marks the dynamic light-mode value.
Contrast ratios below are computed (WCAG relative luminance), not estimated.

| Role | Dark | Light | Used for | Contrast on its surface |
|---|---|---|---|---|
| `surface` | `#16181D` graphite | `#F6F7FA` | screen background | — |
| `raised` | `#232730` slate | `#FFFFFF` | lists, drawer | — |
| `elevated` | `#2B303B` | `#F1F2F6` `L` | chips, sheets | — |
| `text` | `#EDEFF3` | `#14161B` | body | ≥13:1 both |
| `textMuted` | `#A7B0C0` mist | `#55606E` `L` | secondary | 8.13:1 / 5.72:1 |
| **`accent`** | `#7AE2B0` signal | `#1B7E55` `L` | primary action, selection, live dot | 11.28:1 (dark) / **4.51:1 (light)** |
| **`onAccent`** *(new)* | `#08241A` | `#FFFFFF` | text/glyph on an accent fill | see note |
| `success` | `#7AE2B0` | `#1B7E55` | up/serving/ok | as accent |
| `warning` | `#F2A65A` thermal | `#B45309` `L` | waking, attention, idle | 8.78:1 / ~4.8:1 |
| `danger` | `#E56A6A` halt | `#B3261E` `L` | down, failed, destructive | 5.58:1 / ~5.9:1 |
| `neutral` | `#A7B0C0` | `#55606E` | paused, queued, unknown | 8.13:1 / 5.72:1 |
| `separator` | `rgba(237,239,243,.10)` | `rgba(20,22,27,.10)` | hairlines | — |
| **`tintOverlay`** *(new)* | `color.opacity(.14)` | `color.opacity(.12)` | the 4 hand-rolled banner tints | — |

**The accent correction (corrects this PR's first draft).** The mint is
excellent as *text/glyph on dark* (11.28:1) and passes AA as text on the light
surface (**4.51:1** — pass, but zero margin over the 4.5 floor, so it must not
be lightened or used on `#E6E8EE`). What actually fails is a **filled** mint
button with white content: **1.58:1**. That is why `onAccent` becomes a real
token (dark `#08241A`, light `#FFFFFF` — the light fill is `#1B7E55`, giving
5.05:1) instead of `.white` being hardcoded as it is today
(`StreamingChatView:479,483`). My first draft cited a `#157A55` at ~3.9:1;
that hex does not exist in the repo and the ratio was wrong — the real values
are above.

**Semantic de-collision.** Today `mist`/mint carries five different meanings
(accent, selection, live status, informational, secondary) and `secondary` is
also mint. Roles split: mint = accent + live/serving only; paused/queued/
unknown = `neutral`; destructive = `danger`; warning = `warning`. A status is
never communicated by colour alone — a word is always present (existing
discipline, kept).

### 2.2 Typography

| Style | Usage |
|---|---|
| `title` 22 / semibold | screen title in the nav bar (replaces `.large` titles — saves ~52 pt on every screen) |
| `headline` 17 / semibold | section headers, row titles |
| `body` 17 / regular | message text (chat is body, not caption) |
| `callout` 16 | secondary text in rows |
| `footnote` 13 | meta lines, timestamps |
| `caption` 12 | chips, badges |
| `mono` (SF Mono) | code, JSON, ids, tails, tool args/results |

Dynamic Type: **every** text style scales. Today `.hsccMono(15)`,
`.hsccMono(12)`, `.hsccMono(11)` are fixed-size (ProjectsView ~15 sites;
StreamingChatView 591, 600, 845) so large-text users get unscalable, clipped
content — the fix is `Font.hsccMono` taking a *text style* plus size, e.g.
`.hsccMono(style: .footnote)`, and removing `lineLimit(1)`/`(2)` from anything
that must be readable at AX sizes.

### 2.3 Spacing / radii / elevation

Spacing (existing enum, now enforced): `2 · 4 · 8 · 12 · 16 · 20 · 24 · 32`.
Row gutters 16; hairline inset 16 (matches leading edge); composer side inset
12; section header top 20, bottom 6.

Radii: `chip 6 · control 8 · card 12 · sheet 20 · dock 24` (existing `Corner`,
plus `dock`).

Elevation (new, makes dark mode readable): `hairline` (separator), `low`
(`#000` 0/1/3 @20%), `mid` (0/6/16 @28%), plus 0.5 pt hairline stroke on
raised surfaces. Light mode gets a slightly stronger shadow because white-on-
white carries no other cue.

---

## 3. Navigation model

### 3.1 Drawer (replaces the tab bar)

Left-edge swipe or the top-left button opens a drawer over a dimmed
`overlay` scrim, 280–300 pt wide, rows 48 pt with a 24 pt icon in an
`elevated` tile; the active row gets `accent` on a `tintOverlay` background.
Grouped, order by task frequency for a single operator:

```
[paper: cluster name + node count + connection dot]

WORK
  Home                     (dock / start screen)
  Chats                    unread count, "2 need a reply"
  Projects
  Cards
  Approvals                badge = pending count  ← interruptible
CLUSTER
  Fleet
  Nodes & serving          (was ServingControl)
  Autodown & self-heal     (was Autodown; history becomes a section)
  Cron
  Logs
KNOWLEDGE
  Memory
  Templates
  Sessions
APP
  Search everywhere
  Settings
  Switch cluster ▾         (was the ContentView 63-92 banner)
```

**One app-wide `NavigationStack`** owned by the shell: the 10 nested
`NavigationStack` wrappers are unwrapped; the drawer drives routing;
deep links resolve into the same path; Siri intents keep their targets.
`TemplateDetail` converts sheet→push and `ApplyConfirmSheet`
sheet-in-sheet→`confirmationDialog`, removing the L4 chain.

### 3.2 Home dock

Only Home carries the dock: floating rounded bar above the home indicator,
5 icons, 44 pt targets, label under the glyph, badge on the icon, active
indicator below. It is a **start-line, not a tab bar**: it does not persist
selection on other screens (other screens reach everything through the
drawer), which is what keeps it from becoming the tab bar we removed.

**Dock = Chat · Fleet · Cluster · Approvals · Settings**, because those are the
five verbs a cluster operator runs daily: talk to the REPL, is it healthy,
what is on the nodes, what needs my yes, change a setting. Chat leads (the
operator lives there), Fleet next (the 3 a.m. question), Cluster (physical
layer), Approvals (needs to be reachable in one tap; its badge is the only
interrupt channel), Settings (connection/token/cluster switching — currently
a whole tab away).

Deliberately **not** in the dock: Projects (one tap from Chat), Cards (a board
inside Projects), Cron/Logs/Memory/Templates/Sessions (reference surfaces —
drawer), and **Home** (a dock icon pointing at the dock's own screen is
navigation debt).

### 3.3 Home screen

`Good evening, operator` + `Your cluster` title; a status hero (the single
"everything is fine" glance, tap → Fleet); a 2×2 tile grid of the most-tapped
non-dock destinations (Projects, Activity, Approvals, Cards); then nothing
else — the dock is the rest.

Home is **safe to lose**: the hero is a shortcut to Fleet, every tile is
reachable from the drawer. If a later phase trims Home, no feature disappears.

---

## 4. Chat (detailed)

Full-bleed, edge-to-edge. Removed: `ConnectionBanner`, `statusBanner`,
`Divider()` above the composer, the boxed background on every row, and the
`ProjectDetailView` segmented picker (the chat is a screen; Overview, Board
and Settings become drawer/home destinations for the same project).

- **Nav bar (48 pt)**: back/menu button · project + profile · connection dot
  (9 pt, `accessibilityLabel("Live — connected, receiving")`, tap opens a
  one-line status sheet) · one contextual action.
- **Transcript**: all remaining height, `ScrollView` + `LazyVStack`, 16 gutters.
- **Your message**: trailing `accent` bubble (light: `#DCE8FF` on `#F6F7FA`),
  `onAccent` text, radius 18 with a squared bottom-trailing corner, max 76%.
  It is the **only** filled bubble in the app.
- **Reply from the REPL**: plain `text`, no bubble, full width — visually
  attached to the rail, not a card. Reads as "the terminal talking", which is
  what it is.
- **Reasoning row** (greenfield): hairline-topped full-bleed row; brain glyph
  + "Reasoning" + char count + chevron; body expands **in place** in `mono`
  `footnote`, `textMuted`. Default collapsed, with a per-project
  "expand by default" in Settings (the operator wants to see reasoning when
  debugging and not otherwise).
- **Tool-call row**: same row recipe as today's `ToolChip` — glyph + `name` +
  `finished`/`running` chip + duration + chevron, **expanded body is the same
  mono Arguments/Result panels, same store-side `expandedToolIDs`**; only the
  card background and stroke are removed. `running` gets a spinner and amber
  chip; failure turns the chip `danger` and opens the result panel.
- **`via=serve` / `via=tmux`**: promoted from the `caption2` details dump to a
  centred `footnote` `textMuted` row with a hairline rule ("via tmux ·
  hscc-orch pane 0:0.2"), per 2.7.0's notice contract.
- **Turn state**: one 24 pt pill pinned bottom of the transcript, above the
  composer, only while a turn runs — `working…`, `waiting for approval`,
  `reconnecting…`. Never while idle.
- **Composer**: `safeAreaInset(edge: .bottom)`, so the transcript reflows
  above the keyboard instead of competing with it. Field radius 20,
  circular 36 pt `accent` send at the trailing end. Slash palette becomes a
  compact overlay *above* the field (max 5 rows + scroll), preview card
  collapses to a single-line chip + confirm.
- **Scroll**: stick-to-bottom gate — follow only when the user is within ~80 pt
  of the bottom; otherwise show "N new ↓". Fixes the per-token `scrollTo`.
- **Failed state**: real retry (`.refreshable` or a retry button) — today it
  tells the user to pull and provides nothing.
---

## 5. Per-screen layouts

One **list recipe** everywhere (fixes today's two row dialects — some screens
are `List`, most are hand-rolled ScrollView cards): `List` with `.listStyle
(.plain)`, `.listSectionHeadersForRows` off, leading 12 pt status dot, title
(`headline`) over caption (`footnote` `mono` where machine-sourced), trailing
`HSStatusChip` + optional value; section headers as `sgrp` caps; pull-to-refresh
everywhere; `HSEmpty`/`HSError` for states.

**Fleet** (was 5 copy-pasted 5-case `switch` blocks): one section per feed
(Health · Throughput · Stats · Streams · Autoscale · Models) in a `List`; the
byte-twin `verifyBody`/`healthBody` collapse to one `HealthCheckList` used by
Fleet and Ops. Stats was always one of the screen's own feeds — the first draft
of this list left it out; it is not new. **Models** is the serving read: one row
per workload from `GET /v1/cluster/status` (model name · the units serving it ·
stream count), the same feed ServingControl reads — exactly what mockup frames
7/8 draw (`Models · 3 serving`). It is a **new** section, not a merge: there is
no `ModelsView` to absorb (ruling `t_1897550c` — see the note under the §8
table).

**Nodes & serving** (absorbs ServingControl + the NodeTopology strip): topology
as a header, then a unit list with per-row Stop/Restart. **Fix:** Stop and
Restart are both `danger` today (`ServingControlView:175-203`) — Restart is
recovery, so it becomes `accent`/`neutral`; per-row buttons get an
`accessibilityLabel` naming the unit (the confirm does, the button doesn't).

**Cards**: `CardDetailView` keeps the `List` recipe; the card id joins the
Unblock `accessibilityLabel` (Approvals already did this — `ApprovalsView:165`
— Cards did not). Reached from Projects/Board/Search, all through the one stack.

**Approvals** (absorbs BoardHygiene — same `/v1/kanban/blocked` endpoint, two
row dialects today): one `List`, Allow/Recover rows already named per-card,
count in the title, empty state → `HSEmpty` (today it's a `Section` `Label`,
inconsistent). Recover gets the same per-card label Allow has.

**Cluster hub**: **retired as a screen** — its 14 rows become the drawer. Ops
absorbs the run-triggers / perform-escalations actions (today buried mid-scroll)
as sticky bottom controls with confirm dialogs.

**Cron, Logs, Memory, Templates, Sessions, Activity**: convert hand-rolled
cards → the list recipe; replace the 6 bespoke chips with `HSStatusChip`;
Sessions reuses `ProfilePickerSheet` (already `internal`) instead of its
TextField+"Load" (Memory solved this); Activity rows combined + relative-time
ticked by a timer; Templates `appliedCard` and FleetControl `appliedSection`
(two renders of `/v1/template/status`) collapse to one `AppliedTemplateCard`.

**Search**: promoted to a drawer destination + Home ⌘; fixes the two
`client!` force-unwraps (`SearchView:151,183` — crash window), replaces
deprecated `.navigationBarItems` (`SearchView:33`), and routes results through
`DeepLinkRouter` (its private stack is deleted).

**Settings**: keeps `Form`; **Save moves to a sticky bottom bar** (today it's a
row at the bottom of a long form, disconnected from its fields); cluster delete
gets an explicit row button + confirm (not swipe-only); the health dot gets an
`HSStatusDot` label; the Toggle-inside-NavigationLink (`SettingsView:468-473`)
is split into a labelled row + separate toggle.

---

## 6. Component inventory

Built once in Theme.swift, used by every screen. Today's duplicates fold in:

| Component | Replaces |
|---|---|
| `HSSectionCard` **+ tap variant** | ClusterView hubRow/approvalsRow + Memory/Sessions/Activity/Templates inline cards |
| `HSStatusChip` | the 6 bespoke badges (statBadge, sourceBadge, AppliedBadge, bloatBadge, kindBadge, Approvals comment chips) |
| `HSStatusRow` (title+caption+trailing+dot) | all hand-rolled list rows |
| `HSTranscriptRow` (transcript rows; `.reasoning`/`.tool` are collapsible variants) | the 6 boxed row types in StreamingChatView |
| `HSSectionHeader`, `HSEmpty`, `HSError`, `HSLoading` | bare `ProgressView` (Cron:67), hand-built error views (Memory:344), `Section{Label}` empties (Approvals) |
| `HSSidebarItem` | cluster hub rows (drawer) |
| `HSDockIcon`, `HSHeroCard`, `HSTile` | new (home only) |
| `HSConnectionDot` | `ConnectionBanner` (4 tabs) + 2 in-chat pills |
| `HSButton` (primary/secondary/destructive) — fixed slots | the ad-hoc borderedProminent + `Color.red` (OrchestratorChat:836) |
| `HSTopBar` (leading drawer/menu · title · ≤1 action) | 4 divergent toolbar compositions |

**Button-placement contract:** primary action = top-right in the bar, or the
single circular send/confirm in a sheet. Destructive = `danger` + a
`confirmationDialog` that names the target. Never two primaries; never a
destructive in the primary slot; ≤1 toolbar action per screen (kills the
"two trailing items when Board is active" case in ProjectDetailView).

---

## 7. App icon

**Candidate A "Orbit"** (recommended): three nodes — the live one lit mint,
two in `mist`/`thermal` — joined by faint links; reads as "a control centre
watching a cluster". Legible at 29 pt (the mockup shows it at 60 and 29 pt with
a tinted variant). B "Rack" (server rack, one unit lit) and C "Grid+hub"
(cleanest, but the cross reads as "add" at small size) are shown as
alternatives. No text beyond the mark.

Ship as **single-size 1024 px** AppIcon (Xcode 15+ format): one 1024 PNG per
appearance (`light`/`dark`/`tinted`) generated from the SVG master — iOS derives
every required size. Wiring in `project.yml`: add `Assets.xcassets` to the app
target `sources`, set `ASSETCATALOG_COMPILER_APPICON_NAME: AppIcon`, and reuse
the same master for the widget and Live-Activity glyph assets. **Phase 2.**

---

## 8. Feature migration map (nothing dropped)

| Today (level) | New home |
|---|---|
| ProjectsView (tab) | drawer · Work/Projects · Home tile |
| ProjectDetailView Overview | drawer · Projects → detail (overview inline) |
| ProjectDetailView **Chat** segment | **Chat screen, full-bleed** · drawer · Chat · dock Chat |
| ProjectDetailView Board | Projects → detail · Home Cards tile |
| ProjectDetailView Settings | Projects → detail → Settings (drawer) |
| SessionHistoryView | Chat nav-bar action (read-back pager) |
| CardDetailView | drawer · Cards · Projects board · Search |
| ProfileEditorView, CreateCardSheet | Projects detail (sheet, kept) |
| SearchView (Projects sheet) | **drawer · Search + Home ⌘** (promoted) |
| ClusterView hub (tab) | **retired** → drawer (all 14 rows kept) |
| ApprovalsView | drawer + **dock** |
| OpsView | drawer · Cluster (keeps hub actions, surfaced sticky) |
| LogsView · CronView · MemoryView · TemplatesView · SessionsView · ActivityFeedView | drawer (one section each) |
| FleetView · FleetControlView | drawer · Fleet (+ Control section) |
| ServingControlView | drawer · "Nodes & serving" |
| AutodownView + SelfHealHistoryView | drawer · "Autodown & self-heal" (**merged**, history as a section) |
| BoardHygieneView | **merged into Approvals** (same endpoint) |
| TemplateDetailView / ApplyConfirmSheet | Templates → push / confirmationDialog (L4→L2) |
| Memory ProfilePicker/MemoryEdit sheets | sheets (kept) |
| ActivityTraceView | Activity → detail (drawer path) |
| SettingsView (tab) | drawer · App/Settings |
| WidgetSurfaceEditor / QRScannerView | Settings subpages (kept) |
| NodeTopologyView | component (kept; shown in Fleet + Nodes) |
| ~~**ModelsView**~~ | **never existed** (ruling `t_1897550c`, 2026-10-14): no `struct ModelsView` in any commit ever (verified across every ref, stash, and unreachable object), and no `Views/Models*.swift` was ever added or deleted. The row named it because §5's Fleet feed list already wanted a **Models serving feed** (mockup frames 7/8: `Models · 3 serving`) — a *new* read-only section in Fleet over the same `/v1/cluster/status` workloads ServingControl reads, **not** a merge of a screen. Nothing is lost: this row moves no code and deletes no feature. Fleet's Models section ships as its own Phase 2 card. |
| **ProjectDetailView** as a segment-sandwich | **dissolved** (Overview/Board/Settings → detail subpages; Chat → Chat screen) |
| OrchestratorChatView (dead) | **retired** — model migrates to `ChatEntry`/OfflineSendQueue, view deleted |

One merge (Autodown+self-heal) and one dissolve (ProjectDetail
segments) are the only non-1:1 moves; each keeps its data and actions. Say no
to any and it stays a separate screen in Phase 2. (The Models→Fleet row used to
count as a second merge; it does not — see the ruled-out row above. §5's
Models section is new read-only surface, additive, and cannot drop anything.)

---

## 9. Accessibility

- **Dynamic Type**: every text style scales; kill the fixed `hsccMono(11/12/15)`
  sizes; drop `lineLimit` on anything that must be read at AX sizes.
- **VoiceOver**: combine every list row (`accessibilityElement(children:
  .combine)`) into one element ("spark-01, serving qwen3-30b, up, 94 percent");
  per-mutation buttons name their target (Extend/Approve already do — extend
  the same fix to Unblock, Stop, Restart, Compact, Retire); expand/collapse
  rows carry `accessibilityValue(isExpanded:)` and say "expand/collapse", not
  "Chevron up".
- **Live regions**: connection state and turn state announce on change
  ("Live — connected, receiving"); the per-token auto-scroll is gated so VoiceOver
  rotor focus isn't yanked.
- **Hit targets**: 44×44 minimum everywhere — the 24 pt glyph buttons in
  StreamingChat (mic/send) and the ~26 pt Retry/Stop-waiting capsules get padded
  to 44.
- **Colour**: never the sole channel (a word always accompanies a status);
  contrast is the table in §2 (the one real failure — white-on-mint at 1.58:1 —
  is fixed by `onAccent`).
- **Reduce Motion**: the animated per-token scroll and hero transitions respect
  it (scroll-to-bottom becomes instantaneous).

---

## 10. Phase 2 sequencing (on approval)

1. **tokens/theme** in Theme.swift (new roles: accent, onAccent, tintOverlay,
   elevation; the `HSButton`/`HSSectionCard`-tap/`HSStatusChip` component set) —
   behaviour-free, compiles alone.
2. **shell**: single `NavigationStack`, drawer, home + dock, deep-link routing
   preserved. Everything reachable via drawer before any screen is restyled.
3. **chat** (the reason for the redesign) — full-bleed, new row components,
   reasoning row, retry, stick-to-bottom, `via=` notice row.
4. **list screens** in batches: Fleet+Nodes · Approvals(+BoardHygiene) ·
   Cards+Projects detail · Cron/Logs/Memory/Templates/Sessions/Activity ·
   Search · Settings.
5. **app icon** + widget/Live-Activity glyphs.

Per commit: new Swift files registered in `project.yml`; `xcodegen generate`
then revert Info.plist churn; `xcodebuild` generic iOS `CODE_SIGNING_ALLOWED=NO`
= 0 errors; `ws_decode_check.sh` + `reconnect_check.sh` green; simctl
screenshots of each redesigned screen → `docs/design/screens/`. The stream
protocol files are not touched. Full `scripts/run_tests.sh` + public-address
guard before any commit that touches non-iOS files. Branch/PR only — no
release, install, or API/daemon restart.
