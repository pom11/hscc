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
# disqualified by the rule it tests, and because this file writes a file with an
# interpolating heredoc, R2 of heredoc_backtick_check.sh holds even its COMMENTS to
# that rule — a byte in a comment here is a refusal, not a measured-inert note.
#
# CHECK THE MEASUREMENT, not the claim. That sentence was prose for three cards and
# the first edit to this file since put two bytes of the byte in a comment while a
# check that looked like the one below passed: piping tr -dc into "wc -l" counts LINES,
# and tr's output has no newline in it, so it reports 0 for any number of bytes. The
# count below is BYTES, printed on every run and load-bearing before a single leg runs.
#
# Usage: ios-app/scripts/widget_settings_check_backtick_probe.sh
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="${SELF_DIR}/widget_settings_check.sh"
[ -f "$GATE" ] || { echo "PROBE FAILED — gate not found at $GATE" >&2; exit 1; }
GATE_BEFORE="$(shasum -a 256 "$GATE" | cut -d' ' -f1)"
IOSAPP="$(cd "${SELF_DIR}/.." && pwd)"
REPO="$(cd "${IOSAPP}/.." && pwd)"

# Sibling projects the gate reads by relative path, read out of the gate itself.
# widget_settings_check.sh greps MAC_APP="../macos-app" for the desktop-widget wiring
# (t_1143c19b), and a copy that cannot see a sibling prints FAIL where the real gate
# prints ok — the control leg then accuses a healthy guard of refusing legitimate runs.
# Derived rather than listed here for the same reason the banner set is derived: the
# copy tree being written down by hand is exactly what made project.yml (t_d1a9eeb6) and
# then macos-app (t_b62678cc) each redden the runner in turn. Only the double-quoted
# "../name" literal form counts, and any name starting with '.' is dropped so no value
# can point a link above the repo.
gate_siblings=$(grep -o '"\.\./[A-Za-z0-9_.-]*"' "$GATE" 2>/dev/null \
                | tr -d '"' | sed -e 's|^\.\./||' | grep -v '^\.' | sort -u || true)
if [ -n "$gate_siblings" ]; then
  echo "gate siblings to expose to each copy (derived from $GATE): $(printf '%s ' $gate_siblings)"
fi

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

# The zero-byte claim, as a measurement with teeth rather than a sentence. BYTES, via
# wc -c — wc -l on the same output reports 0 for any count, which is exactly how two
# bytes in a comment once passed this file's own check (t_b62678cc). Refused before a
# single leg runs: a probe carrying the byte cannot certify its absence in anyone else,
# and R2 of heredoc_backtick_check.sh refuses this file for it anyway.
SELF_PATH="${SELF_DIR}/$(basename "${BASH_SOURCE[0]}")"
self_bytes=$(tr -dc "$BT" < "$SELF_PATH" | wc -c | tr -d '[:space:]')
echo "self-bytes: $self_bytes byte(s) of the banned character in $SELF_PATH"
if [ "$self_bytes" != "0" ]; then
  echo "PROBE FAILED — this probe carries $self_bytes byte(s) of the character it exists to ban, so every verdict it prints would be worthless. Quote prose with double quotes: this file may carry the byte in NO context, comment included." >&2
  exit 1
fi

sentinel_line=$(awk -v s="$SENTINEL" 'index($0, s) { print NR; exit }' "$GATE")
if [ -z "$sentinel_line" ]; then
  pfail "no line carrying the guard sentinel '$SENTINEL' in $GATE — the boundary leg cannot localise the guard, and a guard that cannot be found cannot be probed"
  sentinel_line=1
fi

# The boundary line, in order of trust: the explicit marker on the ban
# statement, else the first line after the sentinel that bash could execute
# (first line whose first non-blank character is neither '#' nor nothing).
# Both paths require a NON-COMMENT line: the marker string also appears in the
# pointer comment that explains it, and an anchor that matches prose is exactly
# the round-2 defect, so a comment line is never a valid answer here.
ban_ok=1
ban_line=$(awk -v s="$FIRST_EXEC" '
    index($0, s) { t = $0; gsub(/^[[:space:]]/, "", t)
                   if (t != "" && substr(t, 1, 1) != "#") { print NR; exit } }' "$GATE")
ban_how="marker $FIRST_EXEC"
if [ -z "$ban_line" ]; then
  ban_line=$(awk -v s="$SENTINEL" '
      !seen && index($0, s) { seen = 1; next }
      seen { t = $0; gsub(/^[[:space:]]/, "", t)
             if (t != "" && substr(t, 1, 1) != "#") { print NR; exit } }' "$GATE")
  ban_how="derived (no executable line carrying $FIRST_EXEC — first executable line after the sentinel)"
fi
if [ -z "$ban_line" ]; then
  pfail "cannot locate the ban's first executable statement in $GATE — the boundary window cannot be measured, and a boundary that is guessed is the bug this leg exists to catch; refusing to report a number"
  ban_ok=0
else
  # Guard the guard's own measurement: an anchor that lands on a comment is a
  # prose anchor, which is exactly the round-2 failure. Fail rather than print.
  ban_text=$(awk -v n="$ban_line" 'NR == n { print }' "$GATE")
  ban_lead=$(printf '%s' "$ban_text" | sed -e 's/^[[:space:]]*//' -e 's/^\(.\).*/\1/')
  if [ "$ban_lead" = "#" ]; then
    pfail "boundary anchor $GATE:$ban_line is a COMMENT line ($ban_text) — comments do not stop bash, so this is not the boundary; point $FIRST_EXEC at the first statement below it"
    ban_ok=0
  fi
  if [ "$ban_line" -lt "$sentinel_line" ]; then
    pfail "$FIRST_EXEC ($ban_line) sits above $SENTINEL ($sentinel_line) — one of the two markers has moved; the window below would be meaningless"
    ban_ok=0
  fi
fi
# Insertable lines above the ban: line 1 is the shebang, so 2..ban_line-1.
uncovered=0; [ "$ban_ok" = 1 ] && uncovered=$((ban_line - 2)); [ "$uncovered" -ge 0 ] || uncovered=0
if [ "$ban_ok" = 1 ]; then
  echo "guard boundary: ban's first executable statement = $GATE:$ban_line [$ban_how]"
  echo "  sentinel at :$sentinel_line; lines 2..$((ban_line - 1)) = $uncovered insertable line(s) sit ABOVE the ban"
else
  echo "guard boundary: UNMEASURABLE — the anchor was rejected (see FAIL above); this probe prints no span rather than a guessed one"
fi

# A line in shell position (not inside the heredoc, not the guard itself) to
# plant in: the cd that re-roots the gate. Below the guard, so a leg planted
# here is testing the guard rather than the window above it.
shell_anchor=$(awk '$0 ~ /^cd / { print NR; exit }' "$GATE")
if [ -z "$shell_anchor" ]; then
  pfail "no top-level cd found in $GATE — the shell-position legs have nowhere to plant"
  shell_anchor="$ban_line"
fi

# ---- what a clean pass PRINTS is derived from the gate, never transcribed ------
# This is the leg that reddened main: the expectation here was the literal 3, and
# the gate gained a fourth section (SURFACE COVERAGE). The control copy then ran
# byte-identical to the canonical gate, exited 0, printed all four banners and zero
# FAIL lines — a PASS — and was reported as "the guard is refusing legitimate runs".
# The copy mechanism was never the problem; a number was written down. That is the
# transcribed-count trap this repo already documents for the
# defaults.surfaces.count == 4 assertion inside this very gate: any claim about HOW
# MANY of something a thing has must be read out of the thing, or it reddens the day
# the thing grows. (Written without the banned byte on purpose — this file carries an
# unquoted heredoc, so R2 of heredoc_backtick_check.sh holds its COMMENTS to the
# zero-byte rule too; see the check-the-measurement note in the header.)
#
# Derived by NAME rather than counted: a run that stops half-way can match a count
# by accident and cannot match a set. A banner statement is a line whose first
# non-blank character is not '#' (a comment is not a print) that prints a quoted
# ALL-CAPS string ending in the word PASSED — the shape every banner here has, in
# both its bash echo form and its Swift print form inside the harness heredoc.
banner_stmts=$(awk '
  { line = $0
    gsub(/^[[:space:]]/, "", line)
    if (line == "" || substr(line, 1, 1) == "#") next
    if (match($0, /["(][A-Z][A-Z ]*PASSED/)) {
      s = substr($0, RSTART, RLENGTH)
      gsub(/["(]/, "", s)
      gsub(/[[:space:]]+$/, "", s)
      print s
    }
  }' "$GATE")
expected_banners=$(printf '%s\n' "$banner_stmts" | grep -c .)
banners_derived=1
if [ "$expected_banners" -eq 0 ]; then
  pfail "no banner statement could be derived from $GATE — with nothing to expect, 'the unmodified copy passed' would be a claim about an exit code and nothing else, which is the exact evidence this probe exists to refuse. A banner is a non-comment line printing a quoted ALL-CAPS string ending in PASSED; if the gate changed that shape, update this derivation rather than trusting RC 0."
  banners_derived=0
else
  echo "expected banners: $expected_banners, derived from $GATE (not transcribed) — $(printf '%s\n' "$banner_stmts" | paste -sd ',' - | sed 's/,/, /g')"
fi

# missing_banners <out-file>: every derived banner this run did NOT print, joined
# by '|'. Empty means the run printed the whole expected set. Reads the population
# through process substitution, not a pipe, so the accumulator stays in this shell.
missing_banners() {
  local out="$1" b miss=""
  while IFS= read -r b; do
    [ -n "$b" ] || continue
    grep -qF -- "$b" "$out" 2>/dev/null || miss="${miss}|${b}"
  done < <(printf '%s\n' "$banner_stmts")
  printf '%s' "$miss"
}

WORKROOT="$(mktemp -d "${TMPDIR:-/tmp}/wscprobe.XXXXXX")"

# Baseline for the stray-artifact leg: what the checkout already looked like dirty.
# Compared again at the end, so a copy that writes into the tree it audits becomes a
# finding naming the path instead of an untracked file someone finds next week.
repo_dirty_before="$(git -C "$REPO" status --porcelain 2>/dev/null | awk '{print $NF}' | LC_ALL=C sort)"

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
  # The gate derives its bare-executable suite shim from project.yml's one
  # HSCC_BUNDLE_PREFIX. Without this link the unmodified control copy fails to
  # find it, so the probe would be refusing a legitimate gate (t_d1a9eeb6).
  ln -s "${IOSAPP}/project.yml" "${root}/project.yml" 2>/dev/null
  # Sibling projects the gate reads as "../name" (macos-app today) have to exist
  # relative to the copy's PARENT, because the gate cds into the copy root and then
  # resolves MAC_APP="../macos-app" from there. Without this the control copy prints
  # FAIL where the same gate passes in place (macOS app/settings or project spec is
  # missing) — a false accusation against the guard, caused by the harness. The list
  # comes from the gate, so the next sibling it starts reading is followed automatically.
  local sib
  for sib in ${gate_siblings}; do
    [ "$sib" = "$(basename "$IOSAPP")" ] && continue
    if [ -d "${REPO}/${sib}" ]; then
      # ONLY when the destination is absent. ln -s onto an existing symlink that
      # resolves to a directory follows the link and creates the new link INSIDE the
      # target — which is the real sibling project in the checkout. Measured: the second
      # leg of one run left macos-app/macos-app -> <repo>/macos-app in the work tree, and
      # both legs still printed green because ../macos-app resolved either way. Same
      # family as cp onto a symlinked mirror: a link that lands somewhere else changes
      # nothing and looks like a pass.
      if [ ! -e "${WORKROOT}/${sib}" ] && [ ! -L "${WORKROOT}/${sib}" ]; then
        ln -s "${REPO}/${sib}" "${WORKROOT}/${sib}"
      fi
      [ -e "${WORKROOT}/${sib}" ] || pfail "run_leg ${name}: could not expose sibling ${sib} to the copy — the control leg would then judge a tree the real gate never runs in, which is a harness fault, not a finding"
    else
      pnote "run_leg ${name}: the gate names ../${sib} but ${REPO}/${sib} is not a directory — the copy cannot see it, and neither can the real gate"
    fi
  done
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
  local name="$1" expect="$2" banners fails missing
  # Two different measures for two different jobs, deliberately. The loose count is
  # for the REFUSAL legs, where under-counting is the dangerous direction: a run
  # that exited non-zero while mentioning PASSED anywhere at all is not a clean
  # refusal, so anything narrower than "any PASSED on stdout" could flatter a
  # partial run. The PASS legs are judged the other way round, against the derived
  # NAME set, where over-counting is the dangerous direction.
  banners=$(grep -c 'PASSED' "$OUT" 2>/dev/null); banners=${banners:-0}
  fails=$(grep -c 'FAIL:' "$OUT" 2>/dev/null); fails=${fails:-0}
  missing=$(missing_banners "$OUT")
  if [ "$expect" = "report" ]; then
    if [ "$ban_ok" != 1 ]; then
      pfail "$name: cannot report the uncovered span because the boundary anchor was rejected — a leg that cannot measure its own window must not report a number"
    elif [ -e "$MARKER" ]; then
      pnote "$name: payload EXECUTED (planted at line 2, the first of the lines above the ban). Uncovered prologue = lines 2..$((ban_line - 1)) = $uncovered line(s) above $GATE:$ban_line [$ban_how], measured not assumed — bash expands them before the ban's first statement runs. Documented limit, reported rather than failed: content above a guard is outside that guard's reach, and whoever can write here can also delete the guard. Closing it for real is a meta-gate over every script in this directory (t_4558fd32), not a longer comment in the file being judged."
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
      elif [ "$RC" -eq 0 ] && [ "$banners_derived" = 1 ] && [ -z "$missing" ] && [ "$fails" -eq 0 ]; then
        pok "$name: INERT (RC 0, all $expected_banners derived banner(s) present, 0 FAIL:) — a '#' comment never expands and the gate ran anyway; the ban is what keeps that safe once the line stops being a comment"
      else
        pfail "$name: unexplained outcome (RC $RC, $banners PASSED line(s), $fails FAIL: line(s), unprinted derived banner(s): ${missing:-none}) — a comment leg must either be refused by the ban or run clean to the full derived banner set ($expected_banners)"
      fi
      ;;
    passing)
      # "It passed" is four things and the exit code is the weakest of them: RC 0,
      # every derived banner printed, none missing, no FAIL line on the way. A copy
      # that exited 0 without the last banner never reached the end of the gate; one
      # that exited 0 over a FAIL line is the shape of the shipped defect this class
      # descends from. All four are checked, against names read out of the gate.
      if [ "$banners_derived" != 1 ]; then
        pfail "$name: cannot judge a pass at all — no banner statement was derivable from $GATE (see the FAIL above), so this leg has no outside measure and will not guess from an exit code"
      elif [ "$RC" -ne 0 ]; then
        pfail "$name: unmodified copy exited $RC with $banners PASSED line(s), $fails FAIL: line(s), unprinted derived banner(s): ${missing:-none} — the control copy is cp'd from the canonical gate with nothing edited (the gate's own sha is verified unchanged below), so a refusal here is the guard refusing a legitimate run, not an attack"
      elif [ -n "$missing" ]; then
        pfail "$name: copy exited 0 but never printed ${missing#|} — of $expected_banners banner statement(s) derived from $GATE the run printed $banners, so it stopped short of the end of the gate while still exiting 0"
      elif [ "$fails" -ne 0 ]; then
        pfail "$name: copy exited 0 while printing $fails FAIL: line(s) alongside $banners PASSED line(s) — a green exit over a red assertion is the original defect's shape, never a pass"
      elif [ "$banners" -ne "$expected_banners" ]; then
        pfail "$name: every derived banner printed ($expected_banners) yet $banners line(s) carried PASSED — the gate prints that word somewhere no banner statement is; name it and account for it"
      else
        pok "$name: unmodified copy passes (RC 0, $banners/$expected_banners banners, names derived from $GATE, 0 FAIL:) — the expectation is derived, so a fifth section added to the gate cannot redden this leg again"
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

echo "measuring the window above the ban itself"
run_leg "boundary" 1 slash slash above
classify "boundary (payload at line 2, above the ban's first executable statement)" "report"

echo "control: the same copy mechanism, nothing mutated"
run_leg "control" "-" slash
classify "control (unmodified copy)" "passing"

# Did anything land INSIDE the checkout? Exposing a sibling project to a copy is a new
# way to write into it (ln -s onto an existing symlink follows the link and lands the new
# link inside the real project — measured: macos-app/macos-app after a second leg), and
# the whole point of the private work dir is that the audited tree is never the scratch.
# Scoped to the blast radius: a new path under the audited project or under a sibling the
# copies read fails by name; a dirty path elsewhere is disclosed, not blamed on this probe.
repo_dirty_after="$(git -C "$REPO" status --porcelain 2>/dev/null | awk '{print $NF}' | LC_ALL=C sort)"
stray="$(comm -13 <(printf '%s\n' "$repo_dirty_before") <(printf '%s\n' "$repo_dirty_after"))"
if [ -n "$stray" ]; then
  while IFS= read -r spath; do
    [ -n "$spath" ] || continue
    stop="${spath%%/*}"
    shit=0
    [ "$stop" = "$(basename "$IOSAPP")" ] && shit=1
    for sib in ${gate_siblings}; do [ "$stop" = "$sib" ] && shit=1; done
    if [ "$shit" = 1 ]; then
      pfail "stray-artifact: this probe left $spath inside the checkout (under $stop/, a tree the copies read) — a copy writes to its own work dir, never to the tree it audits"
    else
      pnote "stray-artifact: $spath appeared in git status during the run, outside the audited tree — not this probe's subject, disclosed rather than ignored"
    fi
  done < <(printf '%s\n' "$stray")
else
  pok "stray-artifact: nothing new appeared inside the checkout (porcelain identical before and after every leg)"
fi

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
