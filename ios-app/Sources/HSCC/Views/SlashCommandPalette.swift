import SwiftUI

/// Pure, testable logic for the slash-command palette — no SwiftUI, so it can
/// be unit-tested from a headless CLI harness (the repo's convention for
/// isolating logic from the view layer).
enum SlashPaletteLogic {
    /// Whether the current "command word" in a draft is a slash command in
    /// progress. The command word is the text after the LAST whitespace (or
    /// the whole draft if there's no whitespace), so the palette opens when the
    /// operator types "/" at the start or after a space — and does NOT open for
    /// things like "http://x" or "a/b" (those aren't "command position").
    static func isCommandWord(_ draft: String) -> Bool {
        lastWord(of: draft).hasPrefix("/")
    }

    /// The filter query to match commands against: everything after the "/" of
    /// the trailing command word ("" when the operator typed just "/", meaning
    /// "show all"). Example: draft "/clu foo" is not a command position ("foo"
    /// is the last word), so that returns "" (irrelevant — the palette won't
    /// show because isCommandWord is false); "/clu" → "clu".
    static func filterQuery(from word: String) -> String {
        word.hasPrefix("/") ? String(word.dropFirst()) : ""
    }

    /// The trailing command word of a draft: the text after the last whitespace
    /// (or the whole draft when there's none).
    static func lastWord(of draft: String) -> String {
        if let r = draft.rangeOfCharacter(from: .whitespacesAndNewlines, options: .backwards) {
            return String(draft[draft.index(after: r.lowerBound)...])
        }
        return draft
    }

    /// Commands matching the query: name `hasPrefix(query)` first, then name
    /// `contains(query)`, name-match ties broken alphabetically. An empty query
    /// returns every command.
    static func filtered(_ commands: [SlashCommand], query: String) -> [SlashCommand] {
        guard !query.isEmpty else {
            return commands.sorted { $0.name < $1.name }
        }
        let q = query.lowercased()
        let prefixed = commands.filter { $0.name.lowercased().hasPrefix(q) }
            .sorted { $0.name < $1.name }
        let contained = commands
            .filter { $0.name.lowercased().contains(q) && !$0.name.lowercased().hasPrefix(q) }
            .sorted { $0.name < $1.name }
        return prefixed + contained
    }

    /// The draft after inserting `command`. Replaces the trailing "/<query>"
    /// word (the part in flight) with "/<name> " — preserving any text that
    /// precedes the command position, and leaving a trailing space so the
    /// operator can keep typing args immediately. A no-op guard: if the draft
    /// is no longer at a command position, return it unchanged.
    static func draft(afterInserting command: SlashCommand, draft: String) -> String {
        guard isCommandWord(draft) else { return draft }
        if let r = draft.rangeOfCharacter(from: .whitespacesAndNewlines, options: .backwards) {
            // Keep everything through the trailing separator (the leading
            // context/args the operator already typed), then the full command
            // + trailing space so they can type args immediately.
            let prefix = draft[..<r.upperBound]
            return String(prefix) + "/" + command.name + " "
        }
        return "/" + command.name + " "
    }
}

/// A reusable slash-command palette for a chat composer.
///
/// Attach it to a composer's `@Binding` draft and an optional HSCCClient:
///
///   SlashCommandPalette(draft: $store.draft, client: client)
///
/// Behaviour:
///   * The command list is fetched ONCE from the server (`GET /v1/commands`),
///     lazily on first appearance — NOT a hardcoded Swift array.
///   * While the draft's trailing word starts with "/", the palette shows the
///     server commands filtered by what the operator typed after the "/".
///   * Selecting a command substitutes `/name ` into the draft (replacing the
///     in-flight "/<query>") and dismisses the palette.
///   * When the command list can't be fetched (unconfigured, offline, server
///     degraded), the palette shows nothing (stays hidden) — it never shows a
///     stale/hardcoded list, and never fakes commands.
struct SlashCommandPalette: View {
    @Binding var draft: String
    /// The configured client, or nil when settings aren't set. nil → the
    /// palette stays hidden (there is nothing server-driven to offer).
    let client: HSCCClient?

    @State private var commands: [SlashCommand] = []
    @State private var loadFailed = false

    private var isCommandPosition: Bool {
        SlashPaletteLogic.isCommandWord(draft)
    }

    private var query: String {
        SlashPaletteLogic.filterQuery(from: SlashPaletteLogic.lastWord(of: draft))
    }

    private var matches: [SlashCommand] {
        SlashPaletteLogic.filtered(commands, query: query)
    }

    var body: some View {
        // Show only at a "/" command position, and only when we have something
        // server-driven to offer. Loading / failure are NOT shown as rows — the
        // palette simply stays hidden and the operator can type the command
        // blind as before (honest: no fabricated list).
        VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
            if isCommandPosition && !commands.isEmpty {
                ForEach(matches.prefix(8)) { command in
                    paletteRow(command)
                }
            }
        }
        .padding(Theme.Spacing.xs.rawValue)
        .background(
            RoundedRectangle(cornerRadius: Theme.Corner.card.rawValue, style: .continuous)
                .fill(Theme.Semantic.surfaceRaised)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Corner.card.rawValue, style: .continuous)
                .stroke(Color.primary.opacity(0.08), lineWidth: 1)
        )
        .frame(maxWidth: .infinity, alignment: .leading)
        .opacity(isCommandPosition && !commands.isEmpty ? 1 : 0)
        .task {
            // Fetch the server command catalog exactly once, lazily, on first
            // appearance; the task is cancelled automatically when the palette
            // (and thus the composer) goes away.
            await load()
        }
    }

    private func paletteRow(_ command: SlashCommand) -> some View {
        Button {
            withAnimation(.easeOut(duration: 0.15)) {
                draft = SlashPaletteLogic.draft(afterInserting: command, draft: draft)
            }
        } label: {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.sm.rawValue) {
                Text("/\(command.name)")
                    .font(.subheadline.weight(.semibold))
                    .foregroundColor(Theme.Semantic.onSurface)
                Text(command.description)
                    .font(.footnote)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    .lineLimit(2)
                Spacer(minLength: 0)
                if command.takesArgs {
                    Text("…args")
                        .font(.caption2)
                        .foregroundColor(Theme.Semantic.neutral)
                }
            }
            .padding(.horizontal, Theme.Spacing.sm.rawValue)
            .padding(.vertical, Theme.Spacing.xs.rawValue)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Insert /\(command.name), \(command.description)")
    }

    /// Fetch the command catalog from the server exactly once, lazily. Run from
    /// `.task` so it starts when the palette first appears and is cancelled if
    /// the view goes away. On failure the palette simply stays hidden (no
    /// fabricated list, no crash).
    func load() async {
        guard commands.isEmpty, !loadFailed, let client else { return }
        do {
            let resp = try await client.commands()
            commands = resp.commands
        } catch {
            loadFailed = true   // stay hidden — nothing server-driven to offer
        }
    }
}
