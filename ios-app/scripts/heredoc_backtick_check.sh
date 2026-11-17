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
#   HIT <region> <line> <expanding|escaped>  one per line carrying the byte:
#                                         region = body (unquoted heredoc: bash
#                                         expands it), shell (a code-bearing line),
#                                         hash (an all-comment line), qbody (inside
#                                         a quoted heredoc); expanding = not escaped
#                                         away by a backslash. R1 refuses body/shell
#                                         + expanding; the rest is R2 or R3.
#   HERESTRING <line>                    <<< (informational)
#   MENTION <line> <word> <plain|quoted>  a <<X token that is not an opener (text)
#   UNBOUND <line> <word> <plain|quoted>  a real opener bash accepts that this
#                                         scanner cannot bound (delimiter not at EOL)
#   MULTI <line>                         >1 unquoted opener token on one line
AWKPROG=$(cat <<'AWK'
function parity(s, i,   n) {          # backslashes immediately before index i
  n = 0
  while (i - n - 1 >= 1 && substr(s, i - n - 1, 1) == "\\") n++
  return n
}
function livebt(s,   i, seen) {  # 2 = an UNESCAPED backtick, 1 = only escaped ones, 0 = none
  seen = 0
  for (i = 1; i <= length(s); i++)
    if (substr(s, i, 1) == BT) {
      if (parity(s, i) % 2 == 0) return 2
      seen = 1
    }
  return seen
}
function emit(region, line,   v) {   # one record per line that carries the byte
  v = livebt(line)
  # The two axes are separate and both are reported: WHERE the byte sits (region)
  # and whether bash would honour it (expanding vs escaped by a backslash).
  if (v) print "HIT\t" region "\t" NR "\t" (v == 2 ? "expanding" : "escaped")
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
    emit("body", line)                             # a '#' here is body text, not a comment
    next
  }
  if (state == 2) {
    if (terminator(line, delim)) { print "CLOSE\t" NR "\t" delim; state = 0; next }
    emit("qbody", line)                            # inert: bash cannot expand this
    next
  }
  # ---- shell position ----
  if (line ~ /^[[:space:]]*#/) {
    emit("hash", line)
    next                                           # never an opener
  }
  emit("shell", line)
  ncand = 0
  i = index(line, "<<")
  while (i > 0) {
    rest = substr(line, i + 2)
    d = 0
    if (substr(rest, 1, 1) == "-") { d = 1; rest = substr(rest, 2) }
    q = substr(rest, 1, 1)
    if (q == QT || q == DQ) {                 # a quoted word: bash cannot expand it
      # Strict: quote + word + the SAME quote + end of line. Anything else is text.
      # A line merely MENTIONING the operator inside a string — index($0, "<<") —
      # must not open a region that never closes. That shape is planted and
      # asserted by heredoc_backtick_check_probe.sh.
      w = substr(rest, 2)                      # drop the opening quote
      m = match(w, /[^A-Za-z_0-9]/)            # where the word ends
      if (m > 1 && substr(w, m, 1) == q) {
        tail = substr(w, m + 1)
        if (tail ~ /^[[:space:]]*$/) {
          delim = substr(w, 1, m - 1)
          dash = d
          state = 2                            # skip the body through its terminator
          print "QOPEN\t" NR "\t" delim "\t" (q == QT ? "sq" : "dq")
          ncand++
          break
        }
        t = tail; sub(/^[[:space:]]+/, "", t)
        if (t ~ /^[0-9]*[>&|]/) {              # bash: cat <<WORD > f — a real heredoc
          print "UNBOUND\t" NR "\t" substr(w, 1, m - 1) "\tquoted"; ncand++; break
        }
        print "MENTION\t" NR "\t" substr(w, 1, m - 1) "\tquoted"; break
      }
      # not quote + word + quote: text that mentions the operator. Keep looking.
    }
    if (rest ~ /^[A-Za-z_][A-Za-z_0-9]*[[:space:]]*$/) {
      delim = rest; gsub(/[[:space:]]/, "", delim); dash = d
      state = 1; ncand++
      print "OPEN\t" NR "\t" delim "\t" (d ? "dash" : "plain")
      break
    }
    if (rest ~ /^[A-Za-z_]/) {
      # A delimiter word followed by more text. bash accepts a heredoc opener whose
      # delimiter is followed by redirections (cat <<WORD > f), and such a region
      # cannot be bounded by this scanner — so it is REFUSED as unbounded rather
      # than quietly unread. Anything else (a quoted string naming the operator) is
      # text and is counted as a mention, so the reader can see it was not skipped
      # by accident.
      m2 = match(rest, /[^A-Za-z_0-9]/)
      if (m2 > 1) {
        r = substr(rest, m2); sub(/^[[:space:]]+/, "", r)
        if (r ~ /^[0-9]*[>&|]/) {
          print "UNBOUND\t" NR "\t" substr(rest, 1, m2 - 1) "\tplain"; ncand++; break
        }
        print "MENTION\t" NR "\t" substr(rest, 1, m2 - 1) "\tplain"; break
      }
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
# A finding is a line that EXECUTES; a measured line is one that is inert today
# and is printed with the reason. Both are counted in BYTES-OFFENDING-LINES, not
# in messages, so collapsing the output cannot change a number a reader checks.
fail=0
live_findings=0
measured_total=0
files_scanned=0
splice_capable_files=0

nwords() { local n=0 w; for w in $1; do n=$((n + 1)); done; printf '%s' "$n"; }
finding() { echo "  FINDING  $1"; live_findings=$((live_findings + ${2:-1})); fail=1; }
measured() { echo "  measured $1"; measured_total=$((measured_total + ${2:-1})); }

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
  unbound=$(printf '%s\n' "$rec"  | awk -F'\t' '$1=="UNBOUND" {printf "%s ", $2 ":" $3}')
  mention=$(printf '%s\n' "$rec"  | awk -F'\t' '$1=="MENTION" {c++} END {print c + 0}')
  multi=$(printf '%s\n' "$rec"   | awk -F'\t' '$1=="MULTI"  {printf "%s ", $2}')
  hstring=$(printf '%s\n' "$rec" | awk -F'\t' '$1=="HERESTRING" {c++} END{print c+0}')
  hits=$(printf '%s\n' "$rec"     | awk -F'\t' '$1=="HIT"   {printf "%s ", $0}')
  # R1 candidates: the byte sits where bash expands (unquoted heredoc body or shell
  # position) AND is not escaped away. Everything else is inert-by-region or
  # inert-by-escaping, and is reported as one grouped line per file.
  live=$(printf '%s\n' "$rec"     | awk -F'\t' '$1=="HIT" && ($2=="body" || $2=="shell") && $4=="expanding" {printf "%s:%s ", $3, $2}')
  inert=$(printf '%s\n' "$rec"    | awk -F'\t' '$1=="HIT" && !(($2=="body" || $2=="shell") && $4=="expanding") {printf "%s@%s(%s) ", $3, $2, $4}')

  files_scanned=$((files_scanned + 1))
  capable=0
  # UNBOUND counts too: bash really does interpolate that heredoc, the scanner just
  # cannot see its extent — so the file is splice-capable and held to the same ban.
  if [ -n "$opens" ] || [ -n "$unbound" ]; then
    capable=1; splice_capable_files=$((splice_capable_files + 1))
  fi

  printf 'scanned %-38s bytes=%s unquoted-heredoc=%s quoted-heredoc=%s here-strings=%s operator-mentions=%s\n' \
    "$base" "$(nwords "$hits")" "${opens:-none}" "${qopens:-none}" "${hstring:-0}" "${mention:-0}"

  # A region that never closes cannot be bounded, so nothing about the rest of the
  # file is trustworthy: refuse rather than guess where the body ended.
  if [ -n "$unclosed" ]; then
    finding "$base: heredoc opened but never terminated (record(s) $unclosed) — the region cannot be bounded, so the scan cannot vouch for the lines after it"
  fi
  if [ -n "$unbound" ]; then
    # bash expands this heredoc (cat <<WORD > f is valid), but a scanner that only
    # follows a delimiter at end of line cannot see where its body starts. This is
    # the one shape that would otherwise be a SILENT blind spot, so it is a refusal.
    finding "$base: heredoc opener(s) whose delimiter does not end the line (record(s) $unbound) — bash expands this body but its region cannot be bounded here; move the delimiter to the end of the line so the audit can see the whole body"
  fi
  if [ -n "$multi" ]; then
    finding "$base: line(s) $multi carry more than one heredoc opener — only the first is bounded; split them onto separate lines so every body is auditable"
  fi

  for e in $live; do
    l="${e%%:*}"; kind="${e##*:}"
    if [ "$kind" = "body" ]; then
      finding "$base:$l  backtick in an UNQUOTED heredoc body (R1) — bash expands this body; a leading '#' or '//' on such a line is body text, not a comment" 1
    else
      finding "$base:$l  backtick in shell position (R1) — a code-bearing line's trailing comment is still shell position; the byte is expanded when this script runs" 1
    fi
  done
  n_inert=$(nwords "$inert")
  if [ "$capable" = "1" ]; then
    [ "$n_inert" -gt 0 ] && finding \
      "$base: $n_inert byte(s) that do NOT expand — $inert (line@region why). R2 still refuses them: this script writes a file with an interpolating heredoc, so 'it is only a comment' and 'it is only quoted' are not accepted here" "$n_inert"
  else
    [ "$n_inert" -gt 0 ] && measured \
      "$base: $n_inert byte(s) that do NOT expand — $inert (line@region why). A '#' comment never expands and a quoted delimiter cannot expand; this file has no unquoted heredoc, so R3 prints them instead of refusing (R3)" "$n_inert"
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
echo "  limit a <<X is treated as an opener only when X is a well-formed delimiter word"
echo "        ending the line (bare, or quote + word + the same quote). Anything else —"
echo "        a line merely MENTIONING the operator inside a string, or a delimiter not"
echo "        at end of line — is read as text, so no phantom region is opened. A real"
echo "        heredoc whose delimiter does not end its line is therefore NOT bounded by"
echo "        this gate: it is reported as an unbounded region and refused, not guessed."
echo "  limit here-strings are not heredocs; their content is one line and R1 covers it"
echo "  limit this gate reads only. The guard that refuses a mutated gate EARLY —"
echo "        before its heredoc expands — is the in-file guard in the gate itself"
echo "        (widget_settings_check.sh, t_9e4a731b). Both are needed; this one closes"
echo "        the window the in-file guard cannot see: text above its own first line."

echo
echo "summary: $files_scanned shell script(s) scanned, $splice_capable_files splice-capable" \
     "(unquoted heredoc present), $live_findings finding(s), $measured_total measured-inert."
if [ "$measured_total" -gt 0 ]; then
  echo "The measured-inert lines are NOT exemptions handed out silently: each is printed"
  echo "above, per file and region kind, naming EVERY offending line number, with the"
  echo "reason it is inert today and the rule that would make it a finding (the line"
  echo "stopping being a comment, or the file gaining an unquoted heredoc)."
fi

if [ "$fail" -ne 0 ]; then
  echo "HEREDOC BACKTICK CHECK FAILED — $live_findings live finding(s). A backtick in an" \
       "interpolating heredoc or in shell position is EXECUTED when that script runs."
  exit 1
fi
echo "HEREDOC BACKTICK CHECK PASSED — $files_scanned script(s), no unescaped backtick in" \
     "shell position or in any unquoted heredoc body; $measured_total measured-inert."
exit 0
