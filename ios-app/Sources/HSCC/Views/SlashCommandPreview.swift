import SwiftUI

/// Pure, testable logic for the slash-command PREVIEW — the confirmation
/// surface that shows what a selected command WILL do before it runs.
///
/// No SwiftUI here, mirroring `SlashPaletteLogic`: the derivation is isolated
/// so a headless CLI harness can unit-test it (the repo's convention for
/// logic-vs-view separation). Every rule is driven by the server `SlashCommand`
/// metadata — there is no hardcoded command list to rot.
enum SlashPreviewLogic {
    /// The leading command name in a draft, or nil when the draft does not lead
    /// with a slash command. "/cluster-restart confirm" -> "cluster-restart".
    static func commandName(from draft: String) -> String? {
        let trimmed = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.hasPrefix("/") else { return nil }
        let first = trimmed.split(separator: " ", maxSplits: 1).first.map(String.init) ?? ""
        let withoutSlash = first.dropFirst()   // drop the leading "/"
        guard !withoutSlash.isEmpty else { return nil }
        return String(withoutSlash)
    }

    /// The args typed after the command name, as whitespace-separated words.
    /// "/template apply prod" -> ["apply", "prod"]; "/cluster" -> [].
    static func args(of draft: String) -> [String] {
        let trimmed = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        let parts = trimmed.split(separator: " ", maxSplits: 1)
        guard parts.count == 2 else { return [] }
        return parts[1].split(whereSeparator: \.isWhitespace).map(String.init)
    }

    /// Whether the draft is a *committed* slash command worth previewing: it
    /// leads with "/name" followed by whitespace (or more). This keeps the
    /// preview from showing while the operator is still filtering the palette
    /// (draft "/cluster" with no trailing space → palette only); once they pick
    /// a command (draft "/cluster-restart ") the palette hides and the preview
    /// takes over.
    static func isCommittedCommand(_ draft: String) -> Bool {
        // Check the RAW draft (not trimmed): the whitespace after the command
        // name is the signal the operator has moved past it (to args or send).
        // Trimming would strip exactly the signal we're looking for.
        guard draft.hasPrefix("/") else { return false }
        guard let firstSpace = draft.firstIndex(where: { $0 == " " || $0.isNewline }) else {
            return false   // "/cluster" — no space yet → still choosing from palette
        }
        let name = draft[draft.index(after: draft.startIndex)..<firstSpace]
        return !name.isEmpty
    }

    /// Resolve the draft's leading token against the loaded catalog: the
    /// matching command, or nil when the draft doesn't lead with a known slash
    /// command. The preview never fabricates a command the backend doesn't
    /// actually have.
    static func resolve(_ draft: String, commands: [SlashCommand]) -> SlashCommand? {
        guard isCommittedCommand(draft), let name = commandName(from: draft) else { return nil }
        return commands.first { $0.name == name }
    }

    /// Whether running this command changes fleet/service state enough to
    /// warrant an explicit confirmation before it runs.
    ///
    /// Derivation (no hardcoded command list — driven by the authoritative
    /// catalog):
    ///   1. No args-hint (`takesArgs == false`) → not confirm-gated → never
    ///      destructive. (cluster, status, workers-up all carry no args-hint.)
    ///   2. Every OTHER args-hint command in the catalog is confirm-gated and
    ///      destructive (orch-restart, cluster-restart, cluster-reboot,
    ///      cluster-down, cluster-docker-prune, cluster-apt-upgrade,
    ///      cluster-prune, heal). Basing this on the authoritative `takesArgs`
    ///      signal — rather than grepping the description text — is what makes
    ///      cluster-apt-upgrade correct: its description ("apt update+upgrade
    ///      across cluster…") never says "confirm-first", but its confirm-gated
    ///      args-hint proves it is destructive.
    ///   3. `template` is the single exception whose destructive-ness depends
    ///      on the typed subcommand: only `apply` changes the fleet; `list` /
    ///      `preview` / `validate` are read-only. We read the operator's own
    ///      args rather than guessing — `/template apply n` → destructive,
    ///      `/template list` → not.
    static func isDestructive(_ command: SlashCommand, args: [String]) -> Bool {
        if !command.takesArgs { return false }
        if command.name == "template" { return args.contains("apply") }
        return true
    }

    /// The explicit confirm word to append for destructive commands, else nil
    /// (meaning the command is one-tap safe — no confirm required).
    static func confirmWord(for command: SlashCommand, args: [String]) -> String? {
        isDestructive(command, args: args) && command.takesArgs ? "confirm" : nil
    }

    /// A one-line readout of what the command targets (used for the "targets"
    /// line of the preview). Derived from the command name — a display label,
    /// not behaviour that could misfire dangerously.
    static func targetReadout(_ command: SlashCommand) -> String {
        let n = command.name
        if n == "heal" { return "unhealthy workers" }
        if n == "workers-up" { return "down keepalive workers" }
        if n.contains("orch") { return "the orchestrator (vLLM)" }
        if n.contains("template") { return "the active cluster template" }
        if n.contains("cluster") { return "the cluster" }
        return "the cluster"
    }
}

/// A compact, confirm-first preview card for a resolved slash command.
///
/// Shown in the composer once the operator has committed a command name (from
/// the palette or typed) in the draft. Renders:
///   * the command + its authoritative description (what it changes),
///   * a "Targets: …" line (what it targets),
///   * a Destructive / Read-only badge (whether it gates on confirm),
///   * an explicit **Run** action — for a destructive command this appends the
///     required `confirm` word before sending (the backend's confirm-first
///     contract); for a non-destructive command it is a plain one-tap send.
///
/// Best-effort like the palette: if the catalog hasn't loaded, the card shows
/// nothing and the composer behaves exactly as before — an un-previewed send
/// is no worse than today.
struct SlashPreviewCard: View {
    /// The draft to preview (reactive — the card updates live as it changes).
    let draft: String
    /// The configured client (nil when unconfigured → hidden, like the palette).
    let client: HSCCClient?
    /// Invoked with the final text to send. The card appends the confirm word
    /// to destructive commands, then calls this — the composer's send path.
    let onRun: (String) -> Void

    @State private var commands: [SlashCommand] = []
    @State private var loadFailed = false

    private var resolved: SlashCommand? {
        SlashPreviewLogic.resolve(draft, commands: commands)
    }

    private var args: [String] {
        SlashPreviewLogic.args(of: draft)
    }

    var body: some View {
        Group {
            if let cmd = resolved {
                previewCard(cmd)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .task { await load() }
    }

    private func previewCard(_ cmd: SlashCommand) -> some View {
        let destructive = SlashPreviewLogic.isDestructive(cmd, args: args)
        let target = SlashPreviewLogic.targetReadout(cmd)
        let confirm = SlashPreviewLogic.confirmWord(for: cmd, args: args)

        return VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            HStack(spacing: Theme.Spacing.sm.rawValue) {
                Text("/\(cmd.name)")
                    .font(.subheadline.weight(.semibold))
                    .foregroundColor(Theme.Semantic.onSurface)
                Spacer(minLength: 0)
                badge(destructive)
            }

            // What it changes — the authoritative one-line description.
            Text(cmd.description)
                .font(.footnote)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                .fixedSize(horizontal: false, vertical: true)

            // What it targets.
            Text("Targets: \(target)")
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)

            HStack(spacing: Theme.Spacing.sm.rawValue) {
                runButton(cmd, destructive: destructive, confirm: confirm)
                if !destructive {
                    Text("Read-only — one tap to run")
                        .font(.caption2)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
        }
        .padding(Theme.Spacing.md.rawValue)
        .background(
            RoundedRectangle(cornerRadius: Theme.Corner.card.rawValue, style: .continuous)
                .fill(Theme.Semantic.surfaceRaised)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Corner.card.rawValue, style: .continuous)
                .stroke(destructive ? Theme.Semantic.bad.opacity(0.4) : Color.primary.opacity(0.08), lineWidth: 1)
        )
    }

    @ViewBuilder
    private func badge(_ destructive: Bool) -> some View {
        Text(destructive ? "Destructive" : "Read-only")
            .font(.caption2.weight(.semibold))
            .foregroundColor(destructive ? Theme.Semantic.bad : Theme.Semantic.ok)
            .padding(.horizontal, Theme.Spacing.sm.rawValue)
            .padding(.vertical, Theme.Spacing.xxs.rawValue)
            .background(
                Capsule().fill(
                    (destructive ? Theme.Semantic.bad : Theme.Semantic.ok).opacity(0.12)
                )
            )
    }

    private func runButton(_ cmd: SlashCommand, destructive: Bool, confirm: String?) -> some View {
        Button {
            // Destructive → append the backend's required confirm word before
            // committing. Non-destructive → the draft as-is (one tap).
            let trimmed = draft.trimmingCharacters(in: .whitespacesAndNewlines)
            let text = confirm.map { "\(trimmed) \($0)" } ?? trimmed
            onRun(text)
        } label: {
            Label("Run \("/" + cmd.name)" + (destructive ? " confirm" : ""),
                  systemImage: destructive ? "exclamationmark.triangle.fill" : "paperplane.fill")
                .font(.footnote.weight(.semibold))
                .foregroundColor(destructive ? Theme.Semantic.bad : Theme.Semantic.onSurface)
                .padding(.horizontal, Theme.Spacing.md.rawValue)
                .padding(.vertical, Theme.Spacing.xs.rawValue)
                .background(
                    Capsule().fill(Theme.Semantic.surfaceElevated)
                )
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Run \("/" + cmd.name), \(destructive ? "destructive, requires confirm" : "read-only")")
    }

    /// Fetch the command catalog from the server exactly once, lazily. If it
    /// can't be fetched the card simply stays hidden (no fabricated preview).
    func load() async {
        guard commands.isEmpty, !loadFailed, let client else { return }
        do {
            let resp = try await client.commands()
            commands = resp.commands
        } catch {
            loadFailed = true
        }
    }
}
