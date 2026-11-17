#!/bin/bash
# widget_settings_check.sh — prove the Widgets & Live Activities shared
# settings model (t_dea36c48) round-trips: encode/decode, defaulting, bounded
# sections, and persistence through a REAL named UserDefaults suite.
#
# Why this exists: acceptance criterion #1 is "settings persist round-trip
# through the App Group on the DEVICE (set -> relaunch -> still set, readable
# in the extension target)". A physical device is not reachable from this
# build host, and `containerURL(forSecurityApplicationGroupIdentifier:)`
# returns a URL on macOS even for an UNREGISTERED group (shared_store_check.sh
# documents this), so the container itself cannot be proven here. What CAN be
# proven by execution is the EXACT part that breaks: the Codable serialization
# contract + the named-suite store/load path. This harness extracts the
# committed model verbatim from Sources/Shared/SharedStore.swift (no
# hand-copy -> no drift), re-homes it, and asserts the round-trip through a
# real on-disk named suite. Device-only checks (App Group container presence,
# extension host reads) remain in shared_store_check.sh + the on-device smoke
# checklist.
#
# Usage: ios-app/scripts/widget_settings_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
SRC="Sources/Shared/SharedStore.swift"
INTENT="Sources/HSCC/Intents/WidgetScopePickerIntent.swift"

# t_98fb8726 extended this gate in two layers: the executed cases 8-17 below
# run the REAL `WidgetScope.buildPickerSelection` + `apply` (extracted with
# this same verbatim mechanism, since they live on the shared model the intent
# delegates to) against the REAL suite domain — proving the configuration
# intent's write lands exactly where `AppGroupWidgetSettings.load()` reads —
# and the structural greps at the foot of the script prove the committed
# intent file really calls those statics (delegation, not a private copy) and
# never opens a UserDefaults handle of its own.
#
# t_3dd38411 added the PER-WIDGET layer: a scope per surface entry inside this
# same blob (cases 15-17). Case 16-17 drive the real app-side
# `Sources/HSCC/WidgetSettingsStore.swift` — compiled in, not copied — because
# that is the only writer the config screen has, and case 17 pins the race
# where a fleet-wide scope written by the intent while the screen is open used
# to be rolled back by the screen's next write.
[ -f "$INTENT" ] || { echo "intent file missing: $INTENT" >&2; exit 1; }

# Extract the model block verbatim (WidgetSurfaceKind .. end of the availableSections
# extension). Line-anchored on stable markers; no hand-copy -> no drift.
start=$(grep -n "^// Widgets & Live Activities shared settings" "$SRC" | head -1 | cut -d: -f1)
end=$(grep -n "^/// A lightweight read of the token" "$SRC" | head -1 | cut -d: -f1)
model=$(sed -n "${start},$((end-1))p" "$SRC")

# The storage key + suite come from the real AppGroup enum (verbatim).
suite=$(grep "static let suiteName" "$SRC" | sed 's/^ *//')
wkey=$(grep "static let widgetSettingsKey" "$SRC" | sed 's/^ *//')
suite_container="enum AppGroup {
$suite
$wkey
}"

WORK="${TMPDIR:-/tmp}/widget_check_$$"
mkdir -p "$WORK"
# The harness is top-level code, so it MUST be named main.swift: with the real
# Sources/HSCC/WidgetSettingsStore.swift compiled alongside it (case 16 drives
# that store rather than a copy of it), with a second file in the module Swift
# rejects top-level statements in a file that is not main.swift.
cat > "$WORK/main.swift" <<SWIFT
import Foundation

// Minimal real shim: the suite name + storage key extracted verbatim from the
// committed AppGroup enum (the model references these through it).
$suite_container

// The committed model, verbatim, no edits.
$model

// ---- assertions ---------------------------------------------------------
var failures: [String] = []
func check(_ name: String, _ cond: Bool) {
    if cond { print("  ok: \(name)") } else { failures.append(name); print("FAIL: \(name)") }
}

// 1. defaulting is backward compatible: absent store -> every surface on, 5/15
//    min, all sections.
let defaults = AppGroupWidgetSettings.defaults()
// The count is READ, not remembered. This line used to be
// `defaults.surfaces.count == 4`, which every new surface turned red — a
// healthy gate failing on a correct change, which trains everyone to expect
// noise here. `defaults()` is built FROM allCases, so what this case must
// actually prove is that no case is missing from the defaults and none is
// invented, which is a set comparison rather than a number.
check("defaults seed exactly every WidgetSurfaceKind case",
      Set(defaults.surfaces.keys) == Set(WidgetSurfaceKind.allCases)
      && defaults.surfaces.count == WidgetSurfaceKind.allCases.count)
for kind in WidgetSurfaceKind.allCases {
    let s = defaults.setting(for: kind)
    check("\(kind.rawValue) defaults active", s.isActive)
    check("\(kind.rawValue) defaults all sections",
          s.showSections == Set(kind.availableSections))
    check("\(kind.rawValue) defaults cadence \(kind.isLiveActivity ? 15 : 5)",
          s.refreshMinutes == (kind.isLiveActivity ? 15 : 5))
}

// 2. empty store (no entry for a surface) -> setting(for:) still returns ON
//    defaults (the pre-existing Cluster widget must render unchanged).
let empty = AppGroupWidgetSettings(surfaces: [:])
let clusterDefault = empty.setting(for: .widgetCluster)
check("absent surface defaults ON backward compatible", clusterDefault.isActive)
check("absent widget defaults 5 min", clusterDefault.refreshMinutes == 5)
let wakeDefault = empty.setting(for: .liveActivityWake)
check("absent Live Activity defaults 15 min", wakeDefault.refreshMinutes == 15)

// 3. bounded set: a stale/foreign section id is stripped by setting(for:).
var foreign = AppGroupWidgetSettings(surfaces: [
    .widgetCluster: WidgetSurfaceSettings(isActive: true, refreshMinutes: 5,
                                          showSections: ["state", "bogus_section"])
])
let bounded = foreign.setting(for: .widgetCluster)
check("foreign section stripped (bounded set)",
      bounded.showSections == ["state"] && !bounded.showSections.contains("bogus_section"))

// 4. Codable round-trip: encode -> decode yields an equal registry.
foreign = AppGroupWidgetSettings(surfaces: [
    .liveActivityMonitor: WidgetSurfaceSettings(isActive: false, refreshMinutes: 30,
                                                showSections: ["state", "host"]),
    .widgetCluster: WidgetSurfaceSettings(isActive: true, refreshMinutes: 60,
                                          showSections: ["state", "board"]),
])
guard let data = try? JSONEncoder().encode(foreign),
      let decoded = try? JSONDecoder().decode(AppGroupWidgetSettings.self, from: data) else {
    check("encode/decode round-trip", false)
    exit(1)
}
check("encode/decode round-trip equal", decoded == foreign)

// 5. PERSISTENCE through a REAL named suite (the App-Group contract's core):
//    write to a throwaway suite, then load() reads the identical blob back.
//    Uses a unique per-run suite so the test never collides or leaks state.
let suiteName = "group.com.hscc.ios.check.\(UUID().uuidString.prefix(8))"
let suite = UserDefaults(suiteName: suiteName)!
guard let data2 = try? JSONEncoder().encode(foreign) else { check("persist encode", false); exit(1) }
suite.set(data2, forKey: AppGroupWidgetSettings.storageKey)
// Reload from a FRESH defaults handle (same suite domain -> on-disk read),
// simulating the app write -> extension read path.
let reread = UserDefaults(suiteName: suiteName)!.data(forKey: AppGroupWidgetSettings.storageKey)
guard let reread, let decoded2 = try? JSONDecoder().decode(AppGroupWidgetSettings.self, from: reread) else {
    check("persist round-trip (write -> read)", false)
    exit(1)
}
check("persist round-trip (write -> read) equal", decoded2 == foreign)

// 6. Off value: 0 refresh is honored by the store (toggle in UI uses 0 = Off).
let off = AppGroupWidgetSettings(surfaces: [
    .widgetCluster: WidgetSurfaceSettings(isActive: true, refreshMinutes: 0, showSections: ["state"])
])
check("Off (0) refresh is stored", off.setting(for: .widgetCluster).refreshMinutes == 0)

// 7. registry raw values are the stable keys the siblings consume.
check("widget.cluster registry key", WidgetSurfaceKind.widgetCluster.rawValue == "widget.cluster")
check("liveActivity.monitor registry key", WidgetSurfaceKind.liveActivityMonitor.rawValue == "liveActivity.monitor")

// ---- widget scope (t_98fb8726): the intent's write, the widget's read ------
// Everything from here to the restore is the code the configuration intent
// runs: buildPickerSelection folds the sheet's raw strings, apply persists
// into the ONE blob, AppGroupWidgetSettings.load() is the exact call a
// widget timeline makes. All of it is the committed source, extracted above.

// 8. no scope by default: an operator who never picked anything reads nil,
//    which every widget renders as "everything" (not a hidden sentinel).
check("defaults carry no scope", AppGroupWidgetSettings.defaults().scope == nil)
check("nil scope is 'all'", WidgetScope().isAll)

// 9. UPGRADE: a blob written before the scope field existed must still
//    decode, with scope nil — old installs keep their cadences and render
//    fleet-wide. The old shape is the real encoder output with scope nil
//    (synthesized Codable omits absent optionals), asserted to carry no
//    scope key before it is decoded — a hand-typed JSON shape would not
//    prove the upgrade path of blobs this code actually wrote.
let legacyBase = AppGroupWidgetSettings(surfaces: [
    .widgetCluster: WidgetSurfaceSettings(isActive: true, refreshMinutes: 7, showSections: ["state"]),
])
var legacy: AppGroupWidgetSettings?
if let legacyData = try? JSONEncoder().encode(legacyBase) {
    check("old-format blob carries no scope key",
          legacyData.firstRange(of: Data("\"scope\"".utf8)) == nil)
    do { legacy = try JSONDecoder().decode(AppGroupWidgetSettings.self, from: legacyData) }
    catch { print("  legacy decode error: \(error)") }
}
check("legacy blob (no scope key) decodes", legacy != nil)
check("legacy blob scope is nil", legacy?.scope == nil)
check("legacy blob keeps its cadence", legacy?.setting(for: .widgetCluster).refreshMinutes == 7)

// 10. fold: the sentinel in every picker selects nothing (absence, not "*");
//     blanks fold too; a real name is trimmed and kept.
check("sentinel-only folds to nil",
      WidgetScope.buildPickerSelection(board: WidgetScope.wildcard, project: nil, node: nil) == nil)
check("blanks fold to nil",
      WidgetScope.buildPickerSelection(board: "  ", project: "", node: WidgetScope.wildcard) == nil)
check("name is trimmed",
      WidgetScope.buildPickerSelection(board: " hscc ", project: nil, node: nil) == WidgetScope(board: "hscc"))

// 11. fold precedence: a corrupt multi-selection stores EXACTLY ONE thing,
//     board > project > node — the rest are dropped, never silently kept.
let multi = WidgetScope.buildPickerSelection(board: "hscc", project: "radio", node: ".244")
check("multi-selection keeps only the board",
      multi == WidgetScope(board: "hscc") && multi?.project == nil && multi?.node == nil)
check("project-only selection",
      WidgetScope.buildPickerSelection(board: WidgetScope.wildcard, project: "radio", node: nil) == WidgetScope(project: "radio"))
check("node selection is a label",
      WidgetScope.buildPickerSelection(board: nil, project: nil, node: ".244") == WidgetScope(node: ".244"))

// 12. the spoken line is built from the stored values only.
check("summaryLine names the board", WidgetScope(board: "hscc").summaryLine == "board hscc")
check("summaryLine for absence", WidgetScope().summaryLine == "all boards, projects and nodes")

// 13. END-TO-END on the REAL suite domain: seed a blob with a non-default
//     cadence, run the intent's exact two calls (fold, then apply), read back
//     through the widgets' own load(). Then restore the machine's state.
//     (A throwaway suite cannot prove this case: apply and load are pinned
//     to AppGroup.suiteName on purpose, and that pairing is the claim.)
let realSuite = UserDefaults(suiteName: AppGroup.suiteName)!
let priorBlob = realSuite.data(forKey: AppGroupWidgetSettings.storageKey)
let seeded = AppGroupWidgetSettings(surfaces: [
    .widgetCluster: WidgetSurfaceSettings(isActive: true, refreshMinutes: 7, showSections: ["state"]),
])
guard let seededData = try? JSONEncoder().encode(seeded) else { check("seed encode", false); exit(1) }
realSuite.set(seededData, forKey: AppGroupWidgetSettings.storageKey)

let wrote = WidgetScope.apply(WidgetScope.buildPickerSelection(board: "hscc", project: nil, node: nil))
check("apply reports success on the real suite", wrote)
check("the widget's load() reads what the intent wrote",
      AppGroupWidgetSettings.load()?.scope == WidgetScope(board: "hscc"))
check("surfaces survive the scope write",
      AppGroupWidgetSettings.load()?.setting(for: .widgetCluster).refreshMinutes == 7)
if let raw = realSuite.data(forKey: AppGroupWidgetSettings.storageKey),
   let dict = try? JSONSerialization.jsonObject(with: raw) as? [String: Any],
   let scopeDict = dict["scope"] as? [String: Any] {
    check("stored JSON keys the scope under 'scope'/'board'", scopeDict["board"] as? String == "hscc")
} else {
    check("stored JSON keys the scope under 'scope'/'board'", false)
}

// 14. "All" stores ABSENCE: the blob carries scope null, never the sentinel.
let wroteAll = WidgetScope.apply(WidgetScope.buildPickerSelection(board: WidgetScope.wildcard,
                                                                  project: WidgetScope.wildcard,
                                                                  node: WidgetScope.wildcard))
check("apply of 'All' succeeds", wroteAll)
check("'All' reads back as nil", AppGroupWidgetSettings.load()?.scope == nil)
if let rawAll = realSuite.data(forKey: AppGroupWidgetSettings.storageKey),
   let dictAll = try? JSONSerialization.jsonObject(with: rawAll) as? [String: Any] {
    // Synthesized Codable drops an absent optional entirely, so "All" is
    // key-absence (or null) — never the sentinel: no "*" byte anywhere in
    // the blob a widget will read.
    check("'All' stores absence, not the sentinel",
          (dictAll["scope"] == nil || dictAll["scope"] is NSNull)
              && rawAll.firstRange(of: Data("*".utf8)) == nil)
} else {
    check("'All' stores absence, not the sentinel", false)
}

// 15. PER-WIDGET scope (t_3dd38411): the same blob now carries a choice per
//     surface. The three states are distinct and stay distinct on disk — key
//     absence means the surface INHERITS the fleet-wide scope, an empty scope
//     means the operator picked All for THAT surface (so it must NOT inherit),
//     a named scope scopes it. All three resolve through the widgets' own read
//     path, scope(for:), so the config screen and a timeline cannot disagree.
let carried = AppGroupWidgetSettings(surfaces: [
    .widgetCluster: WidgetSurfaceSettings(isActive: true, refreshMinutes: 5,
                                          showSections: ["state"], scope: WidgetScope(board: "radio")),
])
check("per-widget scope survives the bounded read path",
      carried.setting(for: .widgetCluster).scope == WidgetScope(board: "radio"))
check("a surface with no scope of its own reads nil, not an empty scope",
      carried.setting(for: .widgetMonitor).scope == nil)

let mixed = AppGroupWidgetSettings(
    surfaces: [
        .widgetCluster: WidgetSurfaceSettings(isActive: true, refreshMinutes: 5,
                                              showSections: ["state"], scope: WidgetScope(board: "radio")),
        .widgetMonitor: WidgetSurfaceSettings(isActive: true, refreshMinutes: 5,
                                              showSections: ["state"], scope: WidgetScope()),
        // A surface with NO choice of its own: absent key on disk, inherits.
        .liveActivityWake: WidgetSurfaceSettings(isActive: true, refreshMinutes: 15,
                                                 showSections: ["state"]),
    ],
    scope: WidgetScope(node: ".244"))
check("scope(for:) keeps the surface own board",
      mixed.scope(for: .widgetCluster) == WidgetScope(board: "radio"))
check("scope(for:) inherits the fleet default when the surface never chose",
      mixed.scope(for: .liveActivityWake) == WidgetScope(node: ".244"))
check("an explicit All on the surface does not inherit the fleet default",
      mixed.scope(for: .widgetMonitor) == WidgetScope() && (mixed.scope(for: .widgetMonitor)?.isAll ?? false))

// The on-disk shape of the two states that look alike: a never-chosen surface
// carries NO scope key, an explicit All carries an empty object. Hand-typed
// JSON would prove nothing about the shape this encoder writes — and the shape
// IS surprising: `surfaces` is keyed by an enum that is not a CodingKey, so
// Swift writes the FLAT alternating key/value array, not a JSON object. These
// assertions read that real array; a fixture that assumed an object would have
// been a silent false green.
func surfaceEntry(_ blob: [String: Any], _ rawKey: String) -> [String: Any]? {
    guard let flat = blob["surfaces"] as? [Any] else { return nil }
    var i = 0
    while i + 1 < flat.count {
        if let key = flat[i] as? String, key == rawKey, let value = flat[i + 1] as? [String: Any] {
            return value
        }
        i += 2
    }
    return nil
}
if let mixedData = try? JSONEncoder().encode(mixed),
   let mixedDict = try? JSONSerialization.jsonObject(with: mixedData) as? [String: Any] {
    check("the surfaces map is stored as the flat key/value array Swift writes",
          mixedDict["surfaces"] is [Any])
    let wakeEntry = surfaceEntry(mixedDict, "liveActivity.wake")
    check("a never-chosen surface stores no scope key", wakeEntry != nil && wakeEntry?["scope"] == nil)
    let monitorScope = surfaceEntry(mixedDict, "widget.monitor")?["scope"] as? [String: Any]
    check("an explicit All stores an empty scope object",
          monitorScope != nil && monitorScope?.isEmpty == true)
    let clusterScope = surfaceEntry(mixedDict, "widget.cluster")?["scope"] as? [String: Any]
    check("a per-widget scope is keyed inside that surface entry",
          clusterScope?["board"] as? String == "radio")
} else {
    check("the surfaces map is stored as the flat key/value array Swift writes", false)
    check("a never-chosen surface stores no scope key", false)
    check("an explicit All stores an empty scope object", false)
    check("a per-widget scope is keyed inside that surface entry", false)
}

// 16. END-TO-END through the app-side store the config screen drives. Same
//     REAL suite domain case 13 writes (the pairing is the claim), and the
//     same restore covers it: the screen's own write path — WidgetSettingsStore
//     .setScope, the only writer this card added — must land where the
//     extensions' load() + scope(for:) read, and must not disturb the fleet
//     default, the cadence or the sections riding in the same blob.
let perSurfaceSeed = AppGroupWidgetSettings(
    surfaces: [.widgetCluster: WidgetSurfaceSettings(isActive: true, refreshMinutes: 7,
                                                     showSections: ["state"])],
    scope: WidgetScope(board: "hscc"))
guard let perSurfaceSeedData = try? JSONEncoder().encode(perSurfaceSeed) else {
    check("per-widget seed encode", false); exit(1)
}
realSuite.set(perSurfaceSeedData, forKey: AppGroupWidgetSettings.storageKey)

let screen = WidgetSettingsStore()
check("the store reads the seeded fleet default as its own scope",
      screen.scope(for: .widgetCluster) == WidgetScope(board: "hscc"))
// The screen folds with the SAME static the configuration intent folds with —
// one rule for what a three-picker sheet means, two entry points.
screen.setScope(WidgetScope.buildPickerSelection(board: "radio", project: nil, node: nil),
                for: .widgetMonitor)
check("the screen per-widget write lands where the widgets read",
      AppGroupWidgetSettings.load()?.scope(for: .widgetMonitor) == WidgetScope(board: "radio"))
check("the fleet default survives a per-widget write",
      AppGroupWidgetSettings.load()?.scope == WidgetScope(board: "hscc"))
check("cadence survives a per-widget scope write",
      AppGroupWidgetSettings.load()?.setting(for: .widgetCluster).refreshMinutes == 7)

// 17. THE RACE THIS CARD HAD TO FIX. The configuration intent writes the
//     FLEET scope from the app process, and it can do that while the config
//     screen is open. A store that persisted its copy from launch would roll
//     that choice back with its next write — invisible, and the widgets would
//     silently go back to the old scope. This is the regression pin.
_ = WidgetScope.apply(WidgetScope(board: "intent-late"))
screen.setRefreshMinutes(11, for: .widgetCluster)
check("a fleet scope the intent wrote mid-session survives the screen write",
      AppGroupWidgetSettings.load()?.scope == WidgetScope(board: "intent-late"))
check("the screen write still landed",
      AppGroupWidgetSettings.load()?.setting(for: .widgetCluster).refreshMinutes == 11)
check("the per-widget scope still rides the same blob",
      AppGroupWidgetSettings.load()?.scope(for: .widgetMonitor) == WidgetScope(board: "radio"))

screen.setScope(WidgetScope(), for: .widgetMonitor)
check("an explicit All written by the screen stops the inheritance",
      AppGroupWidgetSettings.load()?.scope(for: .widgetMonitor)?.isAll ?? false)
screen.setScope(nil, for: .widgetMonitor)
check("dropping the surface choice re-inherits the fleet default",
      AppGroupWidgetSettings.load()?.scope(for: .widgetMonitor) == WidgetScope(board: "intent-late"))

// 18. restore the host's prior state — the gate must leave nothing behind.
if let priorBlob { realSuite.set(priorBlob, forKey: AppGroupWidgetSettings.storageKey) }
else { realSuite.removeObject(forKey: AppGroupWidgetSettings.storageKey) }
let restoredEqual = UserDefaults(suiteName: AppGroup.suiteName)!
    .data(forKey: AppGroupWidgetSettings.storageKey) == priorBlob
check("host suite restored", restoredEqual)

print("")
if failures.isEmpty {
    print("WIDGET SETTINGS CHECK PASSED — shared model round-trips, defaults backward compatible, suite persistence intact, intent scope lands where the widgets read, per-widget scope resolves through the same read path")
    exit(0)
} else {
    print("WIDGET SETTINGS CHECK FAILED — \(failures.count) assertion(s): \(failures.joined(separator: ", "))")
    exit(1)
}
SWIFT

swiftc -o "$WORK/bin" "$WORK/main.swift" \
  Sources/HSCC/WidgetSettingsStore.swift 2>&1 || {
  echo "compile failed" >&2; exit 1; }
"$WORK/bin"
rc=$?
rm -rf "$WORK"

# Structural checks on the COMMITTED intent file (t_98fb8726): the executed
# cases above prove fold+apply+load agree; these prove the intent delegates to
# exactly that code — a private copy in the intent would keep this gate green
# while the two sides drifted, so delegation itself is asserted. No network,
# no simulator: greps on source.
grepfail=0
gfail() { echo "  FAIL: $1"; grepfail=$((grepfail+1)); }
grepok() { grep -q -- "$2" "$INTENT" && echo "  ok: $1" || gfail "$1"; }
grepno() { grep -q -- "$2" "$INTENT" && gfail "$1" || echo "  ok: $1"; }

grepok  "the intent is a WidgetConfigurationIntent" "struct WidgetScopePickerIntent: WidgetConfigurationIntent"
grepok  "the intent folds through the shared static" "WidgetScope.buildPickerSelection(board:"
grepok  "the intent persists through the shared static" "WidgetScope.apply("
grepok  "the intent reads back through the widgets' load path" "AppGroupWidgetSettings.load()?.scope"
grepok  "the sheet has a Board parameter"      '@Parameter(title: "Board")'
grepok  "the sheet has a Project parameter"    '@Parameter(title: "Project")'
grepok  "the sheet has a Node parameter"       '@Parameter(title: "Node")'
[ "$(grep -c 'var \(board\|project\|node\): WidgetScopeOption?' "$INTENT")" -eq 3 ] \
  && echo "  ok: all three parameters are optional scope entities" \
  || gfail "all three parameters are optional scope entities"
[ "$(grep -c 'WidgetScopeOption(id: WidgetScope.wildcard)' "$INTENT")" -ge 4 ] \
  && echo "  ok: every picker can offer All without a network" \
  || gfail "every picker can offer All without a network"
grepok  "the picker data comes from the read-only client" "WidgetDataClient.makeIfConfigured()"
grepno  "the picker data does NOT use the app console client" "HSCCClient"
grepno  "the intent opens no UserDefaults of its own (one write path)" "UserDefaults"
grepok  "nodes are identified by the display label" "displayLabel"
grepno  "no node address enters the intent" "\.host\b"
# Structural checks on the COMMITTED config screen (t_3dd38411): the executed
# cases 15-17 above prove the per-widget write path works; these prove the
# SCREEN is the thing calling it. A private copy of the fold in the view, or a
# write to some other key, would keep every executed case green while the two
# sides drifted — exactly the failure the intent greps above guard against, one
# entry point later.
CONFIG="Sources/HSCC/Views/ConfigView.swift"
[ -f "$CONFIG" ] || { echo "config screen missing: $CONFIG" >&2; exit 1; }
cok() { grep -q -- "$2" "$CONFIG" && echo "  ok: $1" || gfail "$1"; }

cok "the screen writes the per-widget scope through the shared store" "store.setScope("
cok "the screen folds with the shared static, not a private rule" "WidgetScope.buildPickerSelection("
cok "the editor shows the RESOLVED scope, own choice first" "store.scope(for: kind)"
cok "the surface rows resolve through the same read path" "widgetSettings.scope(for: kind)"
cok "All for this widget is an explicit empty scope, not absence" "folded ?? WidgetScope()"
cok "following the fleet default is a real option, not just a sentence" "store.setScope(nil, for: kind)"
cok "the Board list comes from the sheet's own query" "WidgetScopeBoardQuery().suggestedEntities()"
cok "the Project list comes from the sheet's own query" "WidgetScopeProjectQuery().suggestedEntities()"
cok "the Node list comes from the sheet's own query" "WidgetScopeNodeQuery().suggestedEntities()"
if [ "$grepfail" -gt 0 ]; then
  echo "WIDGET SETTINGS CHECK FAILED — $grepfail intent + config-screen delegation assertion(s)"
  exit 1
fi
if [ "$rc" -ne 0 ]; then exit "$rc"; fi
echo "INTENT DELEGATION CHECK PASSED — the committed intent calls exactly the executed fold + apply, on the one storage key, with no second write path"
echo "CONFIG SCREEN CHECK PASSED — the screen writes through WidgetSettingsStore.setScope, folds with the shared static, reads through the widgets' own scope(for:), and populates its pickers from the sheet's own queries"
exit 0
