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
/// current settings and shows a clear success/failure result.
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
}
