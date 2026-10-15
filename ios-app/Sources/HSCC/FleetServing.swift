import Foundation

/// The read model behind Fleet's "Models" section (ruling `t_1897550c`, spec §5
/// as amended by PR #29): one row per workload of `GET /v1/cluster/status`.
///
/// This is the section's DATA layer — no SwiftUI — for the same reason
/// `ServingPlacement` exists for "Nodes & serving": the interesting decisions
/// (which model a recipe path names, what state a workload row is allowed to
/// claim, what VoiceOver must say) are pure functions over the payload, and a
/// headless harness (`scripts/fleet_models_check.sh`) can assert them against
/// real captured `sparkrun status` output. A `@ViewBuilder` cannot be exercised
/// off-device; these functions can. `build_check.sh` separately proves the view
/// compiles.
///
/// READ-ONLY BY CONSTRUCTION: this file reads a decode and formats strings. It
/// has no access to a client, and nothing here can fire a request — the
/// section's "no Stop/Restart here" rule (the ruling's whole point) is enforced
/// by the type, not by a code-review reminder.
enum FleetServing {

    /// One Models row: the model, its state, and the copy the row and its
    /// VoiceOver label are both derived from (one source, so the spoken label
    /// can never disagree with what is shown — §9's colour-lie rule in its
    /// textual form).
    struct Row: Identifiable {
        /// STABLE AND UNIQUE — see `rows(from:)`. NOT `ClusterWorkload.id`,
        /// which is just the name: the live feed carries the SAME recipe name
        /// once per container (three replicas in
        /// `scripts/model_decode_check/fixtures/v1_cluster_status.json`), and
        /// `ForEach` over duplicate ids is the SwiftUI defect that makes rows
        /// vanish or duplicate on the next update.
        let id: String
        /// What the row headlines. NOT the raw workload name when that is a
        /// recipe path — see `modelDisplayName`.
        let model: String
        let state: State
        /// mono caption, e.g. "tp=2 · pp=1 · 9a0467d69586" (the same
        /// units/tp/pp "Nodes & serving" shows, container shortened to its
        /// docker-style 12-char head so a row stays one line).
        let caption: String
        /// Trailing chip text ("up" / "no container").
        let chipText: String
        /// One VoiceOver element's text, in reading order:
        /// "<model>, <caption>, <state>".
        let accessibilityLabel: String

        init(workload: ClusterWorkload, id: String) {
            self.id = id
            self.model = FleetServing.modelDisplayName(workload.name)
            self.state = FleetServing.state(of: workload)
            self.caption = FleetServing.caption(for: workload)
            self.chipText = state.chipText
            self.accessibilityLabel = "\(model), \(caption), \(chipText)"
        }

        enum State: Equatable {
            /// The workload has a live container id — the server's status parse
            /// extracted `[<id>]` from the live report.
            case up
            /// The workload appears in the report but its line carried no
            /// container id (the parser's `?` placeholder, or the field absent).
            case noContainer

            /// Chip copy. Deliberately NOT the mockup's "waking": see the
            /// section comment in FleetView — this endpoint cannot prove
            /// direction, only presence of a container.
            var chipText: String {
                switch self {
                case .up: return "up"
                case .noContainer: return "no container"
                }
            }
        }
    }

    /// The section's rows, in feed order, each with a UNIQUE id.
    ///
    /// The id is `name|container_id` — what the feed actually distinguishes
    /// (same recipe, different container) — and because nothing in the wire
    /// format guarantees even THAT is unique, a repeated pair gets `#<index>`
    /// appended. The rule `scripts/fleet_models_check.sh` asserts: `rows(from:)`
    /// yields no duplicate ids for ANY input, including the live capture where
    /// three containers share one recipe name.
    static func rows(from workloads: [ClusterWorkload]) -> [Row] {
        var seen: [String: Int] = [:]
        return workloads.map { workload in
            var base = "\(workload.name)|\(workload.container_id ?? "")"
            if let count = seen[base] {
                seen[base] = count + 1
                base += "#\(count)"
            } else {
                seen[base] = 1
            }
            return Row(workload: workload, id: base)
        }
    }

    /// The model name a workload row shows.
    ///
    /// The live feed reports a sparkrun job by its RECIPE PATH — e.g.
    /// `/home/devuser/.sparkrun-local/recipes/local-fixed/qwen3.8-max-fp8-hscc.yaml`
    /// (verified against `scripts/model_decode_check/fixtures/v1_cluster_status.json`
    /// and a live `hscc cluster status`). A path is unreadable as a headline and
    /// is also the only place a personal home directory can enter the screen, so
    /// the last path component with the extension trimmed is the headline:
    /// `qwen3.8-max-fp8-hscc`. When the name is already bare, it passes through
    /// untouched — no trimming, no case change, no rewriting of anything the
    /// cluster actually reported.
    static func modelDisplayName(_ name: String) -> String {
        let leaf = name.components(separatedBy: "/").last.map { $0.isEmpty ? name : $0 } ?? name
        guard leaf.lowercased().hasSuffix(".yaml") || leaf.lowercased().hasSuffix(".yml") else {
            return leaf
        }
        let stem = leaf.components(separatedBy: ".").dropLast().joined(separator: ".")
        // A name that is ONLY an extension (".yaml") has no stem; show the leaf
        // rather than an empty headline.
        return stem.isEmpty ? leaf : stem
    }

    /// The state a workload row may claim. Container id present and not the
    /// parser's "?" placeholder → up; anything else → noContainer. Never a
    /// guess beyond what the payload says.
    static func state(of workload: ClusterWorkload) -> Row.State {
        guard let cid = workload.container_id, !cid.isEmpty, cid != "?" else {
            return .noContainer
        }
        return .up
    }

    /// The mono caption: the same parallelism + container rendering
    /// "Nodes & serving" uses (`tp=… · pp=…` and the container id when real),
    /// so the two views of the same feed read identically.
    static func caption(for workload: ClusterWorkload) -> String {
        var parts: [String] = [parallelismLabel(workload)]
        if let cid = workload.container_id, cid != "?", !cid.isEmpty {
            parts.append(shortContainer(cid))
        }
        return parts.joined(separator: " · ")
    }

    /// "tp=1 · pp=1", or "parallelism unknown" when the status parser could not
    /// extract either — moved logic identical to the serving screen's wording
    /// (same words, so one feed cannot say "unknown" on one screen and "—" on
    /// another).
    static func parallelismLabel(_ workload: ClusterWorkload) -> String {
        let tp = workload.tp ?? "?"
        let pp = workload.pp ?? "?"
        if (tp == "?" || tp.isEmpty) && (pp == "?" || pp.isEmpty) {
            return "parallelism unknown"
        }
        return "tp=\(tp) · pp=\(pp)"
    }

    /// The status parser yields ids like `9a0467d69586ef5f_4e690f2e56f8`
    /// (16 hex + "_" + 12 hex) and `?`. Keep the head readable in a one-line
    /// caption: 12 chars of the hex, the same prefix docker itself prints.
    /// Anything else passes through uncut.
    static func shortContainer(_ id: String) -> String {
        if id == "?" || id.isEmpty { return id }
        let head = String(id.prefix(12))
        return head.count < id.count ? head : id
    }

    /// Section header text — the mockup's `Models · 3 serving`, derived from the
    /// rows actually rendered (a count that can never disagree with the list).
    static func header(workloadCount: Int) -> String {
        "Models · \(workloadCount) serving"
    }
}
