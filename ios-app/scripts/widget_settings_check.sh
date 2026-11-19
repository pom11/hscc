#!/bin/bash
set -uo pipefail
# ---- backtick ban (t_9e4a731b): the FIRST executable code, deliberately -----
# GUARD-SENTINEL names this block. The FIRST-EXEC marker on the next line names
# the real boundary: the ban cannot judge text above its own first statement.
SELF_SRC="${BASH_SOURCE[0]}"   # GUARD-FIRST-EXEC — captured before the cd below, or the path rots
BT="$(printf '\140')"          # the banned byte, so this file never has to carry one
bt_scan=$(grep -nF -- "$BT" "$SELF_SRC" 2>&1); bt_rc=$?
if [ "$bt_rc" -ne 1 ]; then
  if [ "$bt_rc" -eq 0 ]; then
    bt_count=$(printf '%s\n' "$bt_scan" | wc -l | tr -d ' ')
    echo "WIDGET SETTINGS CHECK FAILED — the file that is running ($SELF_SRC) contains $bt_count backtick character(s), listed with line numbers below. In this gate a backtick is command substitution: bash executes it while building the harness and splices shell into the Swift this gate then compiles and asserts on (measured on origin/main @ 3b5e475c: six of them spliced out of the generated harness, the balanced pairs silently). Write prose with quotes; to put a literal backtick into the generated harness interpolate \${BT}, which is inert because a shell never re-scans an expansion. Re-point this guard at the new text if this file moves — do not delete it, it is the only thing standing between the unquoted heredoc below and a gate that proves something other than what it says." >&2
    printf '%s\n' "$bt_scan" >&2
  else
    echo "WIDGET SETTINGS CHECK FAILED — the backtick guard could not read the file that is executing ($SELF_SRC, grep rc $bt_rc): $bt_scan. Refusing to run a gate whose guard could not look at it." >&2
  fi
  exit 1
fi

# ---- why the ban exists, and what it does not cover (t_9e4a731b) ------------
# The reasoning sits BELOW the ban rather than above it, because everything above
# the ban's first executable statement is text bash has already expanded by the
# time the guard can act on it. Documentation loses nothing by sitting here —
# the whole-file scan judges it either way — and the span the guard cannot judge
# shrinks to what the interpreter itself needs plus the marker pointer below.
#
# This gate writes its Swift harness with an UNQUOTED heredoc, deliberately:
# $suite_container and $model are shell-expanded into it, and $model is lifted
# verbatim out of Sources/Shared/SharedStore.swift — that extraction is the
# whole reason the harness cannot drift from the code it tests. Quoting the
# delimiter (<<'SWIFT') would kill the extraction, so it is not the fix.
#
# The price of an unquoted heredoc is that bash runs every backtick in it as a
# command substitution. It is wider than the heredoc, though, and that is the
# part a first fix misses: a backtick anywhere in this file that is not inside
# single quotes runs, whether or not it looks like prose. A '#' comment never
# expands (measured: a backticked touch inside one leaves no marker file), but a
# comment is only a comment while its line stays a comment, and a comment-SHAPED
# line ("// ...") sitting in shell position IS a command word. So the rule is
# the whole file: this gate carries NO BACKTICK CHARACTER AT ALL, in any context.
#
# What that rule is worth, measured against origin/main @ 3b5e475c: three
# "command not found" / "syntax error: unexpected end of file" lines on stderr on
# every single run, and SIX backticks spliced out of the harness this gate
# compiles and asserts on — the balanced pairs vanished silently, only the odd
# ones were reported. That is how a gate quietly changes what it proves while
# still printing ALL PASS: prose in a Swift comment became shell, and whatever
# the backticked words resolved to is what got compiled.
#
# To emit a literal backtick INTO the generated harness, interpolate ${BT}
# (defined above) instead of typing one — braces, because $BTdefaults would be
# read as one variable name. A shell never re-scans the result of an expansion,
# so ${BT} is inert text — which is precisely why it is safe here and a
# typed backtick is not. The three Swift comments that need one use ${BT}, and the
# harness they generate is byte-for-byte what it was before this rule existed.
#
# The limit, stated instead of hidden: this guard can only judge the text at or
# below GUARD-FIRST-EXEC. Lines above it (line 1 is the shebang; line 2 is the
# set line) are outside its reach, and anyone who can edit there can also delete the
# guard. scripts/widget_settings_check_backtick_probe.sh measures that span from
# the marker on this file and prints it every run — deliberately no figure is
# written into prose here, because a number in prose is a number that can lag the
# thing it describes, and lagging is what this card is about. The real fix for
# any span that remains is a meta-gate scanning every script in this directory,
# not a longer comment in the file being judged.
#
# Fail closed: grep rc 1 means clean, rc 0 means the byte was found (refuse),
# rc 2 or more means it could not read the file it is judging (refuse). A guard
# that waves the run through because it failed to look is not a guard.


# ---- what this gate proves -------------------------------------------------
# widget_settings_check.sh — prove the Widgets & Live Activities shared
# settings model (t_dea36c48) round-trips: encode/decode, defaulting, bounded
# sections, and persistence through a REAL named UserDefaults suite.
#
# Why this exists: acceptance criterion #1 is "settings persist round-trip
# through the App Group on the DEVICE (set -> relaunch -> still set, readable
# in the extension target)". A physical device is not reachable from this
# build host, and 'containerURL(forSecurityApplicationGroupIdentifier:)'
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
#        WSC_UPDATE_GOLDEN=1 ios-app/scripts/widget_settings_check.sh
#          -> refreshes scripts/widget_settings_check.harness.golden after the
#             harness is regenerated. Do that deliberately (and commit the
#             golden in the same change) whenever a change to the extracted
#             model block or to this gate's harness is INTENDED; the gate names
#             this line when the two drift apart.
# Resolved BEFORE the cd below: the script then runs from the ios-app root, so a
# path handed to it by the caller ("./ios-app/scripts/widget_settings_check.sh")
# stops resolving. The interpolation guard has to read this very file, so it gets
# a path that cannot rot — which is also what makes a copied-aside probe of the
# gate still probe the right text.
SELF_DIR="$(cd "$(dirname "$SELF_SRC")" && pwd)"
cd "${SELF_DIR}/.."
SRC="Sources/Shared/SharedStore.swift"
INTENT="Sources/HSCC/Intents/WidgetScopePickerIntent.swift"

# t_98fb8726 extended this gate in two layers: the executed cases 8-17 below
# run the REAL 'WidgetScope.buildPickerSelection' + 'apply' (extracted with
# this same verbatim mechanism, since they live on the shared model the intent
# delegates to) against the REAL suite domain — proving the configuration
# intent's write lands exactly where 'AppGroupWidgetSettings.load()' reads —
# and the structural greps at the foot of the script prove the committed
# intent file really calls those statics (delegation, not a private copy) and
# never opens a UserDefaults handle of its own.
#
# t_3dd38411 added the PER-WIDGET layer: a scope per surface entry inside this
# same blob (cases 15-17). Case 16-17 drive the real app-side
# 'Sources/HSCC/WidgetSettingsStore.swift' — compiled in, not copied — because
# that is the only writer the config screen has, and case 17 pins the race
# where a fleet-wide scope written by the intent while the screen is open used
# to be rolled back by the screen's next write.
[ -f "$INTENT" ] || { echo "intent file missing: $INTENT" >&2; exit 1; }

# Extract the model block verbatim (WidgetSurfaceKind .. end of the availableSections
# extension). Line-anchored on stable markers; no hand-copy -> no drift.
start=$(grep -n "^// Widgets & Live Activities shared settings" "$SRC" | head -1 | cut -d: -f1)
end=$(grep -n "^/// A lightweight read of the token" "$SRC" | head -1 | cut -d: -f1)
model=$(sed -n "${start},$((end-1))p" "$SRC")

# The storage key comes from the real AppGroup enum (verbatim). The suite no
# longer exists there as a literal: SharedStore reads HSCCAppGroup from the
# target's Info.plist, derived from project.yml's one HSCC_BUNDLE_PREFIX.
# Reconstruct the SAME default for this bare executable (it has no Info.plist)
# from that one place, so the harness still exercises the shipped suite domain.
prefix=$(sed -n 's/^ *HSCC_BUNDLE_PREFIX: *//p' project.yml | head -1)
[ -n "$prefix" ] || { echo "widget settings check: no HSCC_BUNDLE_PREFIX in project.yml" >&2; exit 1; }
suite="static let suiteName = \"group.${prefix}.ios\""
wkey=$(grep "static let widgetSettingsKey" "$SRC" | sed 's/^ *//')
suite_container="enum AppGroup {
$suite
$wkey
}"

# (the interpolation guard for this file lives at the top of it, t_9e4a731b)

WORK="${TMPDIR:-/tmp}/widget_check_$$"
mkdir -p "$WORK"
# Byte-stability reference for the generated harness (t_9e4a731b).
# "harness" below must equal this file byte for byte. It is a DRIFT check, and
# that is the honest scope of it: the harness is assembled from $model, $suite
# and $wkey, all lifted out of Sources/ at run time, so anything that moves in
# those inputs — or in this file's own heredoc body — moves these bytes. A
# harness whose bytes moved is a harness whose ASSERTIONS moved, which is what
# makes the comparison worth having. It is NOT a backtick detector: the harness
# legitimately carries backticks (Swift doc comments inside the extracted model
# text use them), and a backtick arriving through an expansion is inert text
# anyway — a shell never re-scans an expansion. Backticks are settled once, at
# the top of this file, before any of this runs.
GOLDEN="scripts/widget_settings_check.harness.golden"
# The harness is top-level code, so it MUST be named main.swift: with the real
# Sources/HSCC/WidgetSettingsStore.swift compiled alongside it (case 16 drives
# that store rather than a copy of it), with a second file in the module Swift
# rejects top-level statements in a file that is not main.swift.
#
# QUOTING — settled by main (t_9e4a731b), and this branch's alternative is
# deliberately DROPPED. The heredoc stays UNQUOTED because $suite_container and
# $model must be expanded into it, and $model is lifted verbatim out of
# Sources/Shared/SharedStore.swift — that extraction is the whole reason the
# harness cannot drift from the code it tests. A backtick arriving through an
# expansion is inert text (a shell never re-scans an expansion), and the
# committed reference proves it rather than asserts it:
# scripts/widget_settings_check.harness.golden carries backticked lines taken
# straight out of that model block, intact. The splices this gate used to suffer
# came from backticks typed in THIS file's own prose, which is exactly what the
# ban at the head of the file refuses. t_71deac83 first treated the symptom by
# routing $model through printf inside two quoted heredocs; that also froze the
# body out of ${BT} interpolation and duplicated the rule in a narrower place, so
# one mechanism remains: the ban plus the golden.
cat > "$WORK/main.swift" <<SWIFT
import Foundation

// Minimal real shim: the suite name reconstructed from project.yml's one
// HSCC_BUNDLE_PREFIX (what Info.plist carries in a real build), plus the
// storage key extracted verbatim from the committed AppGroup enum.
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
// ${BT}defaults.surfaces.count == 4${BT}, which every new surface turned red — a
// healthy gate failing on a correct change, which trains everyone to expect
// noise here. ${BT}defaults()${BT} is built FROM allCases, so what this case must
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

// 1a. The registry's own invariants (t_71deac83 added a surface; these are the
//     properties a new surface must not break, asserted over every case rather
//     than over a transcription of one).
var registryKeys: [String] = []
for kind in WidgetSurfaceKind.allCases { registryKeys.append(kind.rawValue) }
var namespacedCount = 0
for key in registryKeys where key.hasPrefix("widget.") || key.hasPrefix("liveActivity.") {
    namespacedCount += 1
}
let noEntries = AppGroupWidgetSettings(surfaces: [:])
check("no two surfaces share a registry key (one toggle, one surface)",
      Set(registryKeys).count == registryKeys.count)
check("every registry key is namespaced by surface kind",
      namespacedCount == registryKeys.count)
// A surface with NO sections is a real answer (one dataset, nothing optional),
// not a gap the editor has to paper over: the bounded read path must keep the
// set empty rather than invent one.
check("a surface with no sections stays section-less through the bounded path",
      defaults.setting(for: .liveActivityCards).showSections.isEmpty)
check("the running-cards surface is push-fed (15-min default)",
      WidgetSurfaceKind.liveActivityCards.isLiveActivity
        && noEntries.setting(for: .liveActivityCards).refreshMinutes == 15)
check("the running-cards registry key is stable",
      WidgetSurfaceKind.liveActivityCards.rawValue == "liveActivity.cards")

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

// 7. registry raw values are the stable keys the siblings consume. A raw value
//    IS the App Group key: renaming one silently orphans whatever the operator
//    already set on an installed device, so each one is pinned to its literal.
//    (The t_fa7926fb pair is the reason this list exists at all — a permanent
//    identifier chosen in a card body deserves one assertion that says so.)
check("widget.cluster registry key", WidgetSurfaceKind.widgetCluster.rawValue == "widget.cluster")
check("liveActivity.monitor registry key", WidgetSurfaceKind.liveActivityMonitor.rawValue == "liveActivity.monitor")
check("widget.cardsRunning registry key", WidgetSurfaceKind.widgetCardsRunning.rawValue == "widget.cardsRunning")
check("widget.kanbanMonitor registry key", WidgetSurfaceKind.widgetKanbanMonitor.rawValue == "widget.kanbanMonitor")

// 7b. The board status surface (t_09cd8abf): the key itself, the two settings
//     this entry exists for (pause + cadence) surviving the encode/decode the
//     extension actually performs, and the honest default for an install that
//     has never seen this surface (ON at the widget cadence — a new registry
//     key must never silently pause a widget that is on the Home Screen).
check("widget.boardStatus registry key",
      WidgetSurfaceKind.widgetBoardStatus.rawValue == "widget.boardStatus")
check("board status is a widget (self-fetching, 5-min default)",
      !WidgetSurfaceKind.widgetBoardStatus.isLiveActivity)
check("board status declares no optional content section",
      WidgetSurfaceKind.widgetBoardStatus.availableSections.isEmpty)
let pausedBoard = AppGroupWidgetSettings(surfaces: [
    .widgetBoardStatus: WidgetSurfaceSettings(isActive: false, refreshMinutes: 0, showSections: [])
])
if let boardData = try? JSONEncoder().encode(pausedBoard),
   let boardBack = try? JSONDecoder().decode(AppGroupWidgetSettings.self, from: boardData) {
    check("paused board status reads back paused",
          boardBack.setting(for: .widgetBoardStatus).isActive == false)
    check("board status Off cadence stays 0 (the provider maps 0 to atEnd, not a loop)",
          boardBack.setting(for: .widgetBoardStatus).refreshMinutes == 0)
} else {
    check("paused board status encodes and decodes", false)
}
let untouchedBoard = AppGroupWidgetSettings(surfaces: [:]).setting(for: .widgetBoardStatus)
check("board status with no stored entry defaults ON", untouchedBoard.isActive)
check("board status with no stored entry defaults 5 min", untouchedBoard.refreshMinutes == 5)

// 7c. the two surfaces t_fa7926fb added (running cards, kanban monitor) behave
//     like the widgets they are. First half: the rule that keeps a fresh install
//     rendering — an absent entry means "the settings screen was never opened",
//     NEVER "paused" — and the seed a brand-new store gets must not pause them
//     either. ${BT}AppGroupWidgetSettings.defaults()${BT} DERIVES from ${BT}isLiveActivity${BT}
//     and ${BT}availableSections${BT}, so this is the executed proof that the pair
//     landed in the 5-minute widget branch and not the 15-minute Live Activity
//     one; a wrong branch there is a widget that refreshes three times too
//     slowly on every device, quietly.
for kind in [WidgetSurfaceKind.widgetCardsRunning, .widgetKanbanMonitor] {
    let s = AppGroupWidgetSettings(surfaces: [:]).setting(for: kind)
    check("\(kind.rawValue) with no entry is ON (absent is not paused)", s.isActive)
    check("\(kind.rawValue) with no entry takes the 5-min widget cadence",
          s.refreshMinutes == 5)
    let seeded = AppGroupWidgetSettings.defaults().setting(for: kind)
    check("\(kind.rawValue) is ON at the widget cadence in the first-launch seed",
          seeded.isActive && seeded.refreshMinutes == 5)
    check("\(kind.rawValue) is not a Live Activity", !kind.isLiveActivity)
    check("\(kind.rawValue) declares no sections (one dataset, nothing optional)",
          kind.availableSections.isEmpty)
    check("\(kind.rawValue)'s seed carries no sections either", seeded.showSections.isEmpty)
}
//     Second half: the pause itself, through the same bounding pass every widget
//     read goes through — OFF must survive, the cadence must ride along with it,
//     and a surface that declares no sections must end up with NONE even when a
//     foreign blob claims otherwise (${BT}showSections: ["state"]${BT} below is stripped
//     because ${BT}availableSections${BT} is empty — the consumer of the ruling written
//     in ${BT}SharedStore${BT}).
let pausedPair = AppGroupWidgetSettings(surfaces: [
    .widgetCardsRunning: WidgetSurfaceSettings(isActive: false, refreshMinutes: 60,
                                               showSections: ["state"]),
    .widgetKanbanMonitor: WidgetSurfaceSettings(isActive: false, refreshMinutes: 0,
                                                showSections: []),
])
let pausedCards = pausedPair.setting(for: .widgetCardsRunning)
check("paused running-cards stays paused through setting(for:)", !pausedCards.isActive)
check("paused running-cards keeps the cadence the operator chose", pausedCards.refreshMinutes == 60)
check("running-cards honours its empty section declaration", pausedCards.showSections.isEmpty)
check("kanban monitor Off (0) survives alongside the pause",
      pausedPair.setting(for: .widgetKanbanMonitor).refreshMinutes == 0)

// 7d. the projects surface (t_5f246177), the LAST widget in HSCCWidgets to join
//     this registry. Same three claims as 7c, asserted separately because this
//     raw value is the one that gets written to an installed device the moment
//     the operator touches its row — and because the failure it guards against is
//     specific to a NEW key: setting(for:) fills an absent surface with ON, so
//     adding the key cannot blank a widget already on the Home Screen of an
//     installed device, and if that ever stopped being true the widget would
//     silently stop drawing projects on every device that never opened Settings.
//     (No backticks in this heredoc body on purpose — the heredoc is UNQUOTED, so
//     bash command-substitutes a backtick pair instead of leaving it in the Swift,
//     which mangles the line and prints a command-not-found nobody reads.)
check("widget.projects registry key (permanent App Group key)",
      WidgetSurfaceKind.widgetProjects.rawValue == "widget.projects")
check("projects is a widget (self-fetching, 5-min default)",
      !WidgetSurfaceKind.widgetProjects.isLiveActivity)
check("projects declares no optional content section",
      WidgetSurfaceKind.widgetProjects.availableSections.isEmpty)
let projectsAbsent = AppGroupWidgetSettings(surfaces: [:]).setting(for: .widgetProjects)
check("widget.projects with no entry is ON (absent is not paused)",
      projectsAbsent.isActive)
check("widget.projects with no entry takes the 5-min widget cadence",
      projectsAbsent.refreshMinutes == 5)
let projectsSeeded = AppGroupWidgetSettings.defaults().setting(for: .widgetProjects)
check("widget.projects is ON at the widget cadence in the first-launch seed",
      projectsSeeded.isActive && projectsSeeded.refreshMinutes == 5)
check("the seed for widget.projects carries no sections either",
      projectsSeeded.showSections.isEmpty)
let pausedProjects = AppGroupWidgetSettings(surfaces: [
    .widgetProjects: WidgetSurfaceSettings(isActive: false, refreshMinutes: 0,
                                           showSections: ["state", "board"])
]).setting(for: .widgetProjects)
check("paused projects stays paused through the bounding read the widget performs",
      !pausedProjects.isActive)
check("widget.projects Off (0) survives alongside the pause (the provider maps it to atEnd)",
      pausedProjects.refreshMinutes == 0)
check("widget.projects honours its empty section declaration against a foreign blob",
      pausedProjects.showSections.isEmpty)

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
// IS surprising: ${BT}surfaces${BT} is keyed by an enum that is not a CodingKey, so
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

// 18. APPEARANCE (t_444519ae): the stored style model. Old blobs must decode
//     to the app Theme default with no migration prompt; a per-surface
//     override must beat the global style; dropping the override must
//     re-inherit; and a hex colour must survive its canonical round-trip.
let themeDefault = WidgetStyle()
let legacyStyleBlob = AppGroupWidgetSettings(surfaces: [
    .widgetCluster: WidgetSurfaceSettings(isActive: true, refreshMinutes: 5,
                                          showSections: ["state"]),
])
if let oldStyleData = try? JSONEncoder().encode(legacyStyleBlob) {
    check("an old blob carries no style key",
          oldStyleData.firstRange(of: Data("\"style\"".utf8)) == nil)
    if let oldStyle = try? JSONDecoder().decode(AppGroupWidgetSettings.self, from: oldStyleData) {
        check("old data decodes with no global style (Theme default)",
              oldStyle.style == nil && oldStyle.resolvedStyle(for: .widgetCluster) == themeDefault)
        check("old data's untouched surface also resolves to the Theme default",
              oldStyle.resolvedStyle(for: .widgetMonitor) == themeDefault)
    } else {
        check("old data decodes with no global style (Theme default)", false)
        check("old data's untouched surface also resolves to the Theme default", false)
    }
} else {
    check("an old blob carries no style key", false)
    check("old data decodes with no global style (Theme default)", false)
    check("old data's untouched surface also resolves to the Theme default", false)
}

if let orange = WidgetColor(hex: "#ff5722") {
    check("6-digit hex gains an opaque alpha", orange.hex == "#FF5722FF")
    check("hex round-trips through its canonical form", WidgetColor(hex: orange.hex) == orange)
    check("hex colour round-trips through Codable",
          (try? JSONDecoder().decode(WidgetColor.self, from: JSONEncoder().encode(orange))) == orange)
} else {
    check("6-digit hex gains an opaque alpha", false)
    check("hex round-trips through its canonical form", false)
    check("hex colour round-trips through Codable", false)
}
if let translucent = WidgetColor(hex: "#11223380") {
    check("8-digit hex keeps its alpha", translucent.hex == "#11223380"
          && abs(translucent.alpha - 128.0 / 255.0) < 0.0001)
    check("transparent is a real stored colour", WidgetColor.transparent.hex == "#00000000")
} else {
    check("8-digit hex keeps its alpha", false)
    check("transparent is a real stored colour", false)
}
check("invalid hex is rejected", WidgetColor(hex: "#GGGGGG") == nil)
check("a partial style decodes missing fields to the Theme default",
      (try? JSONDecoder().decode(WidgetStyle.self, from: Data("{}".utf8))) == themeDefault)

let globalStyle = WidgetStyle.presets.first(where: { preset in preset.id == "highContrast" })!.style
let surfaceStyle = WidgetStyle.presets.first(where: { preset in preset.id == "monoTerminal" })!.style
let styled = AppGroupWidgetSettings(surfaces: [
    .widgetCluster: WidgetSurfaceSettings(isActive: true, refreshMinutes: 5,
                                          showSections: ["state"], style: surfaceStyle),
    .widgetMonitor: WidgetSurfaceSettings(isActive: true, refreshMinutes: 5,
                                          showSections: ["state"]),
], style: globalStyle)
check("per-surface override beats the global style",
      styled.resolvedStyle(for: .widgetCluster) == surfaceStyle)
check("a surface with no override inherits the global style",
      styled.resolvedStyle(for: .widgetMonitor) == globalStyle)
check("style(for:) reports only a real override",
      styled.style(for: .widgetCluster) == surfaceStyle
      && styled.style(for: .widgetMonitor) == nil)
var droppedOverride = styled
droppedOverride.surfaces[.widgetCluster] = WidgetSurfaceSettings(
    isActive: true, refreshMinutes: 5, showSections: ["state"])
check("Use global (no override) falls back to the global style",
      droppedOverride.resolvedStyle(for: .widgetCluster) == globalStyle
      && droppedOverride.style(for: .widgetCluster) == nil)
check("no global style at all resolves to the Theme default",
      AppGroupWidgetSettings(surfaces: [:]).resolvedStyle(for: .widgetMonitor) == themeDefault)

// The app-side write path the editor drives, against the REAL suite domain.
let appearanceSeed = AppGroupWidgetSettings(surfaces: [:], style: globalStyle)
if let appearanceData = try? JSONEncoder().encode(appearanceSeed) {
    realSuite.set(appearanceData, forKey: AppGroupWidgetSettings.storageKey)
}
let appearanceScreen = WidgetSettingsStore()
check("the store reads the global style", appearanceScreen.globalStyle == globalStyle)
appearanceScreen.setStyle(surfaceStyle, for: .widgetMonitor)
check("a per-surface style write lands where the widgets read",
      AppGroupWidgetSettings.load()?.style(for: .widgetMonitor) == surfaceStyle)
check("the store resolves the override", appearanceScreen.resolvedStyle(for: .widgetMonitor) == surfaceStyle)
appearanceScreen.useGlobalStyle(for: .widgetMonitor)
check("Use global drops the override and re-inherits",
      AppGroupWidgetSettings.load()?.style(for: .widgetMonitor) == nil
      && appearanceScreen.resolvedStyle(for: .widgetMonitor) == globalStyle)
appearanceScreen.setGlobalStyle(surfaceStyle)
check("a global style write persists to the shared blob",
      AppGroupWidgetSettings.load()?.resolvedStyle(for: .widgetMonitor) == surfaceStyle)

// 19. restore the host's prior state — the gate must leave nothing behind.
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

# Compile first, then judge the harness, and only then RUN it. The order matters:
# the harness prints its own "WIDGET SETTINGS CHECK PASSED" banner, so an
# integrity refusal that happens after the run would print that banner on its way
# to exit 1 — a red gate whose stdout reads like a green one.
swiftc -o "$WORK/bin" "$WORK/main.swift" \
  Sources/HSCC/WidgetSettingsStore.swift 2>&1 || {
  echo "compile failed" >&2; exit 1; }

harness="$WORK/main.swift"
integrityfail=0
# Refresh first, then assert: a refresh on purpose must not leave the run red.
# Deliberately a "note:" line and not an "ok:" one — the assertion count this
# gate documents is what a plain run prints, and refresh mode must not move it.
if [ "${WSC_UPDATE_GOLDEN:-}" = "1" ]; then
  cp "$harness" "$GOLDEN" && echo "  note: harness reference refreshed (WSC_UPDATE_GOLDEN=1) — commit scripts/widget_settings_check.harness.golden with this change"
fi
# Byte-stability of the generated harness — the byte-stability acceptance item.
if ! cmp -s "$harness" "$GOLDEN" 2>/dev/null; then
  # Either the gate's generated harness moved, or the gate itself did. A harness
  # whose bytes moved is a harness whose ASSERTIONS moved: this gate extracts
  # $model verbatim from Sources/Shared/SharedStore.swift, so an unintended
  # splice (a backtick, a stray $, a marker that slid) shows up HERE even when
  # every one of the assertions would still print ok.
  echo "WIDGET SETTINGS CHECK FAILED — the generated harness is not byte-identical to $GOLDEN." >&2
  diff -u "$GOLDEN" "$harness" 2>&1 | sed -n -e '1,80p' >&2
  echo "If the drift is INTENDED (you changed the model block, the AppGroup key/suite, or this gate's harness), review the diff above, then refresh the reference on purpose: WSC_UPDATE_GOLDEN=1 ios-app/scripts/widget_settings_check.sh && git add $GOLDEN" >&2
  integrityfail=$((integrityfail + 1))
fi
if [ "$integrityfail" -gt 0 ]; then
  echo "WIDGET SETTINGS CHECK FAILED — $integrityfail harness-integrity assertion(s), before the harness was run" >&2
  rm -rf "$WORK"
  exit 1
fi

"$WORK/bin"
rc=$?
# $WORK keeps the generated harness, and the checks above name it when they
# fail — so it is removed at the foot of the script, not here.

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
# t_09cd8abf: a surface with ONE dataset declares no content sections, and the
# editor must then show no "Show Content" group at all — an empty group with a
# "choose which sections" footer would advertise a choice that does not exist.
cok "a surface with no sections shows no content group" "if !sectionRows(kind).isEmpty"
# t_71deac83 added a surface whose routes cannot be scoped: "/v1/kanban/running"
# and "/v1/kanban/blocked" were measured to ignore "?board=" (§4.3, and the
# no-backtick rule at the head of this file is why these paths are in quotes
# rather than in the repo's usual code spans). Offering such a surface the scope
# pickers would write a value the surface ignores and print a board name under a
# row that always shows the whole fleet. Both guards read the surface's own
# metadata rather than naming a case, so a surface that later gains a scopeable
# route still gets its pickers.
cok "the scope pickers appear only where the route honours a scope" 'if kind.supportsScope {'
cok "a surface that cannot be scoped says so instead of printing a scope" 'guard kind.supportsScope else'
cok "the screen renders one row per registry surface" "ForEach(WidgetSurfaceKind.allCases"
cok "every surface row opens the one shared editor" "WidgetSurfaceEditor(kind: kind, store: widgetSettings)"
cok "the shared editor offers the pause toggle" 'Toggle("Active", isOn: Binding('
cok "the shared editor offers the refresh cadence picker" 'Picker("Refresh", selection: Binding('
# t_444519ae: the Appearance editor is a real row on the settings screen, and
# its writes go through the shared store (not a second App Group key).
cok "the settings screen links to the appearance editor" "WidgetAppearanceView()"
cok "the settings screen has an Appearance section" 'Text("Appearance")'

APPEARANCE="Sources/HSCC/Views/WidgetAppearanceView.swift"
[ -f "$APPEARANCE" ] || { echo "appearance editor missing: $APPEARANCE" >&2; exit 1; }
aok() { grep -q -- "$2" "$APPEARANCE" && echo "  ok: $1" || gfail "$1"; }
aok "the appearance editor writes the global style through the store" "store.setGlobalStyle("
aok "the per-surface editor offers Use global" 'Toggle("Use global style"'
aok "Use global drops the override instead of copying the style" "store.useGlobalStyle(for: kind)"
aok "the editor offers ColorPickers" "ColorPicker("
aok "the editor offers the model's presets" "WidgetStyle.presets"
aok "the editor has a live preview" "WidgetStylePreview("
aok "the preview draws a small, medium and lock-screen shape" "lockScreen"
grep -q "WidgetCenter.shared.reloadAllTimelines()" Sources/HSCC/WidgetSettingsStore.swift \
  && echo "  ok: every style write reloads WidgetKit timelines" \
  || gfail "every style write reloads WidgetKit timelines"
grep -q "func widgetStyle(_ style: WidgetStyle)" Sources/HSCC/Views/Theme.swift \
  && echo "  ok: the shared SwiftUI style modifier part 2 applies exists" \
  || gfail "the shared SwiftUI style modifier part 2 applies exists"

# ---- macOS Settings + desktop target (t_1143c19b) --------------------------
# The Appearance editor and the registry store are compiled into the macOS app
# BY PATH (not copied), and every settings write asks WidgetKit to rebuild the
# desktop timelines. The widget extension itself is covered by the surface
# checks above; these two blocks prove the macOS app is wired to the same code
# and that the target exists with the shared widgets in it.
MAC_APP="../macos-app"
MAC_SETTINGS="$MAC_APP/Sources/HSCCMac/MacSettingsView.swift"
MAC_SPEC="$MAC_APP/project.yml"
if [ -f "$MAC_SETTINGS" ] && [ -f "$MAC_SPEC" ]; then
  grep -q "WidgetAppearanceView()" "$MAC_SETTINGS" \
    && echo "  ok: the macOS Settings window embeds the shared Appearance editor" \
    || gfail "the macOS Settings window embeds the shared Appearance editor"
  grep -q "WidgetCenter.shared.reloadAllTimelines()" "$MAC_SETTINGS" \
    && echo "  ok: macOS settings writes reload desktop widget timelines" \
    || gfail "macOS settings writes reload desktop widget timelines"
  for reused in WidgetSettingsStore.swift Views/WidgetAppearanceView.swift Views/WidgetSurfaceLabels.swift; do
    grep -qF -- "- path: ../ios-app/Sources/HSCC/$reused" "$MAC_SPEC" \
      && echo "  ok: macOS app compiles $reused by path" \
      || gfail "macOS app does not compile $reused by path"
  done
  if grep -q '^  HSCCMacWidgets:' "$MAC_SPEC" \
     && grep -qF -- "- path: ../ios-app/Sources/HSCCWidgets/WidgetRendering.swift" "$MAC_SPEC"; then
    echo "  ok: HSCCMacWidgets target exists and compiles the shared rendering adapter"
  else
    gfail "HSCCMacWidgets target or its shared rendering adapter is missing"
  fi
else
  gfail "macOS app/settings or project spec is missing — desktop widget Settings cannot be covered"
fi

# ---- surface coverage (t_2b3084bb) -----------------------------------------
# The rows above are driven by WidgetSurfaceKind.allCases, so a surface whose
# kind never reaches that enum is the one way a shipped widget / Live Activity
# can be ABSENT from Settings. These checks derive the surfaces from the code
# that actually ships them — the widget bundle's Widget list and the Live
# Activity extensions' ActivityConfiguration declarations — and require each to
# have a registry case plus its display metadata in the screen. A new widget or
# activity added without a Settings row therefore reddens this gate instead of
# shipping a settings screen that silently omits it.
sfail() { echo "  FAIL: $1"; grepfail=$((grepfail+1)); }
sok() { echo "  ok: $1"; }
norm_list() { printf '%s\n' "$1" | sed '/^$/d' | sort; }

surface_cases=$(sed -n '/^enum WidgetSurfaceKind: String, CaseIterable, Codable {/,/^}/p' "$SRC" \
  | sed -n 's/^    case \([A-Za-z][A-Za-z0-9]*\).*/\1/p')
surface_case_count=$(printf '%s\n' "$surface_cases" | sed '/^$/d' | wc -l | tr -d ' ')
if [ "$surface_case_count" -lt 1 ]; then
  sfail "the registry enum could not be read from $SRC"
fi

# One row per case is not enough if the row has no name or icon: the shared
# editor title and the list row both read displayName/displayIcon, and each
# case must be present in both switches. The switches moved out of ConfigView
# into WidgetSurfaceLabels.swift (t_1143c19b) so the macOS Settings window
# compiles the same metadata instead of a second copy.
META="Sources/HSCC/Views/WidgetSurfaceLabels.swift"
[ -f "$META" ] || { echo "surface display metadata missing: $META" >&2; exit 1; }
meta_bad=0
for c in $surface_cases; do
  arms=$(grep -c "case \.$c:" "$META")
  if [ "$arms" -lt 2 ]; then
    sfail "surface $c has $arms displayName/displayIcon arm(s), expected 2"
    meta_bad=$((meta_bad + 1))
  fi
done
[ "$meta_bad" -eq 0 ] && sok "all $surface_case_count registry surfaces have a display name and icon"

# Widgets: read the bundle, then each widget's own registry read. The read is
# what proves the row's pause/cadence controls reach the surface rather than a
# second switch the screen does not use.
WIDGETS_DIR="Sources/HSCCWidgets"
WIDGET_BUNDLE="$WIDGETS_DIR/HSCCWidgetsBundle.swift"
widget_names=$(sed -n '/^struct HSCCWidgetsBundle: WidgetBundle {/,/^}/p' "$WIDGET_BUNDLE" \
  | sed -n 's/^        \([A-Za-z][A-Za-z0-9]*\)()$/\1/p')
widget_count=$(printf '%s\n' "$widget_names" | sed '/^$/d' | wc -l | tr -d ' ')
sok "the widget bundle declares $widget_count widget surface(s)"
# Bundle type -> registry case. Explicit because a widget can keep its struct
# and its provider in different files (ClusterWidget does), so the name is the
# stable link. A new bundle entry with no line here fails closed.
widget_kind_for() {
  case "$1" in
    ClusterWidget) echo widgetCluster ;;
    MonitorWidget) echo widgetMonitor ;;
    CardsRunningWidget) echo widgetCardsRunning ;;
    KanbanMonitorWidget) echo widgetKanbanMonitor ;;
    ProjectsWidget) echo widgetProjects ;;
    BoardStatusWidget) echo widgetBoardStatus ;;
    NeedsMeWidget) echo widgetNeedsMe ;;
    *) echo "" ;;
  esac
}
used_widget_kinds=""
for w in $widget_names; do
  if ! grep -rq "^struct $w: Widget" "$WIDGETS_DIR"; then
    sfail "widget $w is in the bundle but its struct is not in $WIDGETS_DIR"
    continue
  fi
  kind=$(widget_kind_for "$w")
  if [ -z "$kind" ]; then
    sfail "widget $w has no registry case, so the settings screen cannot list it"
    continue
  fi
  printf '%s\n' "$surface_cases" | grep -qx "$kind" \
    || sfail "widget $w maps to $kind, which is not a WidgetSurfaceKind case"
  grep -rq "setting(for: .$kind)" "$WIDGETS_DIR" \
    || sfail "widget $w never reads $kind, so its row cannot pause or pace it"
  used_widget_kinds="$used_widget_kinds$kind
"
  sok "widget $w is wired to $kind"
done
registered_widget_kinds=$(printf '%s\n' "$surface_cases" | while IFS= read -r c; do
  grep -q "^    case $c = \"widget\." "$SRC" && printf '%s\n' "$c"
done)
if [ "$(norm_list "$used_widget_kinds")" != "$(norm_list "$registered_widget_kinds")" ]; then
  sfail "the widget cases in the registry and the widgets in the bundle are not the same set"
else
  sok "every widget case has a widget and every widget has a widget case"
fi

# Live Activities: the extension targets are the shipped surface list. The
# mapping to registry cases is explicit because an attributes type carries no
# raw key of its own; a new ActivityConfiguration with no line here fails
# closed rather than being skipped.
la_case_for() {
  case "$1" in
    WakingActivityAttributes) echo liveActivityWake ;;
    MonitorActivityAttributes) echo liveActivityMonitor ;;
    ClusterHealthActivityAttributes) echo liveActivityClusterHealth ;;
    CardsRunningActivityAttributes) echo liveActivityCards ;;
    BoardProgressActivityAttributes) echo liveActivityBoardProgress ;;
    *) echo "" ;;
  esac
}
la_types=$(grep -rhoE 'ActivityConfiguration\(for: [A-Za-z][A-Za-z0-9]*\.self\)' \
  Sources/HSCCLiveActivity Sources/HSCCLiveActivityMonitor \
  | sed -E 's/.*for: ([A-Za-z][A-Za-z0-9]*)\.self.*/\1/' | sort -u)
la_count=$(printf '%s\n' "$la_types" | sed '/^$/d' | wc -l | tr -d ' ')
sok "the Live Activity extensions declare $la_count activity surface(s)"
used_la_kinds=""
for t in $la_types; do
  kind=$(la_case_for "$t")
  if [ -z "$kind" ]; then
    sfail "Live Activity $t has no registry case, so the settings screen cannot list it"
    continue
  fi
  printf '%s\n' "$surface_cases" | grep -qx "$kind" \
    || sfail "Live Activity $t maps to $kind, which is not a WidgetSurfaceKind case"
  used_la_kinds="$used_la_kinds$kind
"
  sok "Live Activity $t is wired to $kind"
done
registered_la_kinds=$(printf '%s\n' "$surface_cases" | while IFS= read -r c; do
  grep -q "^    case $c = \"liveActivity\." "$SRC" && printf '%s\n' "$c"
done)
if [ "$(norm_list "$used_la_kinds")" != "$(norm_list "$registered_la_kinds")" ]; then
  sfail "the Live Activity cases in the registry and the activities in the extensions are not the same set"
else
  sok "every Live Activity case has an activity and every activity has a Live Activity case"
fi
# A raw value IS the App Group key: pin the newest one to its literal for the
# same reason every older surface is pinned in the executed harness.
grep -q 'case liveActivityBoardProgress = "liveActivity.boardProgress"' "$SRC" \
  && sok "liveActivity.boardProgress registry key" \
  || sfail "liveActivity.boardProgress registry key"
grep -q 'setting(for: .liveActivityBoardProgress)' Sources/HSCC/BoardProgressActivityDriver.swift \
  && sok "the board-progress driver reads its registry entry" \
  || sfail "the board-progress driver reads its registry entry"

# Gate-integrity assertions (the generated harness): reported with the greps
# because they are assertions about THIS gate rather than about the app, and
# both must be clean before any PASSED banner may print.
if [ "$integrityfail" -gt 0 ]; then
  echo "WIDGET SETTINGS CHECK FAILED — $integrityfail harness-integrity assertion(s) (see the harness diff above)" >&2
  rm -rf "$WORK"
  exit 1
fi
if [ "$grepfail" -gt 0 ]; then
  echo "WIDGET SETTINGS CHECK FAILED — $grepfail intent + config-screen + surface-coverage assertion(s)"
  rm -rf "$WORK"
  exit 1
fi
if [ "$rc" -ne 0 ]; then rm -rf "$WORK"; exit "$rc"; fi
rm -rf "$WORK"
echo "INTENT DELEGATION CHECK PASSED — the committed intent calls exactly the executed fold + apply, on the one storage key, with no second write path"
echo "CONFIG SCREEN CHECK PASSED — the screen writes through WidgetSettingsStore.setScope, folds with the shared static, reads through the widgets' own scope(for:), and populates its pickers from the sheet's own queries"
echo "SURFACE COVERAGE CHECK PASSED — every widget and Live Activity in the committed code has a registry case, display metadata, and the shared pause/cadence/scope editor"
exit 0
