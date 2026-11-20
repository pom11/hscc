#!/bin/bash
# widget_parity_check.sh — the DESKTOP widget target draws the same widgets and
# data as the iOS widget target, from the same files.
#
# The 2026-11-20 redesign removed the Monitor and Cluster Graph widgets. The
# remaining Cluster tile is C1: small/medium/large, with extra-large reusing the
# large body (C1 defines three sizes; macOS/iPad XL is the same layout, not a
# second density). This gate pins the files both targets compile and the family
# arms the desktop actually offers. The menu-bar app still embeds the shared
# history marks in its Graphs section, so those files must stay in its target.
#
# Usage: macos-app/scripts/widget_parity_check.sh      (from anywhere)
set -uo pipefail
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SELF_DIR/.." || exit 1          # -> macos-app/
IOS=../ios-app                      # the sources the desktop compiles by path

rc=0
ok()  { echo "  ok: $1"; }
bad() { echo "FAIL: $1"; rc=1; }

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
ROOT=../ios-app/Sources/HSCCWidgets/ClusterWidget.swift
KIT=../ios-app/Sources/HSCCWidgets/NativeStyleKit.swift
for f in project.yml "$IOS/project.yml" "$CLUSTER" "$ROOT" "$KIT"; do
  [ -f "$f" ] || { echo "error: $f missing — run this from the repo checkout" >&2; exit 1; }
done

echo "shared source parity — HSCCMacWidgets vs HSCCWidgets (both specs, by path)"
mac_list=$(spec_paths project.yml HSCCMacWidgets)
ios_list=$(spec_paths "$IOS/project.yml" HSCCWidgets)
mac_n=$(printf '%s\n' "$mac_list" | grep -c . )
ios_n=$(printf '%s\n' "$ios_list" | grep -c . )
echo "  HSCCMacWidgets: $mac_n files   HSCCWidgets: $ios_n files"
[ "$mac_n" -gt 0 ] && [ "$ios_n" -gt 0 ] || { echo "FAIL: a target's file list came back empty"; exit 1; }

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
missing=0
while IFS= read -r p; do
  [ -f "$IOS/$p" ] || { echo "FAIL: listed but absent: $p"; missing=1; }
done <<< "$mac_list"
[ "$missing" -eq 0 ] && ok "every listed path resolves to a file on disk"

echo "macOS families — declared and dispatched"
DESKTOP_FAMILIES="systemSmall systemMedium systemLarge systemExtraLarge"
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
declared=$(mac_families "$CLUSTER" | tr -d '[:space:]')
echo "  ClusterWidget macOS list: $(mac_families "$CLUSTER" | grep -o '\.[a-zA-Z][a-zA-Z]*' | tr '\n' ' ')"
for fam in $DESKTOP_FAMILIES; do
  case "$declared" in
    *".$fam"*)
      ok "ClusterWidget declares .$fam on macOS"
      if grep -q "case \.$fam:" "$CLUSTER"; then
        ok "ClusterWidget dispatches .$fam"
      else
        bad "ClusterWidget declares .$fam but has no case arm"
      fi
      ;;
    *) bad "ClusterWidget no longer declares .$fam on macOS" ;;
  esac
done
guarded=$(awk '/^[[:space:]]*#if[[:space:]]+os\(iOS\)/ { f = 1; next } /^[[:space:]]*#endif/ { f = 0 } f { print }' "$CLUSTER" | grep -c 'case \.system' || true)
if [ "$guarded" -eq 0 ]; then
  ok "no system* family is hidden behind an iOS-only guard"
else
  bad "$guarded system* dispatch arm(s) sit inside an #if os(iOS) region"
fi
if grep -q 'containerBackground(.fill.tertiary, for: .widget)' "$CLUSTER"; then
  ok "the desktop tile uses the Native system container background"
else
  bad "the desktop tile does not use the Native system container background"
fi
for needle in 'struct AsOfStamp' 'struct MeterBar' 'struct Tile' 'struct StaleModifier'; do
  if grep -q "$needle" "$KIT"; then
    ok "the shared NativeStyleKit owns $needle"
  else
    bad "the shared NativeStyleKit is missing $needle"
  fi
done

echo "menu bar graphs — the app compiles the shared history marks"
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

echo ""
if [ "$rc" -eq 0 ]; then
  echo "MACOS WIDGET PARITY CHECK PASSED — shared sources equal, C1 families declared+dispatched, native kit wired, and the menu bar app compiles the graph marks"
  exit 0
fi
echo "MACOS WIDGET PARITY CHECK FAILED"
exit 1
