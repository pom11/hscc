# HSCC iOS App

A **private** iOS app to manage the owner's personal DGX cluster + project
kanban over Tailscale. It talks to the HSCC HTTP API (Phase A) which listens on
the Tailscale tailnet.

**This app is for sideloading onto the owner's own device only — it is NEVER
distributed, and is deliberately NOT App Store–ready.**

> ⚠️ **STATUS: UNBUILT, UNTESTED AT RUNTIME.** No one has compiled this app
> onto a device or simulator. Only the **Swift compiler** is verified (the
> whole `Sources/HSCC` + `Sources/Shared` tree type-checks with **0 errors**
> against the iOS 26 simulator SDK), and the **Codable models are verified
> against the real live API** (every response shape decodes field-for-field).
> The app has **never been run** — no iOS platform runtime / simulator is
> installed on this build host, so `xcodebuild` cannot resolve any iOS
> destination. Siri App Intents, the Home Screen widget, and the Live Activity
> are all **unverified at runtime** until a real signed build exists on a
> device. Do not assume anything works until a real build has been run.
> See **"Honest limits"** below.

## What's here — the real shipped surface

The information architecture is **project-first**: three tabs, with everything
fleet-level folded into one Cluster tab.

- `Sources/HSCC/` — Swift sources (SwiftUI, iOS 26+).
  - `HSCCApp.swift` — app entry point.
  - `ContentView.swift` — root view: connection banner + **three tabs:
    Projects · Cluster · Settings**. The old six-tab layout and the duplicate
    nested Settings entry point are gone. Settings is now app-connection only.
  - `HSCCClient.swift` — async/await URLSession API client; Bearer auth on
    every request (reads included); builds URLs from a configurable host +
    port over plain HTTP; decodes the unified error shape; surfaces the
    `speak` field on reads. This is the single seam every screen's data flows
    through. Mutating endpoints (B4) always send `confirm: true`; the view is
    responsible for gating the call behind a confirm UI first.
  - `Models.swift` — the Codable models for the app-only surface. Mirrors the
    real `/v1` JSON field-for-field (see **End-to-end review** below):
    ping, cluster hosts, health/verify, autoscale, fleet stats / throughput /
    streams, cards, standup, review + QA queues, projects + project detail,
    daemon / triggers / escalations / profiles, kanban blocked + stale,
    templates list / status / preview, and the B4 mutating responses.
  - `APIError.swift` — the typed error surfaced by the client.
  - `KeychainStore.swift` — stores the API token in the iOS Keychain.
  - `SettingsStore.swift` — observable settings (host/port in `UserDefaults`,
    token in the Keychain).
  - `Views/` — all screens, grouped:
    - `ContentView` (above) +
      `Views/ProjectsView.swift` — the **primary tab**: the twelve projects
      from `/v1/projects`, each opening a detail screen with segmented sections
      **Overview · Chat · Board · Settings**.
      - **Overview** — `GET /v1/projects/{name}`: board counts + git state.
      - **Chat** — `OrchestratorChatView` fixed to that project on the **JOB
        API**: `POST /v1/orchestrator/chat` returns a `job_id` immediately
        (202), then `GET /v1/orchestrator/chat/{id}` is polled for the reply.
        **Sessions persist per project** on the server — each project is a
        continuing conversation, not one-shots.
      - **Board** — `GET /v1/cards` filtered to the project's board, plus its
        blocked/stale cards (from `/v1/kanban/blocked` + `/v1/kanban/stale`) —
        with `CardDetailView` on `GET /v1/cards/{id}`.
      - **Settings** — project repo/board/topic + orchestrator profile/session
        presented **read-only** (they live on the cluster, not in the app — no
        fake editable controls).
    - `Views/SettingsView.swift` — enter/persist host, port, token + **Test
      connection** (`GET /v1/ping`).
    - `Views/ClusterView.swift` — **Cluster tab / fleet hub**: the node
      topology strip pinned at the top (derived from `/v1/cluster/status`),
      then nested screens folding in everything fleet-level — Health & Ops
      (`Views/OpsView.swift`: verify / daemon / triggers / escalations /
      profiles), Fleet (`Views/FleetView.swift`: health, stats, throughput,
      streams, autoscale), Fleet Control (up/down), Templates, Autodown, and
      Board Hygiene. Nothing fleet-related lives outside this tab.
    - `Views/AutodownView.swift` — the operator's most-used surface: the live
      `/v1/autodown/status` report + confirm-gated enable / disable / wake /
      cancel. Wake enters a polling "waking" state and starts the **Live
      Activity** (the fleet wake is ~9 minutes — never block the UI).
    - `Views/TemplatesView.swift` + `Views/TemplateDetailView.swift` — the
      **Templates library**: browse (`/v1/template/list`), preview against the
      node topology (`/v1/template/preview/{name}`, read-only dry-run), and
      **confirm-gated apply** (`/v1/template/apply`), with post-apply reload
      polling of `/v1/template/status` + `/v1/verify`.
    - `Views/BoardHygieneView.swift` — blocked (with confirm-gated recover)
      and stale cards across every board.
    - `Views/SearchView.swift` — **cross-project search** over last-known
      projects + cards, so it works even when the cluster is unreachable.
    - `Views/OrchestratorChatView.swift` — the confirm-gated chat surface (job
      based) with an honest elapsed-second in-flight footer and a
      prompt→reply transcript. Reachable from a project's Chat section.
    - `Views/Theme.swift` — the design system: `Theme.Semantic.*` dynamic
      roles (surface / ok / warn / bad / …) own the palette — **no raw hex
      anywhere else** — plus `Font.hsccMono(...)` for machine-produced values.
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
  - `Intents/` — Siri App Intents:
    - `ProjectIntents.swift` — **per-project Siri shortcuts (job-based)**:
      `HSCCProject` (an AppEnum of the 12 projects, matching `/v1/projects`
      exactly), `AskOrchestratorIntent` (job-based chat — ack immediately,
      poll in a detached task, speak the reply), `ProjectStatusIntent` (speak
      the per-project `speak` summary verbatim).
    - `ClusterStatusIntent` + `ReviewQueueIntent` — speak each endpoint's
      `speak` one-liner.
    - `CannedCard` + `DispatchCannedCardIntent` — voice-dispatch a KNOWN card
      via the confirm-gated client (never free-form dictation).
    - `AppShortcuts` — the `AppShortcutsProvider` with natural phrases.
    - `IntentClient` — builds the client from the same stored settings.
- `Sources/Shared/` — cross-target glue compiled into all three bundles:
  `Speakable`, the shared endpoint models the widget / Live Activity also need
  (`AutodownStatusResponse`, `ClusterStatusResponse` / `ClusterWorkload`,
  `TopologyPair` / `TopologyNode` / `TopologySnapshot`), the App Group
  `group.com.hscc.ios`, shared Keychain access, `APIConfig`, and the
  last-known `SnapshotStore`.
- `Sources/HSCCWidgets/` — the **Home Screen widget** (`systemSmall` + medium):
  shows the last-known cluster state + compact topology; **`Unreachable` is a
  first-class state** — \"Can't reach the cluster\" with the last-known state
  and its age, never a blank or stale-looking-live widget.
- `Sources/HSCCLiveActivity/` — the fleet-wake **Live Activity** configuration
  (Dynamic Island compact = elapsed + a state dot; expanded = the topology
  pairs coming online; Lock Screen = elapsed + units up). Ends explicitly on
  success or failure.
- `project.yml` — XcodeGen spec, listing every source file explicitly. Defines
  the app plus the two embedded extension targets.
- `scripts/` — verification helpers:
  - `shared_model_check.swift` — decodes the shared widget/Live Activity model
    shapes against live API JSON (runs as a macOS CLI).
  - `model_decode_check.sh` — compiles the **real** `Models.swift` +
    `SharedModels.swift` + `APIError.swift` into a macOS CLI and decodes the
    committed live fixtures (under `scripts/model_decode_check/fixtures/`)
    against them, so it can never drift into a false green like a mirrored
    validator could. Replaces the old field-for-field mirror
    (`model_decode_check.swift`, removed).

**Offline last-known state** is a first-class feature: every successful read is
cached, and when the cluster (or Tailscale) is unreachable, views show the
last-known data clearly marked stale with its age via `StaleBanner` — never a
blank lie. `LoadState.offline`/`.stale`, `StateCache`, and `SnapshotStore` back
this.

No third-party dependencies. Sideload-friendly. **Default port is `8788`**
(8787 is taken by another service on this host — don't change it in the app).

## End-to-end review: view → endpoint → model (verified against the live API)

Verified 2026-08-27 against the live API (READS only).
Every row was confirmed with a real `curl`-equivalent GET, and every app model
was **actually decoded** against the captured live JSON (see
`scripts/model_decode_check.sh` — compiles the real model sources, decodes 26
committed fixtures 26/26 — plus `scripts/shared_model_check.swift`).

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
| Board Hygiene → blocked | `GET /v1/kanban/blocked` | `KanbanBlockedResponse` | OK |
| Board Hygiene → stale | `GET /v1/kanban/stale` | `KanbanStaleResponse` | **FIXED** (same as above) |
| Templates → applied | `GET /v1/template/status` | `TemplateStatusResponse` / `TemplateApplied` | OK |
| Templates → library | `GET /v1/template/list` | `TemplateListResponse` / `ClusterTemplate` | OK |
| Templates → preview | `GET /v1/template/preview/{name}` | `TemplatePreviewResponse` | OK |
| Standup / review / QA models | `GET /v1/standup`, `/v1/review/queue`, `/v1/qa/queue` | `StandupResponse` / `ReviewQueueResponse` / `QAQueueResponse` | OK (client methods exist + decode; not wired to a current screen — kept for the API surface) |
| Widget + Live Activity reads | `GET /v1/autodown/status`, `/v1/cluster/status` | shared models | OK |
| Fleet Control (up/down) | `POST /v1/cluster/up`, `/v1/cluster/down` | `ClusterUpResponse` / `ClusterDownResponse` | OK (confirm-gated) |

### The two fixes this pass made

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

## Honest limits — what is NOT verified

Be plain about what only the compiler has seen:

- **The app has NEVER been built or run on a device or simulator.** Only
  compile (`swiftc -typecheck`, 0 errors) + the model-decode checks are
  verified. There is **no iOS platform runtime installed on this build host** —
  `xcodebuild` cannot resolve any iOS destination, so not even a simulator run
  has happened. Nothing about the UI, navigation, layout, or runtime behaviour
  has been exercised.
- **Siri intents cannot be validated without a signed build on hardware.** The
  AppEnum, parameter summaries, and confirm dialogs are written but unrun; a
  real device build is required to know they resolve and fire.
- **The Home Screen widget and the Live Activity are unverified at runtime.**
  Their model shapes decode the live JSON, but the extension bundles have not
  been installed or run — widget refresh, Dynamic Island rendering, and Live
  Activity lifecycle are all untested.
- **Per-project chat depends on session health.** A healthy session answers in
  ~1.8 s; a **bloated session wedges chat** (measured: a 600 s timeout against
  a fresh 1.8 s). The chat surface can only be as healthy as the orchestrator's
  underlying session — the rotation work (auto-rotate bloated sessions) exists
  precisely because of this, and the app does not and cannot fix a wedged
  session by itself.

Do not read these as \"should work\" — they are real, unexercised surfaces.

## API contract

The app implements the HSCC HTTP API contract in `docs/DESIGN-api.md` (see the
`feat/hscc-api` branch). Key facts the client relies on:

- **Auth:** every request (reads included) must carry
  `Authorization: Bearer ***
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
  no raw hex anywhere outside this file.** Views reference semantic roles only —
  a hardcoded `.green` / `.red`/`.secondary` appears in a few places as an
  accessibility convenience, but the palette lives in `Theme.swift`.
- **`NodeTopologyView.swift` renders the two tensor-parallel pairs.** The
  gateway (`.244`) heads the orchestrator pair; only each pair's head serves
  HTTP. Each node's dot is coloured by live state (up / busy / warn / down /
  unknown).
- **Monospaced type (`Font.hsccMono`) for machine values** (ids, ips, repo
  paths, counts, timestamps); **proportional type for human prose.** This is
  the design rule the whole app follows.

## Building — two paths

The app is **unbuilt and unverified**; use whichever path works on your Mac.

### Path A — XcodeGen (reproducible)

```sh
brew install xcodegen
cd ios-app
scripts/generate.sh      # produces HSCC.xcodeproj from project.yml, with signing
open HSCC.xcodeproj
# Run on your device.
```

**Use `scripts/generate.sh`, not bare `xcodegen generate`.** There are THREE
targets — the app plus two app extensions (`HSCCWidgets`, `HSCCLiveActivity`) —
and each needs its own `DEVELOPMENT_TEAM`. Setting the team on the app alone
still fails with:

```
Signing for "HSCCWidgets" requires a development team.
Signing for "HSCCLiveActivity" requires a development team.
```

Picking a team in Xcode's Signing & Capabilities editor fixes it only until the
next regenerate — `HSCC.xcodeproj` is a generated artifact and is overwritten.
The script sets the team at project level (all three targets inherit it), then
verifies with `xcodebuild -showBuildSettings` that it actually landed on each one.

It auto-detects the team from your signing certificate. Override or supply it
explicitly when auto-detection finds nothing:

```sh
HSCC_DEVELOPMENT_TEAM=YOURTEAMID scripts/generate.sh
```

A free personal Apple ID team works for sideloading onto your own device.

- `project.yml` lists every source file under `Sources`. If you add a new Swift
  file, add it to the `Sources` list in `project.yml` too (or use a folder glob;
  the spec currently lists sources explicitly).
- Info.plist keys come from the `info:` block in `project.yml`; no hand-maintained
  `Info.plist` is required.
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
8. Enable the extended capabilities the extras need: the widget + Live Activity
   extension targets, the App Group `group.com.hscc.ios`, the Keychain access
   group, and `NSSiriUsageDescription` for the App Intents.

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

**Find the Mac's tailnet IP — this is the `host` you enter in the app.** The
read of the `100.x` address is the example from this host. That exact number
will not match your setup — substitute your own tailnet IP:

- **From the Tailscale menu bar app:** click the Tailscale icon on the Mac,
  your machine's IP (a `100.x.y.z`) is shown; or
- **From a terminal via the Tailscale CLI.** Note: on this host Tailscale is
  the macOS **App Store** build, so the CLI is **not on PATH**. It lives at

  ```sh
  /Applications/Tailscale.app/Contents/MacOS/Tailscale ip -4
  ```

  (If your Tailscale was installed a different way, a bare `tailscale ip -4`
  may work instead.)

**In the app:** open the **Settings** screen and enter **host** (the Mac's
`100.x` tailnet IP), **port** (`8788` by default on this deployment), and
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

- The **default port is `8788`** on this deployment — **do not change this in
  the app**; 8787 is taken by another service on this host. Set it in the app
  to match.
- By default the API binds **loopback (`127.0.0.1`)** only. The
  `--tailscale` flag opts in to binding the tailnet IP, which is what makes it
  reachable from your phone. `0.0.0.0` / binding every interface is **refused
  by design** — the API can start/stop GPU work, so it must never be exposed
  publicly.

**The token** is generated automatically the first time you start the API and
written to **`~/.hscc/api-token` on the Mac** (mode `0600`, only readable by
your user). To read it so you can copy it into the app:

```sh
cat ~/.hscc/api-token
```

Copy that single line into the **Token** field in the app's Settings. (This
document deliberately does not print any real token value — and you should be
careful not to paste a real one into a shared chat, issue, or screenshot.)

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
| `401 unauthorized` | Token mismatch / token was rotated | Re-copy the current value from `~/.hscc/api-token` into the app Settings |
| App was working, now won't launch after ~a week | **7-day free-cert expiry** | Open in Xcode and **Run (⌘R)** again to re-sign/reinstall; re-trust the cert if prompted |
| App opens but is blocked by iOS | Developer cert not yet trusted | Settings → General → VPN & Device Management → trust your Apple ID |
| A project's Chat hangs / times out | **Bloated orchestrator session** | The session needs rotation (auto-rotate / re-create it on the cluster). Healthy sessions answer in ~2 s; a bloated one can wedge to 600 s. |

## Further reading

- [**Sideload · Tailscale · Security**](docs/SIDELOAD-TAILSCALE-SECURITY.md) —
  the same guidance above as a standalone reference you can keep handy.
- The API contract and CLI surface live in `docs/API.md` on the
  `feat/hscc-api` branch (and in the installed `hscc` tool itself via
  `hscc api --help`).

## Scope of later phases

- ~~**B3** — kanban views (cards, standup, review/QA queues).~~ ✅ landed.
- ~~**B4** — actions (confirm-gated dispatch / merge / template-apply / stop).~~ ✅ landed.
- ~~**B5** — Siri App Intents + spoken `speak` summaries (in-car, via Siri —
  deliberately NOT a CarPlay text/keyboard surface).~~ ✅ landed (unverified —
  needs a real signed build on a device).
- ~~**B5/C5** — per-project chat (job-based) + a template library.~~ ✅ landed
  (chat unverified at runtime; job API + models are verified).
- ~~**C5/C6** — offline last-known state, cross-project search, project Settings
  read-only, board hygiene, autodown control.~~ ✅ landed (offline verified
  structurally; nothing runtime-tested).
- ~~**Home Screen widget + fleet-wake Live Activity** — three-bundle App Group
  architecture.~~ ✅ landed (model shapes verified; extension runtime unverified).
