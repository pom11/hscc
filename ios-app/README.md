# HSCC iOS App

A **private** iOS app to manage the owner's personal DGX cluster + project
kanban over Tailscale. It talks to the HSCC HTTP API (Phase A) which listens on
the Tailscale tailnet.

**This app is for sideloading onto the owner's own device only — it is NEVER
distributed, and is deliberately NOT App Store–ready.**

> ⚠️ **STATUS: UNBUILT, UNTESTED.** No one has compiled or run this app yet.
> This is Phase **B3** — an app skeleton, an API client, a Settings screen,
> read-only **cluster + fleet + kanban/project views** (health / hosts /
> workloads / stats / throughput / streams / autoscale / cards / standup /
> review + QA queues). Not yet landed: **B4** (confirm-gated actions) and
> **B5** (Siri App Intents). The first `xcodegen` / Xcode build is expected to
> need small fixes. All code has been syntax-checked (`swiftc -parse`) but
> **not built, run, or installed on a device**. Do not assume it works until a
> real build has been done.

## What's here (Phase B3)

- `Sources/HSCC/` — Swift sources (SwiftUI, iOS 17+).
  - `HSCCApp.swift` — app entry point.
  - `ContentView.swift` — root view: connection banner + tabs (Cluster,
    Kanban, Settings). B2 implemented the Cluster tab; B3 filled in Kanban.
  - `HSCCClient.swift` — `async/await` URLSession API client; Bearer auth on
    every request; builds URLs from a configurable host + port over plain
    HTTP; decodes the unified error shape; exposes the `speak` field on reads
    (B2 added cluster + fleet read methods).
  - `Models.swift` — `Codable` models matching the actual `/v1` response
    shapes (B2 added fleet stats / throughput / streams models).
  - `APIError.swift` — the typed error surfaced by the client.
  - `KeychainStore.swift` — stores the API token in the iOS Keychain.
  - `SettingsStore.swift` — observable settings (host/port in `UserDefaults`,
    token in the Keychain).
  - `Views/SettingsView.swift` — enter/persist host, port, token + **Test
    connection** (calls `GET /v1/ping`).
  - `Views/ClusterView.swift` — Cluster tab (B2): overall health at a glance
    (hosts up / workloads running / idle), running workload list, registered
    hosts, cluster config, pull-to-refresh, and a link to the Fleet view.
    Replaces the old cluster placeholder.
  - `Views/FleetView.swift` — Fleet view (B2): health (5-check verify),
    throughput, stats, daemon streams, autoscale — each with its own
    loading / error / empty state.
  - `Views/LoadState.swift` — a small generic async-load container shared by
    the cluster/fleet/kanban views.
  - `Views/KanbanView.swift` — Kanban tab (B3): project board read views.
  - `Views/StandupView.swift`, `Views/CardsView.swift`,
    `Views/ReviewQueueView.swift`, `Views/QAQueueView.swift` — the individual
    kanban read panels (standup, cards, review queue, QA queue), each with
    loading / error / empty states.
- `project.yml` — XcodeGen spec (all sources listed explicitly).

No third-party dependencies. Sideload-friendly.

## API contract

The app implements the HSCC HTTP API contract in `docs/DESIGN-api.md` (see the
`feat/hscc-api` branch). Key facts the client relies on:

- **Auth:** every request (reads included) must carry
  `Authorization: Bearer ***`
- **Transport:** plain HTTP over Tailscale is fine; TLS is deliberately out of
  scope (Tailscale is the encrypted transport).
- **Errors:** every error is `{ "error": { code, message, speak } }`.
  The client maps 401 → "check your token" and connection failure → "can't
  reach the cluster — is Tailscale connected?".
- **`speak`:** every READ response carries a first-class `speak` string, which
  B5 (Siri App Intents) will read aloud. The client exposes it on all read
  models.

## Building — two paths

The app is **unbuilt and unverified**; use whichever path works on your Mac.

### Path A — XcodeGen (reproducible)

```sh
brew install xcodegen
cd ios-app
xcodegen generate        # produces HSCC.xcodeproj from project.yml
open HSCC.xcodeproj
# Select your signing team under Signing & Capabilities, then Run on your device.
```

- `project.yml` lists every source file under `Sources`. If you add a new Swift
  file, add it to the `Sources` list in `project.yml` too (or use a folder glob;
  the spec currently lists sources explicitly).
- Info.plist keys come from the `info:` block in `project.yml`; no hand-maintained
  `Info.plist` is required.

### Path B — Manual Xcode project

1. In Xcode, **File → New → Project → iOS → App**.
2. Name it **HSCC**, interface **SwiftUI**, language **Swift**, lifecycle
   **SwiftUI App**, and set the bundle id to **com.hscc.ios**.
3. Delete the generated `ContentView.swift` / `*App.swift` if present.
4. **Drag the `Sources/` folder** from this repo into the project (create
   folder references so files update automatically).
5. Set the **deployment target to iOS 17.0** (or higher).
6. Under **Signing & Capabilities**, choose your development team (any personal
   team works for sideloading).
7. Add to the target's **Info.plist / build settings** an App Transport Security
   exception allowing arbitrary loads, since the app talks to the cluster over
   plain HTTP on the tailnet. (XcodeGen's Path A sets this via
   `NSAppTransportSecurity.NSAllowsArbitraryLoads`.)

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
public/internet path.

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
`100.x` tailnet IP), **port** (`8787` by default — matches the HSCC API), and
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

- The **default port is `8787`** (set it in the app to match).
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
| Can't connect | Wrong host or port in the app | `hscc api status` on the Mac shows the real bound host:port — make the app match |
| Can't connect | API isn't running | `hscc api start --tailscale` on the Mac |
| Can't connect | API bound to loopback, not tailnet | Start with `--tailscale` (loopback is unreachable from the phone); see `hscc api status` |
| `401 unauthorized` | Token mismatch / token was rotated | Re-copy the current value from `~/.hscc/api-token` into the app Settings |
| App was working, now won't launch after ~a week | **7-day free-cert expiry** | Open in Xcode and **Run (⌘R)** again to re-sign/reinstall; re-trust the cert if prompted |
| App opens but is blocked by iOS | Developer cert not yet trusted | Settings → General → VPN & Device Management → trust your Apple ID |

## Further reading

- [**Sideload · Tailscale · Security**](docs/SIDELOAD-TAILSCALE-SECURITY.md) —
  the same guidance above as a standalone reference you can keep handy.
- The API contract and CLI surface live in `docs/API.md` on the
  `feat/hscc-api` branch (and in the installed `hscc` tool itself via
  `hscc api --help`).

## Scope of later phases

- ~~**B3** — kanban views (cards, standup, review/QA queues).~~ ✅ landed.
- **B4** — actions (confirm-gated dispatch / merge / stop) — *pending*.
- **B5** — Siri App Intents + spoken `speak` summaries (in-car, via Siri —
  deliberately NOT a CarPlay text/keyboard surface) — *pending*.
