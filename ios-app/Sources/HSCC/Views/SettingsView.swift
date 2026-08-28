import SwiftUI

/// Settings screen: enter + persist host, port, and token.
///
/// Persistence:
///   * host  — UserDefaults (via `SettingsStore`)
///   * port  — UserDefaults (via `SettingsStore`)
///   * token — Keychain ONLY (via `KeychainStore` / `SettingsStore.saveToken`).
///     Never UserDefaults, never a plist, never hardcoded in source.
///
/// Includes a "Test connection" button that calls GET /v1/ping against the
/// current settings and shows a clear success/failure result, and a "Scan QR"
/// button that fills host/port/token from the setup code printed by
/// `hscc api status`/`hscc api start`.
struct SettingsView: View {
    @EnvironmentObject private var settings: SettingsStore

    // Local editing state seeded from the store.
    @State private var hostField: String = ""
    @State private var portField: String = ""
    @State private var tokenField: String = ""
    @State private var showingToken = false

    // Test-connection state.
    @State private var testResult: String?
    @State private var testIsSuccess: Bool?
    @State private var isTesting = false

    // QR-scan flow state.
    @State private var showingScanner = false
    /// A valid scanned code awaiting the operator's confirm before it is
    /// applied. Host/port shown for confirmation; token never shown in full.
    @State private var scannedCode: SetupQRCode?
    /// A rejected scan (bad shape / wrong version) — surfaced as an error alert.
    @State private var scanError: String?
    @State private var showingConfirm = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    LabeledContent {
                        TextField("e.g. dgx-tailscale (hostname or IP)", text: $hostField)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .multilineTextAlignment(.trailing)
                            .keyboardType(.URL)
                    } label: {
                        Text("Host")
                    }

                    LabeledContent {
                        TextField("8788", text: $portField)
                            .keyboardType(.numberPad)
                            .multilineTextAlignment(.trailing)
                    } label: {
                        Text("Port")
                    }

                    LabeledContent {
                        HStack {
                            if showingToken {
                                TextField("Bearer token", text: $tokenField)
                                    .textInputAutocapitalization(.never)
                                    .autocorrectionDisabled()
                            } else {
                                SecureField("Bearer token", text: $tokenField)
                                    .textInputAutocapitalization(.never)
                                    .autocorrectionDisabled()
                            }
                            Button {
                                showingToken.toggle()
                            } label: {
                                Image(systemName: showingToken ? "eye.slash" : "eye")
                                    .foregroundColor(.secondary)
                            }
                            .buttonStyle(.borderless)
                        }
                    } label: {
                        Text("Token")
                    }
                } header: {
                    Text("Connection")
                } footer: {
                    Text("Plain HTTP over Tailscale is fine — Tailscale is the encrypted transport. The token is stored only in the iOS Keychain.")
                }

                Section {
                    Button {
                        showingScanner = true
                    } label: {
                        Label("Scan QR", systemImage: "qrcode.viewfinder")
                    }
                } footer: {
                    Text("Scan the setup code from `hscc api status` to fill host, port, and token instead of typing them.")
                }

                Section {
                    Button {
                        Task { await testConnection() }
                    } label: {
                        if isTesting {
                            HStack {
                                ProgressView()
                                Text("Testing…")
                            }
                        } else {
                            Text("Test connection")
                        }
                    }
                    .disabled(!isReadyToTest || isTesting)

                    if let testResult {
                        Label {
                            Text(testResult)
                        } icon: {
                            Image(systemName: testIsSuccess == true ? "checkmark.circle.fill" : "xmark.circle.fill")
                        }
                        .foregroundColor(testIsSuccess == true ? .green : .red)
                    }
                }

                Section {
                    Button("Save", action: save)
                        .disabled(!hasEdits)
                }
            }
            .navigationTitle("Settings")
            .onAppear(perform: loadFromStore)
            // MARK: QR-scan flow — camera sheet → confirm → apply → test.
            .sheet(isPresented: $showingScanner) {
                QRScannerView(
                    onScan: handleScan,
                    onCancel: { showingScanner = false }
                )
            }
            // Confirm-gated apply: like every mutating surface, a single tap
            // never mutates. After a valid scan we show host+port (token
            // redacted) and the operator must explicitly tap "Apply".
            .confirmationDialog(
                "Apply this connection?",
                isPresented: $showingConfirm,
                titleVisibility: .visible
            ) {
                Button("Apply") { Task { await applyScanned() } }
                Button("Cancel", role: .cancel) {}
            } message: {
                if let code = scannedCode {
                    Text("Connect to \(code.host):\(code.port) and set a new token (from the scanned code)?")
                }
            }
            .alert("Scan rejected", isPresented: Binding(
                get: { scanError != nil },
                set: { if !$0 { scanError = nil } }
            )) {
                Button("OK") { scanError = nil }
            } message: {
                Text(scanError ?? "")
            }
        }
    }

    // MARK: - State helpers

    private var isReadyToTest: Bool {
        !hostField.trimmingCharacters(in: .whitespaces).isEmpty
            && !tokenField.trimmingCharacters(in: .whitespaces).isEmpty
            && Int(portField) != nil
    }

    private var hasEdits: Bool {
        hostField != settings.host
            || portField != settings.port
            || tokenField != (settings.token ?? "")
    }

    private func loadFromStore() {
        hostField = settings.host
        portField = settings.port
        tokenField = settings.token ?? ""
    }

    // MARK: - Actions

    private func save() {
        settings.host = hostField.trimmingCharacters(in: .whitespaces)
        settings.port = portField.trimmingCharacters(in: .whitespaces)
        settings.saveToken(tokenField.isEmpty ? nil : tokenField)
        // The root view re-probes on isConfigured change.
    }

    private func testConnection() async {
        isTesting = true
        testResult = nil
        testIsSuccess = nil
        defer { isTesting = false }

        // Persist first so the test uses the same settings the app will use.
        save()

        guard let token = settings.token,
              let port = Int(settings.port),
              !settings.host.isEmpty else {
            testResult = "Set a host, port, and token first."
            testIsSuccess = false
            return
        }

        let client = HSCCClient(host: settings.host, port: port, token: token)
        do {
            let pong = try await client.ping()
            if pong.ok {
                if let service = pong.service, let version = pong.version {
                    testResult = "Connected to \(service) v\(version)."
                } else {
                    testResult = "Connected — HSCC API is up."
                }
                testIsSuccess = true
            } else {
                testResult = "Reached the API, but it reported not-ok."
                testIsSuccess = false
            }
        } catch {
            testResult = (error as? HSCCError)?.localizedDescription
                ?? "Connection failed."
            testIsSuccess = false
        }
    }

    // MARK: - QR scan

    /// Handle a raw scanned payload: decode + validate it, then either move to
    /// the confirm step (valid) or reject it (never partially apply a bad
    /// payload). Dismisses the camera sheet in both cases; a bad scan surfaces
    /// a clear error and the operator can re-open the scanner.
    ///
    /// Runs on the main actor because it mutates `@State`; the AVFoundation
    /// delegate delivers `onScan` on a background queue, so the hop back here
    /// is what keeps the state updates safe.
    @MainActor
    private func handleScan(_ text: String) {
        showingScanner = false
        do {
            let code = try SetupQRCode.decode(text)
            scannedCode = code
            showingConfirm = true
        } catch {
            scanError = (error as? SetupQRCodeError)?.localizedDescription
                ?? "That isn't a valid HSCC setup code."
        }
    }

    /// Apply a confirmed scan: write host/port to the store, the token to the
    /// Keychain (never UserDefaults, never logged, never shown in full on
    /// screen), then run the connection test so the operator immediately sees
    /// whether the pairing worked.
    @MainActor
    private func applyScanned() async {
        guard let code = scannedCode else { return }
        // Bring the local edit fields in line so `save()` keeps them consistent
        // with the applied values (otherwise a stale field would win later).
        hostField = code.host.trimmingCharacters(in: .whitespaces)
        portField = String(code.port)
        tokenField = code.token
        save()
        await testConnection()
    }
}
