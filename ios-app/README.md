# HSCC iOS App

A **private** iOS app to manage the owner's personal DGX cluster + project
kanban over Tailscale. It talks to the HSCC HTTP API (Phase A) which listens on
the Tailscale tailnet.

**This app is for sideloading onto the owner's own device only — it is NEVER
distributed, and is deliberately NOT App Store–ready.**

> ⚠️ **STATUS: UNBUILT, UNTESTED.** No one has compiled or run this app yet.
> This is Phase B1 — an app skeleton, an API client, and a Settings screen only.
> The first `xcodegen` / Xcode build is expected to need small fixes. The code
> has been syntax-checked (`swiftc -parse`) but **not built, run, or installed
> on a device**. Do not assume it works until a real build has been done.

## What's here (Phase B1)

- `Sources/HSCC/` — Swift sources (SwiftUI, iOS 17+).
  - `HSCCApp.swift` — app entry point.
  - `ContentView.swift` — root view: connection banner + placeholder tabs
    (Cluster, Kanban, Settings). B2/B3 fill in the feature tabs.
  - `HSCCClient.swift` — `async/await` URLSession API client; Bearer auth on
    every request; builds URLs from a configurable host + port over plain
    HTTP; decodes the unified error shape; exposes the `speak` field on reads.
  - `Models.swift` — `Codable` models matching the actual `/v1` response
    shapes.
  - `APIError.swift` — the typed error surfaced by the client.
  - `KeychainStore.swift` — stores the API token in the iOS Keychain.
  - `SettingsStore.swift` — observable settings (host/port  in `UserDefaults`,
    token in the Keychain).
  - `Views/SettingsView.swift` — enter/persist host, port, token + **Test
    connection** (calls `GET /v1/ping`).
  - `Views/PlaceholderViews.swift` — placeholder tabs for B2/B3/B4.
- `project.yml` — XcodeGen spec.

No third-party dependencies. Sideload-friendly.

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

## Before first launch

- Host: starts **empty** — enter your machine's Tailscale hostname or IP
  (e.g. `100.x.y.z` or `my-host`).
- Port: defaults to `8787` (the HSCC API default) and is editable.
- Token: the bearer token from `~/.hscc/api-token` on the cluster host. It is
  stored **only in the iOS Keychain** — never in UserDefaults, a plist, or
  source.
- Tap **Test connection** to hit `GET /v1/ping`.

No tailnet IP, token, or API key is hardcoded anywhere in this repo.

## Scope of later phases

- **B2** — cluster + fleet views (status, hosts, health, monitor).
- **B3** — kanban views (cards, standup, review/QA queues).
- **B4** — actions (confirm-gated dispatch / merge / stop).
- **B5** — Siri App Intents + spoken `speak` summaries (in-car, via Siri —
  deliberately NOT a CarPlay text/keyboard surface).
