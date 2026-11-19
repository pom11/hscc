#!/bin/bash
# negative_legs.sh — the negative proof behind §1 of ../cluster_graph_check.sh
# (t_4311500c). Run it when you change that leg; a green gate proves nothing on its
# own, and this file is the only thing that shows the leg can fail, and fail BY NAME.
#
#   ios-app/scripts/cluster_graph_check/negative_legs.sh   (run from anywhere)
#
# IT MUTATES TRACKED SOURCE FILES. It refuses to start on a dirty tree and restores
# every file byte-for-byte (sha256) plus its mode afterwards, but a run killed halfway
# leaves the tree mutated — so run it on a clean checkout, and if it dies, the
# "!!! RESTORE MISMATCH" line tells you whether it got that far. Nothing executes this
# file automatically: probes_check.sh globs only scripts/*_probe.sh, and the workflows
# name their scripts, which is why this lives here and is not named *_probe.sh.
#
# WHY EVERY LEG ASSERTS THREE THINGS AND NOT ONE. The first draft of this harness lost
# five legs to BSD awk rejecting newlines in `-v` values: the mutation never applied,
# the gate ran on an UNCHANGED file, printed rc=0, and the leg reported FAIL — which
# reads to its author like "the gate did not catch it" and is really "this leg measured
# nothing". A leg cannot tell those apart unless it checks that something happened, so
# (a) the mutated file's sha MUST differ from the pre-mutation value, else the leg is
# reported BROKEN, not failed. Then (b) rc and message, and (c) restore + mode.
# Restoring uses cp, never mv: mv from mktemp silently replaced a 100755 gate with a
# 644 file, and a gate nobody can exec is a gate nobody runs. Never `git checkout --`
# either — it restores from the INDEX and would wipe uncommitted work.
set -uo pipefail
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
R="$(cd "$SELF_DIR/../.." && pwd)"          # ios-app/
cd "$R" || exit 1
ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "not a git repo"; exit 1; }

W=Sources/HSCCWidgets
GATE=scripts/cluster_graph_check.sh
SURFACE=scripts/widget_view_dispatch/widgetClusterGraph.sh
FILES=("$W/GraphWidget.swift" "$W/GraphWidgetViews.swift" "$W/GraphWidgetMetrics.swift" "$GATE" "$SURFACE")
NEEDLE='.supportedFamilies(WidgetFamily.clusterGraphFamilies)'

# Refuse on a dirty tree: this harness writes into tracked files, so uncommitted work
# in one of them would be silently indistinguishable from a mutation.
dirty=0
for f in "${FILES[@]}"; do
  [ -n "$(git -C "$ROOT" status --porcelain -- "ios-app/$f")" ] && { echo "refusing: $f is dirty — commit or stash it first"; dirty=1; }
done
[ "$dirty" -eq 0 ] || exit 1

BK=$(mktemp -d)
trap 'rm -rf "$BK"' EXIT
declare -a SHA_BEFORE
for i in "${!FILES[@]}"; do
  SHA_BEFORE[$i]=$(shasum -a 256 "${FILES[$i]}" | cut -d' ' -f1)
  cp -p "${FILES[$i]}" "$BK/$i"
done
sha_of() { shasum -a 256 "$1" | cut -d' ' -f1; }
# place <file> <needle> <replacement-file> — line-accurate, no escapes, no mv.
place() {
  awk -v REP="$3" -v NEEDLE="$2" '
    BEGIN { while ((getline l < REP) > 0) b[++n] = l }
    index($0, NEEDLE) > 0 { for (i = 1; i <= n; i++) print b[i]; next }
    { print }' "$1" > "$BK/out" && cp "$BK/out" "$1"
}
drop_line() { awk -v NEEDLE="$2" 'index($0, NEEDLE) > 0 {next} {print}' "$1" > "$BK/out" && cp "$BK/out" "$1"; }

PASS=0; FAILED=0; BROKEN=0
restore() {
  local bad=0 i
  # Plain cp: when the destination exists, cp writes INTO it and keeps its mode.
  for i in "${!FILES[@]}"; do cp "$BK/$i" "${FILES[$i]}"; done
  for i in "${!FILES[@]}"; do
    [ "$(sha_of "${FILES[$i]}")" = "${SHA_BEFORE[$i]}" ] || bad=1
  done
  [ "$bad" -eq 0 ] || { echo "  !!! RESTORE MISMATCH — tree may be dirty"; BROKEN=$((BROKEN+1)); }
}
# run_leg <id> <mutated-file-index> <expect-string> <mutate-fn> <want_rc: nonzero|zero>
# Which gate runs is DERIVED from what got mutated, so the two cannot drift apart here:
# mutate the graph gate and you are testing the file that delegates to it; mutate a Swift
# subject and you are testing the graph gate. The first draft keyed both off one index,
# and L10 consequently asserted that the mutation had landed in a file it never touched.
run_leg() {
  local id="$1" tidx="$2" want="$3" mut="$4" wantrc="$5" gate out rc before
  gate="$GATE"; [ "$tidx" = "3" ] && gate="$SURFACE"
  before="${SHA_BEFORE[$tidx]}"
  "$mut"
  if [ "$(sha_of "${FILES[$tidx]}")" = "$before" ]; then
    echo "  BROKEN  $id  — the mutation did not change ${FILES[$tidx]}; the leg asserts nothing"
    restore; BROKEN=$((BROKEN+1)); return
  fi
  out=$(bash "$gate" 2>&1); rc=$?
  restore
  if { [ "$wantrc" = nonzero ] && [ "$rc" -ne 0 ]; } || { [ "$wantrc" = zero ] && [ "$rc" -eq 0 ]; }; then
    if printf '%s' "$out" | grep -qF -- "$want"; then
      echo "  PASS  $id  (rc=$rc, want-rc=$wantrc, message matched)"; PASS=$((PASS+1)); return
    fi
  fi
  echo "  FAIL  $id  rc=$rc want-rc=$wantrc want-msg=[$want]"
  printf '%s\n' "$out" | grep -E "FAIL:|ok: the \[" | head -5 | sed 's/^/          /'
  FAILED=$((FAILED+1))
}
leg() { run_leg "$1" "${2:-0}" "$3" "$4" nonzero; }

# --- mutations (index: 0=GraphWidget 1=Views 2=Metrics 3=gate) --------------------
# L1 — the bug this card was opened for, restored verbatim: a desktop arm of three,
#      no written reason. The leg that proves the gate now sees the arm nobody read.
m1() { printf '#if os(iOS)\n%s\n#else\n        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge])\n#endif\n' "$NEEDLE" > "$BK/b"; place "$W/GraphWidget.swift" "$NEEDLE" "$BK/b"; }
# L2 — the SAME asymmetry, written. Must PASS: the marker is the sanctioned form, and
#      a rule that only ever reddens is a ban wearing a rule's clothes.
m2() { printf '#if os(iOS)\n%s\n#else\n        // families-omit: .systemExtraLarge — the desktop gallery caps at large for this surface\n        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge])\n#endif\n' "$NEEDLE" > "$BK/b"; place "$W/GraphWidget.swift" "$NEEDLE" "$BK/b"; }
# L3 — marker names the size but carries no reason.
m3() { printf '#if os(iOS)\n%s\n#else\n        // families-omit: .systemExtraLarge (desktop only)\n        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge])\n#endif\n' "$NEEDLE" > "$BK/b"; place "$W/GraphWidget.swift" "$NEEDLE" "$BK/b"; }
# L4 — a literal arm identical to the shared list: a second list with nothing to say.
m4() { printf '#if os(iOS)\n%s\n#else\n        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge, .systemExtraLarge])\n#endif\n' "$NEEDLE" > "$BK/b"; place "$W/GraphWidget.swift" "$NEEDLE" "$BK/b"; }
# L5 — t_b3ae3809 itself: extraLarge loses its own arm and falls into the large body.
m5() { drop_line "$W/GraphWidgetViews.swift" 'case .systemExtraLarge:'; }
# L6 — a size dropped from the shared list: the pin must fail BY NAME, not as a count.
m6() { sed 's/        \.systemSmall, \.systemMedium, \.systemLarge, \.systemExtraLarge/        .systemSmall, .systemMedium, .systemExtraLarge/' \
        "$W/GraphWidgetMetrics.swift" > "$BK/out" && cp "$BK/out" "$W/GraphWidgetMetrics.swift"; }
# L7 — a call the leg cannot resolve (spans lines) must REFUSE, not read clean.
m7() { printf '.supportedFamilies(\n            WidgetFamily.clusterGraphFamilies\n        )\n' > "$BK/b"; place "$W/GraphWidget.swift" "$NEEDLE" "$BK/b"; }
# L8 — no call at all: WidgetKit's default would decide the sizes, so silence is a finding.
m8() { drop_line "$W/GraphWidget.swift" "$NEEDLE"; }
# L9 — an accessory-only list: the extractor sees no .system* at all, which is a
#      refusal, never "the widget offers nothing and that is fine". Grep-only by
#      design: an accessory family on a macOS arm is exactly what it plants, and it
#      does not compile — which is itself the reason the guard exists.
m9() { printf '        .supportedFamilies([.accessoryInline])\n' > "$BK/b"; place "$W/GraphWidget.swift" "$NEEDLE" "$BK/b"; }
# L10 — the delegation: delete the rule FROM ITS OWNER and the delegating file must
#       redden by name rather than the claim quietly orphaning. The block goes
#       whole (if/sfail/else/sok/fi) and the owner is proven to still PARSE, because
#       an owner broken by syntax would redden the delegate for the wrong reason.
m10() {
  local n
  n=$(grep -nF 'if has "$widget_code" "\.accessory"; then' "$GATE" | cut -d: -f1)
  [ -n "$n" ] || { echo "    (m10: owner's rule line not found)"; return 1; }
  awk -v s="$n" 'NR>=s && NR<=s+4 {next} {print}' "$GATE" > "$BK/out" && cp "$BK/out" "$GATE"
  bash -n "$GATE" || { echo "    (m10: owner no longer parses after removing the block)"; return 1; }
}
# L11 — the one-armed form of the shape this card exists to kill (re-review item 1):
#       `#if os(iOS)` around the call with NO `#else`. The desktop then gets no
#       supportedFamilies at all and WidgetKit picks the size, while every per-arm
#       measurement stays green because each one reads only the arm it found. This is
#       the MOST likely reintroduction — this card's parent text and the file's own old
#       comment both said the `#if os(iOS)` "is real and must stay".
m11() { printf '#if os(iOS)\n%s\n#endif\n' "$NEEDLE" > "$BK/b"; place "$W/GraphWidget.swift" "$NEEDLE" "$BK/b"; }
# L12 — a `families-omit` reason written on the WRONG arm (re-review item 2). The iOS
#       arm drops .systemSmall with nothing written, and the .systemSmall reason sits on
#       the `#else` arm — where .systemSmall IS offered, so the reason is true there and
#       excuses nothing here. File-wide marker matching let exactly this through (rc=0);
#       the reason must fall inside the span of the arm that owes it.
m12() { printf '#if os(iOS)\n        .supportedFamilies([.systemMedium, .systemLarge, .systemExtraLarge])\n#else\n        // families-omit: .systemSmall — desktop tiles start at medium\n        // families-omit: .systemExtraLarge — the desktop gallery caps at large\n        .supportedFamilies([.systemMedium, .systemLarge])\n#endif\n' > "$BK/b"; place "$W/GraphWidget.swift" "$NEEDLE" "$BK/b"; }
# L13 — the same two asymmetries, each justified ON ITS OWN ARM. Must PASS: the span
#       rule is a scope, not a ban, and without this leg L12 would only prove that a
#       literal arm reddens, not that the marker was in the wrong place.
m13() { printf '#if os(iOS)\n        // families-omit: .systemSmall — the iPhone small tile has no room for a chart\n        .supportedFamilies([.systemMedium, .systemLarge, .systemExtraLarge])\n#else\n        // families-omit: .systemSmall — desktop tiles start at medium too\n        // families-omit: .systemExtraLarge — the desktop gallery caps at large\n        .supportedFamilies([.systemMedium, .systemLarge])\n#endif\n' > "$BK/b"; place "$W/GraphWidget.swift" "$NEEDLE" "$BK/b"; }

echo "sanity (the harness's needles must be unique — ASSERTED):"
# Every leg above plants and removes text BY NEEDLE, so a second occurrence of one
# would make a mutation hit two places (or the wrong one) and the leg would then
# assert something about a file it never shaped — the same "measured nothing" failure
# class the BROKEN verdict exists for, arriving by another route. The block used to
# PRINT these counts and carry on regardless: it informed nobody and refused nothing.
sanity_bad=0
check_needle() {   # <file> <pattern> — exactly one occurrence or the harness stops
  local n
  n=$(grep -cF -- "$2" "$1")
  echo "  $n occurrence(s) of [$2] in $1 (want 1)"
  if [ "$n" -ne 1 ]; then sanity_bad=1; fi
}
check_needle "$W/GraphWidget.swift" "$NEEDLE"
check_needle "$W/GraphWidgetViews.swift" 'case .systemExtraLarge:'
check_needle "$GATE" 'if has "$widget_code" "\.accessory"; then'
if [ "$sanity_bad" -ne 0 ]; then
  echo "REFUSING: a needle is not unique, so the legs below would assert nothing"
  exit 1
fi
echo "control run (clean tree, want rc=0):"
bash "$GATE" > "$BK/control.txt" 2>&1; echo "  rc=$? ok=$(grep -c '  ok: ' "$BK/control.txt")"
echo "legs:"
leg  L1 0 "omits systemExtraLarge from the shared list with no written reason" m1
run_leg L2 0 "structural leg: all checks passed" m2 zero
leg  L3 0 "omits systemExtraLarge from the shared list with no written reason" m3
leg  L4 0 "repeats the shared list as a literal" m4
leg  L5 1 "systemExtraLarge is offered on the [all platforms] arm but has no dispatch arm" m5
leg  L6 2 "systemLarge is missing from clusterGraphFamilies" m6
leg  L7 0 "spanning more than one line" m7
leg  L8 0 "no supportedFamilies call in" m8
leg  L9 0 "resolves to NO family" m9
run_leg L10 3 "no accessory family anywhere in the graph widget (home-screen-only) is delegated to" m10 nonzero
leg  L11 0 "no supportedFamilies arm reaches macOS" m11
leg  L12 0 "the [os(iOS)] arm omits systemSmall" m12
run_leg L13 0 "structural leg: all checks passed" m13 zero
echo "control run again (must match the first byte for byte):"
bash "$GATE" > "$BK/control2.txt" 2>&1; echo "  rc=$? ok=$(grep -c '  ok: ' "$BK/control2.txt")"
cmp -s "$BK/control.txt" "$BK/control2.txt" && echo "  control output BYTE-IDENTICAL" || echo "  !!! control drifted"
echo "  tree vs HEAD: $(git status --porcelain | wc -l | tr -d ' ') file(s) modified (want 0)"
echo ""
echo "NEGATIVE LEGS PASSED=$PASS FAILED=$FAILED BROKEN=$BROKEN"
[ "$FAILED" -eq 0 ] && [ "$BROKEN" -eq 0 ] && echo "PASSED" || echo "FAILED"
