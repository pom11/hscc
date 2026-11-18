#!/bin/bash
# widget_style_check.sh — every widget / Live Activity view renders the stored
# WidgetStyle, and no view carries its own colour or font any more.
#
# This is the part-2 companion to widget_settings_check.sh: that gate proves the
# model round-trips; this one proves the committed rendering paths actually read
# it. Structural only (there is no iOS runtime on this host), but it fails on the
# exact regressions that would silently ignore the operator's Appearance choices:
#
#   1. a literal Color(...) or .font(.system...) / .hsccMono in a view,
#   2. a Theme palette/semantic colour left in a widget/LA view,
#   3. a registry surface with no WidgetStyle.resolved(for:) call,
#   4. an accessory (Lock Screen) body that applies style COLOURS (system-tinted),
#   5. a view file with no style environment, header switch or icon switch.
#
# Usage: scripts/widget_style_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

WIDGET_VIEWS=(
  Sources/HSCCWidgets/ClusterWidgetViews.swift
  Sources/HSCCWidgets/MonitorViews.swift
  Sources/HSCCWidgets/BoardStatusWidget.swift
  Sources/HSCCWidgets/ProjectsWidget.swift
  Sources/HSCCWidgets/KanbanMonitorWidget.swift
  Sources/HSCCWidgets/CardsRunningWidget.swift
  Sources/HSCCLiveActivity/HSCCLiveActivity.swift
  Sources/HSCCLiveActivityMonitor/MonitorLiveActivity.swift
  Sources/HSCCLiveActivityMonitor/CardsRunningLiveActivity.swift
  Sources/HSCCLiveActivityMonitor/ClusterHealthLiveActivity.swift
  Sources/HSCCLiveActivityMonitor/BoardProgressLiveActivity.swift
)

fail=0
ok()   { echo "  ok: $1"; }
bad()  { echo "  FAIL: $1"; fail=1; }

# 1. No literal colour constructors and no hard-coded fonts in a view. Comment
#    lines are excluded: prose that names Theme.Semantic is documentation, not a
#    call site. A backend colour type (WidgetColor) is not matched because the
#    pattern requires a non-identifier byte before Color.
for f in "${WIDGET_VIEWS[@]}"; do
  [ -f "$f" ] || { bad "missing view file $f"; continue; }
  hits=$(grep -nE '(^|[^A-Za-z0-9_])Color\(|\.font\(\.system|Font\.system|\.font\(\.hsccMono|Font\.hsccMono' "$f" \
         | grep -v '^[[:space:]]*[0-9]*[[:space:]]*//' || true)
  if [ -z "$hits" ]; then
    ok "$(basename "$f"): no literal Color(...) or hard-coded system font"
  else
    bad "$(basename "$f") uses a literal colour/font instead of the style:"
    printf '%s\n' "$hits" | head -8
  fi
done

# 2. No Theme palette/semantic colour or status helper left in a view.
for f in "${WIDGET_VIEWS[@]}"; do
  hits=$(grep -nE 'Theme\.(Semantic|Palette|cardStatusColor)' "$f" \
         | grep -v '^[[:space:]]*[0-9]*[[:space:]]*//' || true)
  if [ -z "$hits" ]; then
    ok "$(basename "$f"): colours come from WidgetStyle, not Theme"
  else
    bad "$(basename "$f") still reads a Theme colour:"
    printf '%s\n' "$hits" | head -8
  fi
done

# 3. Every registry surface resolves exactly one style, by its own kind.
SRC="Sources/Shared/SharedStore.swift"
cases=$(sed -n '/^enum WidgetSurfaceKind: String, CaseIterable, Codable {/,/^}/p' "$SRC" \
  | sed -n 's/^    case \([A-Za-z][A-Za-z0-9]*\).*/\1/p')
case_count=$(printf '%s\n' "$cases" | sed '/^$/d' | wc -l | tr -d ' ')
if [ "$case_count" -lt 1 ]; then
  bad "could not read WidgetSurfaceKind cases from $SRC"
else
  missing=0
  for kind in $cases; do
    n=$(grep -rhoF "WidgetStyle.resolved(for: .$kind)" "${WIDGET_VIEWS[@]}" | wc -l | tr -d ' ')
    if [ "$n" -eq 1 ]; then
      ok "surface $kind resolves WidgetStyle.resolved(for: .$kind)"
    else
      bad "surface $kind has $n style resolutions (want exactly 1)"
      missing=$((missing + 1))
    fi
  done
  [ "$missing" -eq 0 ] && ok "all $case_count registry surfaces resolve their own style"
fi

# 4. Accessory (Lock Screen) families are system-tinted: only fonts and the two
#    toggles may vary there. Names ending in Circular/Rectangular/Inline are the
#    accessory bodies in this codebase; a style colour inside one is the exact
#    mistake the editor footer warns about.
for f in "${WIDGET_VIEWS[@]}"; do
  names=$(grep -oE '^(private )?struct [A-Za-z0-9_]*(Circular|Rectangular|Inline)[A-Za-z0-9_]*: View' "$f" \
          | sed -E 's/^(private )?struct ([A-Za-z0-9_]+): View/\2/' || true)
  [ -z "$names" ] && continue
  for name in $names; do
    body=$(awk -v n="$name" '
      $0 ~ "^(private )?struct " n ": View" { f=1 }
      f { print }
      f && NR>1 && $0 ~ "^(private )?struct [A-Za-z0-9_]+: (View|Widget)" && $0 !~ "struct " n ": View" { exit }
      f && /^\/\/ MARK:/ { exit }
    ' "$f")
    if printf '%s\n' "$body" | grep -qE 'style\.[A-Za-z]*Color'; then
      bad "$name applies a style colour — Lock Screen accessories are system-tinted"
    else
      ok "$name keeps accessory colours system-tinted"
    fi
  done
done

# 5. Every view file reads the environment and wires both toggles; the roots
#    also set the resolved background. This catches a new family copied from an
#    old one without the style plumbing.
for f in "${WIDGET_VIEWS[@]}"; do
  file_ok=1
  grep -q '@Environment(\\.widgetStyle)' "$f" || { bad "$(basename "$f") has no widgetStyle environment"; file_ok=0; }
  grep -q 'widgetHeader(style)' "$f" || { bad "$(basename "$f") has no header toggle"; file_ok=0; }
  grep -qE 'widgetIcon\(style\)|labelStyle\(\.widget\)' "$f" || { bad "$(basename "$f") has no icon toggle"; file_ok=0; }
  [ "$file_ok" -eq 1 ] && ok "$(basename "$f"): style environment + header/icon toggles wired"
done

nbg=0
bgok=1
nadapt=0
for f in Sources/HSCCWidgets/*.swift; do
  c=$(grep -c 'containerBackground(for: .widget)' "$f" || true)
  [ "$c" -eq 0 ] && continue
  nbg=$((nbg + c))
  # The background moved behind the shared rendering-mode adapter
  # (t_1143c19b): fullColor draws style.backgroundColor, accented/vibrant
  # leaves the system material visible. A root that paints a raw colour
  # again would make a desktop widget an opaque black rectangle on the
  # wallpaper, so the wrapper is what is asserted here.
  grep -A1 'containerBackground(for: .widget)' "$f" | grep -q 'WidgetSurfaceBackground(style: style)' || bgok=0
  nadapt=$((nadapt + $(grep -c '\.widgetRenderingAdaptation(style: style)' "$f" || true)))
done
ADAPTER=Sources/HSCCWidgets/WidgetRendering.swift
adapter_ok=1
[ -f "$ADAPTER" ] \
  && grep -q 'renderingMode == .fullColor' "$ADAPTER" \
  && grep -q 'style.backgroundColor' "$ADAPTER" \
  && grep -q 'Color.clear' "$ADAPTER" \
  && grep -q 'systemTinted(for:' "$ADAPTER" \
  || adapter_ok=0
if [ "$nbg" -eq 6 ] && [ "$bgok" -eq 1 ] && [ "$nadapt" -eq 6 ] && [ "$adapter_ok" -eq 1 ]; then
  ok "all 6 widget roots paint containerBackground through the rendering-mode adapter (fullColor = style.backgroundColor, accented/vibrant = system material)"
else
  bad "a widget root does not paint containerBackground through the rendering-mode adapter (roots=$nbg wrapped=$nadapt bg=$bgok adapter=$adapter_ok)"
fi
nla=$(grep -l 'activityBackgroundTint(style.backgroundColor)' Sources/HSCCLiveActivityMonitor/*.swift Sources/HSCCLiveActivity/*.swift | wc -l | tr -d ' ')
if [ "$nla" -eq 5 ]; then
  ok "all 5 Live Activity roots tint from style.backgroundColor"
else
  bad "only $nla/5 Live Activity roots tint from style.backgroundColor"
fi

if [ "$fail" -ne 0 ]; then
  echo "WIDGET STYLE CHECK FAILED — a widget or Live Activity view bypasses WidgetStyle"
  exit 1
fi
echo "WIDGET STYLE CHECK PASSED — every widget and Live Activity view reads WidgetStyle, accessory families stay system-tinted, and no hard-coded colour/font remains"
