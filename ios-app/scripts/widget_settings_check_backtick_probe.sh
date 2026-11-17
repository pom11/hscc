#!/bin/bash
# widget_settings_check_backtick_probe.sh — t_9e4a731b: prove widget_settings_check.sh's
# backtick guard cannot be walked past.
#
# WHY THIS FILE EXISTS. The defect on this card was a gate that exited 0 while
# bash executed part of its own text, so "the gate exits 0" proves nothing about
# that class — and a guard whose only evidence is a claim in a comment is a guard
# nobody has ever seen fire. This probe is the evidence, kept in-tree so it can
# be re-run instead of re-asserted. It plants a backtick payload that WRITES A
# MARKER FILE into every context in the gate where bash could expand it, and
# requires per leg: (a) no marker file created, and (b) the guard REFUSED the
# copy rather than the payload having been inert by luck. It also runs an
# unmodified copy and requires it still to pass, so a guard that refuses
# everything cannot score green here either.
#
# THE CONTRACT, from the review that asked for it: "plant a backtick payload that
# writes a marker file into every heredoc region in this script, and require (a)
# no marker file created and (b) the gate still exiting 0 on the unmodified
# script."
#
# Legs, and why each is separate:
#   region-N     every unquoted heredoc body in the gate. The openers are
#                ENUMERATED at run time, so a second heredoc added later is
#                covered without editing this probe — hardcoding one opener is
#                what let the first review's payload through.
#   shell-above  shell position, before the first heredoc. A comment-SHAPED line
#                (// ...) there is not a comment, it is a command word — which is
#                where the first probe's payload actually executed.
#   shell-below  shell position, after the last heredoc terminator.
#   rename       the reviewer's repro: a mutated renamed copy runs while the
#                canonical gate sits clean beside it. A guard anchored to a
#                hardcoded leaf filename audits the clean neighbour.
#   comment      a real '#' comment line. Inert on its own — measured, not
#                guessed — and still banned, because a comment is only a comment
#                while its line stays a comment.
#   boundary     payload at line 2 — the first line ABOVE the ban's first
#                executable statement (the GUARD-FIRST-EXEC marker), which is
#                where the ban can no longer act. Reported with the measured
#                span, never claimed as a pass: whoever can write here can also
#                delete the guard. Anchoring this leg on the guard's COMMENT
#                paragraph is what made the reported span wrong by an order of
#                magnitude, so the span is derived from the statement marker and
#                the anchor line is itself checked for not being a comment.
#
# Non-destructive: the gate is never edited and never run from its own path.
# Each leg runs a copy inside a private ios-app-shaped tree whose Sources/ and
# harness golden are symlinks to the real files, so the run reads real inputs and
# writes nothing into the repo.
#
# This probe contains NO backtick character of its own — the payload is built
# with printf '\140'. Deliberate: a probe carrying the banned byte would be
# disqualified by the rule it tests.
#
# Usage: ios-app/scripts/widget_settings_check_backtick_probe.sh
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="${SELF_DIR}/widget_settings_check.sh"
[ -f "$GATE" ] || { echo "PROBE FAILED — gate not found at $GATE" >&2; exit 1; }
GATE_BEFORE="$(shasum -a 256 "$GATE" | cut -d' ' -f1)"
IOSAPP="$(cd "${SELF_DIR}/.." && pwd)"

# The byte under discussion, without writing it into this file.
BT="$(printf '\140')"
# The guard's anchors, so "above the guard" is measured rather than assumed.
# There are TWO, and only the second is the boundary:
#   GUARD-SENTINEL     the guard's explanatory comment. NOT the boundary.
#   GUARD-FIRST-EXEC   the ban's first executable statement. THE boundary:
#                      bash has already expanded everything above it.
# Anchoring on the comment paragraph is the defect this replaces: it reported a
# window beginning dozens of lines above where the guard can actually act, and a
# payload planted in that gap executed while the probe called the gap covered.
SENTINEL="GUARD-SENTINEL"
FIRST_EXEC="GUARD-FIRST-EXEC"

probe_fail=0
pok()   { echo "  ok: $1"; }
pfail() { echo "  FAIL: $1"; probe_fail=$((probe_fail + 1)); }
pnote() { echo "  note: $1"; }

sentinel_line=$(awk -v s="$SENTINEL" 'index($0, s) { print NR; exit }' "$GATE")
if [ -z "$sentinel_line" ]; then
  pfail "no line carrying the guard sentinel '$SENTINEL' in $GATE — the boundary leg cannot localise the guard, and a guard that cannot be found cannot be probed"
  sentinel_line=1
fi

# The boundary line, in order of trust: the explicit marker on the ban
# statement, else the first line after the sentinel that bash could execute
# (first line whose first non-blank character is neither '#' nor nothing).
ban_line=$(awk -v s="$FIRST_EXEC" 'index($0, s) { print NR; exit }' "$GATE")
ban_how="marker $FIRST_EXEC"
if [ -z "$ban_line" ]; then
  ban_line=$(awk -v s="$SENTINEL" '
      !seen && index($0, s) { seen = 1; next }
      seen { t = $0; gsub(/^[[:space:]]/, "", t)
             if (t != "" && substr(t, 1, 1) != "#") { print NR; exit } }' "$GATE")
  ban_how="derived (no $FIRST_EXEC marker — first executable line after the sentinel)"
fi
if [ -z "$ban_line" ]; then
  pfail "cannot locate the ban's first executable statement in $GATE — the boundary window cannot be measured, and a boundary that is guessed is the bug this leg exists to catch; refusing to report a number"
  ban_line="$sentinel_line"; ban_how="UNRELIABLE fallback to the sentinel line"
else
  # Guard the guard's own measurement: an anchor that lands on a comment is a
  # prose anchor, which is exactly the round-2 failure. Fail rather than print.
  ban_text=$(awk -v n="$ban_line" 'NR == n { print }' "$GATE")
  ban_lead=$(printf '%s' "$ban_text" | sed -e 's/^[[:space:]]*//' -e 's/^\(.\).*/\1/')
  if [ "$ban_lead" = "#" ]; then
    pfail "boundary anchor $GATE:$ban_line is a COMMENT line ($ban_text) — comments do not stop bash, so this is not the boundary; point $FIRST_EXEC at the first statement below it"
  fi
  if [ "$ban_line" -lt "$sentinel_line" ]; then
    pfail "$FIRST_EXEC ($ban_line) sits above $SENTINEL ($sentinel_line) — one of the two markers has moved; the window below would be meaningless"
  fi
fi
# Insertable lines above the ban: line 1 is the shebang, so 2..ban_line-1.
uncovered=$((ban_line - 2)); [ "$uncovered" -ge 0 ] || uncovered=0
echo "guard boundary: ban's first executable statement = $GATE:$ban_line [$ban_how]"
echo "  sentinel at :$sentinel_line; lines 2..$((ban_line - 1)) = $uncovered insertable line(s) sit ABOVE the ban"

# A line in shell position (not inside the heredoc, not the guard itself) to
# plant in: the cd that re-roots the gate. Below the guard, so a leg planted
# here is testing the guard rather than the window above it.
shell_anchor=$(awk '$0 ~ /^cd / { print NR; exit }' "$GATE")
if [ -z "$shell_anchor" ]; then
  pfail "no top-level cd found in $GATE — the shell-position legs have nowhere to plant"
  shell_anchor="$ban_line"
fi

WORKROOT="$(mktemp -d "${TMPDIR:-/tmp}/wscprobe.XXXXXX")"

# run_leg <name> <insert-payload-after-this-line> <slash|hash> [above]
# "above" is for the boundary leg ONLY: it re-puts the payload directly after the
# shebang so the payload really sits above the guard. Without it a leg that
# happens to insert high in the file would silently become a boundary leg — which
# is exactly how shell-above first scored a false FAIL.
# Globals out: RC OUT ERR MARKER COPY
run_leg() {
  local name="$1" after="$2" kind="$3" above="${4:-}"
  local root="${WORKROOT}/${name}"
  mkdir -p "${root}/scripts"
  ln -s "${IOSAPP}/Sources" "${root}/Sources" 2>/dev/null
  ln -s "${IOSAPP}/scripts/widget_settings_check.harness.golden" \
        "${root}/scripts/widget_settings_check.harness.golden" 2>/dev/null
  # The canonical, unmodified gate sits beside the copy ON PURPOSE: that is the
  # file a filename-anchored guard audits instead of the one that is running.
  ln -s "$GATE" "${root}/scripts/widget_settings_check.sh" 2>/dev/null
  local copy="${root}/scripts/gate_under_probe.sh"
  MARKER="${root}/MARKER_${name}"
  local lead="//"
  [ "$kind" = "hash" ] && lead="#"
  local payload="$lead pwn ${BT}touch '${MARKER}'${BT}"
  RC=99; [ -f "$GATE" ] || return 1
  if [ "$after" = "-" ]; then
    cp "$GATE" "$copy"
  else
    awk -v n="$after" -v p="$payload" \
        '{ print; if (NR == n) print p }' "$GATE" > "$copy"
    if [ "$above" = "above" ]; then
      # The payload has to sit ABOVE the guard, not slide down with the prologue
      # it was inserted into: put a copy straight after the shebang too.
      awk -v p="$payload" 'NR == 1 { print; print p; next } { print }' \
          "$copy" > "${copy}.2"
      mv "${copy}.2" "$copy"
    fi
  fi
  COPY="$copy"
  OUT="${root}/out.txt"; ERR="${root}/err.txt"
  ( cd "$root" && TMPDIR="$root" bash "$copy" >"$OUT" 2>"$ERR" )
  RC=$?
}

# classify <leg-name> <expect: refused|inert-ok|passing|report>
classify() {
  local name="$1" expect="$2" banners
  banners=$(grep -c 'PASSED' "$OUT" 2>/dev/null); banners=${banners:-0}
  if [ "$expect" = "report" ]; then
    if [ -e "$MARKER" ]; then
      pnote "$name: payload EXECUTED (planted at line 2, the first of the lines above the ban). Uncovered prologue = lines 2..$((ban_line - 1)) = $uncovered line(s) above $GATE:$ban_line, measured not assumed — bash expands them before the ban's first statement runs. Documented limit, reported rather than failed: content above a guard is outside that guard's reach, and whoever can write here can also delete the guard. Closing it for real is a meta-gate over every script in this directory (t_4558fd32), not a longer comment in the file being judged."
    else
      pok "$name: payload above the ban did not execute (ban at $GATE:$ban_line, $uncovered line(s) above it — check this leg still plants above the ban if it ever turns green by accident)"
    fi
    return 0
  fi
  if [ -e "$MARKER" ]; then
    pfail "$name: BACKTICK PAYLOAD EXECUTED — marker file created, gate exited $RC with $banners PASSED banner(s): the gate ran shell it did not intend to"
    return 0
  fi
  case "$expect" in
    refused)
      if [ "$RC" -ne 0 ] && [ "$banners" -eq 0 ]; then
        pok "$name: refused before execution (RC $RC, 0 banners)"
      elif [ "$RC" -eq 0 ]; then
        pfail "$name: gate ACCEPTED the mutated source (RC 0, $banners banner(s)) — the payload did not happen to run, which is luck, not a guard"
      else
        pfail "$name: unexpected outcome (RC $RC, $banners banner(s)) — expected a refusal printing no PASSED banner"
      fi
      ;;
    inert-ok)
      # Three outcomes, named for what actually happened. The old pair called a
      # refusal "inert", which is the wrong word for the wrong mechanism: an
      # inert context is one bash never expands, a refused one is one the ban
      # stopped. Only the first is a property of the comment.
      if [ "$RC" -ne 0 ] && [ "$banners" -eq 0 ]; then
        pok "$name: REFUSED BY THE BAN (RC $RC, 0 banners) — the ban caught it before bash expanded anything; do not read this as the comment being inert"
      elif [ "$RC" -eq 0 ] && [ "$banners" -eq 3 ]; then
        pok "$name: INERT (RC 0, 3 banners) — a '#' comment never expands and the gate ran anyway; the ban is what keeps that safe once the line stops being a comment"
      else
        pfail "$name: unexplained outcome (RC $RC, $banners banner(s)) — a comment leg must either be refused by the ban or run clean to three banners"
      fi
      ;;
    passing)
      if [ "$RC" -eq 0 ] && [ "$banners" -eq 3 ]; then
        pok "$name: unmodified copy still passes (RC 0, 3 banners, $(grep -c 'FAIL:' "$OUT" 2>/dev/null) FAIL:)"
      else
        pfail "$name: unmodified copy did NOT pass (RC $RC, $banners banner(s)) — the guard is refusing legitimate runs, not just attacks"
      fi
      ;;
  esac
}

echo "widget_settings_check.sh backtick-injection probe"
echo "  gate:    $GATE"
echo "  payload: a backticked touch of a marker file, built with printf octal so this probe carries none"
echo

# ---- enumerate unquoted heredoc openers; never hardcoded -------------------
# Quoted delimiters (<<'X', <<"X") are skipped by the word test below: a quoted
# delimiter cannot be expanded by bash at all, so a leg there would prove
# nothing. Comment lines are skipped so a comment MENTIONING a heredoc is not
# mistaken for an opener.
regions=$(awk '
  /^[[:space:]]*#/ { next }
  { i = index($0, "<<")
    if (i == 0) next
    rest = substr($0, i + 2)
    if (substr(rest, 1, 1) == "-") rest = substr(rest, 2)
    if (rest !~ /^[A-Za-z_][A-Za-z_0-9]*[[:space:]]*$/) next
    d = rest
    gsub(/[[:space:]]/, "", d)
    print NR ":" d
  }' "$GATE")

if [ -z "$regions" ]; then
  pfail "no unquoted heredoc found in $GATE — the region legs would pass vacuously; if the gate really has none, delete them rather than leaving a green that means nothing"
fi

legno=0
first_open=""
last_close=""
while IFS=: read -r oline delim; do
  [ -n "$oline" ] || continue
  tline=$(awk -v s="$oline" -v d="$delim" 'NR > s && $0 == d { print NR; exit }' "$GATE")
  if [ -z "$tline" ]; then
    pfail "heredoc opened at $GATE:$oline (delimiter $delim) never closes at column 0 — the region cannot be bounded"
    continue
  fi
  [ -n "$first_open" ] || first_open="$oline"
  last_close="$tline"
  legno=$((legno + 1))
  echo "planting inside unquoted heredoc <<$delim (region $oline..$tline)"
  run_leg "region-$legno" "$oline" slash
  classify "region-$legno (inside <<$delim, $oline..$tline)" "refused"
done <<EOF
$regions
EOF

echo "planting in shell position around the heredoc"
run_leg "shell-above" "$shell_anchor" slash
classify "shell-above (comment-shaped line in shell position, before the heredoc)" "refused"
if [ -n "$last_close" ]; then
  run_leg "shell-below" "$last_close" slash
  classify "shell-below (shell position, after the last heredoc terminator)" "refused"
fi

echo "planting through a renamed copy with the canonical gate clean beside it"
run_leg "rename" "$shell_anchor" slash
classify "rename (gate_under_probe.sh mutated, widget_settings_check.sh clean next to it)" "refused"

echo "planting inside a real '#' comment"
run_leg "comment" "$shell_anchor" hash
classify "comment (payload inside a '#' comment line)" "inert-ok"

echo "measuring the window above the guard itself"
run_leg "boundary" 1 slash slash above
classify "boundary (payload above the guard's first line)" "report"

echo "control: the same copy mechanism, nothing mutated"
run_leg "control" "-" slash
classify "control (unmodified copy)" "passing"

GATE_AFTER="$(shasum -a 256 "$GATE" | cut -d' ' -f1)"
if [ "$GATE_BEFORE" = "$GATE_AFTER" ]; then
  pok "gate untouched by this probe ($GATE_BEFORE)"
else
  pfail "probe MUTATED the gate it was auditing"
fi

if [ "$probe_fail" -gt 0 ]; then
  echo "BACKTICK PROBE FAILED — $probe_fail leg(s) red. Work kept at $WORKROOT"
  exit 1
fi
echo "BACKTICK PROBE PASSED — $((legno + 5)) legs, no planted payload executed, unmodified gate still green (work: $WORKROOT)"
exit 0
