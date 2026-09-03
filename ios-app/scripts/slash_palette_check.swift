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

struct CommandsResponse: Decodable {
    let commands: [SlashCommand]
    let speak: String
}

// ============================================================
// Mirror of Sources/HSCC/Views/SlashCommandPalette.swift (pure logic only)
// ============================================================
enum SlashPaletteLogic {
    static func isCommandWord(_ draft: String) -> Bool {
        lastWord(of: draft).hasPrefix("/")
    }

    static func filterQuery(from word: String) -> String {
        word.hasPrefix("/") ? String(word.dropFirst()) : ""
    }

    static func lastWord(of draft: String) -> String {
        if let r = draft.rangeOfCharacter(from: .whitespacesAndNewlines, options: .backwards) {
            return String(draft[draft.index(after: r.lowerBound)...])
        }
        return draft
    }

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

    static func draft(afterInserting command: SlashCommand, draft: String) -> String {
        guard isCommandWord(draft) else { return draft }
        if let r = draft.rangeOfCharacter(from: .whitespacesAndNewlines, options: .backwards) {
            let prefix = draft[..<r.upperBound]
            return String(prefix) + "/" + command.name + " "
        }
        return "/" + command.name + " "
    }
}

// ============================================================
// Test harness
// ============================================================
var failures = 0
func check(_ cond: Bool, _ label: String) {
    if cond { print("  ✓ \(label)") }
    else { print("  ✗ FAIL: \(label)"); failures += 1 }
}

func cmd(_ n: String, _ d: String = "", _ t: Bool = false) -> SlashCommand {
    SlashCommand(name: n, description: d, takesArgs: t)
}

let commands = [
    cmd("cluster", "Show HSCC cluster status."),
    cmd("orch-restart", "Restart the orchestrator."),
    cmd("cluster-restart", "Restart the whole cluster."),
    cmd("template", "Apply a template.", true),
    cmd("workers-up", "Bring workers up.", true),
]

print("== isCommandWord (command position) ==")
check(SlashPaletteLogic.isCommandWord("/") == true, "\"/\" is command position")
check(SlashPaletteLogic.isCommandWord("/clu") == true, "\"/clu\" is command position")
check(SlashPaletteLogic.isCommandWord("hey /clu") == true, "\"hey /clu\" is command position")
check(SlashPaletteLogic.isCommandWord("http://x") == false, "\"http://x\" is NOT (contains // mid-word) — last word is full string")
check(SlashPaletteLogic.isCommandWord("a/b") == false, "\"a/b\" is NOT command position")
check(SlashPaletteLogic.isCommandWord("plain text") == false, "\"plain text\" is NOT")
check(SlashPaletteLogic.isCommandWord("/clu foo") == false, "\"/clu foo\" last word \"foo\" is NOT command position")

print("== filterQuery ==")
check(SlashPaletteLogic.filterQuery(from: "/") == "", "just \"/\" → empty query (show all)")
check(SlashPaletteLogic.filterQuery(from: "/clu") == "clu", "\"/clu\" → \"clu\"")
check(SlashPaletteLogic.filterQuery(from: "foo") == "", "non-command word → \"\"")

print("== filtered (matching as you type) ==")
check(SlashPaletteLogic.filtered(commands, query: "").count == 5, "empty query lists all")
let clu = SlashPaletteLogic.filtered(commands, query: "clu").map { $0.name }
check(clu == ["cluster", "cluster-restart"], "\"clu\" → cluster before cluster-restart (prefix, alpha)")
let rest = SlashPaletteLogic.filtered(commands, query: "restart").map { $0.name }
check(rest == ["cluster-restart", "orch-restart"], "\"restart\" → contains, alpha: cluster-restart, orch-restart")
let wu = SlashPaletteLogic.filtered(commands, query: "workers").map { $0.name }
check(wu == ["workers-up"], "\"workers\" → workers-up (prefix)")

print("== draft(afterInserting:) ==")
let tpl = cmd("template", "x", true)
check(SlashPaletteLogic.draft(afterInserting: tpl, draft: "/tem") == "/template ", "replaces trailing word")
check(SlashPaletteLogic.draft(afterInserting: tpl, draft: "please /te") == "please /template ", "preserves leading text + separator")
check(SlashPaletteLogic.draft(afterInserting: tpl, draft: "/te stuff") == "/te stuff", "no-op when NOT at command position")
check(SlashPaletteLogic.draft(afterInserting: cmd("cluster"), draft: "/") == "/cluster ", "bare \"/\" inserts command")
check(SlashPaletteLogic.draft(afterInserting: cmd("cluster"), draft: "plain") == "plain", "no-op on plain text")

print("== decoding against real server contract ==")
let realJSON = """
{"commands":[{"name":"cluster","description":"Show HSCC cluster status.","takes_args":false},{"name":"template","description":"Apply a template.","takes_args":true}],"speak":"2 slash commands available."}
"""
let resp = try! JSONDecoder().decode(CommandsResponse.self, from: Data(realJSON.utf8))
check(resp.commands.count == 2, "decodes 2 commands")
check(resp.commands[0].takesArgs == false && resp.commands[1].takesArgs == true, "takes_args maps to takesArgs correctly")
check(resp.speak == "2 slash commands available.", "speak decodes")

print()
if failures == 0 {
    print("✅ slash_palette_check: ALL PASS")
} else {
    print("❌ \(failures) failure(s)")
    exit(1)
}
