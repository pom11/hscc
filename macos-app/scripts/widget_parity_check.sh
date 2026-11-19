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
  python3 - "$1" "$2" <<'PY'
import os, re, sys
spec_path, target = sys.argv[1], sys.argv[2]
base = os.path.dirname(os.path.abspath(spec_path))
ios = os.path.abspath(os.path.join(base, '..', 'ios-app')) if os.path.basename(base) == 'macos-app' else os.path.abspath(base)
text = open(spec_path).read()
text = '\n' + text.split('\ntargets:\n', 1)[1]
blk = text.split(f'\n  {target}:', 1)[1]
blk = re.split(r'\n  [A-Za-z]\w*:\n', blk, maxsplit=1)[0]
out = []
for path in re.findall(r'- path: ([^\s]+)', blk):
    full = os.path.normpath(os.path.join(base, path))
    if path.endswith('.swift'):
        out.append(os.path.relpath(full, ios))
    elif os.path.isdir(full):
        for root, _, files in os.walk(full):
            for f in sorted(files):
                if f.endswith('.swift'):
                    out.append(os.path.relpath(os.path.join(root, f), ios))
for p in sorted(set(out)):
    print(p)
PY
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

# mac_families <file> — the supportedFamilies list that reaches macOS: the
# #else (non-iOS) arm, or an unguarded/shared call. Reading the whole file
# instead of that list is what lets a family offered ONLY on iOS satisfy a
# macOS assertion. Since t_4311500c the graph widget has ONE shared list, so
# an unguarded call is a macOS declaration too rather than a missing one.
mac_families() {
  awk '
    /^[[:space:]]*#if[[:space:]]+os\(iOS\)/ { state = "ios" }
    /^[[:space:]]*#else/ { state = "mac" }
    /#endif/          { state = "" }
    state != "ios" && /\.supportedFamilies\(/ { f = 1 }
    f { print }
    f && /\]\)/ { f = 0 }
  ' "$1"
}
# ios_only_region <file> — the lines between #if os(iOS) and its #endif.
ios_only_region() {
  awk '/^[[:space:]]*#if[[:space:]]+os\(iOS\)/ { f = 1; next } /^[[:space:]]*#endif/ { f = 0 } f { print }' "$1"
}
# ios_families <file> — the supportedFamilies list inside the #if os(iOS) arm.
# The mirror of mac_families: a family may be added on the phone and forgotten on
# the desktop, and it may also be *deleted* from the phone by an edit that only
# meant to change the desktop list. Both directions are checked below.
ios_families() {
  awk '
    /^[[:space:]]*#if[[:space:]]+os\(iOS\)/ { state = "ios" }
    /^[[:space:]]*#else/ { state = "mac" }
    /#endif/          { state = "" }
    state == "ios" && /\.supportedFamilies\(/ { f = 1 }
    f { print }
    f && /\]\)/ { f = 0 }
  ' "$1"
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
  # The mirror: an edit aimed at the desktop list must not quietly remove a
  # family from the phone list. Every desktop family has to stay offered there.
  ios_declared=$(ios_families "$file" | tr -d '[:space:]')
  ios_missing=""
  for fam in $DESKTOP_FAMILIES; do
    case "$ios_declared" in
      *".$fam"*) ;;
      *) ios_missing="$ios_missing .$fam" ;;
    esac
  done
  if [ -z "$ios_missing" ]; then
    ok "$widget: the iOS list still offers all four desktop families"
  else
    bad "$widget: iOS no longer offers$ios_missing — a desktop-side edit took a phone family away"
  fi
  echo "  $widget iOS-only region: $(ios_only_region "$file" | grep -c 'case \.accessory' || true) accessory arm(s), $(ios_only_region "$file" | grep -c supportedFamilies || true) supportedFamilies list(s)"
done

# ---------------------------------------------------------------------------
echo "extra-large payload — the six fields the CLI host table has no column for"

for label in VRAM "GPU clock" Net Up Fan Load; do
  need "XL detail field '$label' is owned by the shared layout model" "$TABLE_MODEL" "\"$label\""
done
# The widget directory is compiled as ONE computed source entry, so adding a
# widget file no longer edits either project spec. The set-for-set source check
# above still proves this directory carries the table views.
need "the desktop compiles the widget directory" project.yml \
     "- path: ../ios-app/Sources/HSCCWidgets"
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
echo "cluster graph tile — every desktop size declared+dispatched, and the marks the dropdown embeds"
#
# The graph tile had no desktop assertion at all: the loop above pins Cluster and
# Monitor, and the graph widget's own gate (ios-app/scripts/cluster_graph_check.sh)
# iterates the iOS family list, so a macOS arm that quietly offered three of the
# four sizes was invisible to both gates. The graph marks are also the ONLY thing
# the menu bar dropdown draws for history (t_be4e1a48), and they live in the
# WIDGET's view file — so the app target has to compile them by path, and dropping
# that line takes the charts out of the dropdown while both build gates stay green
# (the widget target compiles the same file either way).

GRAPH=../ios-app/Sources/HSCCWidgets/GraphWidget.swift
GRAPH_METRICS=../ios-app/Sources/HSCCWidgets/GraphWidgetMetrics.swift
GRAPH_VIEWS=../ios-app/Sources/HSCCWidgets/GraphWidgetViews.swift
GRAPH_ENTRY=../ios-app/Sources/HSCCWidgets/GraphWidgetEntry.swift
GRAPH_READING=../ios-app/Sources/Shared/ClusterGraphReading.swift

if [ ! -f "$GRAPH" ] || [ ! -f "$GRAPH_VIEWS" ] || [ ! -f "$GRAPH_ENTRY" ] || [ ! -f "$GRAPH_READING" ]; then
  bad "a graph widget source is absent — the graph tile cannot be asserted"
else
  gdeclared=$(mac_families "$GRAPH" | tr -d '[:space:]')
  # The shared list is named (`WidgetFamily.clusterGraphFamilies`), not a
  # literal, since t_4311500c. Resolve it to its members so the per-family
  # assertions below still measure the sizes the desktop actually offers.
  case "$gdeclared" in
    *clusterGraphFamilies*)
      gdeclared="$gdeclared$(sed -n '/static let clusterGraphFamilies/,/]/p' "$GRAPH_METRICS" | tr -d '[:space:]')"
      ;;
  esac
  if [ -z "$gdeclared" ]; then
    bad "ClusterGraphWidget: no macOS-arm supportedFamilies list in $GRAPH"
  else
    echo "  ClusterGraphWidget macOS list: $(mac_families "$GRAPH" | grep -o '\.[a-zA-Z][A-Za-z]*' | tr '\n' ' ')"
    for fam in $DESKTOP_FAMILIES; do
      case "$gdeclared" in
        *".$fam"*)
          ok "ClusterGraphWidget declares .$fam on macOS"
          if grep -q "case \.$fam:" "$GRAPH_VIEWS"; then
            ok "ClusterGraphWidget dispatches .$fam to its own arm"
          else
            bad "ClusterGraphWidget declares .$fam but has no 'case .$fam:' arm in the graph dispatch"
          fi
          ;;
        *) bad "ClusterGraphWidget no longer declares .$fam on macOS (the desktop lost a size)" ;;
      esac
    done
    # Both small-multiple sizes draw the SAME struct on purpose (the tile's own
    # density map puts large and extra-large on one layout), so what must differ
    # is the family the arm hands the layout — that is the number the size reads
    # its mini height from. A dropped argument is how XL would quietly become a
    # stretched large.
    if grep -q -- "family: .systemExtraLarge" "$GRAPH_VIEWS"; then
      ok "the extra-large arm carries .systemExtraLarge into the panel layout"
    else
      bad "the extra-large arm does not pass family: .systemExtraLarge — XL draws the large size's numbers"
    fi
    # The mirror of the accessory rule: the four home sizes are desktop sizes, so
    # none of them may live behind an iOS-only guard.
    gguarded=$(ios_only_region "$GRAPH" | grep -c 'case \.system' || true)
    if [ "$gguarded" -eq 0 ]; then
      ok "ClusterGraphWidget: no system* family is hidden behind #if os(iOS)"
    else
      bad "ClusterGraphWidget: $gguarded system* arm(s) sit inside an #if os(iOS) region"
    fi
    gios=$(ios_families "$GRAPH" | tr -d '[:space:]')
    if [ -z "$gios" ]; then
      # t_4311500c deleted the two per-platform arms in favour of one shared
      # list, so there is no second list to mirror; the macOS assertions above
      # already read the list both platforms use.
      ok "ClusterGraphWidget: one shared supportedFamilies list serves iOS and macOS"
    else
      gios_missing=""
      for fam in $DESKTOP_FAMILIES; do
        case "$gios" in
          *".$fam"*) ;;
          *) gios_missing="$gios_missing .$fam" ;;
        esac
      done
      if [ -z "$gios_missing" ]; then
        ok "ClusterGraphWidget: the iOS list still offers all four desktop families"
      else
        bad "ClusterGraphWidget: iOS no longer offers$gios_missing — a desktop-side edit took a phone family away"
      fi
    fi
  fi

  # The dropdown's charts are the widget's marks, compiled into the APP target.
  # These four are the chart itself and what it needs to be a chart: the shared
  # projection, the marks, the entry type the marks' file draws, and the family
  # map that file reads. Drop one and the graphs section becomes a list of
  # numbers again — with both build gates green, because the widget extension
  # compiles the same paths either way.
  app_list=$(spec_paths project.yml HSCCMac)
  for p in Sources/Shared/ClusterGraphReading.swift \
           Sources/HSCCWidgets/GraphWidgetViews.swift \
           Sources/HSCCWidgets/GraphWidgetEntry.swift \
           Sources/HSCCWidgets/GraphWidgetMetrics.swift; do
    case "$app_list" in
      *"$p"*) ok "the menu bar app compiles $p (the dropdown draws its marks)" ;;
      *) bad "the menu bar app does not compile $p — the graphs section would show numbers with no chart" ;;
    esac
  done
fi

# ---------------------------------------------------------------------------
echo ""
if [ "$rc" -eq 0 ]; then
  echo "MACOS WIDGET PARITY CHECK PASSED — $mac_n shared sources equal on both targets, four desktop families declared+dispatched+distinct for every tile including the graph, extra-large routes through the shared CLI tables with its six extra fields, and the menu bar app compiles the graph marks the dropdown draws"
  exit 0
fi
echo "MACOS WIDGET PARITY CHECK FAILED"
exit 1
