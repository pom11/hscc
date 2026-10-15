import Foundation

/// One renderer for a `JSONValue` into a single readable line.
///
/// Spec §2 "Duplicated renderers": this existed twice — Ops (was 366-386)
/// rendered objects as `key: value · key: value`, FleetControl (was 171-180)
/// returned the placeholder `"<complex>"` for anything nested. Same type, same
/// screen family, two answers: the escalation list (whose entries are
/// `{task, action, category}`) showed real fields on Ops and `<complex>` on
/// Fleet Control, so the operator could see *which* tasks were pending from one
/// destination and not the other.
///
/// The Ops behaviour wins the merge: a placeholder that hides every field is
/// strictly worse than a slightly long line, and every call site here is a
/// `LabeledContent`/caption row where the keys ARE the information.
enum DisplayJSON {
    /// Render one value. Scalars print bare; objects print sorted
    /// `key: value` pairs joined by a middot; arrays print a bracketed,
    /// comma-separated list. Nested values recurse.
    static func render(_ value: JSONValue) -> String {
        switch value {
        case .string(let s): return s
        case .int(let n): return "\(n)"
        case .double(let d): return String(format: "%.2f", d)
        case .bool(let b): return b ? "true" : "false"
        case .null: return "—"
        case .object(let o):
            let items = o.keys.sorted().compactMap { key -> String? in
                guard let v = o[key] else { return nil }
                return key + ": " + render(v)
            }
            return items.joined(separator: " · ")
        case .array(let a):
            return "[" + a.map { render($0) }.joined(separator: ", ") + "]"
        }
    }
}
