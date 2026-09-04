import SwiftUI

/// Read-only per-file diff for a card (GET /v1/review/{card_id}/diff).
///
/// Renders each changed file as a tappable header (path + status badge) that
/// expands into a monospaced block of `+`/`-`/context lines tinted green/red/
/// neutral. Files are fetched in pages via `cardDiff(offset:limit:)`; a small
/// trailing loader appears once the current page is exhausted and appends the
/// next page, so a large (2000+-line) diff renders incrementally instead of
/// blocking the main thread. `truncated` (server line cap) and "more files on
/// the branch" are surfaced via the server's `speak` line and a footer, so a
/// huge diff degrades rather than blows up. Read-only: never mutates.
struct DiffDetailView: View {
    @EnvironmentObject private var settings: SettingsStore
    let cardID: String

    @State private var files: [DiffFile] = []            // accumulated across pages
    @State private var totalFileCount = 0                // server's file_count
    @State private var nextOffset = 0                    // next file index to fetch
    @State private var hasMore = false
    @State private var loadError: HSCCError?
    @State private var isLoading = false
    @State private var expandedPaths: Set<String> = []
    @State private var speak = ""
    @State private var truncated = false

    /// Page size for lazy file loading. Matches the endpoint default (20) and
    /// cap-friendly size (max 200); the view keeps each page small so a long
    /// diff streams in across many incremental fetches.
    private let pageLimit = 20

    var body: some View {
        Group {
            if let loadError {
                HSError(loadErrorTitle, message: loadError.localizedDescription) {
                    Task { await loadFirstPage(clear: true) }
                }
            } else if files.isEmpty && !isLoading {
                // Empty state: no changes (clean diff) — not an error.
                HSEmpty("No file changes",
                        message: "Nothing changed on this card's branch.",
                        systemImage: "doc.text")
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        headerBlock
                        ForEach(files) { file in
                            fileSection(file)
                        }
                        if isLoading && files.isEmpty == false {
                            ProgressView("Loading more…")
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 12)
                        }
                        footer
                        // Load the next page lazily when this sentinel scrolls
                        // into view — incremental, off the main thread.
                        if hasMore {
                            Color.clear
                                .frame(height: 1)
                                .onAppear { Task { await loadNextPage() } }
                        }
                    }
                }
            }
        }
        .navigationTitle("Diff — \(cardID)")
        .navigationBarTitleDisplayMode(.inline)
        .task { await loadFirstPage() }
    }

    /// One line of context about the diff as a whole (project · branch · files).
    private var headerBlock: some View {
        VStack(alignment: .leading, spacing: 4) {
            if !speak.isEmpty {
                Text(speak)
                    .font(.footnote)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
            if truncated {
                Label("Diff truncated by server line cap — some hunks are not shown.",
                      systemImage: "ellipsis.circle")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.warn)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    /// A collapsible per-file block: header (path + status) toggles the hunk body.
    @ViewBuilder
    private func fileSection(_ file: DiffFile) -> some View {
        let path = file.path ?? "(unknown path)"
        let expanded = expandedPaths.contains(path)
        VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(.easeInOut(duration: 0.15)) {
                    if expanded {
                        expandedPaths.remove(path)
                    } else {
                        expandedPaths.insert(path)
                    }
                }
            } label: {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(.caption2)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    Text(path)
                        .font(.hsccMono(13, weight: .medium))
                        .foregroundColor(Theme.Semantic.onSurface)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer()
                    statusBadge(file)
                }
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)

            if expanded {
                Divider()
                hunkBody(file)
            }
        }
        .background(Theme.Semantic.surfaceRaised)
        .padding(.vertical, 2)
    }

    /// The `A`/`M`/`D` chip with the file's +/- line counts.
    @ViewBuilder
    private func statusBadge(_ file: DiffFile) -> some View {
        let text = file.statusBadge
        let color: Color =
            file.status == "D" ? Theme.Semantic.bad
            : file.status == "A" ? Theme.Semantic.ok
            : file.status == "M" ? Theme.Semantic.neutral
            : Theme.Semantic.onSurfaceMuted
        Text(text)
            .font(.hsccMono(11, weight: .semibold))
            .foregroundColor(Theme.Semantic.onSurface)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color.opacity(0.18), in: Capsule())
    }

    /// The monospaced, tinted line block for a file's hunks (read-only).
    private func hunkBody(_ file: DiffFile) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(file.hunks ?? []) { hunk in
                if let header = hunk.header, !header.isEmpty {
                    Text(header)
                        .font(.hsccMono(11, weight: .semibold))
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        .padding(.vertical, 2)
                }
                ForEach(hunk.lines ?? []) { line in
                    diffLineRow(line)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 8)
        .padding(.vertical, 2)
        .background(
            // Very light tint so added/deleted blocks read as bands, not noise.
            Color.clear
        )
    }

    /// One diff line, tinted by its type. `+`→ok(green), `-`→bad(red), else the
    /// neutral body colour. The trailing marker is preserved by `renderedText`.
    @ViewBuilder
    private func diffLineRow(_ line: DiffLine) -> some View {
        Text(line.renderedText)
            .font(.hsccMono(12))
            .foregroundColor(lineColor(line))
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(lineBackground(line))
            .padding(.vertical, 0.5)
    }

    private func lineColor(_ line: DiffLine) -> Color {
        if line.isAddition { return Theme.Semantic.ok }
        if line.isDeletion { return Theme.Semantic.bad }
        return Theme.Semantic.onSurface
    }

    /// A full-row translucent fill makes added/deleted bands legible even when
    /// a line is long enough to wrap — keeps the +/- meaning without issuing a
    /// syntax highlighter.
    @ViewBuilder
    private func lineBackground(_ line: DiffLine) -> some View {
        if line.isAddition {
            Theme.Semantic.ok.opacity(0.08)
        } else if line.isDeletion {
            Theme.Semantic.bad.opacity(0.08)
        } else {
            Color.clear
        }
    }

    /// Footer note when the diff is truncated / there is more on the branch.
    @ViewBuilder
    private var footer: some View {
        if truncated || hasMore {
            HStack(spacing: 4) {
                Image(systemName: "info.circle")
                Text(truncated
                     ? "Some files were truncated by the server line cap."
                     : "Showing \(files.count) of \(totalFileCount) file\(totalFileCount == 1 ? "" : "s").")
            }
            .font(.footnote)
            .foregroundColor(Theme.Semantic.onSurfaceMuted)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(12)
        }
    }

    // MARK: - Loading

    private func loadFirstPage(clear: Bool = false) async {
        guard !isLoading else { return }
        if clear {
            files = []
            totalFileCount = 0
            hasMore = false
            nextOffset = 0
            loadError = nil
        }
        guard let client = makeClient() else {
            loadError = .invalidURL
            return
        }
        isLoading = true
        defer { isLoading = false }
        do {
            let page = try await client.cardDiff(cardID, offset: nil, limit: pageLimit)
            apply(page, replace: true)
            loadError = nil
        } catch {
            loadError = (error as? HSCCError) ?? .transport(underlying: nil)
        }
    }

    private func loadNextPage() async {
        guard !isLoading, hasMore else { return }
        guard let client = makeClient() else {
            loadError = .invalidURL
            return
        }
        isLoading = true
        defer { isLoading = false }
        do {
            let page = try await client.cardDiff(cardID, offset: nextOffset, limit: pageLimit)
            apply(page, replace: false)
        } catch {
            // A failed page fetch shouldn't blank the whole screen — keep what
            // we have and stop the loader; the footer/error still explain.
            hasMore = false
            loadError = (error as? HSCCError) ?? .transport(underlying: nil)
        }
    }

    /// Fold one page into state. `replace` is true for the first page (clears
    /// any prior file list); false appends so pagination accumulates.
    private func apply(_ page: DiffDetailResponse, replace: Bool) {
        let newFiles = page.files ?? []
        if replace { files = newFiles } else { files.append(contentsOf: newFiles) }
        totalFileCount = page.file_count ?? files.count
        speak = page.speak
        truncated = page.truncated ?? false
        let servedBase = page.offset ?? 0
        let servedCount = newFiles.count
        nextOffset = servedBase + servedCount
        hasMore = nextOffset < totalFileCount
    }

    /// A configured client from the current settings, or nil when the operator
    /// hasn't set a usable host/port/token yet. Mirrors CardDetailView.
    private func makeClient() -> HSCCClient? {
        guard settings.isConfigured,
              let token = settings.token,
              let port = Int(settings.port) else { return nil }
        return HSCCClient(host: settings.host, port: port, token: token)
    }

    private var loadErrorTitle: String {
        switch loadError {
        case .api(_, _, 404):
            return "No reviewable diff"
        default:
            return "Couldn't load diff"
        }
    }
}
