# HSCC iOS App

A **private** iOS app to manage the owner's personal DGX cluster + project
kanban over Tailscale. It talks to the HSCC HTTP API (Phase A) which listens on
the Tailscale tailnet.

**This app is for sideloading onto the owner's own device only — it is NEVER
distributed, and is deliberately NOT App Store–ready.**

> **STATUS: built and exercised in automated gates; not yet on a device.**
> Every target compiles clean (`scripts/build_check.sh`), the whole surface has
> been walked on an iPhone-16-class simulator by the XCUITest nav-graph gate
> (17/17 drawer rows — enter, back, pull-to-refresh, honest empty + error
> states; t_810ab152), and every Codable model decodes the committed live
> fixtures (62/62, `scripts/model_decode_check.sh`). Still **unverified at
> runtime**: Siri App Intents, the Home Screen widget, and the Live Activities
> need a signed build on real hardware. See **"Honest limits"** below.

## Quickstart

```sh
brew install xcodegen
cd ios-app
scripts/generate.sh        # NEVER bare `xcodegen generate` — see below
open HSCC.xcodeproj        # select your device, Run (⌘R)
```

On the Mac, start the API and let the phone scan its way in — the QR that
`hscc api status` prints carries host, port **and the auth token**, so treat it
like a password (never on a stream or screen-share):

```sh
hscc api start --tailscale
hscc api status            # prints the pairing QR (or --no-qr to suppress)
```

In the app: Settings → **Scan QR** (or type host/port/token by hand) →
**Test connection**. The full sideload + Tailscale + token walkthrough is in
[Sideloading onto your own device](#sideloading-onto-your-own-device) and
[docs/SIDELOAD-TAILSCALE-SECURITY.md](docs/SIDELOAD-TAILSCALE-SECURITY.md).

## What's here — the real shipped surface

The information architecture is a **drawer over one app-wide
`NavigationStack`** (`IOS_REDESIGN_2026-10-14` §3): `AppShell` owns a
slide-over drawer with **17 rows in 4 groups**, plus a 5-verb start-line dock
on Home only. The old three-tab layout (Projects · Cluster · Settings) is gone;
`ContentView.Tab` survives only as the deep-link router's legacy vocabulary,
mapped onto drawer destinations.

- `Sources/HSCC/` — Swift sources (SwiftUI, iOS 26+).
  - `HSCCApp.swift` — app entry point; roots `AppShell`.
  - `Views/AppShell.swift` — the root shell: owns `AppDestination` (one enum,
    one stack; legacy aliases like `.boardHygiene` resolve to their merged
    destination instead of failing), the drawer state, the shared detail
    stack, the approvals-badge poller, and the offline-queue banner. The
    drawer is a slide-over the shell owns — **not** the compact column of a
    `NavigationSplitView`, which is a proven dead end on iPhone compact
    (t_5ed5d281, RED-first on the sim).
  - `Views/Sidebar.swift` — the drawer's 17 rows:
    - **Work**: Home · Chats · Projects · Cards · Approvals
    - **Cluster**: Fleet · Nodes & serving · Ops & health · Autodown &
      self-heal · Cron · Logs
    - **Knowledge**: Memory · Templates · Sessions · Activity feed
    - **App**: Search · Settings
    Its header shows the active cluster + a live connection dot (the
    ConnectionMonitor probe, t_e5d24c6d).
  - `ContentView.swift` — the legacy tab vocabulary + `ConnectionBanner`
    (most-recent-API-outcome banner, driven by `ConnectionMonitor`).
  - `HSCCClient.swift` — async/await URLSession API client; Bearer auth on
    every request (reads included); builds URLs from a configurable host +
    port over plain HTTP; decodes the unified error shape; surfaces the
    `speak` field on reads. This is the single seam every screen's data flows
    through. Mutating endpoints (B4) always send `confirm: true`; the view is
    responsible for gating the call behind a confirm UI first.
  - `Models.swift` — the Codable models for the app-only surface, mirrored
    field-for-field against live API captures and kept honest by
    `scripts/model_decode_check.sh` (never a hand-mirrored validator).
  - `APIError.swift` — the typed error surfaced by the client.
  - `KeychainStore.swift` — stores API tokens in the iOS Keychain.
  - `SettingsStore.swift` — saved **clusters** (a named list of
    host/port/token sets, one active) in the shared App-Group defaults, each
    token in the Keychain; legacy single-cluster configs import on first run.
  - `Views/` — all screens, grouped:
    - `AppShell` + `HomeView` (above) — Home is the launch screen with the
      5-verb dock (Chat · Fleet · Cluster · Approvals · Settings; a
      start-line, not a tab bar).
    - `Views/ChatsRootView.swift` — the global Chat destination: every
      project row pushes straight into its full-bleed chat with unread counts.
    - `Views/ProjectsView.swift` — the project list from `/v1/projects`
      (14 projects in the registry as of 2026-10-19 — the app renders whatever
      the endpoint returns), each opening a detail screen with segmented
      sections **Overview · Chat · Board · Settings**.
      - **Overview** — `GET /v1/projects/{name}`: board counts + git state.
      - **Chat** — `StreamingChatView`, the live window onto the project's
        session over the API's WebSocket stream (`/v1/projects/{name}/session/ws`,
        `URLSessionWebSocketTask`). The old polling `OrchestratorChatView` was
        retired in the redesign pass; the job-based endpoints survive behind the
        Siri intents. **Sessions persist per project** on the server — each
        project is a continuing conversation, not one-shots.
      - **Board** — `GET /v1/cards` filtered to the project's board, plus its
        blocked/stale cards (from `/v1/kanban/blocked` + `/v1/kanban/stale`) —
        with `CardDetailView` on `GET /v1/cards/{id}`.
      - **Settings** — project repo/board/topic + orchestrator profile/session
        presented **read-only** (they live on the cluster, not in the app — no
        fake editable controls).
    - `Views/SettingsView.swift` — saved clusters: enter/persist host, port,
      token; **Scan QR** (QR from `hscc api status`, see
      `SetupQRCode.swift`); **Test connection** (`GET /v1/ping`).
    - `Views/NodesServingView.swift` — the machines + what runs on them:
      the node topology strip header (derived from `/v1/cluster/status`),
      hosts, and the serving units grouped under the node that runs them
      (unit→node attribution parses the verbatim `sparkrun status` text in
      `GET /v1/cluster/hosts` — see `ServingPlacement.swift`). The old Cluster
      hub was retired into this screen (t_65d791da).
    - `Views/OpsView.swift` — Ops & health: verify / daemon / triggers /
      escalations / profiles (+ profile editor).
    - `Views/FleetView.swift` — Fleet: health, stats, throughput, streams,
      autoscale, the applied-template read, and the confirm-gated fleet
      up/down (the old FleetControl screen folded in here, t_2a29a7ab).
    - `Views/AutodownView.swift` — Autodown **& self-heal** (the old
      SelfHeal screen is this screen's trailing section): the live
      `/v1/autodown/status` report + confirm-gated enable / disable / wake /
      cancel. Wake enters a polling "waking" state and starts the **Live
      Activity** (the fleet wake is ~9 minutes — never block the UI).
    - `Views/CronView.swift` · `Views/LogsView.swift` — cron jobs; daemon/log
      tail with redaction (`Views/LogRedactor.swift`).
    - `Views/MemoryView.swift` — the memory provider list / delete.
    - `Views/SessionsView.swift` + `Views/SessionHistoryView.swift` — live
      sessions and per-session event history.
    - `Views/ActivityFeedView.swift` — the fleet activity feed.
    - `Views/TemplatesView.swift` + `Views/TemplateDetailView.swift` — the
      **Templates library**: browse (`/v1/template/list`), preview against the
      node topology (`/v1/template/preview/{name}`, read-only dry-run), and
      **confirm-gated apply** (`/v1/template/apply`), with post-apply reload
      polling of `/v1/template/status` + `/v1/verify`.
    - `Views/ApprovalsView.swift` — the **approvals inbox + board hygiene**
      (spec §8 merge: `BoardHygieneView` retired): ONE List whose sections are
      the blocked cards genuinely waiting on a human decision — presented as a
      decision surface ("worker wants to force-push X — allow?") — then the
      auto-resume blocked cards (recoverable early), then the stale cards.
      Uses `/v1/kanban/blocked` partitioned by
      `BlockedCard.isPendingApproval` (human-decision kinds
      `needs_input`/`capability`/unclassified; `dependency`/`transient`
      auto-resume and drop to the "Other blocked" section) plus
      `/v1/kanban/stale`. **Allow** / **Recover** recover the card so the
      worker proceeds; leaving it blocked is the decline.
      `Views/ApprovalBadge.swift` backs the drawer row's pending-approvals
      badge (polls the same read, same classification).
    - `Views/CardsRootView.swift` — the drawer's cross-board card list (every
      open card, grouped by board).
    - `Views/SearchView.swift` — **cross-project search** over last-known
      projects + cards, so it works even when the cluster is unreachable.
    - `Views/QRScannerView.swift` — camera pairing (see Quickstart).
    - `Views/Theme.swift` — the design system: `Theme.Semantic.*` dynamic
      roles (surface / ok / warn / bad / …) own the palette — **no raw hex
      anywhere else** (enforced by `scripts/check_theme.sh`) — plus
      `Font.hsccMono(...)` for machine-produced values.
    - `Views/NodeTopologyView.swift` — the signature element: the two
      tensor-parallel pairs, each node's dot coloured by live state; encodes
      that only each pair's head serves HTTP.
    - `Views/LoadState.swift` — the generic async-load container (idle /
      loading / loaded / failed / **stale**) shared by every read surface,
      wired to the offline cache.
  - `LiveActivityManager.swift` — the app side of the **fleet-wake Live
    Activity**: starts it when a wake begins (autodown state → `waking`),
    polls `/v1/autodown/status` + `/v1/cluster/status` for per-node readiness,
    and **ends** it with an explicit success/failure message. Honest progress
    only — elapsed time + per-unit readiness, never a fabricated percentage.
  - `SessionActivityDriver.swift` / `MonitorActivityDriver.swift` — drive the
    two extra Live Activities: the per-project **session activity** (a live
    turn visible on Lock Screen / Dynamic Island) and the **HSCC Monitor**
    (per-node CPU/GPU/RAM while pinned).
  - `OfflineSendQueue.swift` — chat composed while unreachable is queued
    (persisted), visually distinct, flushed on reconnect — never lost, never
    sent twice.
  - `DeepLink.swift` — the `hscc://` scheme: open a project / card / session
    directly; the single entry point for notifications and Handoff.
  - `Notify/` — the "operator needed" local-notification engine
    (`NeedsOperatorNotifier` computes alerts purely;
    `NotificationCoordinator` delivers them).
  - `DebugHarness.swift` — DEBUG-only seams the gates rely on (fixture dir,
    forced failures, offline mode, the live served-count probe). Never
    compiled into Release.
  - `Intents/` — Siri App Intents:
    - `ProjectIntents.swift` — **per-project Siri shortcuts (job-based)**:
      `HSCCProject` (a String `AppEnum` of the canned project list),
      `AskOrchestratorIntent` (job-based chat — ack immediately, poll in a
      detached task, speak the reply), `ProjectStatusIntent` (speak the
      per-project `speak` summary verbatim).
    - `ClusterStatusIntent` + `ReviewQueueIntent` — speak each endpoint's
      `speak` one-liner.
    - `ApprovalsIntent` — voices the pending approval count (read-only,
      classified by `BlockedCard.isPendingApproval`, no confirm needed):
      "3 pending approvals await your decision." / "No pending approvals."
    - `CannedCard` + `DispatchCannedCardIntent` — voice-dispatch a KNOWN card
      via the confirm-gated client (never free-form dictation).
    - `AppShortcuts` — the `AppShortcutsProvider` with natural phrases.
    - `IntentClient` — builds the client from the same stored settings.
- `Sources/Shared/` — cross-target glue compiled into all bundles:
  `Speakable`, the shared endpoint models the widget / Live Activities also
  need (`AutodownStatusResponse`, `ClusterStatusResponse` / `ClusterWorkload`,
  `TopologyPair` / `TopologyNode`), the App Group `group.com.hscc.ios`,
  shared Keychain access, `APIConfig`, the last-known `SnapshotStore`, and
  the three `ActivityKit` attribute types (`WakingActivity`,
  `SessionActivity`, `MonitorActivity`).
- `Sources/HSCCWidgets/` — the widget extension: the cluster Home Screen
  widget (`systemSmall` + medium — last-known state + compact topology;
  **`Unreachable` is a first-class state** — "Can't reach the cluster" with
  the last-known state and its age, never a blank or stale-looking-live
  widget) and the **HSCC Monitor** widget (per-node monitor data).
- `Sources/HSCCLiveActivity/` + `Sources/HSCCLiveActivitySession/` +
  `Sources/HSCCLiveActivityMonitor/` — the three Live Activity extensions
  (fleet-wake progress, project-session activity, monitor). Each ends
  explicitly on success or failure; a widget extension body allows only one
  Live Activity per target, hence three bundles.
- `project.yml` — XcodeGen spec, listing every source file explicitly. Five
  app-facing targets (app, widgets, three Live Activities) plus the
  `HSCCUITests` UI-test bundle.
- `UITests/` — the XCUITest suites the gates run: `DrawerNavigationUITests`
  (drawer open/close/round-trips), `NavGraphUITests` + the generated
  `NavGraphScreenTable` (the walker below).
- `scripts/` — verification helpers:
  - `build_check.sh` — full per-target **compile** of every target with its
    real file set (typecheck alone hides SIL-level errors; see its header).
  - `nav_graph_check.sh` — the **navigation-graph gate** (t_810ab152):
    XCUITest walker over every drawer screen — enter via drawer → marker →
    back → same-row re-tap, one level into detail pushes, real pull-to-refresh
    (coordinate drag, receipt = the app's live served-count AX probe), honest
    EMPTY + ERROR states, a disconnected pass, and a guarded negative
    self-check that must turn the walk RED. Screen list is code-derived
    (`nav_graph_screens.py`); fails naming the screen + missing edge. See
    `docs/design/NAV_GRAPH_t_810ab152.md`.
  - `capture_screens.sh` — tap-free per-screen screenshot capture
    (`HSCC_DEBUG_SCREEN` + fixture seeding); the current clean set — 12
    screens × light/dark — is in `docs/design/screens/`.
  - `model_decode_check.sh` — compiles the **real** `Models.swift` +
    `SharedModels.swift` + `APIError.swift` into a macOS CLI and decodes the
    committed live fixtures (54 JSONs under
    `scripts/model_decode_check/fixtures/`, 62 decode checks — some fixtures
    are checked against more than one model), so it can never drift into a
    false green like a mirrored validator could.
  - `shared_model_check.swift` — decodes the shared widget/Live Activity
    model shapes against live API JSON (runs as a macOS CLI).
  - `shared_store_check.sh` — proves the App Group sharing failure is **loud,
    not silent**: a grep guard asserts SettingsStore no longer falls back to
    `?? .standard` on the shared suite and that the share write is gated on
    availability, plus a compiled harness exercises the real `SharedStore`
    availability logic (nil provider → unavailable; real store → available).
  - `check_theme.sh` — the raw-colour guard (every colour must be a Theme
    semantic; see `Views/Theme.swift`).

**Offline last-known state** is a first-class feature: every successful read is
cached, and when the cluster (or Tailscale) is unreachable, views show the
last-known data clearly marked stale with its age via `StaleBanner` — never a
blank lie. `LoadState.offline`/`.stale`, `StateCache`, and `SnapshotStore` back
this; outbound chat composes into `OfflineSendQueue` until the link returns.

No third-party dependencies. Sideload-friendly. **This deployment runs the API
on port `8788`** (an override in `~/.hscc/api.json`), so the app must be set to
match the port `hscc api status` reports. The API's code default is `8787`;
the app pre-fills `8788` to match this deployment.

## Honest limits — what is NOT verified

Be plain about what the gates have and have not seen:

- **Never run on a real device.** The simulator gates (compile, nav-graph
  walk, screenshots, decode) cover the app target's UI and data path. A
  signed on-device build — the actual sideload the operator uses — has not
  happened yet.
- **Siri intents cannot be validated without a signed build on hardware.**
  The AppEnum, parameter summaries, and confirm dialogs are written but
  unrun; a real device build is required to know they resolve and fire.
- **The widget and the three Live Activities are unverified at runtime.**
  Their model shapes decode the live JSON and their targets compile, but the
  extension bundles have not been installed or run — widget refresh, Dynamic
  Island rendering, and Live Activity lifecycle are all untested.
- **The gates are fixture-fed.** The walk and the screenshots run against the
  committed live captures (kept decode-current by the decode gate), not a
  live fleet. Live-server behaviour (WS streams, real wake timings) is
  covered only structurally.
- **Per-project chat depends on session health.** A healthy session answers in
  ~1.8 s; a **bloated session wedges chat** (measured: a 600 s timeout against
  a fresh 1.8 s). The chat surface can only be as healthy as the orchestrator's
  underlying session — the rotation work (auto-rotate bloated sessions) exists
  precisely because of this, and the app does not and cannot fix a wedged
  session by itself.

Do not read these as "should work" — they are real, unexercised surfaces.

## API contract

The app implements the HSCC HTTP API contract in `docs/DESIGN-api.md` (see the
`feat/hscc-api` branch). Key facts the client relies on:

- **Auth:** every request (reads included) must carry
  `Authorization: Bearer <token>`.
- **Transport:** plain HTTP over Tailscale is fine; TLS is deliberately out of
  scope (Tailscale is the encrypted transport).
- **Errors:** every error is `{ "error": { code, message, speak } }`.
  The client maps 401 → "check your token" and connection failure → "can't
  reach the cluster — is Tailscale connected?".
- **`speak`:** every READ response carries a first-class `speak` string, which
  Siri App Intents read aloud. The client exposes it on all read models.

## Design system

- **`Theme.swift` owns the palette.** `Theme.Semantic.*` provides dynamic
  semantic roles (surface, surfaceRaised, surfaceElevated, onSurface,
  onSurfaceMuted, ok, warn, bad, neutral) that adapt to light/dark. **There is
  no raw hex anywhere outside this file** — `scripts/check_theme.sh` fails the
  build on raw colours, hex literals, and `UIColor(red:)` constructors
  elsewhere. Views reference semantic roles only.
- **`NodeTopologyView.swift` renders the two tensor-parallel pairs.** The
  gateway (the orchestrator head) heads the orchestrator pair; only each pair's
  head serves HTTP. Each node's dot is coloured by live state (up / busy / warn /
  down / unknown).
- **Monospaced type (`Font.hsccMono`) for machine values** (ids, ips, repo
  paths, counts, timestamps); **proportional type for human prose.** This is
  the design rule the whole app follows.

## Building — two paths

### Path A — XcodeGen (reproducible)

```sh
brew install xcodegen
cd ios-app
scripts/generate.sh      # produces HSCC.xcodeproj from project.yml, with signing
open HSCC.xcodeproj
# Run on your device.
```

**Use `scripts/generate.sh`, not bare `xcodegen generate`.** There are FIVE
app-facing targets — the app plus four app extensions (`HSCCWidgets`,
`HSCCLiveActivity`, `HSCCLiveActivitySession`, `HSCCLiveActivityMonitor`) —
and each needs its own `DEVELOPMENT_TEAM`. Setting the team on the app alone
still fails with:

```
Signing for "HSCCWidgets" requires a development team.
Signing for "HSCCLiveActivity" requires a development team.
```

Picking a team in Xcode's Signing & Capabilities editor fixes it only until the
next regenerate — `HSCC.xcodeproj` is a generated artifact and is overwritten.
The script auto-detects the team from your signing certificate (override with
`HSCC_DEVELOPMENT_TEAM=YOURTEAMID scripts/generate.sh`), sets it at project
level (all targets inherit), also refuses to generate while a source file is
missing from `project.yml` (`check_sources.sh`), then verifies with
`xcodebuild -showBuildSettings` that the team actually landed on HSCC,
HSCCWidgets and HSCCLiveActivity.

A free personal Apple ID team works for sideloading onto your own device —
**but a free team cannot provision App Groups**, so the Home Screen widget,
Live Activities, and Siri will not see the app's shared settings
(host/port/token) even though the app itself works. The app detects this and
shows a loud "App Group sharing unavailable" banner (see `SharedStore` in
`Sources/Shared/SharedModels.swift`). Sharing across the app, widget, and Live
Activities requires a **paid** Apple Developer team.

- `project.yml` lists every source file under `Sources`. If you add a new Swift
  file, add it to the `Sources` list in `project.yml` too (or use a folder glob;
  the spec currently lists sources explicitly). `generate.sh` refuses to
  generate if you forget.
- Info.plist keys come from the `info:` blocks in `project.yml` (one per
  target, pointing at a committed `Sources/**/Info.plist` whose `properties`
  the spec owns); do not hand-edit those plists.
- **Do not commit `HSCC.xcodeproj`** — it is a generated artifact (the repo
  builds it with `xcodegen`). Regenerate, don't commit.

### Path B — Manual Xcode project

1. In Xcode, **File → New → Project → iOS → App**.
2. Name it **HSCC**, interface **SwiftUI**, language **Swift**, lifecycle
   **SwiftUI App**, and set the bundle id to **com.hscc.ios**.
3. Delete the generated `ContentView.swift` / `*App.swift` if present.
4. **Drag the `Sources/` folder** from this repo into the project (create
   folder references so files update automatically).
5. Set the **deployment target to iOS 26.0** (matches the SDK the code is
   type-checked against).
6. Under **Signing & Capabilities**, choose your development team (any personal
   team works for sideloading).
7. Add to the target's **Info.plist / build settings** an App Transport Security
   exception allowing arbitrary loads, since the app talks to the cluster over
   plain HTTP on the tailnet. (XcodeGen's Path A sets this via
   `NSAppTransportSecurity.NSAllowsArbitraryLoads`.)
8. Enable the extended capabilities the extras need: the widget + three Live
   Activity extension targets, the App Group `group.com.hscc.ios`, the Keychain
   access group, and `NSSiriUsageDescription` for the App Intents.

## Running the gates

Everything above the "unverified at runtime" line is reproducible on any Mac
with Xcode — none of it needs the real cluster:

```sh
cd ios-app
scripts/build_check.sh          # full per-target compile
scripts/nav_graph_check.sh      # XCUITest walk of all 17 drawer rows + negative self-check
scripts/model_decode_check.sh   # real models vs the 54 committed live fixtures
scripts/capture_screens.sh      # tap-free screenshot set → docs/design/screens/
```

## Sideloading onto your own device

This app is **sideloaded** — installed directly from Xcode onto your own iPhone,
not distributed through the App Store. You can do this with a **free Apple ID
personal team**; a paid Apple Developer account is **not** required.

1. **Sign the app with your Apple ID.** In Xcode, open **Signing &
   Capabilities** for the HSCC target, check **Automatically manage signing**,
   and select **your Apple ID** as the team. Xcode will register a free
   personal development team the first time.
2. **Set a unique bundle id.** The spec defaults to `com.hscc.ios`. Personal
   team signing sometimes collides with an id already used by another free app
   on your device — if signing fails, change the bundle id (e.g.
   `com.yourname.hscc`) under **Signing & Capabilities → Bundle Identifier**
   (also update `options.bundleIdPrefix` / `PRODUCT_BUNDLE_IDENTIFIER` in
   `project.yml` for the XcodeGen path).
3. **Connect your iPhone** and select it as the **Run** destination, then press
   **Run (⌘R)**. Xcode builds and installs the app on the device.
4. **Trust the developer certificate on the device.** The first time you try to
   open a sideloaded app the device blocks it. On the iPhone: **Settings →
   General → VPN & Device Management → tap your Apple ID under "Developer App"
   → Trust "Apple Development: …"**. Then the app opens normally.

> ⚠️ **The #1 surprise: a free-cert build expires after 7 days.** A free
> personal-team signature only lasts **7 days**. After that the app **stops
> launching** (it still opens, but crashes/refuses to run; iOS will not launch
> it). To keep using it, open it in Xcode and **Run (⌘R)** again — this
> re-signs and reinstalls it for another 7 days. You do not lose the app or its
> settings (the Keychain survives reinstall via the device-backup/signing tie)
> — you just have to re-run from Xcode roughly weekly.

**Free-tier limits worth knowing** (free personal team): only a handful of app
ids / devices are supported, no push notifications, no CloudKit entitlements,
no iCloud, and on-device app ids can't exceed a few — irrelevant here since
this is a solo client for one person's own cluster.

## Connecting over Tailscale

The app reaches the HSCC API **only over your Tailscale tailnet** — there is no
public/internet path. **The API must be bound to the tailnet for the phone to
reach it** (see below).

**Before you can connect, both devices must be on the same tailnet:**

- The **iPhone** has Tailscale installed, running, and **connected** (green
  "Connected" in the Tailscale app; make sure you're signed into the **same
  Tailscale account/tailnet** as the Mac).
- The **Mac** (the cluster host) is also signed into that same tailnet.

**Find the Mac's tailnet IP — this is the `host` you enter in the app.** It is
a `100.x.y.z` CGNAT address (your own value will differ; placeholder
throughout):

- **From the Tailscale menu bar app:** click the Tailscale icon on the Mac,
  your machine's IP (a `100.x.y.z`) is shown; or
- **From a terminal via the Tailscale CLI.** Note: if Tailscale is the macOS
  **App Store** build, the CLI is **not on PATH**. It lives at

  ```sh
  /Applications/Tailscale.app/Contents/MacOS/Tailscale ip -4
  ```

  (If your Tailscale was installed a different way, a bare `tailscale ip -4`
  may work instead.)

**In the app:** open the **Settings** screen and enter **host** (the Mac's
`100.x` tailnet IP), **port** (this deployment runs the API on `8788` — set it
to match what `hscc api status` reports), and
**token** (see below). Tap **Test connection** — it calls `GET /v1/ping`
against those settings and reports success or a clear error.

## Getting the API running + the token

**Start the API on the Mac** (the cluster host). You need the tailnet bind:
without it the API only listens on loopback and is unreachable from the phone
(loopback is the safe default; tailnet is an explicit opt-in):

```sh
hscc api start --tailscale   # bind to this host's tailnet IP (reachable from phone)
hscc api status              # confirm it's running and see the bound host:port
# ...and when you're done:
hscc api stop
```

- This deployment runs the API on **port `8788`** (set in `~/.hscc/api.json`).
  The API's code default is `8787`; the app pre-fills `8788` to match this
  deployment. Set the app's **port** to match what `hscc api status` reports.
- By default the API binds **loopback (`127.0.0.1`)** only. The
  `--tailscale` flag opts in to binding the tailnet IP, which is what makes it
  reachable from your phone. `0.0.0.0` / binding every interface is **refused
  by design** — the API can start/stop GPU work, so it must never be exposed
  publicly.

**The token** is generated automatically the first time you start the API and
written to **`~/.hscc/api-token` on the Mac** (mode `0600`, only readable by
your user). The fastest pairing path is the QR that `hscc api status` prints —
the app's Settings → **Scan QR** fills host, port and token at once.
Or read the file and paste it yourself:

```sh
cat ~/.hscc/api-token
```

(This document deliberately does not print any real token value — and you
should be careful not to paste a real one into a shared chat, issue, or
screenshot. The QR encodes the same credential: treat the QR like a password.)

**To rotate the token** (do this if it may have leaked):

```sh
hscc api stop
rm ~/.hscc/api-token
hscc api start --tailscale   # writes a fresh token; update it in the app too
```

## Security notes

Short and honest — this is a private tool, not a hardened public service:

- **Transport:** Tailscale IS the encrypted transport (WireGuard). The HSCC API
  itself does **not** terminate TLS — it serves plain HTTP, which is fine only
  because it rides over the encrypted tailnet. Never expose it any other way.
- **Auth:** the bearer token is required on **every** request — reads included.
  On the device it lives in the **iOS Keychain**, never in UserDefaults,
  UserDefaults-backed plists, or source.
- **Never public-expose the API port.** Do not port-forward it, do not bind it
  to a public interface (the API refuses `0.0.0.0` by design), and do not
  expose it over the internet. Tailscale is the only intended path.
- **Treat the token like an SSH key.** Anyone with the token **plus** tailnet
  access can dispatch work and stop workloads on your cluster. Keep it secret;
  rotate it (`hscc api stop` → `rm ~/.hscc/api-token` → `hscc api start
  --tailscale`) if it may have leaked, and update the app with the new value.

No tailnet IP, token, or API key is hardcoded anywhere in this repo.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| App shows an error / can't connect | Tailscale is **down on the phone** | Open the Tailscale app on the iPhone and confirm it shows **Connected** |
| Can't connect | The Mac isn't on the same tailnet / not signed into the same account | Confirm both devices are in the **same tailnet** with the same account |
| Can't connect | Wrong host or port in the app | `hscc api status` on the Mac shows the real bound host:port — make the app match (8788) |
| Can't connect | API isn't running | `hscc api start --tailscale` on the Mac |
| Can't connect | API bound to loopback, not tailnet | Start with `--tailscale` (loopback is unreachable from the phone); see `hscc api status` |
| `401 unauthorized` | Token mismatch / token was rotated | Re-copy the current value from `~/.hscc/api-token` into the app Settings (or re-scan the QR) |
| App was working, now won't launch after ~a week | **7-day free-cert expiry** | Open in Xcode and **Run (⌘R)** again to re-sign/reinstall; re-trust the cert if prompted |
| App opens but is blocked by iOS | Developer cert not yet trusted | Settings → General → VPN & Device Management → trust your Apple ID |
| Widget / Siri show nothing though the app works | **Free team can't provision App Groups** | Paid developer team required for sharing; the app shows an "App Group unavailable" banner when this is the case |
| A project's Chat hangs / times out | **Bloated orchestrator session** | The session needs rotation (auto-rotate / re-create it on the cluster). Healthy sessions answer in ~2 s; a bloated one can wedge to 600 s. |

## Further reading

- [**E2E API review 2026-08-27**](docs/E2E-API-REVIEW-2026-08-27.md) — the
  dated view → endpoint → model verification table (every row confirmed against
  the live API, every model decoded against captured JSON) plus the two decode
  fixes that pass made; relocated out of this README per the 2026-10-15 audit,
  with its historical rows marked.
- [**Sideload · Tailscale · Security**](docs/SIDELOAD-TAILSCALE-SECURITY.md) —
  the same guidance above as a standalone reference you can keep handy.
- [**Device smoke checklist**](docs/DEVICE-SMOKE-CHECKLIST.md) — five
  on-device checks (QR pairing, chat, backgrounding, wrong/revoked token) that
  can't be automated on the build host; run before handing a build to the
  operator for testing.
- [**Nav-graph gate design**](docs/design/NAV_GRAPH_t_810ab152.md) — what the
  walker proves and what it deliberately cannot.
- The API contract and CLI surface live in `docs/API.md` on the
  `feat/hscc-api` branch (and in the installed `hscc` tool itself via
  `hscc api --help`).
- The current IA spec: `docs/design/IOS_REDESIGN_2026-10-14.md` (repo root).
