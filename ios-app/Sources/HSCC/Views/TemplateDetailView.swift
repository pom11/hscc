import SwiftUI

/// The reload state after a successful apply — the fleet is restarting.
enum TemplateReloadPhase {
    /// No apply has been issued in this present view session.
    case idle
    /// Apply issued; the fleet is reloading / serving units are restarting.
    case reloading
    /// Apply confirmed and the fleet came back healthy.
    case applied
    /// Apply issued, but the reload check failed or timed out.
    case degraded(String)

    /// Whether the reload has resolved to a terminal state (applied or
    /// degraded) — shown as a completion banner on top of the normal content.
    var isReloadComplete: Bool {
        if case .applied = self { return true }
        if case .degraded = self { return true }
        return false
    }

    /// Whether the fleet is currently reloading after an apply.
    var isReloading: Bool {
        if case .reloading = self { return true }
        return false
    }
}

/// A selected template's detail — shape, read-only preview, and confirm-gated
/// apply with post-apply reload polling.
///
/// The operator sees the template's SHAPE (`TemplateTopologyView`), then the
/// dry-run preview of exactly what applying it would change
/// (`/v1/template/preview/{name}`), BEFORE any apply. Apply never fires from a
/// single tap: the confirmation sheet states the real consequence in plain
/// words (stops + restarts serving units, several minutes, fleet cannot serve)
/// and offers `force_recreate` as a clearly-explained option. After apply the
/// view shows the fleet reloading and polls `/v1/template/status` + `/v1/verify`
/// rather than claiming instant success.
struct TemplateDetailView: View {
    let client: HSCCClient
    let template: ClusterTemplate
    /// Called after the fleet reload check completes, so the parent's applied
    /// card can refresh.
    let onApplied: () async -> Void

    @Environment(\.dismiss) private var dismiss

    @State private var preview = LoadState<TemplatePreviewResponse>.idle
    @State private var showApplyConfirm = false
    @State private var forceRecreate = false
    @State private var phase: TemplateReloadPhase = .idle
    @State private var reloadStatus: TemplateStatusResponse?
    @State private var reloadVerify: VerifyResponse?
    @State private var pollTask: Task<Void, Never>?

    var body: some View {
        NavigationStack {
            ScrollView {
                if phase.isReloading {
                    reloadingSection
                } else {
                    VStack(alignment: .leading, spacing: 16) {
                        if phase.isReloadComplete {
                            reloadCompletionBanner
                        }
                        shapeSection
                        previewSection
                        applySection
                    }
                    .padding()
                }
            }
            .navigationTitle(template.name)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done") { dismiss() }
                }
            }
            .task {
                if preview.value == nil, !preview.isLoading {
                    await loadPreview()
                }
            }
            .refreshable {
                // The error state says "Pull to retry" — make that gesture real.
                // Only the read-only preview is re-fetched, never a re-apply.
                await loadPreview()
            }
            .onDisappear { pollTask?.cancel() }
        }
        .sheet(isPresented: $showApplyConfirm) {
            ApplyConfirmSheet(
                templateName: template.name,
                forceRecreate: $forceRecreate,
                onCancel: { showApplyConfirm = false },
                onApply: {
                    showApplyConfirm = false
                    Task { await apply() }
                }
            )
        }
    }

    // MARK: - Load preview

    private func loadPreview() async {
        preview = .loading
        do { preview = .loaded(try await client.templatePreview(name: template.name)) }
        catch { preview = .failed(errorMessage(for: error)) }
    }

    private func errorMessage(for error: Error) -> String {
        operatorErrorMessage(error)
    }

    // MARK: - Apply (confirm-gated; then poll the reload)

    /// Fired ONLY from the confirmation sheet's Apply button.
    private func apply() async {
        // The fleet is now restarting — show that immediately.
        phase = .reloading
        reloadStatus = nil
        reloadVerify = nil

        // Store the task so `.onDisappear` can cancel the poll if the operator
        // leaves the detail screen mid-reload.
        pollTask = Task { @MainActor in
            var outcome: TemplateReloadPhase = .reloading
            do {
                // POST /v1/template/apply with confirm:true (the client always
                // sends confirm). force_recreate is passed through as a clearly
                // explained option, not a bare toggle.
                _ = try await client.applyTemplate(name: template.name,
                                                   forceRecreate: forceRecreate)
                // Apply issued. Poll until the fleet comes back healthy.
                outcome = await pollReload()
            } catch {
                // The apply itself failed (blocked / partial / unreachable). The
                // fleet may still be transitioning — surface the real error.
                outcome = .degraded(errorMessage(for: error))
            }

            phase = outcome
            pollTask = nil
            await onApplied()
        }
        await pollTask?.value
    }

    /// Poll `/v1/template/status` + `/v1/verify` until the applied template
    /// matches AND the fleet is healthy, or a timeout elapses (~9 min, the
    /// real restart budget). Returns the terminal phase.
    private func pollReload() async -> TemplateReloadPhase {
        let deadline = Date().addingTimeInterval(9 * 60)
        while Date() < deadline, !Task.isCancelled {
            do {
                async let s = client.templateStatus()
                async let v = client.verify()
                let (statusResp, verifyResp) = try await (s, v)
                reloadStatus = statusResp
                reloadVerify = verifyResp

                let appliedNow = statusResp.applied?.template ?? ""
                let isApplied = appliedNow == template.name
                let healthy = verifyResp.ok
                if isApplied && healthy {
                    return .applied
                }
                // Not yet — the fleet is still reloading. Keep polling.
            } catch {
                // A transient read failure during reload: keep polling; the
                // fleet may not be accepting reads yet.
            }
            try? await Task.sleep(nanoseconds: 5_000_000_000)  // 5s
        }
        return .degraded("The fleet hasn't confirmed it's back up within 9 minutes. It may still be loading — check the Cluster tab.")
    }

    // MARK: - Shape

    @ViewBuilder
    private var shapeSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Layout", systemImage: "square.grid.2x2")
                .font(.headline)
            TemplateTopologyView(template: template)
            if let desc = template.description, !desc.isEmpty {
                Text(desc)
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: - Preview (read-only, BEFORE apply)

    @ViewBuilder
    private var previewSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("What applying it changes", systemImage: "doc.text.magnifyingglass")
                .font(.headline)
            switch preview {
            case .loading:
                ProgressView()
            case .failed(let message):
                VStack(alignment: .leading, spacing: 8) {
                    errorLabel(message)
                    Text("Couldn't load the preview. Pull to retry, or try again later.")
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            case .loaded(let state):
                previewContent(state)
            default:
                EmptyView()
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Theme.Semantic.surfaceRaised)
        )
    }

    @ViewBuilder
    private func previewContent(_ state: TemplatePreviewResponse) -> some View {
        let changes = state.changes ?? []
        let routing = state.routing ?? []

        if changes.isEmpty && routing.isEmpty {
            // Minimal { speak } body or empty preview → say what happened and
            // what to do next. Applying still works; the server just has no
            // dry-run detail for this template yet.
            Label(state.speak, systemImage: "info.circle")
                .font(.subheadline)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            Text("No detailed preview is available for this template yet. You can still apply it — the cluster will reconfigure to this layout.")
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        } else {
            if !changes.isEmpty {
                Text("Config changes")
                    .font(.subheadline.weight(.semibold))
                ForEach(changes) { change in
                    changeRow(change)
                }
            }
            if !routing.isEmpty {
                if !changes.isEmpty { Divider().padding(.vertical, 4) }
                Text("Workload routing")
                    .font(.subheadline.weight(.semibold))
                ForEach(routing) { route in
                    routingRow(route)
                }
            }
        }
    }

    private func changeRow(_ change: TemplateChange) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                Text(change.action?.uppercased() ?? "CHANGE")
                    .font(.caption2.weight(.bold))
                    .foregroundColor(changeActionColor(change.action))
                Text(change.file ?? "")
                    .font(.hsccMono(13, weight: .semibold))
                    .foregroundColor(Theme.Semantic.onSurface)
            }
            if let summary = change.summary, !summary.isEmpty {
                Text(summary)
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
            if let details = change.details, !details.isEmpty {
                ForEach(details, id: \.self) { line in
                    Text(line)
                        .font(.caption2)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        .padding(.leading, 8)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }

    private func routingRow(_ route: TemplateRouting) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                Text(route.consumer ?? "")
                    .font(.caption.weight(.semibold))
                    .foregroundColor(Theme.Semantic.onSurface)
                Text("→")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                Text(route.target ?? "")
                    .font(.hsccMono(13))
                    .foregroundColor(Theme.Semantic.ok)
            }
            if let model = route.model, !model.isEmpty {
                Text("model: \(model)")
                    .font(.caption2)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }

    private func changeActionColor(_ action: String?) -> Color {
        switch action?.lowercased() {
        case "provision", "create": return Theme.Semantic.ok
        case "write", "update": return Theme.Semantic.warn
        case "delete", "remove": return Theme.Semantic.bad
        default: return Theme.Semantic.neutral
        }
    }

    // MARK: - Apply

    @ViewBuilder
    private var applySection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Apply", systemImage: "arrow.down.circle")
                .font(.headline)
            Text("Applying this template stops and restarts serving units and takes several minutes, during which the fleet cannot serve.")
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            Button {
                // A single tap ONLY arms the confirmation sheet — no request is
                // sent here. The real apply fires only from the sheet's Apply.
                showApplyConfirm = true
            } label: {
                Label("Apply \(template.name)", systemImage: "arrow.down.circle.fill")
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 6)
            }
            .buttonStyle(.borderedProminent)
            .tint(Theme.Semantic.ok)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Theme.Semantic.surfaceRaised)
        )
    }

    // MARK: - Reloading (post-apply)

    /// Shown in place while the fleet is reloading after a successful apply.
    private var reloadingSection: some View {
        VStack(spacing: 16) {
            ProgressView()
                .controlSize(.large)
                .padding(.top, 40)
            Text("Applying \(template.name)…")
                .font(.headline)
            Text("The fleet is reloading. Serving units are stopping and restarting — this takes several minutes, during which the cluster cannot serve.")
                .font(.subheadline)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                .multilineTextAlignment(.center)
            if let status = reloadStatus, let name = status.applied?.template, !name.isEmpty {
                Label("\(name) applied", systemImage: "checkmark.circle")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.ok)
            }
            if let verify = reloadVerify {
                Label(verify.speak, systemImage: verify.ok ? "checkmark.seal" : "timer")
                    .font(.caption)
                    .foregroundColor(verify.ok ? Theme.Semantic.ok : Theme.Semantic.warn)
            }
            Text("Stay on this screen — it refreshes itself as the fleet returns.")
                .font(.caption2)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 20)
        .padding(.horizontal)
    }

    /// The banner shown when the reload completed (applied or degraded).
    @ViewBuilder
    private var reloadCompletionBanner: some View {
        VStack(alignment: .leading, spacing: 6) {
            switch phase {
            case .applied:
                Label("\(template.name) applied — the fleet is back up.",
                      systemImage: "checkmark.seal.fill")
                    .font(.subheadline.weight(.semibold))
                    .foregroundColor(Theme.Semantic.ok)
            case .degraded(let message):
                Label(message, systemImage: "exclamationmark.triangle.fill")
                    .font(.subheadline)
                    .foregroundColor(Theme.Semantic.bad)
            default:
                EmptyView()
            }
            Text("The Cluster tab reflects the fleet's live state.")
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Theme.Semantic.surfaceRaised)
        )
    }

    // MARK: - Building blocks

    private func errorLabel(_ message: String) -> some View { HSErrorLabel(message: message) }
}

/// The confirm-gated apply sheet. This is where the operator reads the REAL
/// consequence and takes the deliberate second step. `force_recreate` is
/// offered as an explained option, not a bare toggle.
private struct ApplyConfirmSheet: View {
    let templateName: String
    @Binding var forceRecreate: Bool
    let onCancel: () -> Void
    let onApply: () -> Void

    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 8) {
                    Label("Apply \(templateName)?", systemImage: "arrow.down.circle.fill")
                        .font(.headline)
                    Text("Applying this template **stops and restarts every serving unit** and takes **several minutes**. During that time the fleet **cannot serve requests**.")
                        .font(.subheadline)
                        .foregroundColor(Theme.Semantic.onSurface)
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                // force_recreate — explained, not a bare toggle.
                Toggle(isOn: $forceRecreate) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Force recreate")
                            .font(.body)
                        Text("Re-applies changed serve flags. Use when only settings changed and the current layout is already what you want.")
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                }
                .toggleStyle(.switch)

                Spacer()
            }
            .padding()
            .navigationTitle("Confirm Apply")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss(); onCancel() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button {
                        dismiss()
                        onApply()
                    } label: {
                        Text("Apply \(templateName)")
                            .bold()
                    }
                    .tint(Theme.Semantic.ok)
                }
            }
        }
    }
}
