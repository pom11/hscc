#!/bin/bash
# heredoc_backtick_check.sh — t_4558fd32: meta-gate for the backtick-splice class.
#
# WHY A META-GATE EXISTS. widget_settings_check.sh carries its own guard that bans
# the backtick byte from its own source and refuses before expanding anything
# (t_9e4a731b, PR #345). That guard is real and it stays, but it has a limit it
# cannot fix from inside: a guard can only judge text below its own first
# executable line, and it only audits the one file it lives in. So the property
# "no shell script in this tree splices a backtick into an interpolating heredoc"
# depended on each audited file cooperating with its own audit. This gate is the
# external scanner that removes that dependency: it judges EVERY shell script in
# ios-app/scripts/ from outside, whole-file, with no in-file sentinel to plant
# above and no leaf filename to rename around.
#
# THE CLASS. A shell expands an unquoted (interpolating) heredoc — cat > f <<SWIFT —
# the same way it expands a double-quoted string: a dollar-VAR reference substitutes and a
# balanced pair of backticks is REPLACED BY THE OUTPUT OF RUNNING IT. A gate whose
# body carries backticks in Swift comments therefore (a) runs shell it never
# intended to and (b) silently deletes balanced pairs from the file it writes
# (measured in t_9e4a731b: 3 stderr lines but 6 missing bytes — under-reporting
# is the signature of this class, which is why an "exits 0" is not evidence here).
#
# THE RULES, and why each line is where it is.
#
#   R1  LIVE. An unescaped backtick in an unquoted heredoc body, or on any shell
#       line that is not entirely a '#' comment, is a FINDING in every script.
#       Inside a heredoc body a leading '#' or '//' is NOT a comment — it is body
#       text being expanded, which is exactly where the shipped defect lived. A
#       line carrying code plus a trailing comment is treated as shell position
#       (fail closed: deciding "this half is a comment" needs quote tracking whose
#       own corruption would be a silent exemption).
#   R2  FILE-WIDE for splice-capable scripts. If a script contains at least one
#       unquoted heredoc opener, an unescaped backtick ANYWHERE in it is a
#       finding — '#' comment lines included. That is t_9e4a731b's ruling, and it
#       keeps this gate's verdict identical to the in-file guard's verdict, so the
#       two scanners can never disagree about the same file. Rationale: in a file
#       that writes generated source, "it is only a comment" is the exact claim
#       that failed — a comment is only a comment while its line stays a comment,
#       and a comment-SHAPED '// ...' line in shell position IS a command word.
#   R3  MEASURED, NEVER SILENTLY EXEMPTED. Every backtick this gate does not
#       treat as a finding is printed with file:line, region and the reason it is
#       inert, and counted in the summary. Two regions are inert because BASH
#       ITSELF cannot expand them, so a payload there proves nothing (the same
#       reasoning that makes a quoted delimiter a skip in
#       widget_settings_check_backtick_probe.sh): a body written with a QUOTED
#       delimiter (a single-quoted or double-quoted word) and a real '#' comment
#       line in a script that has no unquoted heredoc.
#
# DETECTION IS STRUCTURAL, NOT REGEX-GUESSY. Heredoc openers are enumerated at
# run time (a bare <<DELIM or <<-DELIM word at end of line), each body is bounded by its
# terminator, and the body is scanned. A quoted delimiter enters a skip state
# through its terminator, so a <<FOO mention inside a quoted body is not
# mistaken for an opener and quoted text is never read as shell code. Comment
# lines are skipped while looking for OPENERS (so a comment mentioning a heredoc
# is not an opener) but never while scanning a BODY. Here-strings (the triple-lt
# operator) are not heredocs — their content is on the same line, so R1 already
# covers them.
#
# HONEST ABOUT EVERYTHING IT DOES NOT SCAN. Every file in scripts/ that is not a
# shell script is reported with its reason (python never expands backticks; a
# .golden file is generated output that no shell ever re-reads, and it legitimately
# carries backticks). No count in this file's output is transcribed from a card —
# all of them are measured on the tree being scanned, on this run.
#
# NON-DESTRUCTIVE: reads only, writes nothing into the tree, never executes a
# script it scans. Run the mutated copies through it from a private tree; the
# probe heredoc_backtick_check_probe.sh is that harness and its evidence.
#
# This file contains NO backtick byte of its own — the byte is obtained with
# printf octal. Deliberate: a scanner carrying the byte it bans is disqualified
# by its own rule (and R2 would refuse it the moment it grew a heredoc).
#
# Usage: ios-app/scripts/heredoc_backtick_check.sh [ios-app-dir]
#   default ios-app-dir = the parent of this script's directory
#   exit 0 = no live backtick anywhere in the scanned shell scripts
#   exit 1 = findings listed, or a heredoc region that cannot be bounded
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IOSAPP="${1:-$(cd "${SELF_DIR}/.." && pwd)}"
SCRIPTS="${IOSAPP}/scripts"

if [ ! -d "$SCRIPTS" ]; then
  echo "HEREDOC BACKTICK CHECK FAILED — no scripts dir at $SCRIPTS" >&2
  exit 1
fi

BT="$(printf '\140')"          # the byte under audit, never written into this file
QT="$(printf '\047')"          # single quote, for quoted-delimiter detection
DQ='"'

# ---- the analyzer -----------------------------------------------------------
# Emits tab-separated records; the shell below turns them into verdicts.
#   OPEN <line> <delim> <plain|dash>     unquoted heredoc opened
#   CLOSE <line> <delim>                 its terminator
#   UNCLOSED <line> <delim>              ran off EOF without a terminator
#   QOPEN <line> <delim>                 quoted-delimiter heredoc (inert region)
#   QBODY <line>                         backtick inside a quoted body
#   BODY <line> <delim>                  backtick inside an unquoted body
#   SHELL <line>                         backtick on a non-comment-led shell line
#   HASH <line>                          backtick on an all-comment line
#   HERESTRING <line>                    <<< (informational)
#   MULTI <line>                         >1 unquoted opener token on one line
AWKPROG=$(cat <<'AWK'
function parity(s, i,   n) {          # backslashes immediately before index i
  n = 0
  while (i - n - 1 >= 1 && substr(s, i - n - 1, 1) == "\\") n++
  return n
}
function livebt(s,   i) {             # 1 if an UNESCAPED backtick occurs in s
  for (i = 1; i <= length(s); i++)
    if (substr(s, i, 1) == BT && parity(s, i) % 2 == 0) return 1
  return 0
}
function terminator(t, d) {           # is this line the heredoc terminator?
  if (dash) sub(/^\t+/, "", t)
  return t == d
}
BEGIN { state = 0 }                    # 0 shell, 1 unquoted body, 2 quoted body
{
  line = $0
  if (state == 1) {
    if (terminator(line, delim)) { print "CLOSE\t" NR "\t" delim; state = 0; next }
    if (livebt(line)) print "BODY\t" NR "\t" delim       # '#' here is body text
    next
  }
  if (state == 2) {
    if (terminator(line, delim)) { print "CLOSE\t" NR "\t" delim; state = 0; next }
    if (livebt(line)) print "QBODY\t" NR                 # inert: bash cannot expand
    next
  }
  # ---- shell position ----
  if (line ~ /^[[:space:]]*#/) {
    if (livebt(line)) print "HASH\t" NR
    next                                                # never an opener
  }
  if (livebt(line)) print "SHELL\t" NR
  ncand = 0
  i = index(line, "<<")
  while (i > 0) {
    rest = substr(line, i + 2)
    d = 0
    if (substr(rest, 1, 1) == "-") { d = 1; rest = substr(rest, 2) }
    q = substr(rest, 1, 1)
    if (q == QT || q == DQ) {                 # quoted word: bash cannot expand it
      delim = rest
      gsub(/[[:space:]]/, "", delim)
      sub(/^[^A-Za-z_]/, "", delim)           # drop the opening quote
      sub(/[^A-Za-z_0-9]$/, "", delim)        # drop the closing quote
      print "QOPEN\t" NR "\t" delim "\t" (q == QT ? "sq" : "dq")
      dash = d
      if (length(delim)) state = 2             # skip the body through its terminator
      ncand++
      break
    }
    if (rest ~ /^[A-Za-z_][A-Za-z_0-9]*[[:space:]]*$/) {
      delim = rest; gsub(/[[:space:]]/, "", delim); dash = d
      state = 1; ncand++
      print "OPEN\t" NR "\t" delim "\t" (d ? "dash" : "plain")
      break
    }
    if (substr(rest, 1, 1) == "<") print "HERESTRING\t" NR   # <<<, not a heredoc
    j = index(substr(line, i + 1), "<<")
    if (j == 0) break
    i = i + j
  }
  if (ncand > 1) print "MULTI\t" NR
}
END { if (state == 1) print "UNCLOSED\t" NR "\t" delim
      else if (state == 2) print "UNCLOSED\t" NR "\t" delim "\tquoted" }
AWK
)

# ---- verdict machinery ------------------------------------------------------
fail=0
live_findings=0
measured_total=0
files_scanned=0
splice_capable_files=0
notes=()

finding() { echo "  FINDING  $1"; live_findings=$((live_findings + 1)); fail=1; }
measured() { echo "  measured $1"; measured_total=$((measured_total + 1)); }

echo "heredoc backtick meta-check (t_4558fd32)"
echo "  tree:    $IOSAPP"
echo "  scanning: every *.sh directly under scripts/ (whole file, from outside)"
echo "  the byte: obtained with printf octal — this gate carries none"
echo

# Collect the population first, so the skip list and the scanned list are both
# printed from the same measurement and cannot disagree.
shfiles=()
for f in "$SCRIPTS"/*.sh; do
  [ -e "$f" ] && shfiles+=("$f")
done
if [ "${#shfiles[@]}" -eq 0 ]; then
  echo "HEREDOC BACKTICK CHECK FAILED — no *.sh found in $SCRIPTS: the scan would" >&2
  echo "pass vacuously, which is the worst kind of green this class can produce." >&2
  exit 1
fi

for f in "${shfiles[@]}"; do
  base="$(basename "$f")"
  rec="$(awk -v BT="$BT" -v QT="$QT" -v DQ="$DQ" "$AWKPROG" "$f")"

  opens=$(printf '%s\n' "$rec" | awk -F'\t' '$1=="OPEN"     {printf "%s ", $2 ":" $3}')
  qopens=$(printf '%s\n' "$rec" | awk -F'\t' '$1=="QOPEN"   {printf "%s ", $2}')
  unclosed=$(printf '%s\n' "$rec" | awk -F'\t' '$1=="UNCLOSED" {printf "%s ", $2 ":" $3}')
  multi=$(printf '%s\n' "$rec"   | awk -F'\t' '$1=="MULTI"  {printf "%s ", $2}')
  hstring=$(printf '%s\n' "$rec" | awk -F'\t' '$1=="HERESTRING" {c++} END{print c+0}')
  body=$(printf '%s\n' "$rec"    | awk -F'\t' '$1=="BODY"   {printf "%s:%s ", $2, $3}')
  shell=$(printf '%s\n' "$rec"   | awk -F'\t' '$1=="SHELL"  {printf "%s ", $2}')
  hash=$(printf '%s\n' "$rec"    | awk -F'\t' '$1=="HASH"   {printf "%s ", $2}')
  qbody=$(printf '%s\n' "$rec"   | awk -F'\t' '$1=="QBODY"  {printf "%s ", $2}')

  files_scanned=$((files_scanned + 1))
  capable=0
  [ -n "$opens" ] && { capable=1; splice_capable_files=$((splice_capable_files + 1)); }

  printf 'scanned %-38s unquoted-heredoc=%s quoted-heredoc=%s here-strings=%s\n' \
    "$base" "${opens:-none}" "${qopens:-none}" "${hstring:-0}"

  # A region that never closes cannot be bounded, so nothing about the rest of the
  # file is trustworthy: refuse rather than guess where the body ended.
  if [ -n "$unclosed" ]; then
    finding "$base: heredoc opened but never terminated (record $unclosed) — the region cannot be bounded, so the scan cannot vouch for the lines after it"
  fi
  if [ -n "$multi" ]; then
    finding "$base: line(s) $multi carry more than one heredoc opener — only the first is bounded; split them onto separate lines so every body is auditable"
  fi

  for l in $body; do
    finding "$base:$l  backtick in an UNQUOTED heredoc body (R1) — bash expands this body; a leading '#' or '//' on such a line is body text, not a comment"
  done
  for l in $shell; do
    finding "$base:$l  backtick in shell position (R1) — a code-bearing line's trailing comment is still shell position; the byte is expanded when this script runs"
  done
  if [ "$capable" = "1" ]; then
    for l in $hash; do
      finding "$base:$l  backtick in a '#' comment of a SPLICE-CAPABLE script (R2) — this file writes a file with an interpolating heredoc, so 'it is only a comment' is not accepted here"
    done
    for l in $qbody; do
      finding "$base:$l  backtick inside a quoted heredoc body of a SPLICE-CAPABLE script (R2) — inert by itself, but this file is held to the file-wide ban"
    done
  else
    for l in $hash; do
      measured "$base:$l  '#' comment in a script with no unquoted heredoc — a comment line never expands, and there is no splice surface in this file to make that claim load-bearing (R3)"
    done
    for l in $qbody; do
      measured "$base:$l  inside a QUOTED heredoc body — bash cannot expand a quoted delimiter, so a payload here would prove nothing (R3)"
    done
  fi
done

echo
echo "---- what this gate does NOT scan, and why ----"
for f in "$SCRIPTS"/*.py; do
  [ -e "$f" ] || continue
  echo "  skip  $(basename "$f") — python source: no shell expansion ever touches it, the class cannot exist here"
done
golds=0
for f in "$SCRIPTS"/*.golden; do
  [ -e "$f" ] || continue
  n=$(tr -dc "$BT" < "$f" | wc -c | tr -d ' ')
  echo "  skip  $(basename "$f") — generated OUTPUT, compared byte-for-byte, never re-read by a shell: it legitimately carries $n backtick byte(s), which is why a golden is a drift check and not a backtick check"
  golds=$((golds + 1))
done
for f in "$SCRIPTS"/*.md; do
  [ -e "$f" ] || continue
  echo "  skip  $(basename "$f") — prose; not executable by any shell"
done
subsh=$(find "$SCRIPTS" -mindepth 2 -name '*.sh' 2>/dev/null | wc -c)
if [ "$subsh" -gt 1 ]; then
  echo "  NOTE  shell scripts exist in scripts/ SUBDIRS and are NOT scanned by this gate:"
  find "$SCRIPTS" -mindepth 2 -name '*.sh' 2>/dev/null | sed "s|$SCRIPTS/|        |"
  echo "        (this gate scans the top level, where every executed gate lives — a subdir"
  echo "         script is harness support code; extend the glob rather than trusting it)"
else
  echo "  cover scripts/*/ harness subdirs contain no *.sh at all, so the top-level glob is the whole population"
fi
echo "  limit quoted-delimiter bodies are skipped by R1 (bash cannot expand them) but"
echo "        are still reported under R3, and are findings inside a splice-capable file"
echo "  limit here-strings are not heredocs; their content is one line and R1 covers it"
echo "  limit this gate reads only. The guard that refuses a mutated gate EARLY —"
echo "        before its heredoc expands — is the in-file guard in the gate itself"
echo "        (widget_settings_check.sh, t_9e4a731b). Both are needed; this one closes"
echo "        the window the in-file guard cannot see: text above its own first line."

echo
echo "summary: $files_scanned shell script(s) scanned, $splice_capable_files splice-capable" \
     "(unquoted heredoc present), $live_findings finding(s), $measured_total measured-inert."
if [ "$measured_total" -gt 0 ]; then
  echo "The measured-inert lines are NOT exemptions handed out silently: they are printed"
  echo "above, one per line, with the reason each is inert today and the rule that would"
  echo "make it a finding (the line stopping being a comment, or the file gaining a heredoc)."
fi

if [ "$fail" -ne 0 ]; then
  echo "HEREDOC BACKTICK CHECK FAILED — $live_findings live finding(s). A backtick in an" \
       "interpolating heredoc or in shell position is EXECUTED when that script runs."
  exit 1
fi
echo "HEREDOC BACKTICK CHECK PASSED — $files_scanned script(s), no unescaped backtick in" \
     "shell position or in any unquoted heredoc body; $measured_total measured-inert."
exit 0
