import AppIntents

/// B5 — spoken cluster status, hands-free (the in-car path).
///
/// Calls the existing read for `/v1/cluster/status` and speaks the API's own
/// `speak` one-liner (e.g. "4 hosts up. 2 workloads running, 2 idle.") as the
/// dialog result. It does NOT re-derive prose from raw JSON on-device — the
/// server already produced a short, plain-language summary precisely for this.
///
/// Honesty rules (B5): every spoken number comes from an actual API response;
/// nothing is fabricated. If the app isn't configured or the request fails,
/// Siri gets a clear spoken message — a crash or silent no-op is never ok.
struct ClusterStatusIntent: AppIntent {
    static let title: LocalizedStringResource = "Cluster status"
    static let description = IntentDescription(
        "Reads the current cluster status aloud — hosts and running workloads.")
    static let openAppWhenRun: Bool = false

    static var parameterSummary: some ParameterSummary {
        Summary("Get cluster status")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        guard let client = IntentClient.make() else {
            // Not configured: fail with a clear spoken message, not a crash.
            return .result(dialog: IntentDialog(IntentSettingsMessage.notConfigured))
        }
        do {
            let status = try await client.clusterStatus()
            // Speak the server-derived one-liner verbatim.
            return .result(dialog: IntentDialog(status.speak))
        } catch {
            // Honest failure speech — never a fabricated number.
            let message = (error as? HSCCError)?.localizedDescription
                ?? "Couldn't get the cluster status."
            return .result(dialog: IntentDialog(message))
        }
    }
}
