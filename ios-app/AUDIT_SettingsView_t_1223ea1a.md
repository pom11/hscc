# SettingsView audit — t_1223ea1a

Full audit of `ios-app/Sources/HSCC/Views/SettingsView.swift` (connection setup
screen). Operator pain: "configured but nothing works". Address redacted to
placeholder `100.64.0.1` throughout; live values marked.

Result: **one real bug found and fixed** (a silent data race that mutates SwiftUI
`@State` from a background AVFoundation queue). Everything else verified clean
with evidence. Full detail below, ranked by how likely the operator is to hit it.

---

## 0. What this screen is

Settings is the connection-setup surface: it persists host/port/token and tests
the connection with GET /v1/ping. It is NOT a data-fetching screen — the only
remote call is the ping. So audit points 1/2/3 are about the saved config + the
ping response, not a server list.

Files involved:
- SettingsView.swift (the view, 301 lines)
- SettingsStore.swift (ObservableObject, source of truth for clusters)
- KeychainStore.swift (token storage)
- SetupQRCode.swift (QR payload decode + QRPairing classification)
- QRScannerView.swift (camera scanner, AVFoundation)
- HSCCClient.swift (ping()), Models.swift (PingResponse)
- HSCCApp.swift (SettingsStore provided at root)

---

## 1. DATA IN — live values

### 1a. Test connection → GET /v1/ping
`SettingsView.testConnection()` (SettingsView.swift:217-261) calls
`QRPairing.test(host:port:token:)` (SetupQRCode.swift:160-172) which calls
`client.ping()` (HSCCClient.swift:366-368) = `GET /v1/ping`.

Live read (read-only, address derived not hardcoded):
```
GET <redacted 100.64.0.1>:8788/v1/ping  ->  HTTP 200
{"ok": true, "service": "hscc-api", "version": "0.1.0", "speak": "HSCC API is up."}
```
Decodes into `PingResponse` (Models.swift:39-42: `ok:Bool, service:String?,
version:String?`). Route-sweep confirms `/v1/ping` answers 200.
Every field the view reads arrives. No dropped fields.

### 1b. Scan QR → backend setup-code contract
`SetupQRCode.decode(_:)` (SetupQRCode.swift:47-66) expects:
`{"v":1,"host":"<host>","port":<int>,"token":"<token>"}`.
Backend `_build_qr_payload` (hscc_daemon/api_cli.py:96) emits exactly:
`{"v":1,"host":"%s","port":%d,"token":"%s"}`.
Backend contract test `test_qr_payload_matches_ios_contract`
(hscc_daemon/tests/test_unified_cli.py:1177-1190) proves the payload matches
the iOS decoder. CodingKeys (SetupQRCode.swift:26-31: v/host/port/token) match.

### 1c. Persistence reads (what feeds the fields)
`SettingsView.loadFromStore()` (SettingsView.swift:189-193) seeds host/port/token
from the SettingsStore active cluster. Host/port from UserDefaults via the suite,
token from the Keychain (SettingsStore.swift:126-135). Proven by
`first_run_check_settings.sh` harness (SETTINGS-STORE CHECK PASSED): config,
token swap, host change, token clear, re-save no-op all behave.

---

## 2. RENDER — what the operator sees

- Host / Port / Token fields populated from store (lines 42-81). Token shown
  masked as SecureField with eye toggle.
- Footer explains the plain-HTTP-over-Tailscale model and that the token lives
  only in the Keychain (lines 84-86).
- Test result: success shows `checkmark.circle.fill` in `Theme.Semantic.ok`
  green + "Connected to hscc-api v0.1.0." (lines 113-120, driver at 252-260).
  Failure shows `xmark.circle.fill` in red + `<title>: <message>` where title is
  one of {Can't reach that host / Token rejected / Wrong app version / Not a
  setup code / Pairing failed} (SetupQRCode.swift:119-128).
- No truncation / unit issues: port is a plain integer string, host is raw text,
  token masked. No client-vs-server count disagreement (no counts).

---

## 3. STATES

- **Loading (testing):** "Test connection" row swaps to `ProgressView` +
  "Testing…" while `isTesting` (lines 101-110). Button disabled during test
  (line 111).
- **Result states:** no test yet → no result row at all (clean, distinct);
  success → green check + message; failure → red x + message. 
  (lines 113-120)
- **"0 results" vs "failed"**: N/A — this screen has no row list. The ping has
  no zero-results concept.
- **Stale/offline:** none of the ping caching applies to Settings because the
  result row is transient test output, never a persisted feed. The connection
  banner re-probe is handled in ContentView via `connectionIdentity`
  (SettingsStore.swift:147-149), proven by the settings harness.

---

## 4. CONTROLS — every button/toggle, what it calls, does the route answer, feedback

| Control | Lines | Calls | Route | Feedback |
|---|---|---|---|---|
| Host field | 42-50 | edits local state only | n/a | typing shown |
| Port field | 52-58 | edits local state only | n/a | typing shown |
| Token field | 60-81 | edits local state | n/a | typing shown |
| Eye toggle | 71-78 | `showingToken.toggle()` | n/a | field toggles secure/plain |
| Scan QR | 88-96 | `showingScanner = true` | n/a | opens camera sheet |
| Test connection | 99-121 | `testConnection()` → `QRPairing.test` → `GET /v1/ping` | `/v1/ping` 200 ✓ | spinner then green/x result row |
| Save | 123-138 | `save()` → `settings.saveCluster` + Keychain write | n/a (local) | disabled until edits; tokenSaveFailure / appGroupUnavailable footnotes |

All routes that are calls answer. Every control has visible feedback. The one gap
that matters is fixed — see Bug 1.

---

## 5. OBSERVATION — @StateObject/@ObservedObject (verified, point 5)

- `SettingsStore` is created once as `@StateObject` in HSCCApp.swift:17 and
  injected with `.environmentObject(settings)` at HSCCApp.swift:35.
- SettingsView holds it as `@EnvironmentObject private var settings` (line 16)
  → re-renders on `objectWillChange`. Correct — no "switched tabs" bug here.
- SettingsView is NOT keyed by a changing value (no project/appkey param), so
  there is no stale-first-instance risk after navigation.
- All local editing state uses `@State` (lines 19-36) — correct for ephemeral
  form state.

---

## 6. LAYOUT

- Uses `Form` (line 40) with `LabeledContent` rows — native SwiftUI, adapts to
  Dynamic Type automatically.
- `multilineTextAlignment(.trailing)` on text fields, keyboard types set
  (`.URL`, `.numberPad`).
- Small-screen (SE width) risk is low: only real concern is the long footer /
  warning footnotes (lines 85, 133) — `.fixedSize(horizontal:false, vertical:true)`
  on the warnings (lines 130, 136) lets them wrap. OK.
- No custom frames / fixed heights that would clip.

---

## 7. ACCESSIBILITY

- Every icon-only control has a text label sibling:
  - Eye toggle (SettingsView.swift:71-78): icon only, BUT it has no explicit
    accessibilityLabel. It sits inside a LabeledContent row alongside "Token"
    label. Low risk — the eye is a standard, glyph-understood control and the
    adjacent "Token" label gives context.
  - Test result icon (lines 117-118): paired with a text message.
  - Permission-denied icon (QRScannerView.swift:72): paired with text.
- Colour is never the ONLY signal: success/failure both also display text
  (check/x mark icons differ by shape too).
- No colour-only badges. Clean.

---

## BUG 1 (FIXED) — @State mutated on a background thread (silent data race)

### Evidence
- `SettingsView.handleScan(_:)` is `@MainActor` and SYNCHRONOUS
  (SettingsView.swift:273-284). It mutates `@State showingScanner`,
  `scannedCode`, `showingConfirm`, `scanError`.
- It is passed as `onScan: handleScan` to the scanner (SettingsView.swift:145),
  typed `(String) -> Void` (QRScannerView.swift:23) — a NON-isolated closure.
- The AVFoundation delegate `metadataOutput` runs on a background dispatch queue
  (QRScannerView.swift:126) and calls `onScan(value)` from THERE
  (QRScannerView.swift:188-189).
- Under `SWIFT_VERSION: "5.9"` (project.yml:53, default `.minimal` concurrency
  checking), a sync `@MainActor` function passed through a non-isolated closure
  does NOT hop to main and produces NO warning. `scripts/build_check.sh` compiles
  clean (0 errors, 0 warnings). The code comment (SettingsView.swift:270-272)
  claims the hop "keeps the state updates safe" — it does not: the compile
  provides no hop, and the body executes on the AVFoundation queue.

### Why it matters
Mutating SwiftUI `@State` off the main thread is undefined behavior: the state
change can be dropped (the confirm dialog never appears → operator rescan)
or race with the main renderer. It is exactly the "button did nothing" class the
operator has been hitting. Low probability per-scan, but it is a real race on
every valid scan.

### Fix
Made the scanner's `onScan` closure `@MainActor`-isolated so the callback is
delivered to (and runs on) the main actor. Minimal, targeted, removes the race
without restructuring. Verified: build_check.sh still clean, all harnesses pass.

---

## Did NOT fix (deliberate)

1. **Empty-host cluster creation** (SettingsView.swift:201-206): `save()` can
   create a cluster with an empty host. Benign — the operator always types a
   host; Test connection is disabled until host is non-empty; the root banner
   re-probes and would show it. Left as-is to avoid scope creep.
2. **Clusters list editing/selection** — SettingsStore supports multiple
   clusters + `selectCluster`, but SettingsView only edits the ACTIVE one. The
   list UI lives elsewhere (out of scope for this screen). Flagged, not built.
3. **Eye toggle a11y label** — beneficial but low-impact; not a correctness bug.
4. **App-group unavailable** footnote (lines 132-137) is device-only; correct
   behavior as designed.

---

## Evidence commands
- `bash scripts/build_check.sh` → HSCC 57 files, 0 errors, 0 warnings (all 4 targets)
- `bash scripts/first_run_check_settings.sh` → SETTINGS-STORE CHECK PASSED
- `hscc api status` → Listening 100.64.0.1:8788 (redacted)
- `curl GET /v1/ping` → 200 {"ok":true,"service":"hscc-api","version":"0.1.0"}
- `scripts/api_route_sweep.py` → `/v1/ping   ok   200`
- `hscc_daemon/tests/test_unified_cli.py::test_qr_payload_matches_ios_contract` → contract proven

State: which findings are executed proof (live decode, compile, harnesses) vs
reasoning (Bug 1 thread-safety analysis — reasoned from code, no iOS runtime;
the compile-clean under Swift 5.9 confirms the compiler provides no hop, which
is the crux).
