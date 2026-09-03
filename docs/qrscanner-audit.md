# QRScannerView audit — t_8320013b

Full audit of `ios-app/Sources/HSCC/Views/QRScannerView.swift` — the camera QR
pairing scanner. Addresses redacted to `100.64.0.x` / `100.115.x.x` placeholders;
live values marked.

Status: IN PROGRESS (skeleton).

## What this screen is
Presentational modal camera scanner launched from Settings ("Scan QR",
SettingsView.swift:88-96 via `.sheet(isPresented: $showingScanner)` at 143-148).
The view itself fetches NO endpoint — it reads a QR code from the camera and
hands the raw payload string to the presenter via `onScan(String)`
(QRScannerView.swift:30). The real "data" is the setup-code contract:
`{"v":1,"host":"<host>","port":<int>,"token":"<token>"}` (SetupQRCode.swift:9).

Flow: scan QR → raw string → `SettingsView.handleScan` (decode) → confirm
dialog → `applyScanned` (save + test) → `QRPairing.test` → GET /v1/ping →
outcome classified into QRPairingOutcome.

Prior audit (settingsview-audit.md, t_1223ea1a) already fixed ONE bug in this
path: the `@MainActor` data race (onScan delivered from AVFoundation queue).
That fix is confirmed PRESENT in the current code (see below).

## Findings so far
(none fully written yet — filling in)
