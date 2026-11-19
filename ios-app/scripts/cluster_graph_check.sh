#!/bin/bash
# cluster_graph_check.sh — execute the history graph widget's projection layer
# and hold its structure to what the card and the fleet's gates require.
#
# Subject: ios-app/Sources/Shared/ClusterGraphReading.swift (t_f4551339) — the
# rules that decide what each Home Screen size DRAWS from a
# GET /v1/cluster/history answer. Like cluster_history_chart_check.sh this
# compiles the REAL source plus the REAL wire types into a plain macOS CLI and
# runs it: no redeclaration, no network, no simulator, no WidgetKit.
#
# WHY A GATE OF ITS OWN. build_check.sh proves the widget compiles and
# model_decode_check.sh proves the payload decodes. Neither can notice that a
# panel put GPU% and °C on one axis (eight metrics, four units), that a
# 40-pixel draw dropped the spike the operator opens the widget to see, that a
# window with no reading got joined into a straight line, that a 7-day chart
# printed a 2-second claim, or that a missing value got drawn as a zero. A chart
# lies by SHAPE, which is precisely what no shape-checking gate sees.
#
# THE STRUCTURAL LEG exists because the four family bodies cannot be executed on
# macOS at all: WidgetKit's `WidgetFamily` is Int-backed and its accessory cases
# are `@available(macOS, unavailable)`, so the family → density map is written as
# a POSITIVE LIST in Sources/HSCCWidgets/GraphWidgetMetrics.swift. The harness
# asserts the shared half of that map's vocabulary; this leg asserts the
# WidgetKit half — one dispatch arm per declared family (the t_b3ae3809 failure,
# where a declared systemLarge silently drew the medium layout), no accessory
# family anywhere (widget_view_dispatch gates this surface as home-screen-only),
# the canonical-route rule of DECISION-t_447cc7fc, and the registry wiring that
# makes the Settings row able to pause and pace this widget at all.
#
# NEGATIVE TEST (guarded): break the layer deliberately and confirm the harness
# FAILS. A harness that cannot fail is worthless:
#   export CLUSTER_GRAPH_NEGATIVE_TEST=1
#
# THE STRUCTURAL LEG'S OWN NEGATIVE PROOF is a separate harness, because nothing can
# execute these legs for you — they mutate tracked Swift and must restore it. No count
# is quoted here on purpose: a number in a comment drifts the day a leg is added, and a
# drifted count reads like the harness is describing a different file.
#   scripts/cluster_graph_check/negative_legs.sh
# Run it whenever §1 changes. It asserts the mutation applied before it trusts the rc,
# so a leg that measures nothing reports BROKEN rather than looking like a pass.
#
# Usage: ios-app/scripts/cluster_graph_check.sh

set -uo pipefail
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

subject="Sources/Shared/ClusterGraphReading.swift"
real_models=(
  Sources/Shared/SharedModels.swift
  Sources/Shared/SharedStore.swift
  Sources/Shared/ClusterHistorySeries.swift
  Sources/Shared/ClusterMonitorViewModel.swift
)
harness="scripts/cluster_graph_check/main.swift"
theme_stub="scripts/model_decode_check/ThemeStub.swift"
fixture_dir="scripts/model_decode_check/fixtures"

widget_dir="Sources/HSCCWidgets"
widget_files=(
  "$widget_dir/GraphWidget.swift"
  "$widget_dir/GraphWidgetEntry.swift"
  "$widget_dir/GraphWidgetMetrics.swift"
  "$widget_dir/GraphWidgetViews.swift"
)
ios_project="project.yml"
mac_project="../macos-app/project.yml"

for f in "$subject" "$harness" "$theme_stub"; do
  if [ ! -f "$f" ]; then echo "error: $f missing — did it get moved?" >&2; exit 1; fi
done
for f in "${real_models[@]}"; do
  if [ ! -f "$f" ]; then echo "error: $f missing — the harness can no longer build the real model" >&2; exit 1; fi
done
for f in "${widget_files[@]}"; do
  if [ ! -f "$f" ]; then echo "error: $f missing — the widget this gate guards is gone" >&2; exit 1; fi
done
for f in v1_cluster_history_6h.json v1_cluster_history_7d.json v1_cluster_history_6h_engine_only.json v1_cluster_history_15m.json; do
  if [ ! -f "$fixture_dir/$f" ]; then
    echo "error: $fixture_dir/$f missing — regenerate with scripts/make_cluster_history_fixture.py" >&2
    exit 1
  fi
done

# The harness must drive the REAL layer; if these markers move it is testing a
# copy, which is the false-green these harnesses exist to prevent.
if ! grep -q "ClusterGraphReading.panels" "$harness" ||
   ! grep -q "ClusterGraphReading.envelope" "$harness" ||
   ! grep -q "ClusterGraphReading.minis" "$harness"; then
  echo "error: harness no longer drives the real projection layer (markers moved?)" >&2
  exit 1
fi
if ! grep -q "Sources/Shared/ClusterGraphReading.swift" "$SELF"; then
  echo "error: runner no longer compiles the real projection layer (marker moved?)" >&2
  exit 1
fi

echo "compiling the REAL projection layer + wire models + view-model + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/cluster_graph_check" \
     "$subject" "${real_models[@]}" "$theme_stub" "$harness" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the graph projection layer — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi
# A warning here is a finding: build_check.sh holds every target to 0 warnings,
# and the same source compiles into all of them.
if [ -s "$TMP/compile.err" ]; then
  echo "error: the projection layer compiled with diagnostics — treated as findings:" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi

echo "running the projection checks against the route's own fixtures..."
"$TMP/cluster_graph_check" "$PWD/$fixture_dir"
harness_rc=$?

# ---------------------------------------------------------------------------
# Structural leg. Each check prints one line and says which file it read, so a
# failure points at the file to fix rather than at this script.
#
# WHY THE CHECKS READ FROM VARIABLES, NOT PIPES. This script runs under
# `set -o pipefail`, and `grep -q` exits the MOMENT it matches: sed then dies of
# SIGPIPE with status 141, pipefail reports 141, and `if sed … | grep -q X` takes
# the ELSE branch on a file that contains X. That is not hypothetical — this leg
# was wrong in exactly that way and reported "drawSegments vanished" for a file
# that has it on line 385, while the same pattern passed on a 300-line file whose
# match sat near EOF. So every file is read ONCE into a variable and the greps run
# against a here-string, where there is no pipe for anyone to break. A short
# helper (`has`) keeps that readable.
# ---------------------------------------------------------------------------
echo ""
echo "structural leg: the WidgetKit half the macOS harness cannot execute..."

struct_fail=0
sok() { echo "  ok: $1"; }
# The assignment is the load-bearing half. A `sfail` that only echoes prints a
# FAIL line and exits 0, which is the worst shape a gate can have: it looks like
# a check and behaves like a comment. It was exactly that here once — restored,
# and negative-proven by planting a bad pattern in a copy of this leg at the same
# depth and requiring a non-zero exit.
sfail() { echo "  FAIL: $1"; struct_fail=1; }
# `has PATTERN TEXT` — true when TEXT contains PATTERN (BRE). No pipe, so a
# match cannot be turned into a failure by an early grep exit.
has() { grep -q -- "$2" <<<"$1"; }
hasnt() { ! grep -q -- "$2" <<<"$1"; }

families_file="$widget_dir/GraphWidgetMetrics.swift"
views_file="$widget_dir/GraphWidgetViews.swift"
intent_file="$widget_dir/GraphWidget.swift"
entry_file="$widget_dir/GraphWidgetEntry.swift"
reading="Sources/Shared/ClusterGraphReading.swift"
layer="Sources/Shared/ClusterHistorySeries.swift"
client="Sources/Shared/WidgetDataClient.swift"
store="Sources/Shared/SharedStore.swift"
bundle="Sources/HSCCWidgets/HSCCWidgetsBundle.swift"
labels="Sources/HSCC/Views/WidgetSurfaceLabels.swift"

# Comment text is stripped before any code-pattern search. Without that, a
# sentence saying "no accessory family is declared" trips the accessory
# assertion and a doc comment naming the retired path trips the path assertion:
# the gate then fails on PROSE, and a gate that fails on prose gets silenced by
# the next person rather than fixed.
code_text() { sed -e 's://[^"]*::' -e '/^[[:space:]]*$/d' "$@"; }

reading_code="$(code_text "$reading")"
layer_code="$(code_text "$layer")"
client_code="$(code_text "$client")"
store_code="$(code_text "$store")"
bundle_code="$(code_text "$bundle")"
labels_code="$(code_text "$labels")"
families_code="$(code_text "$families_file")"
views_code="$(code_text "$views_file")"
intent_code="$(code_text "$intent_file")"
widget_code="$(code_text "${widget_files[@]}")"

# 1. One dispatch arm and one density entry for every size each PLATFORM ARM
#    offers, and a written reason for every size an arm leaves out. Two failure
#    classes live here, both invisible to a compile:
#      * t_b3ae3809 — a family in `supportedFamilies` whose body was reached
#        through `default:` drew another size's layout.
#      * t_4311500c — the widget declared its sizes twice, once per platform, and
#        the desktop arm hardcoded three of the shared list's four. The density map
#        and the dispatch switch carried the fourth, so the desktop compiled,
#        shipped and gated a layout it could never be asked to draw — and the check
#        that used to sit here read the shared list and grepped the text of the
#        iOS call, so it printed "reads the same positive list" while the other
#        arm disagreed. A grep for one arm's text cannot see the other arm.
#    The widget MAY keep a per-platform arm; what it may not do is offer a size
#    nothing draws, repeat the shared list as a literal, or drop a size without
#    writing why. Nothing below is transcribed: the shared list and every arm's
#    list are extracted at run time, and a call site that cannot be resolved is a
#    refusal rather than a clean run.
shared_list=$(awk '/static let clusterGraphFamilies/,/^    \]/' <<<"$families_code" \
  | grep -o '\.system[A-Za-z]*' | sed 's/^\.//' | sort -u)
if [ -z "$shared_list" ]; then
  sfail "no family list found in $families_file — the positive list is gone"
fi

# The sizes this widget owes the operator, pinned BY NAME. A count is the wrong
# pin and this leg used one (`fam_count -eq 4`): a dropped size then fails as a
# number instead of naming itself, and the next size decision gets "resolved" by
# editing the gate rather than by a decision.
for fam in systemSmall systemMedium systemLarge systemExtraLarge; do
  if printf '%s\n' $shared_list | grep -qx -- "$fam"; then
    sok "$fam is in the widget's positive family list"
  else
    sfail "$fam is missing from clusterGraphFamilies in $families_file — if dropping it is a decision, land it with this check edited in the same commit"
  fi
done

# The family -> density map, sliced so `default: return nil` cannot be mistaken for
# coverage. A size the map skips resolves to nil, and the provider's
# `family.clusterGraphDensity ?? .sparkline` would then draw it with a layout it was
# never designed for — the same lie as a missing dispatch arm, one file later.
density_region=$(awk '/var clusterGraphDensity/{f=1} f {print} f && /^    \}$/ {exit}' <<<"$families_code")
if [ -z "$density_region" ]; then
  sfail "could not slice clusterGraphDensity out of $families_file — the density-coverage checks below would be vacuous"
  density_fams=""
else
  density_fams=$(awk '/case \.system/ && /return \./ {print}' <<<"$density_region" \
    | grep -o '\.system[A-Za-z]*' | sed 's/^\.//' | sort -u)
  sok "$(printf '%s\n' $density_fams | sed '/^$/d' | wc -l | tr -d ' ') families get a density from the map (a size missing here draws via the provider's fallback)"
fi

# --- helpers for the family-arm pass -------------------------------------------
# arm_reach <condition> <flip> — which of the two platforms that build this widget
# (iOS, macOS) an `#if` condition can be true on, as a space list; "unknown" when the
# gate cannot resolve the condition. Restricted to the shipped platforms on purpose:
# the question is never "which of Swift's platforms", only "does an app that compiles
# this file get a family list at all". flip=1 asks for the `#else` arm, i.e. the
# complement inside {ios, macos}. `os(tvOS)` resolves to NOTHING — it reaches neither
# app. Anything that is not a plain `os(...)` disjunction (`canImport`, `&&`,
# `#elseif`, `targetEnvironment`) comes back unknown rather than guessed at: a guessed
# condition is how a gate starts printing green for a platform it never measured, and
# that is precisely the shape the t_4311500c re-review found (an `#if os(iOS)` guard
# with no `#else` — the arm-nobody-reads bug in its one-armed form).
arm_reach() {
  local c="$1" flip="$2" parts part out="" comp
  if [ -z "$c" ]; then printf 'ios macos\n'; return; fi   # unguarded: every platform
  c=$(printf '%s' "$c" | tr 'A-Z' 'a-z' | tr -d ' \t')
  parts=$(printf '%s' "$c" | tr '|' '\n')
  # HERE-STRING, not a pipe: `out` has to survive the loop (same trap as the arm loop).
  while IFS= read -r part; do
    [ -n "$part" ] || continue
    case "$part" in
      'os(ios)')   out="$out ios" ;;
      'os(macos)') out="$out macos" ;;
      'os('*)      ;;                                    # another platform: reaches neither
      *) printf 'unknown\n'; return ;;
    esac
  done <<< "$parts"
  if [ "$flip" = 1 ]; then
    comp=""
    case " $out " in *" ios "*) ;; *) comp="$comp ios" ;; esac
    case " $out " in *" macos "*) ;; *) comp="$comp macos" ;; esac
    out="$comp"
  fi
  printf '%s\n' "$out"
}

# omit_reason_in_span <span text> <family> — does THIS span carry a `families-omit`
# marker naming <family> and actually saying why (the em dash is the reason separator)?
# The span is one arm's own line range, so a reason written for the desktop arm cannot
# excuse a size the iOS arm dropped (re-review item 2: the failure text always said
# "on that arm" while the grep read the whole file). One grep over a here-string — a
# pipeline here would let `grep -q` + pipefail turn a real match into a false negative
# (see the note on `has`).
omit_reason_in_span() {
  grep -Eq "^[[:space:]]*//[[:space:]]*families-omit:.*\.${2}([^A-Za-z]|$).*—" <<<"$1"
}

# Does the DESKTOP target compile this file? The macOS half of the coverage rule below
# is only owed when the answer is yes — demanding an arm for a platform that does not
# build the file would be a false red, and "the desktop builds it" is a fact the desktop
# project states, not an inference from the shared list being valid on macOS.
# `mac_project` has sat unused since the weaker duplicate check was deleted; this is the
# job it was declared for.
macos_builds=0
if [ -f "$mac_project" ] \
   && grep -Eq "^[[:space:]]*-[[:space:]]*path:.*GraphWidget\.swift[[:space:]]*$" "$mac_project"; then
  macos_builds=1
fi

# Every supportedFamilies call site, labelled with the platform arm it sits in AND the
# line span that arm owns. Fields, separated by ASCII unit separator (037 — NOT a tab:
# `read` classifies a tab as IFS whitespace and COLLAPSES runs of it, so the unguarded
# arm's empty condition field shifted every later field left and the pass failed on a
# clean file. A non-whitespace separator keeps an empty field empty):
#   arm label | condition text | flip (1 = the #else arm) | span start | span end | call text
# The span is what makes both arm-nobody-reads shapes measurable: a `families-omit`
# reason has to fall inside the span of the arm that owes it, and the spans together
# say whether every platform that builds this file gets a supportedFamilies call at all.
# This pass runs over the RAW file and strips comments line by line with the same rule
# `code_text` applies, instead of reading a comment-stripped copy: the copy's line
# numbers do not match the file the marker is searched in, and that mismatch is what
# made the marker check file-global. Arm boundaries stay anchored at line start, so
# prose mentioning `#if os(iOS)` cannot invent one, and `#if`/`#elseif`/`#else`/
# `#endif` are tracked on a stack so an inner guard cannot steal the outer arm's span.
arm_sites=$(awk '
  BEGIN { depth = 0; label[0] = "all platforms"; cond[0] = ""; flip[0] = 0; start[0] = 1; np[0] = 0 }
  function flush(d, endline,   i) {
    for (i = 1; i <= np[d]; i++)
      print label[d] "\037" cond[d] "\037" flip[d] "\037" start[d] "\037" endline "\037" pend[d, i]
    np[d] = 0
  }
  {
    line = $0
    p = index(line, "//")
    if (p > 0) {
      tail = substr(line, p + 2); q = index(tail, "\"")
      line = substr(line, 1, p - 1) (q > 0 ? substr(tail, q) : "")
    }
    gsub(/^[ \t]+/, "", line); gsub(/[ \t]+$/, "", line)
    if (line ~ /^#[ \t]*if[ \t]/) {
      c = line; sub(/^#[ \t]*if[ \t]*/, "", c)
      depth++; label[depth] = c; cond[depth] = c; flip[depth] = 0; start[depth] = NR; np[depth] = 0
      next
    }
    if (line ~ /^#[ \t]*elseif/) {
      flush(depth, NR - 1)
      c = line; sub(/^#[ \t]*elseif[ \t]*/, "", c)
      # An elseif narrows the arm before it, so the chain is not modelled here. Hand
      # the resolver the text as something it will refuse rather than let the bare
      # os(...) term claim coverage the chain may not have.
      label[depth] = c " elseif"; cond[depth] = "elseif(" c ")"; flip[depth] = 0; start[depth] = NR
      next
    }
    if (line ~ /^#[ \t]*else([ \t]|$)/) {
      flush(depth, NR - 1)
      label[depth] = cond[depth] " else"; flip[depth] = 1; start[depth] = NR
      next
    }
    if (line ~ /^#[ \t]*endif/) {
      if (depth > 0) { flush(depth, NR - 1); depth-- }
      next
    }
    if (line ~ /supportedFamilies\(/) {
      gsub(/[ \t]+/, " ", line)
      np[depth]++; pend[depth, np[depth]] = line
    }
  }
  END { for (d = depth; d >= 0; d--) flush(d, NR) }
' "$intent_file")
if [ -z "$arm_sites" ]; then
  sfail "no supportedFamilies call in $intent_file — the widget would offer whatever WidgetKit defaults to, and a leg that finds nothing must not print clean"
else
  # A HERE-STRING, not a pipe: piped into a while loop the whole body runs in a
  # subshell and every struct_fail it sets would be discarded with it.
  ios_reached=0; macos_reached=0; unresolved_arms=""
  while IFS="$(printf '\037')" read -r arm arm_cond arm_flip arm_start arm_end site; do
    [ -n "$arm" ] || continue
    # Which shipped platforms can this arm be true on? Measured FIRST, before any
    # check below can `continue` past it: a site that fails a later rule is still a
    # site the compiler reaches on its platform, and coverage must not silently lose
    # it. An arm whose condition cannot be resolved is remembered and refused after
    # the loop — guessing here is what let the one-armed `#if os(iOS)` pass green.
    cov=$(arm_reach "$arm_cond" "$arm_flip")
    if [ "$cov" = unknown ]; then
      unresolved_arms="$unresolved_arms [$arm]"
    else
      case " $cov " in *" ios "*) ios_reached=1 ;; esac
      case " $cov " in *" macos "*) macos_reached=1 ;; esac
    fi
    case "$site" in
      *")"*) ;;
      *) sfail "$intent_file has a supportedFamilies call spanning more than one line on the [$arm] arm — this leg refuses rather than measure nothing"
         continue ;;
    esac
    if printf '%s' "$site" | grep -q 'WidgetFamily\.clusterGraphFamilies'; then
      arm_fams="$shared_list"
      arm_src="the shared list"
    else
      arm_fams=$(printf '%s' "$site" | grep -o '\.system[A-Za-z]*' | sed 's/^\.//' | sort -u)
      arm_src="a literal list of its own"
    fi
    if [ -z "$arm_fams" ]; then
      sfail "the [$arm] arm's supportedFamilies call resolves to NO family ($site) — an unresolvable list is a refusal, never a clean run"
      continue
    fi
    sok "the [$arm] arm declares $(printf '%s\n' $arm_fams | sed '/^$/d' | wc -l | tr -d ' ') size(s) via $arm_src, each measured below"

    for fam in $arm_fams; do
      if ! printf '%s\n' $shared_list | grep -qx -- "$fam"; then
        sfail "the [$arm] arm offers $fam, which is not in $families_file's clusterGraphFamilies — no layout layer knows that size"
      fi
      if has "$views_code" "case .$fam:"; then
        sok "$fam (offered on the [$arm] arm) has its own dispatch arm in $views_file"
      else
        sfail "$fam is offered on the [$arm] arm but has no dispatch arm in $views_file — it would draw through default:"
      fi
      if ! printf '%s\n' $density_fams | grep -qx -- "$fam"; then
        sfail "$fam is offered on the [$arm] arm but $families_file maps it to no density — it would draw on the provider's fallback layout"
      fi
    done

    # A literal arm is only defensible when it says something the shared list does
    # not. A literal repeating the shared list is a second list with nothing to say,
    # and a second list nothing reads is exactly how the desktop drifted.
    if [ "$arm_src" = "a literal list of its own" ]; then
      lit=$(printf '%s\n' $arm_fams | tr '\n' ' ')
      sha=$(printf '%s\n' $shared_list | tr '\n' ' ')
      if [ "$lit" = "$sha" ]; then
        sfail "the [$arm] arm repeats the shared list as a literal instead of reading WidgetFamily.clusterGraphFamilies — a duplicate list is the drift that started t_4311500c"
      else
        sok "the [$arm] arm's literal differs from the shared list, so it is a decision rather than a duplicate"
      fi
    fi

    # And every size the arm leaves OUT has to be justified IN THAT ARM, in writing:
    # "an unwritten asymmetry gets fixed by the next person" is the whole reason this
    # card exists — and a reason written on the OTHER arm fixes nothing over there. The
    # span comes from the same extractor that found the call, so the marker is looked
    # for in the lines that arm actually owns. Previously the grep read the whole file
    # (and did it through a pipe, where grep -q + pipefail can lose a real match), so
    # one desktop reason silenced an undocumented iOS drop — re-review item 2, against
    # a failure text that has always said "on that arm".
    arm_span=$(awk -v s="$arm_start" -v e="$arm_end" 'NR>=s && NR<=e' "$intent_file")
    omissions=""
    for fam in $shared_list; do
      if printf '%s\n' $arm_fams | grep -qx -- "$fam"; then continue; fi
      omissions="$omissions $fam"
      if omit_reason_in_span "$arm_span" "$fam"; then
        sok "the [$arm] arm omits $fam and $intent_file:$arm_start-$arm_end writes why (families-omit marker)"
      else
        sfail "the [$arm] arm omits $fam from the shared list with no written reason on that arm (lines $arm_start-$arm_end) — offer it, or add a '// families-omit: .$fam — <reason>' marker inside that span"
      fi
    done
    if [ -z "$omissions" ]; then
      sok "the [$arm] arm omits none of the shared families, so no families-omit marker is owed"
    fi
  done <<< "$arm_sites"

  # Do the arms, TAKEN TOGETHER, reach every platform that builds this file? This is
  # the one-armed form of the bug this card was opened for: `#if os(iOS)` around the
  # call with no `#else` deletes the desktop's family list entirely, WidgetKit then
  # picks a size by itself, and every per-arm measurement above stays green because
  # each one reads only the arm it found. The reason the other arm is owed is measured
  # rather than assumed: $mac_project names this file, so the desktop really does
  # compile it, and the iOS app compiles everything under Sources/ by being the iOS app.
  if [ -n "$unresolved_arms" ]; then
    sfail "the$unresolved_arms arm's supportedFamilies condition is not a plain os(...) disjunction this gate can resolve, so it cannot tell which platform the call reaches — an unresolvable arm is a refusal, never a clean run (rewrite the guard as os(...), or teach arm_reach the condition)"
  elif [ "$macos_builds" -eq 1 ] && [ "$macos_reached" -eq 0 ]; then
    sfail "no supportedFamilies arm reaches macOS while $mac_project compiles $intent_file — the desktop tile would get NO supportedFamilies at all and WidgetKit would pick its size: the arm-nobody-reads bug of t_4311500c in its one-armed form (#if os(iOS) with no #else)"
  elif [ "$ios_reached" -eq 0 ]; then
    sfail "no supportedFamilies arm reaches iOS while $intent_file builds into the iOS app — the Home Screen widget would take WidgetKit's default family and every size the operator can pick would be gone"
  elif [ "$macos_builds" -eq 1 ]; then
    sok "the supportedFamilies arms together reach both platforms that build $intent_file (iOS: Sources/, macOS: $mac_project names it)"
  else
    sok "the supportedFamilies arms reach iOS, the only platform that builds $intent_file ($mac_project does not name it)"
  fi
fi

# 2. Home-screen-only. No accessory family anywhere: the card asks for Home
#    Screen sizes, `widget_view_dispatch` gates this surface as home-screen-only,
#    and WidgetFamily's accessory cases are `@available(macOS, unavailable)` — so
#    adding one means an availability guard, a label rule, and switching the
#    dispatch mode, not just adding a case. (The positive list above is what makes
#    that true on both platforms: all four families exist on macOS, which is why
#    this file needs no `#if os(iOS)` around the list itself.)
if has "$widget_code" "\.accessory"; then
  sfail "an .accessory family appears in the graph widget: this surface is gated home-screen-only"
else
  sok "no accessory family anywhere in the graph widget (home-screen-only holds)"
fi
# The single-source claim used to sit here as a grep for the TEXT of one arm's call
# (`supportedFamilies(WidgetFamily.clusterGraphFamilies)`). It is deleted rather than
# kept alongside §1, because that grep passed while the desktop arm hardcoded a
# different list — grepping one arm's text cannot see the other arm, and a second
# owner of a weaker version of a rule is how a rule stops being true without any gate
# printing anything. §1 now compares every arm by its RESOLVED families instead.

# 3. The family → density map is a positive list written out per size, because
#    WidgetFamily is Int-backed off-iOS and the harness cannot execute this side.
if has "$families_code" "case .systemSmall: return .sparkline" \
   && has "$families_code" "case .systemMedium: return .nodeOverlay" \
   && has "$families_code" "case .systemLarge, .systemExtraLarge: return .smallMultiples" \
   && has "$families_code" "default: return nil"; then
  sok "family -> density is the card's map, written as a positive list"
else
  sfail "family -> density drifted from the card's map in $families_file"
fi
# The card asks for ONE PANEL PER NODE, so the panel count must not be capped by
# a transcribed constant: a `limit: 4` would quietly drop a real host from a
# nine-node fleet. If a cap is ever justified it has to be a decision that says
# why, and this check has to be edited with it.
if has "$reading_code" "var panelLimit: Int? { nil }"; then
  sok "small multiples are uncapped by design (ClusterGraphDensity.panelLimit says so)"
else
  sfail "the 'no panel cap' rule vanished from $reading — a cap must be a decision, not a leftover"
fi

# 4. The canonical route ruling (DECISION-t_447cc7fc): `/v1/metrics/history` is
#    RETIRED and every history read goes through WidgetDataClient.clusterHistory
#    on `/v1/cluster/history`. A grep is the right tool here precisely because the
#    retired path would compile and run perfectly well — this is a decision the
#    type system cannot hold. `MetricsHistoryTrend` is not edited on this card (a
#    separate card retires it), so the check is that nothing NEW reaches for it.
if has "$widget_code" "v1/metrics/history" || has "$widget_code" "MetricsHistoryTrend"; then
  sfail "a graph-widget file names the RETIRED /v1/metrics/history read (see DECISION-t_447cc7fc)"
else
  sok "no graph-widget file reaches the retired /v1/metrics/history"
fi
if has "$intent_code" "client.clusterHistory("; then
  sok "the widget's only history read is WidgetDataClient.clusterHistory"
else
  sfail "$intent_file no longer reads through WidgetDataClient.clusterHistory"
fi
if has "$client_code" "func clusterHistory"; then
  sok "the client method the widget calls exists on the canonical path"
else
  sfail "WidgetDataClient.clusterHistory is gone — the widget's read would silently stop working"
fi
if has "$client_code" "cluster/history"; then
  sok "the client itself still points at the canonical /v1/cluster/history path"
else
  sfail "the client no longer names /v1/cluster/history"
fi

# 5. The operator's amendment: the DEFAULT window is the 2-second tier, that
#    recommendation lives in the shared layer rather than as a picker literal, and
#    the resolution the ROUTE served is what gets printed (a 7-day chart must not
#    read like live 2-second data).
if has "$intent_code" "default: .fifteenMinutes"; then
  sok "the window parameter defaults to 15 m (the 2 s tier), as the operator ruled"
else
  sfail "the window parameter no longer defaults to 15 m in $intent_file"
fi
if has "$reading_code" "static let recommended: ClusterGraphWindow = .fifteenMinutes"; then
  sok "the 15 m default is a shared-layer decision, not a picker literal"
else
  sfail "ClusterGraphWindow.recommended is gone — the 2 s default now lives only in the UI"
fi
if has "$reading_code" "static func text(resolutionS: Int) -> String"; then
  sok "the served resolution has a caption rule the harness executes"
else
  sfail "ClusterGraphResolutionCaption.text vanished — the resolution would be inferred again"
fi
if has "$views_code" "ClusterGraphAxisRule.pixelColumns(measuredWidth:"; then
  sok "the reduction is driven by the view's MEASURED width, not a guessed one"
else
  sfail "$views_file no longer measures its own width — the pixel-column count would be invented"
fi
if has "$views_code" "chartYScale(domain: mini.axisFloor...mini.axisCeiling)"; then
  sok "every axis is drawn over the data's own domain"
else
  sfail "$views_file no longer uses the derived axis domain — an axis ceiling would be invented again"
fi
if hasnt "$widget_code" "0...100" && hasnt "$reading_code" "0...100"; then
  sok "no hard-coded axis range anywhere in the graph widget"
else
  sfail "a hard-coded 0...100 axis is back — a fleet that never left 40 C would draw a flat line"
fi
# A hole in the data must end the polyline. The reduction's own `segments()`
# cannot guarantee that (it computes breaks over the points it was handed, and
# when nothing needed reducing those still include the valueless samples whose
# timestamps step along at the tier interval), so the draw-time rule lives in the
# shared layer and the harness proves it. Both halves are checked here because a
# view that went back to raw points would pass the harness.
if has "$reading_code" "static func drawSegments(of line: ClusterGraphLine)"; then
  sok "the draw-time gap rule is in the shared layer, where the harness can execute it"
else
  sfail "drawSegments vanished from $reading — a hole would again be drawn as a slope"
fi
if has "$views_code" "ClusterGraphReading.drawSegments(of: line)"; then
  sok "the views draw through drawSegments, not through the reduction's raw points"
else
  sfail "$views_file draws raw points again"
fi

# 6. The pickers. If one offers a value the shared vocabulary or the route's
#    windows do not have, the widget sends a request the API answers with a 400
#    and the user sees an empty chart. Every extraction is asserted NON-EMPTY
#    before it is compared: a silent empty extraction would turn a check into a
#    pass, which is the one outcome worse than a failure.
option_block() { awk "/^enum $1: String, AppEnum {/,/^}/" <<<"$intent_code"; }

metric_set_cases=$(option_block ClusterMetricSetOption | grep -oE '^    case [a-z]+$' | awk '{print $2}' | tr '\n' ' ')
if [ -z "$metric_set_cases" ]; then
  sfail "could not read ClusterMetricSetOption's cases out of $intent_file"
else
  sok "picker metric sets:$metric_set_cases"
fi
metric_set_count=$(option_block ClusterMetricSetOption | grep -cE '^    case ')
if [ "${metric_set_count:-0}" -ne 5 ]; then
  sfail "the metric-set picker offers ${metric_set_count:-0} sets, not the card's five"
fi

window_pairs=$(option_block ClusterGraphWindowOption \
  | grep -oE '^    case [a-zA-Z0-9]+ = "[^"]+"' \
  | sed -E 's/^    case [a-zA-Z0-9]+ = "([^"]+)"/\1/' | tr '\n' ' ')
if [ -z "$window_pairs" ]; then
  sfail "could not read ClusterGraphWindowOption's raw values out of $intent_file"
else
  sok "picker window wire values:$window_pairs"
fi
window_count=$(option_block ClusterGraphWindowOption | grep -cE '^    case ')
if [ "${window_count:-0}" -ne 5 ]; then
  sfail "the window picker offers ${window_count:-0} windows: the card's four plus the operator's 15 m"
fi

# Each picker window must be a window the shared layer can send, with the SAME
# wire value — comparing the raw-value literals is what catches a picker that
# offers "30m" while `_parse_history_window` only accepts 15m/1h/6h/24h/7d.
wire_missing=""
for value in $window_pairs; do
  if ! has "$reading_code" "= \"$value\""; then
    wire_missing="$wire_missing $value"
  fi
done
if [ -z "$wire_missing" ]; then
  sok "every window the picker offers is a ClusterGraphWindow the route parses"
else
  sfail "picker windows with no shared-layer entry:$wire_missing"
fi

set_missing=""
for m in $metric_set_cases; do
  if ! has "$layer_code" "    case $m"; then
    set_missing="$set_missing $m"
  fi
done
if [ -z "$set_missing" ]; then
  sok "every metric set the picker offers exists in ClusterHistoryMetricGroup"
else
  sfail "picker sets with no shared vocabulary entry:$set_missing"
fi

# 7. The wiring that makes the Settings row reach this widget at all. A widget
#    that compiles but is missing from the bundle never appears in the gallery; a
#    surface with no settings key cannot be paused or paced, which the fleet's
#    settings contract requires of every configurable surface.
if has "$bundle_code" "ClusterGraphWidget()"; then
  sok "the graph widget is registered in HSCCWidgetsBundle"
else
  sfail "the graph widget is not in HSCCWidgetsBundle — it would never appear in the gallery"
fi
if has "$store_code" 'case widgetClusterGraph = "widget.clusterGraph"'; then
  sok "the surface has a settings key, so it can be paused and paced"
else
  sfail "the graph surface has no settings key in $store — the Settings row would be missing"
fi
if has "$labels_code" 'case .widgetClusterGraph: return "Cluster Graph Widget"'; then
  sok "the surface has a display label (the settings screen never prints a raw key or address)"
else
  sfail "the graph surface has no display label in $labels"
fi

# 8. The refresh policy comes from the surface settings, not from a literal:
#    `.atEnd` when the operator paused the surface, `.after(date)` otherwise. A
#    widget that hard-codes a cadence ignores a pause someone can see.
if has "$intent_code" "policy: .atEnd" && has "$intent_code" "policy: .after("; then
  sok "the timeline honours the registry's pause and cadence"
else
  sfail "$intent_file no longer derives its timeline policy from the surface settings"
fi

# 8b. The GATE ORDER, measured inside the function that owns it. Comparing
#     file-global `grep -n` line numbers is the trap this repo has already been
#     bitten by (makeIfConfigured() appears more than once in this file), so the
#     provider's own entry function is sliced out first and the three decisions
#     are ordered within it.
#
#     The shipped order here is invite, then pause, then read — which is the
#     INVERSE of the wording in the fleet's widget convention ("decide !isActive
#     before makeIfConfigured()"). It is deliberately the other way round for this
#     widget, and the reason is that `ClusterGraphEntry.status` is ONE value, not
#     two booleans: `configured` and `paused` cannot both be true, so whichever
#     guard fires first decides the word the user reads. Ask the paused question
#     first and a device that has lost its token AND has the surface switched off
#     prints "Paused in Settings" forever, hiding the one sentence that would tell
#     someone to configure the app. The rule the fleet ordering actually protects
#     — a paused surface must issue no request — is what the third assertion
#     measures, and it holds either way round.
entry_region=$(awk '/private func entry\(for configuration:/{f=1} f {print} f && /^    \}$/ {exit}' <<<"$intent_code")
if [ -z "$entry_region" ]; then
  sfail "could not slice the provider's entry function out of $intent_file — the ordering checks below would be vacuous"
else
  line_of() { awk -v pat="$2" 'index($0, pat) {print NR; exit}' <<<"$1"; }
  invite_at=$(line_of "$entry_region" "WidgetDataClient.makeIfConfigured()")
  pause_at=$(line_of "$entry_region" "isActive")
  read_at=$(line_of "$entry_region" "clusterHistory(")
  if [ -n "$invite_at" ] && [ -n "$pause_at" ] && [ "$invite_at" -lt "$pause_at" ]; then
    sok "the invite is decided before the pause (a lost token can never be reported as 'Paused')"
  else
    sfail "the invite is no longer decided before the pause in the provider's entry function (invite=$invite_at pause=$pause_at)"
  fi
  if [ -n "$pause_at" ] && [ -n "$read_at" ] && [ "$pause_at" -lt "$read_at" ]; then
    sok "the pause is decided before any history request (a paused surface issues zero requests)"
  else
    sfail "the provider reads history before deciding the pause (pause=$pause_at read=$read_at) — a paused surface would still fetch"
  fi
fi

# 8c. Every gated body gates BOTH states in ONE step. A body that tests
#     `configured` and then separately tests `paused` is a body that will forget
#     one of them the next time someone edits it — the failure this repo's audit
#     found twice, and invisible to a compile because both branches render
#     something. So a configured-only `if` is a finding.
if has "$views_code" "if !entry.configured {"; then
  sfail "a body in $views_file tests !configured alone — it must test both states in one step"
else
  sok "every gated body tests configured and paused together"
fi
gate_steps=$(grep -c "if !entry.configured || entry.paused {" <<<"$views_code")
if [ "${gate_steps:-0}" -lt 3 ]; then
  sfail "only ${gate_steps:-0} gated bodies found in $views_file — the three content views must each gate"
else
  sok "${gate_steps} content bodies each carry the one-step gate"
fi

# 9. Every metric set the picker can produce must be exercised by a fixture. That
#    check runs in the HARNESS: it decodes each fixture with the REAL wire model
#    and asks whether the set produced any series at all. A filename pattern here
#    would pass on a payload that carries nothing of the set.

echo ""
if [ "$struct_fail" -eq 0 ]; then
  echo "structural leg: all checks passed"
else
  echo "structural leg: FAILED"
fi

rm -rf -- "$TMP"
exit $(( harness_rc + struct_fail ))
