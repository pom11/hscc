#!/bin/bash
# widget_parity_check.sh — the DESKTOP widget target draws the same density the
# iOS widget target draws, on every family macOS actually offers.
#
# Why this gate exists: `macos-app/project.yml`'s HSCCMacWidgets target compiles
# the iOS extension's own files BY PATH (`../ios-app/Sources/HSCCWidgets/…`), so
# the desktop tile has no view of its own to drift — but it has two ways to be
# quietly shrunk, and `build_check.sh` catches neither:
#
#   1. a shared file the iOS target compiles and the desktop target does not
#      list, which leaves the desktop drawing whatever survives without it; and
#   2. a family in the `#else` (macOS) `supportedFamilies` list whose dispatch
#      arm falls through to another size's layout. Before t_9e64d9d9 that is
#      exactly what `.systemExtraLarge` did on the desktop: it was declared, and
#      drew the large layout, so the six XL-only fields (VRAM, GPU clock, net,
#      uptime, fan, load) silently never reached a Mac.
#
# So: the declared macOS families are pinned BY NAME, each must own a `case`
# arm, the extra-large arm must be a layout of its own that routes through the
# shared table block, and the two targets' shared source lists must be equal
# SET-for-SET. Nothing here renders pixels — there is no widget runtime on a
# compile-only host — it proves the wiring that decides what would render.
#
# Usage: macos-app/scripts/widget_parity_check.sh      (from anywhere)
set -uo pipefail
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SELF_DIR/.." || exit 1          # -> macos-app/
IOS=../ios-app                      # the sources the desktop compiles by path

rc=0
ok()  { echo "  ok: $1"; }
bad() { echo "FAIL: $1"; rc=1; }
need() { # need <label> <file> <pattern>
  if grep -q -- "$3" "$2" 2>/dev/null; then ok "$1"; else bad "$1 (no '$3' in $2)"; fi
}

# spec_paths <file> <target> — the target's Swift paths, normalised to be
# relative to ios-app/ so the two specs can be compared directly.
spec_paths() {
  awk -v t="$2" '
    /^targets:[[:space:]]*$/ { intargets = 1 }
    !intargets { next }
    $0 ~ ("^  " t ":[[:space:]]*$") { f = 1; next }
    f && /^  [A-Za-z][A-Za-z0-9]*:[[:space:]]*$/ { exit }
    f && /- path: .*\.swift/ { p = $3; sub(/^\.\.\/ios-app\//, "", p); print p }
  ' "$1" | LC_ALL=C sort -u
}

CLUSTER=../ios-app/Sources/HSCCWidgets/ClusterWidgetViews.swift
MONITOR=../ios-app/Sources/HSCCWidgets/MonitorViews.swift
TABLE_VIEWS=../ios-app/Sources/HSCCWidgets/ClusterMonitorTableViews.swift
TABLE_MODEL=../ios-app/Sources/Shared/ClusterMonitorTables.swift

for f in project.yml "$IOS/project.yml" "$CLUSTER" "$MONITOR" "$TABLE_VIEWS" "$TABLE_MODEL"; do
  [ -f "$f" ] || { echo "error: $f missing — run this from the repo checkout" >&2; exit 1; }
done

# ---------------------------------------------------------------------------
echo "shared source parity — HSCCMacWidgets vs HSCCWidgets (both specs, by path)"

mac_list=$(spec_paths project.yml HSCCMacWidgets)
ios_list=$(spec_paths "$IOS/project.yml" HSCCWidgets)
mac_n=$(printf '%s\n' "$mac_list" | grep -c . )
ios_n=$(printf '%s\n' "$ios_list" | grep -c . )
echo "  HSCCMacWidgets: $mac_n files   HSCCWidgets: $ios_n files"
[ "$mac_n" -gt 0 ] && [ "$ios_n" -gt 0 ] || { echo "FAIL: a target's file list came back empty"; exit 1; }

# Temp files, not <(comm …) inside $(…): bash 3.2 — the /bin/bash on every Mac
# here — cannot parse a process substitution nested in a command substitution,
# and a gate that will not parse is a gate nobody ran.
WORK=$(mktemp -d) || exit 1
trap 'rm -rf "$WORK"' EXIT
printf '%s\n' "$ios_list" > "$WORK/ios.txt"
printf '%s\n' "$mac_list" > "$WORK/mac.txt"
only_ios=$(LC_ALL=C comm -23 "$WORK/ios.txt" "$WORK/mac.txt")
only_mac=$(LC_ALL=C comm -13 "$WORK/ios.txt" "$WORK/mac.txt")
if [ -z "$only_ios" ]; then
  ok "every iOS widget source is compiled by the desktop target too"
else
  bad "the desktop target does not compile: $(printf '%s\n' "$only_ios" | tr '\n' ' ')"
fi
if [ -z "$only_mac" ]; then
  ok "the desktop target lists no shared source the iOS target lacks"
else
  bad "desktop-only shared source (a copy, or a stale - path): $(printf '%s\n' "$only_mac" | tr '\n' ' ')"
fi
# Every listed path must exist, or the parity above is a set-equality over
# filenames that no compiler will ever open.
missing=0
while IFS= read -r p; do
  [ -f "$IOS/$p" ] || { echo "FAIL: listed but absent: $p"; missing=1; }
done <<< "$mac_list"
[ "$missing" -eq 0 ] && ok "every listed path resolves to a file on disk"

# ---------------------------------------------------------------------------
echo "macOS families — declared, dispatched, and distinguishable"

# mac_families <file> — the supportedFamilies list inside the #else (non-iOS)
# arm of the entry. Reading the whole file instead of that arm is what lets a
# family offered ONLY on iOS satisfy a macOS assertion.
mac_families() {
  awk '
    /#if os\(iOS\)/  { state = "ios" }
    /^[[:space:]]*#else/ { state = "mac" }
    /#endif/          { state = "" }
    state == "mac" && /\.supportedFamilies\(/ { f = 1 }
    f { print }
    f && /\]\)/ { f = 0 }
  ' "$1"
}
# ios_only_region <file> — the lines between #if os(iOS) and its #endif.
ios_only_region() {
  awk '/#if os\(iOS\)/ { f = 1; next } /#endif/ { f = 0 } f { print }' "$1"
}
# arm_view <file> <family> — the struct the dispatch arm draws.
arm_view() {
  awk -v fam="$2" '
    $0 ~ ("case \\." fam ":") { f = 1; next }
    f && NF { print; exit }
  ' "$1" | sed -n 's/^[[:space:]]*\([A-Za-z][A-Za-z0-9]*\)(.*/\1/p'
}

DESKTOP_FAMILIES="systemSmall systemMedium systemLarge systemExtraLarge"
for spec in "ClusterWidget:$CLUSTER" "MonitorWidget:$MONITOR"; do
  widget=${spec%%:*}; file=${spec#*:}
  declared=$(mac_families "$file" | tr -d '[:space:]')
  # `|| { …; continue }` is a parse error on bash 3.2 (the /bin/bash on every
  # Mac): `continue` swallows the `}` as its count argument. Spell it as an if.
  if [ -z "$declared" ]; then bad "$widget: no macOS-arm supportedFamilies list in $file"; continue; fi
  echo "  $widget macOS list: $(mac_families "$file" | grep -o '\.[a-zA-Z][a-zA-Z]*' | tr '\n' ' ')"
  for fam in $DESKTOP_FAMILIES; do
    case "$declared" in
      *".$fam"*)
        ok "$widget declares .$fam on macOS"
        if grep -q "case \.$fam:" "$file"; then
          ok "$widget dispatches .$fam to its own arm"
        else
          bad "$widget declares .$fam but has no 'case .$fam:' arm — the family falls through to default:"
        fi
        ;;
      *) bad "$widget no longer declares .$fam on macOS (the desktop lost a size)" ;;
    esac
  done

  large=$(arm_view "$file" systemLarge)
  xl=$(arm_view "$file" systemExtraLarge)
  if [ -n "$xl" ] && [ "$xl" != "$large" ]; then
    ok "$widget extra-large has a layout of its own ($xl, not $large)"
  else
    bad "$widget extra-large draws '$xl' — the same layout as large, so the XL-only fields never render"
  fi
  # The XL arm must be the size that adds the detail fields, and it must ask
  # the entry whether a table may be drawn at all (one gate, not one per body).
  xlbody=$(awk -v v="$xl" '$0 ~ ("^struct " v) { f = 1 } f { print } f && /^}$/ { exit }' "$file")
  case "$xlbody" in
    *"MonitorTablesBlock(tables: tables, surface: .extraLarge)"*)
      ok "$widget $xl routes through the shared table block at .extraLarge" ;;
    *) bad "$widget $xl does not route through MonitorTablesBlock at .extraLarge" ;;
  esac
  case "$xlbody" in
    *"if let tables = entry.tables"*)
      ok "$widget $xl asks the entry for the table gate instead of deciding it locally" ;;
    *) bad "$widget $xl decides the table gate itself" ;;
  esac

  # A desktop family may never live behind an iOS-only guard: the guard is for
  # the accessory families, which WidgetKit does not offer on macOS at all.
  guarded=$(ios_only_region "$file" | grep -c 'case \.system' || true)
  if [ "$guarded" -eq 0 ]; then
    ok "$widget: no system* family is hidden behind #if os(iOS)"
  else
    bad "$widget: $guarded system* dispatch arm(s) sit inside an #if os(iOS) region"
  fi
  echo "  $widget iOS-only region: $(ios_only_region "$file" | grep -c 'case \.accessory' || true) accessory arm(s), $(ios_only_region "$file" | grep -c supportedFamilies || true) supportedFamilies list(s)"
done

# ---------------------------------------------------------------------------
echo "extra-large payload — the six fields the CLI host table has no column for"

for label in VRAM "GPU clock" Net Up Fan Load; do
  need "XL detail field '$label' is owned by the shared layout model" "$TABLE_MODEL" "\"$label\""
done
need "the desktop compiles the table views" project.yml \
     "- path: ../ios-app/Sources/HSCCWidgets/ClusterMonitorTableViews.swift"
need "the desktop compiles the table layout model" project.yml \
     "- path: ../ios-app/Sources/Shared/ClusterMonitorTables.swift"
if grep -q "#if os(iOS)" "$TABLE_VIEWS" || grep -q "#if os(iOS)" "$TABLE_MODEL"; then
  bad "the shared table file grew a platform guard — the desktop would draw a shrunk table"
else
  ok "the table views and the table layout are platform-common code (no #if os(iOS))"
fi
# The desktop's XL is the same arm the iPad gets: both go through the layout
# model, so a Mac tile and an iPad tile cannot print different units.
need "the layout model is compiled by the iOS widget target as well" "$IOS/project.yml" \
     "- path: Sources/Shared/ClusterMonitorTables.swift"

# ---------------------------------------------------------------------------
echo ""
if [ "$rc" -eq 0 ]; then
  echo "MACOS WIDGET PARITY CHECK PASSED — $mac_n shared sources equal on both targets, four desktop families declared+dispatched+distinct, extra-large routes through the shared CLI tables with its six extra fields"
  exit 0
fi
echo "MACOS WIDGET PARITY CHECK FAILED"
exit 1
