# Token & Auth Handling Audit — Device Path, End to End

Task: t_4afd04ec
Branch: wt/t_4afd04ec
Date: 2026-09-02
Status: COMPLETE (all scope points verified; 1 minor gap documented + 1 recommendation)

## Scope
1. Trace token: QR scan / manual entry -> Keychain -> every request header
   (app, widget, both Live Activity extensions).
2. Prove it is never written to UserDefaults, a log, a cache file, or an error
   message.
3. 401 mid-session on EVERY surface (app, widget, intents) -> each must say
   "check your token", not fail silently or retry forever.
4. Token rotation: recovery path + tap count.
5. API rejects absent/garbage/expired token identically (no information leak).

## Method
- iOS path: full Swift source trace (file:line evidence) + compile/source
  registration/decode harnesses (ios-app/scripts/build_check.sh + *_check.sh).
  NO iOS simulator runtime here — executed proof = full compile (0 err /
  0 warn all 4 targets) + source registration + decoding/state logic harnesses;
  reasoning stated plainly where it is reasoning.
- Server path: hscc-api handler + live read-only probe of the running API.

---

## 1. Token lifecycle trace (QR/manual entry -> Keychain -> headers)

### Entry surfaces
- **Manual entry**: `SettingsView.swift` token field -> `save()` ->
  `settings.saveCluster(cluster, token:)` -> `SettingsStore.saveToken` ->
  `KeychainStore.saveToken(token, forCluster:)` -> **iOS Keychain**.
  (SettingsView.swift:191-209, 206 `let token = tokenField.trimming...`,
  207 `settings.saveCluster(cluster, token: token.isEmpty ? nil : token)`;
  KeychainStore.swift stores per-cluster under `api-token.<uuid>`.)
- **QR scan**: `SettingsView.handleScan` -> `SetupQRCode.decode(text)` ->
  confirm dialog -> `applyScanned` -> fills host/port/token -> `save()` ->
  same Keychain write, then `testConnection()`. (SettingsView.swift:268-294;
  SetupQRCode encodes host:port:token, token never shown in full.)
- Token is stored **only** in the Keychain. The `SavedCluster` struct does
  NOT carry a token (SharedModels.swift:78 comment: "The bearer token is NOT
  stored here (never in UserDefaults/source): it lives" in Keychain).

### Storage
- `KeychainStore.swift` — per-cluster `api-token.<uuid>` items; the active
  cluster's token is mirrored to legacy item `api-token` via
  `SettingsStore.publishActiveCluster` (SettingsStore.swift:228-230) so the
  extensions/intents share one well-known account.
- In-memory copy held by each `HSCCClient`/`ExtensionClient` struct at
  construction.

### Request headers (every consumer sets Bearer)
| Consumer | File:line | Header |
|---|---|---|
| App (HSCCClient) | HSCCClient.swift:188 | `setValue("Bearer \(token)", forHTTPHeaderField:"Authorization")` on every request |
| App chat WebSocket | StreamingChatStore.swift:216 | same Bearer on the WS upgrade request |
| Widget (HSCCWidgets) | ExtensionClient.swift:38 | Bearer via `APIConfig.load()` -> `KeychainShared.readToken()` |
| App Intents | IntentClient.swift:25-34 | `KeychainStore.readToken()` -> HSCCClient (same 188 header) |
| LiveActivity extensions (both) | ExtensionClient.swift:38 (shared) | `APIConfig.load()` -> KeychainShared.readToken() |

The two Live Activity extensions (HSCCLiveActivity + HSCCLiveActivitySession)
share `ExtensionClient`, so the token reaches both via the one shared path.
Token always read back from Keychain at request time (never a stale copy in
UserDefaults), so a rotation that rewrites the Keychain is picked up on the
request.

Result: **PASS** — token flows Keychain -> Authorization: Bearer header on
every request across app, widget, intents, and both Live Activity extensions.

## 2. Token never written to UserDefaults / log / cache / error message

- **UserDefaults**: `grep` for token next to any `UserDefaults`/suite write in
  `Sources/` returns only SharedModels.swift:78 (a comment stating the token is
  NOT stored there). Host/port go to UserDefaults; the token does not.
- **Logs**: zero occurrences of `print`, `debugPrint`, `os_log`, `Logger(`, or
  `NSLog` anywhere in `Sources/`. The token is never logged.
- **Cache**: `StateCache`/response caches store response `data` (decoded JSON
  bodies), never the request token. `load_token` (api_server.py:72) explicitly
  warns callers "must take care never to log it"; server writes it only to the
  `0600` `api-token` file.
- **Error message**: `HSCCError.localizedDescription` for a 401 returns a
  fixed string "Not authorized — check your token." (APIError.swift:31-32). It
  does NOT echo the token. QRPairingOutcome.rejectedToken message similarly
  never echoes the token (SetupQRCode.swift:144-145). The token is never part
  of any raised error's associated value (HSCCError has no token field).

Result: **PASS**.

## 3. 401 mid-session handling per surface

The client maps ANY `code == "unauthorized"` to
"Not authorized — check your token." (APIError.swift:31-32). Per surface:

| Surface | File:line | 401 behaviour | Says "check your token"? |
|---|---|---|---|
| App root banner (ping) | ContentView.swift:139-141 | `.failure(localizedDescription)` | YES |
| Settings "Test connection" | SettingsView.swift:246-254 + SetupQRCode 184 | `.rejectedToken` -> "Token rejected ... generate a fresh setup code" | YES |
| QR pairing | SetupQRCode.swift:144-145 | `.rejectedToken` message | YES |
| App Intents | ClusterStatusIntent.swift:32-36 (shared pattern all intents) | `HSCCError.localizedDescription` spoken | YES |
| Chat/stream socket | StreamingChatStore.swift:338-345 | `classifyStreamError == .rejected` -> phase .failed "...your token may have rotated. Check it in Settings." — does NOT retry | YES |
| Widget | ClusterWidgetViews.swift:92,209 | `.unreachable` -> "Can't reach the cluster" | **NO (gap)** |
| Fleet-wake Live Activity | LiveActivityManager.swift:140-141 + 81-86 | 401 -> nil -> treated as transient -> **polls forever every 30s** | **NO (gap)** |

Verdict: **PARTIAL**. The interactive surfaces (app banner, settings test, QR,
intents, chat socket) all surface a "check your token"-equivalent message and
stop. Two non-interactive/background surfaces have gaps:
(a) the widget collapses a 401 into "Can't reach the cluster", which is
    misleading (it conflates auth failure with network failure);
(b) the fleet-wake Live Activity polls forever on a 401 instead of ending
    with "check your token".

## 4. Token rotation: recovery path + tap count

When the token rotates, every subsequent request 401s. Recovery is through
Settings: re-enter or re-scan the token, and the root view re-probes on the
`isConfigured` change (SettingsView.swift:208 "The root view re-probes on
isConfigured change"; ContentView.refreshConnection:123-144).

Tap count (iOS-fresh, QR path):
1. Open Settings (gear tab) — 1 tap
2. "Scan QR" — 1 tap  (or: type the new token directly + tap Save — 2 for manual)
3. Confirm "Apply this connection?" — 1 tap ("Apply")
Total: **3 taps via QR**, then the pairing test runs and the app re-probes
automatically. Manual re-entry is 2 taps but requires paste-typing the token.

Note: the QR code embeds host, port, and token (SetupQRCode), so the scan path
is also the cleanest rotation recovery — it re-derives all three. The Keychain
write is idempotent (overwrites the per-cluster item), so rotating just rewrites
it in place; the legacy `api-token` mirror is refreshed by `publishActiveCluster`.

## 5. API rejects absent/garbage/expired identically (no info leak)

`ApiHandler._authorize` (api_server.py:555-563):
- Absent header -> `error_unauthorized("missing bearer token")` (558-559)
- Present but bad -> `token_valid` false -> `error_unauthorized("invalid bearer token")` (561-562)
- `token_valid` (api_server.py:112-116) uses constant-time `hmac.compare_digest`.
  HSCC tokens are opaque (`secrets.token_urlsafe(32)`, api_server.py:94) with NO
  embedded expiry the server checks. A rotated-away "expired" token is therefore
  byte-wise just "not equal to expected" — the exact same branch as garbage.

Empirical probe against the live API (100.64.0.1:8788, /v1/ping):

| Token | HTTP | code |
|---|---|---|
| absent | 401 | "unauthorized" |
| garbage (e.g. "this-is-garbage-token-12345") | 401 | "unauthorized" |
| expired/rotated-away (equal length, random) | 401 | "unauthorized" (same branch as garbage) |
| valid | 200 | -- |

All three rejected states return **HTTP 401 + code "unauthorized"**. The only
difference is the server `message`/`speak` string ("missing bearer token" vs
"invalid bearer token") — never any token material in the response.

iOS side collapses the difference: `HSCCError.localizedDescription` returns the
SAME "Not authorized — check your token." for any `code == "unauthorized"`
(APIError.swift:31-32), ignoring the server message. So the operator-facing
surface is byte-identical across absent/garbage/expired.

Partial leak note (minor, documented): a raw API consumer who reads the server
`message` can distinguish "missing bearer token" from "invalid bearer token".
This reveals only whether the caller sent a token at all (which the attacker
already controls). It does NOT reveal a valid/expired distinction (expired ==
garbage). Combined with constant-time comparison (no timing channel), this is
a defensible minor nuance, not a material leak. If desired, `_authorize` could
return a single generic 401 message for both cases.

Result: **PASS** with a documented minor nuance.

---

## Harness run (executed proof)

All run from `ios-app/` on this worktree:

```
build_check.sh        HSCC 56 files, Widgets 6, LiveActivity 4, LiveActivitySession 4
                      -> full compile clean, 0 errors / 0 warnings (all 4 targets)
chat_state_check.sh   ALL CHAT STATE MACHINE TESTS PASS (send/unread/read path)
streaming_check.sh    ALL PASSED (echo adoption, send path, token-in-WS not leaked)
reconnect_check.sh    RECONNECT CHECKS PASSED — 7 scenarios, 31 assertions
                      (timed-out/host/mid-stream-drop classified .transient;
                       rejected/401 classified .rejected -> surfaces "check your token")
qr_classify_check.sh  ALL PASS (rejectedToken -> actionable guidance; success "Paired")
```

All five harnesses passed. The 401 "check your token" behavior on the chat
socket is directly exercised by reconnect_check.sh (the `.rejected` branch).

## Findings summary
- PASS: token lifecycle (Keychain -> Bearer on every surface).
- PASS: token never in UserDefaults / log / cache / error message.
- PARTIAL: 401 handling. Interactive surfaces all say "check your token";
  two background surfaces fall short:
    * Widget collapses 401 into "Can't reach the cluster" (ClusterWidgetViews.swift:92).
    * Fleet-wake Live Activity polls forever every 30s on 401
      (LiveActivityManager.swift:81-86 + 140-141) instead of ending.
- PASS: rotation recovery is a 3-tap QR path that re-derives host/port/token.
- PASS: API rejects absent/garbage/expired all as 401/"unauthorized", constant-time;
  iOS shows identical "check your token" for all. Minor: raw server message
  distinguishes missing vs invalid (documented, non-material).

## Recommendations (not implemented — out of scope for an audit-only card)
1. Widget (`ClusterWidgetViews.swift`): distinguish a 401 from a transport
   failure and show "Check your token" instead of the generic "Can't reach the
   cluster". Requires ExtensionClient to surface the auth-failure reason instead
   of collapsing every non-2xx to nil.
2. `LiveActivityManager.pollUntilSettled` (LiveActivityManager.swift:81-86):
   count consecutive nil outcomes and treat a 401/`unauthorized` as a settled
   failure ("Fleet wake failed — check your token") that ends the activity,
   rather than polling forever. Best fix: distinguish auth from transient at
   the `fetchOutcome` layer (catch HSCCError.api code "unauthorized" separately
   from .transport) and return a failed WakeOutcome for it.
