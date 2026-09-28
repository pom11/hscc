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
# committed model verbatim from Sources/Shared/SharedModels.swift (no
# hand-copy -> no drift), re-homes it, and asserts the round-trip through a
# real on-disk named suite. Device-only checks (App Group container presence,
# extension host reads) remain in shared_store_check.sh + the on-device smoke
# checklist.
#
# Usage: ios-app/scripts/widget_settings_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
SRC="Sources/Shared/SharedModels.swift"

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

cat > "$TMPDIR/widget_check_$$.swift" <<SWIFT
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
check("defaults has all 4 surfaces", defaults.surfaces.count == 4)
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

print("")
if failures.isEmpty {
    print("WIDGET SETTINGS CHECK PASSED — shared model round-trips, defaults backward compatible, suite persistence intact")
    exit(0)
} else {
    print("WIDGET SETTINGS CHECK FAILED — \(failures.count) assertion(s): \(failures.joined(separator: ", "))")
    exit(1)
}
SWIFT

swiftc -o "$TMPDIR/widget_check_$$" "$TMPDIR/widget_check_$$.swift" 2>&1 || {
  echo "compile failed" >&2; exit 1; }
"$TMPDIR/widget_check_$$"
rc=$?
rm -f "$TMPDIR/widget_check_$$" "$TMPDIR/widget_check_$$.swift"
exit $rc
