import Foundation

// ============================================================
// Mirror of Sources/HSCC/Models.swift
// ============================================================
struct SlashCommand: Decodable, Hashable, Identifiable {
    let name: String
    let description: String
    let takesArgs: Bool

    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, description
        case takesArgs = "takes_args"
    }
}

// ============================================================
// Mirror of Sources/HSCC/Views/SlashCommandPreview.swift (pure logic only)
// ============================================================
enum SlashPreviewLogic {
    static func commandName(from draft: String) -> String? {
        let trimmed = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.hasPrefix("/") else { return nil }
        let first = trimmed.split(separator: " ", maxSplits: 1).first.map(String.init) ?? ""
        let withoutSlash = first.dropFirst()
        guard !withoutSlash.isEmpty else { return nil }
        return String(withoutSlash)
    }

    static func args(of draft: String) -> [String] {
        let trimmed = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        let parts = trimmed.split(separator: " ", maxSplits: 1)
        guard parts.count == 2 else { return [] }
        return parts[1].split(whereSeparator: \.isWhitespace).map(String.init)
    }

    static func isCommittedCommand(_ draft: String) -> Bool {
        guard draft.hasPrefix("/") else { return false }
        guard let firstSpace = draft.firstIndex(where: { $0 == " " || $0.isNewline }) else {
            return false
        }
        let name = draft[draft.index(after: draft.startIndex)..<firstSpace]
        return !name.isEmpty
    }

    static func resolve(_ draft: String, commands: [SlashCommand]) -> SlashCommand? {
        guard isCommittedCommand(draft), let name = commandName(from: draft) else { return nil }
        return commands.first { $0.name == name }
    }

    static func isDestructive(_ command: SlashCommand, args: [String]) -> Bool {
        if !command.takesArgs { return false }
        if command.name == "template" { return args.contains("apply") }
        return true
    }

    static func confirmWord(for command: SlashCommand, args: [String]) -> String? {
        isDestructive(command, args: args) && command.takesArgs ? "confirm" : nil
    }

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

// ============================================================
// The REAL catalog, verbatim from hscc-commands/__init__.py register():
//   {name, description, args_hint} — takes_args = bool(args_hint).
// ============================================================
let real: [(name: String, desc: String, hint: String?)] = [
  ("cluster", "Show HSCC cluster status (orchestrator + workers, live health).", nil),
  ("status", "Rich HSCC dashboard: topology, free-VRAM, proxy, daemon, template.", nil),
  ("orch-restart", "Restart the orchestrator vLLM (confirm-first).", "confirm"),
  ("cluster-restart", "Recover the cluster by re-applying the active template (confirm-first).", "confirm"),
  ("cluster-reboot", "Reboot all nodes (workers parallel, orchestrator last) — confirm-first.", "confirm"),
  ("cluster-down", "Stop all vLLM units cluster-wide (hosts stay up) — confirm-first.", "confirm"),
  ("cluster-docker-prune", "docker system prune -af on every node (volumes preserved) — confirm-first.", "confirm"),
  ("cluster-apt-upgrade", "apt update+upgrade across cluster; chains to /cluster-reboot if needed.", "confirm"),
  ("cluster-prune", "Macro: down → docker-prune → apt-upgrade → restart (confirm-first).", "confirm"),
  ("heal", "Heal unhealthy workers; advise on orchestrator wedge (confirm-first).", "confirm"),
  ("template", "List/preview/validate/apply cluster templates.", "list|preview <name>|apply <name> [confirm]"),
  ("workers-up", "Bring up down keepalive workers only (non-destructive, no confirm).", nil),
]

let commands: [SlashCommand] = real.map {
    SlashCommand(name: $0.name, description: $0.desc, takesArgs: $0.hint != nil)
}

// Ground truth for the destructive classification — from the backend's
// confirm-first contract + template's apply-only destructive semantics.
let expected: [String: Bool] = [
  "cluster": false, "status": false, "workers-up": false,
  "orch-restart": true, "cluster-restart": true, "cluster-reboot": true,
  "cluster-down": true, "cluster-docker-prune": true, "cluster-apt-upgrade": true,
  "cluster-prune": true, "heal": true,
  "template": false,   // bare "template" (no apply subcommand) is read-only
]

var failures = 0
func expect(_ cond: Bool, _ msg: String) {
    if cond { print("PASS  \(msg)") } else { print("FAIL  \(msg)"); failures += 1 }
}

// --- committed-command gate + resolve ---
expect(SlashPreviewLogic.isCommittedCommand("/cluster-restart ") == true, "isCommitted '/cluster-restart ' (with trailing space)")
expect(SlashPreviewLogic.isCommittedCommand("/cluster-restart confirm") == true, "isCommitted '/cluster-restart confirm'")
expect(SlashPreviewLogic.isCommittedCommand("/cluster") == false, "isCommitted '/cluster' (still choosing) → false")
expect(SlashPreviewLogic.isCommittedCommand("hello /cluster") == false, "isCommitted 'hello /cluster' (doesn't lead with /) → false")
expect(SlashPreviewLogic.resolve("/cluster-restart ", commands: commands)?.name == "cluster-restart", "resolve known committed command")
expect(SlashPreviewLogic.resolve("/cluster", commands: commands) == nil, "resolve uncommitted '/cluster' → nil")
expect(SlashPreviewLogic.resolve("/definitely-not-real ", commands: commands) == nil, "resolve unknown command → nil")
expect(SlashPreviewLogic.resolve("hello world", commands: commands) == nil, "resolve prose → nil")

// --- command name + args extraction ---
expect(SlashPreviewLogic.commandName(from: "/cluster-restart confirm") == "cluster-restart", "commandName with args")
expect(SlashPreviewLogic.args(of: "/template apply prod") == ["apply", "prod"], "args extraction template")
expect(SlashPreviewLogic.args(of: "/cluster-restart") == [], "no args when none typed")

// --- destructive classification for EVERY registered command ---
for c in real {
    let info = commands.first { $0.name == c.name }!
    func check(_ label: String, _ a: [String], _ want: Bool) {
        let got = SlashPreviewLogic.isDestructive(info, args: a)
        let confirm = SlashPreviewLogic.confirmWord(for: info, args: a) ?? "-"
        expect(
            got == want,
            "destructive /\(c.name) \(label) args=\(a) → \(got) (want \(want)) confirm=\(confirm) target=\(SlashPreviewLogic.targetReadout(info))")
    }
    check("(no args)", [], expected[c.name] ?? false)
    if c.name == "template" {
        check("apply", ["apply", "prod"], true)
        check("list", ["list"], false)
        check("preview", ["preview", "prod"], false)
        check("validate", ["validate", "prod"], false)
    }
}

// --- confirmWord feeds the explicit Run ---
expect(SlashPreviewLogic.confirmWord(for: commands.first { $0.name == "cluster-restart" }!, args: []) == "confirm",
       "destructive command gets confirm word")
expect(SlashPreviewLogic.confirmWord(for: commands.first { $0.name == "status" }!, args: []) == nil,
       "read-only command has NO confirm word (one tap)")
expect(SlashPreviewLogic.confirmWord(for: commands.first { $0.name == "template" }!, args: ["apply", "prod"]) == "confirm",
       "template apply gets confirm word")
expect(SlashPreviewLogic.confirmWord(for: commands.first { $0.name == "template" }!, args: ["list"]) == nil,
       "template list has NO confirm word")

print("")
print(failures == 0 ? "ALL PASS — preview derivation is correct for every registered command."
                    : "\(failures) FAILURE(S)")
exit(failures == 0 ? 0 : 1)
