import SwiftUI

/// Main Cluster tab (B2). Shows the cluster at a glance: overall health
/// (hosts up / workloads running / idle), the running workload list, and the
/// registered hosts — all read from the HSCC API over Tailscale.
///
/// Networking lives in `HSCCClient`, not in this view. Each section carries its
/// own `LoadState` so loading / error / empty are handled independently and
/// never dump a raw error — errors surface `HSCCError.localizedDescription`
/// (e.g. "Can't reach the cluster — is Tailscale connected?").
///
/// The `speak` one-liner from each read is shown as the summary line at the
/// top of a section — B5 reuses it for spoken summaries.
struct ClusterView: View {
    /// The configured client, or nil when the user hasn't entered host/port/token
    /// yet. Nil shows a "set up in Settings" prompt instead of a spinner.
    let client: HSCCClient?

    @State private var status = LoadState<ClusterStatusResponse>.idle
    @State private var hosts = LoadState<ClusterHostsResponse>.idle
    @State private var info = LoadState<ReadResponse>.idle
    @State private var templateName = ""
    @State private var templateForceRecreate = false

    var body: some View {
        NavigationStack {
            ScrollView {
                if let client {
                    VStack(alignment: .leading, spacing: 16) {
                        statusSection
                        workloadsSection
                        infoSection
                        templateSection
                        hostsSection
                        fleetLink(client: client)
                    }
                    .padding()
                } else {
                    notConfiguredView
                }
            }
            .navigationTitle("Cluster")
            .refreshable { if client != nil { await loadAll() } }
            .task {
                // Load once on first appearance (refreshable handles later reloads).
                if client != nil, status.value == nil, !status.isLoading {
                    await loadAll()
                }
            }
        }
    }

    // MARK: - Not configured

    private var notConfiguredView: some View {
        VStack(spacing: 12) {
            Image(systemName: "bolt.slash")
                .font(.system(size: 44))
                .foregroundColor(.secondary)
            Text("Connect to your cluster")
                .font(.headline)
            Text("Set the host, port, and token in Settings to see cluster status.")
                .font(.subheadline)
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 60)
        .padding(.horizontal)
    }

    // MARK: - Load

    private func loadAll() async {
        guard let client else { return }
        async let statusTask: Void = loadStatus(client)
        async let hostsTask: Void = loadHosts(client)
        async let infoTask: Void = loadInfo(client)
        _ = await (statusTask, hostsTask, infoTask)
    }

    private func loadStatus(_ client: HSCCClient) async {
        status = .loading
        do {
            status = .loaded(try await client.clusterStatus())
        } catch {
            status = .failed(errorMessage(for: error))
        }
    }

    private func loadHosts(_ client: HSCCClient) async {
        hosts = .loading
        do {
            hosts = .loaded(try await client.clusterHosts())
        } catch {
            hosts = .failed(errorMessage(for: error))
        }
    }

    private func loadInfo(_ client: HSCCClient) async {
        info = .loading
        do {
            info = .loaded(try await client.clusterInfo())
        } catch {
            info = .failed(errorMessage(for: error))
        }
    }

    private func errorMessage(for error: Error) -> String {
        (error as? HSCCError)?.localizedDescription ?? "Something went wrong."
    }

    // MARK: - Status (overall health at a glance)

    @ViewBuilder
    private var statusSection: some View {
        switch status {
        case .loading:
            sectionCard(title: "Health", systemImage: "heart.fill") { ProgressView() }
        case .failed(let message):
            sectionCard(title: "Health", systemImage: "heart.fill") {
                errorLabel(message)
            }
        case .loaded(let state):
            sectionCard(title: "Health", systemImage: "heart.fill") {
                VStack(alignment: .leading, spacing: 10) {
                    Text(state.speak)
                        .font(.subheadline)
                        .italic()
                        .foregroundColor(.secondary)

                    HStack(spacing: 10) {
                        statBadge(value: "\(state.total_hosts)", label: "hosts up",
                                  color: state.total_hosts > 0 ? .green : .secondary)
                        statBadge(value: "\(state.workloads.count)", label: "running",
                                  color: state.workloads.isEmpty ? .secondary : .blue)
                        statBadge(value: "\(state.idle_hosts.count)", label: "idle",
                                  color: state.idle_hosts.isEmpty ? .secondary : .orange)
                    }
                }
            }
        default:
            EmptyView()
        }
    }

    // MARK: - Workloads

    @ViewBuilder
    private var workloadsSection: some View {
        switch status {
        case .loading:
            sectionCard(title: "Workloads", systemImage: "cube") { ProgressView() }
        case .failed(let message):
            sectionCard(title: "Workloads", systemImage: "cube") { errorLabel(message) }
        case .loaded(let state):
            sectionCard(title: "Workloads", systemImage: "cube") {
                if state.workloads.isEmpty {
                    emptyLabel("No workloads running.")
                } else {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(state.workloads) { workload in
                            workloadRow(workload)
                        }
                    }
                }
            }
        default:
            EmptyView()
        }
    }

    private func workloadRow(_ w: ClusterWorkload) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(w.name)
                .font(.body)
            HStack(spacing: 12) {
                if let tp = w.tp, tp != "?" {
                    Label(tp, systemImage: "cpu")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                if let pp = w.pp, pp != "?" {
                    Label(pp, systemImage: "cpu")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                if let cid = w.container_id, cid != "?" {
                    Text("container \(cid)")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
            }
            if let cid = w.container_id, cid != "?" {
                stopButton(for: cid)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// The confirm-gated Stop button for a workload. Never fires without the
    /// user's explicit confirmation, and never claims a failed stop succeeded.
    private func stopButton(for containerID: String) -> some View {
        MutationButton(
            title: "Stop",
            systemImage: "stop.circle",
            destructive: true,
            prompt: "Stop container \\(containerID)?",
            run: {
                let result = try await client!.stopCluster(containerID: containerID)
                return result.message ?? "Stopped container \\(containerID)."
            }
        )
        .font(.caption)
        .buttonStyle(.borderless)
    }

    // MARK: - Hosts

    @ViewBuilder
    private var hostsSection: some View {
        switch hosts {
        case .loading:
            sectionCard(title: "Hosts", systemImage: "server.rack") { ProgressView() }
        case .failed(let message):
            sectionCard(title: "Hosts", systemImage: "server.rack") { errorLabel(message) }
        case .loaded(let state):
            sectionCard(title: "Hosts", systemImage: "server.rack") {
                VStack(alignment: .leading, spacing: 6) {
                    Text(state.speak)
                        .font(.subheadline)
                        .italic()
                        .foregroundColor(.secondary)
                    if state.hosts.isEmpty {
                        emptyLabel("No hosts registered.")
                    } else {
                        ForEach(state.hosts, id: \.self) { host in
                            Label(host, systemImage: "server.rack")
                                .font(.body)
                        }
                    }
                }
            }
        default:
            EmptyView()
        }
    }

    // MARK: - Cluster info

    @ViewBuilder
    private var infoSection: some View {
        switch info {
        case .loading:
            sectionCard(title: "Configuration", systemImage: "slider.horizontal.3") { ProgressView() }
        case .failed(let message):
            sectionCard(title: "Configuration", systemImage: "slider.horizontal.3") { errorLabel(message) }
        case .loaded(let state):
            sectionCard(title: "Configuration", systemImage: "slider.horizontal.3") {
                Text(state.speak)
                    .font(.subheadline)
                    .italic()
                    .foregroundColor(.secondary)
            }
        default:
            EmptyView()
        }
    }

    // MARK: - Template apply (B4, confirm-gated)

    @ViewBuilder
    private var templateSection: some View {
        if let client {
            sectionCard(title: "Template", systemImage: "rectangle.stack.badge.plus") {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Apply an HSCC project template to (re)deploy the fleet.")
                        .font(.subheadline)
                        .foregroundColor(.secondary)

                    TextField("Template name", text: $templateName)
                        .textFieldStyle(.roundedBorder)
                        .autocapitalization(.none)
                        .disableAutocorrection(true)

                    Toggle("Force recreate", isOn: $templateForceRecreate)
                        .font(.subheadline)

                    // The confirm-gated apply. The closure captures the current
                    // name/force at call time — which only happens after the
                    // user confirms. Never fires from a single tap.
                    MutationButton(
                        title: "Apply Template",
                        systemImage: "play.rectangle.on.rectangle",
                        prompt: templatePrompt(),
                        run: {
                            let result = try await client.applyTemplate(
                                name: templateName,
                                forceRecreate: templateForceRecreate
                            )
                            return result.message ?? "Applied template \\(templateName)."
                        }
                    )
                    .disabled(templateName.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
        }
    }

    /// Confirmation wording for the template apply — names the template.
    private func templatePrompt() -> String {
        let name = templateName.trimmingCharacters(in: .whitespaces)
        return name.isEmpty ? "Apply this template?" : "Apply template \\\"\\(name)\\\" and (re)deploy the fleet?"
    }

    // MARK: - Fleet link

    private func fleetLink(client: HSCCClient) -> some View {
        NavigationLink {
            FleetView(client: client)
        } label: {
            HStack(spacing: 8) {
                Image(systemName: "wave.3.right.circle.fill")
                    .font(.title2)
                    .foregroundColor(.blue)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Fleet")
                        .font(.headline)
                    Text("Health, stats, throughput, streams, autoscale")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                Spacer()
                Image(systemName: "chevron.right")
                    .foregroundColor(.secondary)
            }
            .padding()
            .background(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(Color(.secondarySystemBackground))
            )
        }
        .buttonStyle(.plain)
    }

    // MARK: - Shared building blocks

    private func sectionCard<Content: View>(
        title: String,
        systemImage: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(title, systemImage: systemImage)
                .font(.headline)
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Color(.secondarySystemBackground))
        )
    }

    private func statBadge(value: String, label: String, color: Color) -> some View {
        VStack(spacing: 2) {
            Text(value)
                .font(.title3.bold())
                .foregroundColor(color)
            Text(label)
                .font(.caption)
                .foregroundColor(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Color(.tertiarySystemBackground))
        )
    }

    private func errorLabel(_ message: String) -> some View {
        Label(message, systemImage: "exclamationmark.triangle.fill")
            .font(.subheadline)
            .foregroundColor(.red)
    }

    private func emptyLabel(_ text: String) -> some View {
        Label(text, systemImage: "tray")
            .font(.subheadline)
            .foregroundColor(.secondary)
    }
}
