#!/bin/bash
# heredoc_backtick_check_probe.sh — t_4558fd32: prove the meta-gate cannot be walked past.
#
# WHY. heredoc_backtick_check.sh is a scanner, and a scanner lies in exactly two
# directions: it can refuse everything (green on every attack, red on every
# commit), or pass everything (green on every commit, silent on every attack).
# Both print a clean exit code from the outside, so neither shows up in a single
# run. The acceptance here is therefore a planted payload that WRITES A MARKER
# FILE, per leg, with two assertions per leg: no marker may exist, and the gate
# must have REFUSED BY NAMING the mutated file. An "exits 0" is not evidence in
# this class — the defect this card descends from exited 0 while bash executed its
# own text.
#
# Because a static scanner never runs its target, "no marker" would also be true
# for a payload that could never have executed. So every position is first proven
# LIVE by a fixture that IS executed (legs live-*): same payload, same position,
# same marker. A refusal of a position that was never live proves nothing.
#
# LEGS
#   self-audit       this file must contain ZERO bytes of the character it plants
#                    (printf octal only). It fails fast, before any attack is scored.
#   control          the unmutated mirrored tree must PASS, must scan the same
#                    NUMBER of scripts as the real tree, and must report at least
#                    as many splice-capable scripts as this probe independently
#                    enumerates. Three anti-vacuity checks: a harness that scans an
#                    empty or misparsed population makes every leg below
#                    meaningless, and looks exactly like success.
#   scope            the gate's per-file population and this probe's own recursive
#                    enumeration must agree PATH FOR PATH (t_d8f34c60). A count would
#                    not catch the failure this card is about: if the walk ever stopped
#                    at the top level again, both sides could still print the same
#                    number while a whole subtree went unaudited. Also asserts the
#                    limits block no longer disclaims directories the walk enters.
#   measure          the per-file lines-with-byte column must equal grep -c on the SAME
#                    file, for EVERY scanned file, with a 0-line / 1-line / many-line
#                    sample printed (t_e89edf70). It had been a word count of the joined
#                    HIT records — four fields per record, so 4x the lines on every file,
#                    green summary and all. Every other leg reads the records, so only a
#                    measurement taken from outside them could see it.
#   depth-live       payload in an unquoted heredoc body of a script BELOW scripts/,
#                    EXECUTED: marker required. A refusal at that depth is worth
#                    nothing until the same position is shown to be live there.
#   depth-scan       the identical fixture, SCANNED: refused by path and listed in the
#                    scanned population by path.
#   link-edge        a DANGLING symlink named *.sh in the tree must be REFUSED as
#                    unreadable (it yields zero records, and zero records must not read
#                    as clean), while a symlink to a real script must appear in the
#                    population — the one-level glob the gate replaced matched links, so
#                    a regular-file predicate would be a new silent exemption.
#   samename         two files called common.sh in different directories: the clean one
#                    listed and the offending one refused, each BY PATH. A finding that
#                    printed only a leaf name would not say which file it refused.
#   live-body        payload in an unquoted heredoc body, EXECUTED: marker required.
#   live-shell       payload in shell position, EXECUTED: marker required.
#   live-comment     payload inside a real '#' comment, EXECUTED: marker FORBIDDEN.
#                    This is the fact R3 rests on, so it is measured, not assumed.
#   fixtures-refuse  one synthetic script per shape that must be refused: live byte
#                    in a body, in shell position, in a '#' comment of a
#                    splice-capable file, escaped-but-present in a splice-capable
#                    file, a <<WORD > f opener that cannot be bounded, the same with
#                    a quoted delimiter, an opener whose terminator never comes, and
#                    a <<-TAB body with a tabbed terminator.
#   fixtures-pass    the shapes that must PASS and must SAY SO: a byte inside a
#                    quoted heredoc body, a byte in a '#' comment of a
#                    non-splice-capable file, an escaped byte in shell position, and
#                    a script that only MENTIONS the operator inside a string (the
#                    shape that once made this scanner invent a region that never
#                    closed). Refusal here is a bug, and so is silence.
#   body-<n>         every unquoted heredoc of every real script (enumerated at run
#                    time): refused by name, no marker.
#   shell-<name>     every real script, payload in shell position: refused, no marker.
#   rename           a mutated copy under a NEW name with the canonical file clean
#                    beside it: the scan must follow content, not a leaf filename.
#   comment-capable  the same '#' payload inside a SPLICE-CAPABLE script: refused (R2).
#   comment / quoted / escaped  inert positions in a NON-splice-capable script: must
#                    NOT refuse and MUST print the planted line number — an exemption
#                    nobody can read is what R3 forbids.
#   window-above     the residual this meta-gate exists for. Payload ABOVE
#                    widget_settings_check.sh's own guard, copy EXECUTED: the marker
#                    is expected and REPORTED, never counted as a pass — an in-file
#                    guard cannot see that high. The closure assertion is that the
#                    meta-gate refuses the same bytes.
#   integrity        the real scripts' combined digest, any NEW porcelain path inside the
#                    audited tree (a dirty path elsewhere is a note, not an accusation),
#                    and a marker census must all be unchanged/empty at the end.
#
# NON-DESTRUCTIVE. The real tree is never edited. Every leg scans a private
# ios-app-shaped tree where Sources/ and each file of scripts/ is a SYMLINK to the
# real one, and only the file under attack becomes a real mutated copy. The legs
# that execute (live-*, window-above) write only inside their own leg dir, and a
# nesting guard makes a re-entered copy of this probe exit at once instead of
# recursing — a real hazard, since this file is itself in the scanned population.
#
# This file contains NO backtick byte of its own — the byte comes from printf
# octal. A probe carrying the byte it plants is disqualified by the rule it tests,
# and this file has an unquoted heredoc of its own, so R2 would refuse it.
#
# Usage: ios-app/scripts/heredoc_backtick_check_probe.sh
set -uo pipefail

if [ -n "${HEREDOC_BACKTICK_PROBE_NESTED:-}" ]; then
  echo "heredoc_backtick_check_probe.sh: refusing to run — this is a copy being" \
       "executed from inside a probe run (nesting guard, t_4558fd32)"
  exit 0
fi
export HEREDOC_BACKTICK_PROBE_NESTED=1

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="${SELF_DIR}/heredoc_backtick_check.sh"
IOSAPP="$(cd "${SELF_DIR}/.." && pwd)"
SCRIPTS="${IOSAPP}/scripts"

BT="$(printf '\140')"
QT="$(printf '\047')"

if [ ! -f "$GATE" ]; then
  echo "PROBE FAILED — meta-gate not found at $GATE" >&2
  exit 1
fi

probe_fail=0
pok()   { echo "  ok: $1"; }
pfail() { echo "  FAIL: $1"; probe_fail=$((probe_fail + 1)); }
pnote() { echo "  note: $1"; }

# The population, enumerated the SAME way the gate does it (t_d8f34c60: the gate went
# recursive because subject files moved into scripts/<name>/; a probe that still globs
# one directory would then keep agreeing with a gate that had silently lost a subtree,
# which is the opposite of a cross-check). '! -type f' would be wrong here too — see
# the gate's comment — so non-directories of every kind are listed, sorted, NUL-safe.
all_sh() {
  find "$SCRIPTS" ! -type d -name '*.sh' -print0 | LC_ALL=C sort -z
}
rel() { printf '%s' "${1#"$SCRIPTS"/}"; }      # scripts-relative path, as the gate names it
legid() { printf '%s' "${1//\//_}"; }          # a path is not a valid leg DIRECTORY name

# ---- baseline, measured before any leg runs --------------------------------
# Recursive, and digesting the WHOLE population the gate now walks: a digest of the
# top level only would stay green while a leg wrote into scripts/widget_view_dispatch/.
baseline_list=$(all_sh | xargs -0 shasum -a 256 2>/dev/null | shasum -a 256 | cut -d' ' -f1)
real_scripts=0
while IFS= read -r -d '' _; do real_scripts=$((real_scripts + 1)); done < <(all_sh)
REPO="$(cd "${IOSAPP}/.." && pwd)"
have_worktree=0
if git -C "$REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  have_worktree=1
  baseline_dirty=$(git -C "$REPO" status --porcelain 2>/dev/null)
else
  baseline_dirty=""
  pnote "$REPO is not a work tree — the porcelain-status integrity leg is skipped"
fi

WORKROOT="$(mktemp -d "${TMPDIR:-/tmp}/hbcprobe.XXXXXX")"

# ---- self-audit: this file carries ZERO bytes of the character it plants -------
# Rule of the class (hscc-probe-harness-pitfalls): verify, do not trust. This leg is
# not decoration — the first draft of the enumeration-edges legs below spelled a
# find(1) predicate in backticks inside a comment of THIS file, and because this file
# has an unquoted heredoc of its own, R2 made every one of those bytes a finding: the
# meta-gate went red on a tree that looked untouched and the control leg went red
# before any attack was scored. A probe carrying the byte cannot certify anything,
# because its own green would itself be the false green.
SELF_NAME="$(basename "${BASH_SOURCE[0]}")"
self_bytes=$(tr -dc "$BT" < "${SELF_DIR}/${SELF_NAME}" | wc -c | tr -d ' ')
if [ "$self_bytes" = "0" ]; then
  pok "self-audit: $SELF_NAME carries 0 bytes of the character it plants (printf octal only)"
else
  pfail "self-audit: $SELF_NAME carries $self_bytes byte(s) of the character it plants — this probe is disqualified by the rule it tests, and R2 refuses it as a splice-capable file. Quote the word or reword it (measured: two bytes in one comment did exactly that)"
  exit 1
fi

# ---- harness ---------------------------------------------------------------
mk_tree() {   # mk_tree <leg>: private ios-app dir; scripts/ mirrored, Sources/ symlinked
  local leg; leg="$(legid "$1")"
  TREE="${WORKROOT}/${leg}/ios-app"
  mkdir -p "${TREE}/scripts"
  ln -s "${IOSAPP}/Sources" "${TREE}/Sources" 2>/dev/null
  local d f
  # Real DIRECTORIES, symlinked files. Symlinking a whole directory — which is exactly
  # what a one-level mirror of scripts/* does — hides the subtree from a recursive walk,
  # because find does not descend a symlink it was not told to follow. A private tree
  # built that way would let every leg below report green on files it never entered,
  # which is the silent-green shape this harness exists to avoid.
  while IFS= read -r -d '' d; do
    mkdir -p "${TREE}/${d#"$IOSAPP"/}"
  done < <(find "$SCRIPTS" -type d -print0)
  while IFS= read -r -d '' f; do
    ln -s "$f" "${TREE}/${f#"$IOSAPP"/}" 2>/dev/null
  done < <(find "$SCRIPTS" ! -type d -print0)
  MARKER="${WORKROOT}/${leg}/MARKER_${leg}"
  OUT="${WORKROOT}/${leg}/out.txt"
  ERR="${WORKROOT}/${leg}/err.txt"
}

payload_for() { printf '%s pwn %stouch "%s"%s' "$1" "$BT" "$MARKER" "$BT"; }

scan() {   # scan <tree>: run the meta-gate over a private tree; never runs the target
  # A leg that lost its tree (mk_tree sets globals — calling it in a command
  # substitution loses them, and $1 then arrives EMPTY) must not quietly scan the
  # real repo instead: the real repo is clean by definition, so every leg would
  # report green. Refuse loudly.
  if [ -z "${1:-}" ] || [ ! -d "${1}/scripts" ]; then
    printf 'FATAL: scan called with %s — that is not a private tree, and falling through to the real repo would fake a green leg\n' "\"${1:-<empty>}\"" >&2
    exit 2
  fi
  ( cd "$1" && bash "$GATE" "$1" >"$OUT" 2>"$ERR" )
  G_RC=$?
  BANNERS=$(grep -c 'CHECK PASSED' "$OUT" 2>/dev/null); BANNERS=${BANNERS:-0}
}

expect_refused() {   # <what> <file-name-it-must-name>
  if [ -e "$MARKER" ]; then
    pfail "$1: PAYLOAD EXECUTED — marker file created. A scan leg must never run its target; if this leg executed something, the harness is the bug"
    return 0
  fi
  if [ "$G_RC" -ne 0 ] && [ "$BANNERS" -eq 0 ] && grep -q -- "$2" "$OUT"; then
    pok "$1: refused (RC $G_RC, 0 PASSED banners) and named the mutated file $2"
  elif [ "$G_RC" -eq 0 ]; then
    pfail "$1: ACCEPTED the mutated source (RC 0, $BANNERS banner(s)) — nothing ran only because nothing is ever run; the class is unrefused"
  elif ! grep -q -- "$2" "$OUT"; then
    pfail "$1: refused but never named $2 (RC $G_RC) — a refusal for some other reason cannot count as catching this payload"
  else
    pfail "$1: unexpected outcome (RC $G_RC, banners $BANNERS)"
  fi
}

# expect_refused_only — like expect_refused but WITHOUT the marker assertion. Used
# by the window-above leg, whose payload is executed on purpose (that execution IS
# the measured residual), so a marker there is expected and must not mask the
# closure check.
expect_refused_only() {   # <what> <file-name-it-must-name>
  if [ "$G_RC" -ne 0 ] && [ "$BANNERS" -eq 0 ] && grep -q -- "$2" "$OUT"; then
    pok "$1: refused (RC $G_RC, 0 PASSED banners) and named the mutated file $2"
  else
    pfail "$1: the meta-gate did NOT refuse the bytes the in-file guard could not see (RC $G_RC, banners $BANNERS) — the window above a guard is exactly what this gate exists to close"
  fi
}

expect_note() {   # <what> <planted-line-number> — inert region: pass AND disclose
  if [ -e "$MARKER" ]; then
    pfail "$1: PAYLOAD EXECUTED on a leg that was supposed to be inert"
    return 0
  fi
  if [ "$G_RC" -ne 0 ]; then
    pfail "$1: refused a position bash cannot expand (RC $G_RC) — an inert region refused without explanation is as unusable as a blind spot"
    return 0
  fi
  if grep -qE "(^|[ (])$2@" "$OUT"; then
    pok "$1: passed (RC 0) and printed the planted line $2 as measured-inert — the skip is disclosed, not silent"
  else
    pfail "$1: passed but did NOT print line $2 — an exemption nobody can read is exactly what R3 forbids"
  fi
}

first_exec() { awk 'NR > 1 && /^[[:space:]]*[^#[:space:]]/ { print NR; exit }' "$1"; }

# unquoted openers: "line<TAB>delim". Comment lines are skipped so a comment
# MENTIONING a heredoc is never an opener (the same rule the gate applies).
openers_of() {
  awk -v QT="$QT" '
    /^[[:space:]]*#/ { next }
    { i = index($0, "<<")
      while (i > 0) {
        rest = substr($0, i + 2)
        if (substr(rest, 1, 1) == "-") rest = substr(rest, 2)
        c = substr(rest, 1, 1)
        if (c != QT && c != "\"") {
          if (rest ~ /^[A-Za-z_][A-Za-z_0-9]*[[:space:]]*$/) {
            d = rest; gsub(/[[:space:]]/, "", d); print NR "\t" d; break
          }
        }
        j = index(substr($0, i + 1), "<<"); if (j == 0) break; i = i + j
      } }' "$1"
}

# first quoted-delimiter opener line, for the honest-skip leg
qopeners_of() {
  awk -v QT="$QT" '
    /^[[:space:]]*#/ { next }
    { i = index($0, "<<")
      while (i > 0) {
        rest = substr($0, i + 2)
        if (substr(rest, 1, 1) == "-") rest = substr(rest, 2)
        c = substr(rest, 1, 1)
        if (c == QT || c == "\"") {
          w = substr(rest, 2); m = match(w, /[^A-Za-z_0-9]/)
          if (m > 1 && substr(w, m, 1) == c && substr(w, m + 1) ~ /^[[:space:]]*$/) { print NR; break }
        }
        j = index(substr($0, i + 1), "<<"); if (j == 0) break; i = i + j
      } }' "$1"
}

plant() {   # plant <file> <after-line> <lead>: real mutated copy inside current TREE
  local name="$1" after="$2" lead="$3"
  local dest="${TREE}/scripts/${name}"
  local p; p="$(payload_for "$lead")"
  rm -f "$dest"
  # The name is a path relative to scripts/ now (t_d8f34c60), so its directory has to
  # exist before the mutated copy can replace the mirrored symlink inside it.
  mkdir -p "$(dirname "$dest")"
  awk -v n="$after" -v p="$p" '{ print; if (NR == n) print p }' \
      "${SCRIPTS}/${name}" > "$dest"
  chmod +x "$dest"
}

# A splice-capable real script to use as a subject, skipping this gate's own files:
# the scanner under test must not be its own subject.
subject_capable() {
  local f b
  while IFS= read -r -d '' f; do
    b="$(rel "$f")"
    case "$b" in heredoc_backtick_check*) continue ;; esac
    [ -n "$(openers_of "$f")" ] && { printf '%s' "$b"; return 0; }
  done < <(all_sh)
  return 1
}

echo "heredoc_backtick_check.sh injection probe (t_4558fd32)"
echo "  gate:      $GATE"
echo "  tree:      $IOSAPP"
echo "  payload:   a backticked touch of a marker file, built with printf octal so this probe carries none"
echo "  work:      $WORKROOT"
echo

# ---- control ---------------------------------------------------------------
echo "control: the same tree mechanism, nothing mutated"
mk_tree control
scan "$TREE"
control_scanned=$(grep -oE '[0-9]+ shell script\(s\) scanned' "$OUT" | head -1 | cut -d' ' -f1)
control_capable=$(grep -oE '[0-9]+ splice-capable' "$OUT" | head -1 | cut -d' ' -f1)
probe_capable=0
while IFS= read -r -d '' f; do
  [ -n "$(openers_of "$f")" ] && probe_capable=$((probe_capable + 1))
done < <(all_sh)
if [ "$G_RC" -eq 0 ] && [ "$BANNERS" -eq 1 ]; then
  pok "control: the unmutated tree passes (RC 0, 1 banner) — the gate is not refusing everything"
else
  pfail "control: the unmutated tree did not pass (RC $G_RC, $BANNERS banners) — a gate that refuses its own clean tree cannot score green on any attack either"
fi
if [ "${control_scanned:-0}" = "$real_scripts" ]; then
  pok "control: the private tree scanned $control_scanned scripts = the real population, so no leg below is vacuous"
else
  pfail "control: the private tree scanned '${control_scanned:-0}' of $real_scripts real scripts — every leg below would be vacuous"
fi
if [ "$probe_capable" -gt 0 ] && [ "${control_capable:-0}" -ge "$probe_capable" ]; then
  pok "control: the gate calls $control_capable scripts splice-capable while this probe independently enumerates $probe_capable with unquoted openers — the opener grammar is not silently missing them"
else
  pfail "control: the gate reports ${control_capable:-0} splice-capable but the probe enumerates $probe_capable — the opener parser is blind and every body leg would be worthless"
fi

# ---- scope: the gate and this probe must see the SAME population -------------
# A count check alone would not do: if the gate's walk ever stopped at the top level
# again while this harness globbed the top level too, both numbers would agree and the
# whole subtree would be un-audited and unreported. So the comparison is path-exact.
echo "scope: gate population vs probe population, path for path"
# Label-INDEPENDENT, deliberately (t_e89edf70 renamed two columns on the very line this
# parses, and the old parse keyed on one of them). The path is the second
# whitespace-separated field of a scanned line, so this population check can be neither
# reddened nor — the worse failure — silently vacuified by a future rename. The measure
# leg below is the one that binds itself to a label, because what it audits IS a label.
gate_pop=$(awk '/^scanned /{print $2}' "$OUT" | LC_ALL=C sort)
probe_pop=$(all_sh | while IFS= read -r -d '' p; do printf '%s\n' "${p#"$SCRIPTS"/}"; done | LC_ALL=C sort)
depth_n=$(printf '%s\n' "$probe_pop" | grep -c '/' || true)
if [ "$gate_pop" = "$probe_pop" ]; then
  pok "scope: the gate scanned exactly the $(printf '%s\n' "$probe_pop" | grep -c . | tr -d ' ') scripts this probe enumerates recursively, path for path ($depth_n of them below scripts/)"
else
  pfail "scope: gate and probe disagree — gate-only:[$(comm -23 <(printf '%s\n' "$gate_pop") <(printf '%s\n' "$probe_pop") | tr '\n' ' ')] probe-only:[$(comm -13 <(printf '%s\n' "$gate_pop") <(printf '%s\n' "$probe_pop") | tr '\n' ' ')]. A file one of the two can see and the other cannot is an un-audited file, whatever the summary line says"
fi
# The limits block used to disclaim subdirectories. If that sentence ever comes back
# while the walk is still recursive, the gate's prose and its behaviour disagree and a
# reader would rightly ignore the whole limits section.
if grep -q 'are NOT scanned by this gate' "$OUT"; then
  pfail "scope: the gate disclaims scripts/ subdirectories in its own limits block while the population above includes them — prose and walk disagree"
else
  pok "scope: the limits block no longer disclaims the subdirectories the walk enters"
fi
if [ "$depth_n" = "0" ]; then
  pnote "scope: no shell script sits below scripts/ on this tree, so the recursive walk is not yet load-bearing here — the depth legs below are what prove it can bite"
fi

# ---- measure: the printed per-file count, checked from OUTSIDE the records ---------
# (t_e89edf70). The column this leg checks was computed as a WORD COUNT of the
# concatenated HIT records. A HIT record is four tab-separated fields, so the column
# printed 4x the number of lines carrying the byte — on every file, on main and after the
# recursion change alike, with rc=0 and a green summary. No other leg here can see it: the
# verdict legs read the records, the control leg reads the summary line, and a constant
# factor is exactly the kind of wrong number that leaves both of them green. So this check
# comes from outside the record machinery — grep -c counts the LINES that carry the byte,
# tr counts the BYTES — and the gate's printed value is compared against grep -c for EVERY
# scanned file, not for a hand-picked few. The tr number is printed next to it so that a
# reader can see for themselves that the column is lines and not bytes instead of trusting
# the label: only a file where the two differ can tell those meanings apart, and such files
# are in the sample on purpose.
echo "measure: the printed lines-with-byte vs grep -c, for every scanned file"
mk_tree measure
scan "$TREE"
m_printed="${WORKROOT}/measure/printed.tsv"
# Label-bound ON PURPOSE: what this leg audits IS the label, so it must break if the label
# moves (the scope leg above is deliberately the opposite way round).
awk '$1 == "scanned" { v = "<absent>"; for (i = 3; i <= NF; i++) { t = $i; if (sub(/^lines-with-byte=/, "", t)) v = t } print $2 "\t" v }' "$OUT" > "$m_printed"

m_checked=0; m_bad=0; m_absent=0; m_diff_tree=0
m_zero=""; m_one=""; m_many=""
while IFS= read -r -d '' f; do
  b="$(rel "$f")"
  printed=$(awk -F'\t' -v p="$b" '$1 == p { print $2; exit }' "$m_printed")
  if [ -z "$printed" ] || [ "$printed" = "<absent>" ]; then
    m_absent=$((m_absent + 1))
    pfail "measure: $b has a scanned line but no lines-with-byte= column on it — a leg that parses nothing compares nothing, which is a green, not a result"
    continue
  fi
  case "$printed" in
    ''|*[!0-9]*) m_bad=$((m_bad + 1)); pfail "measure: $b printed lines-with-byte='$printed', which is not a count of anything"; continue ;;
  esac
  # Same file, both sides. The gate scanned the private mirror; this leg measures the real
  # tree, so prove they are the same bytes rather than assuming a symlink kept its word.
  cmp -s "$f" "${TREE}/scripts/${b}" || m_diff_tree=$((m_diff_tree + 1))
  lines=$(grep -c "$BT" "$f" 2>/dev/null); g_rc=$?
  if [ "$g_rc" -gt 1 ]; then
    pfail "measure: grep could not read $f (rc $g_rc) — the comparison for this file is not a result"
    continue
  fi
  if [ "$printed" != "$lines" ]; then
    m_bad=$((m_bad + 1))
    pfail "measure: $b printed lines-with-byte=$printed but grep -c counts $lines line(s) carrying the byte — the label and the number disagree (if the number is a multiple of $lines, a record's FIELD count has leaked into it again)"
  fi
  m_checked=$((m_checked + 1))
  [ "$printed" = "0" ] && [ -z "$m_zero" ] && m_zero="$b"
  [ "$printed" = "1" ] && [ -z "$m_one" ] && m_one="$b"
  [ "$printed" -ge 3 ] && [ -z "$m_many" ] && m_many="$b"
done < <(all_sh)

if [ "$m_checked" -gt 0 ] && [ "$m_bad" -eq 0 ] && [ "$m_absent" -eq 0 ]; then
  pok "measure: every one of the $m_checked scanned files carries a lines-with-byte= column and every value equals grep -c on the same file — measured outside the record machinery, so a field-count factor cannot hide here"
else
  pfail "measure: $m_bad mismatch(es) and $m_absent missing column(s) out of $m_checked checked — the per-file column does not say what it is labelled"
fi
if [ "$m_diff_tree" -ne 0 ]; then
  pfail "measure: $m_diff_tree file(s) differ in CONTENT between the real tree and the private tree the gate scanned — the equality above would be comparing two different files"
else
  pok "measure: every compared file is byte-identical between the real tree and the private mirror the gate scanned (cmp), so the pair printed below is one file measured two ways"
fi

m_sample() {   # m_sample <path>: print the gate's column beside the two independent measures
  local b="$1" printed lines bytes
  printed=$(awk -F'\t' -v p="$b" '$1 == p { print $2; exit }' "$m_printed")
  lines=$(grep -c "$BT" "${SCRIPTS}/${b}")
  bytes=$(tr -dc "$BT" < "${SCRIPTS}/${b}" | wc -c | tr -d ' ')
  if [ "$bytes" != "$lines" ]; then
    pok "measure sample: $b — gate prints lines-with-byte=$printed, grep -c finds $lines line(s) carrying the byte, tr finds $bytes byte(s): the column is LINES. The defect this leg kills printed $((lines * 4)) here (4 lines-worth of fields per line)"
  else
    pok "measure sample: $b — gate prints lines-with-byte=$printed, grep -c finds $lines line(s) carrying the byte, tr finds $bytes byte(s): equal on this file, so it cannot tell the two meanings apart alone — the differing samples and the all-file equality above carry that"
  fi
}
[ -n "$m_zero" ] && m_sample "$m_zero"
[ -n "$m_one" ]  && m_sample "$m_one"
[ -n "$m_many" ] && m_sample "$m_many"
if [ -z "$m_zero" ] || [ -z "$m_one" ] || [ -z "$m_many" ]; then
  pnote "measure: no scanned file on this tree falls in $( [ -z "$m_zero" ] && printf 'zero-line ' )$( [ -z "$m_one" ] && printf 'one-line ' )$( [ -z "$m_many" ] && printf 'many-line ' )— the per-file equality above still covers every file that does exist"
fi

# ---- depth: a heredoc below scripts/ must be both LIVE and REFUSED -----------
# The gate went recursive (t_d8f34c60) because the widget-surface checks moved into
# scripts/<name>/. A widened scan is only proven by the same two halves every other leg
# here uses: the payload has to RUN at that depth (or a refusal there scores nothing),
# and the identical bytes have to be REFUSED by the scan (or the widened walk is a
# claim rather than a gate). Both legs plant inside a real subdirectory of the private
# tree — one symlinked as a directory would hide the whole subtree from the walk.
echo "depth: an unquoted heredoc below scripts/ — executed, then refused"
d2dir="zz_depth_check"; d2file="${d2dir}/zz_depth_gate.sh"
mk_tree depth2-live
mkdir -p "${TREE}/scripts/${d2dir}"
{ printf '%s\n' '#!/bin/bash' 'cat > "${1:-out.txt}" <<BODY' "$(payload_for '//')" 'BODY'
  printf '%s\n' 'echo fixture done'; } > "${TREE}/scripts/${d2file}"
chmod +x "${TREE}/scripts/${d2file}"
( cd "$TREE" && bash "${TREE}/scripts/${d2file}" >"$OUT" 2>"$ERR" )
D2_RC=$?
if [ -e "$MARKER" ]; then
  pok "depth-live: the payload inside an unquoted heredoc BELOW scripts/ EXECUTED (fixture rc $D2_RC) — so refusing that depth refuses something real"
else
  pfail "depth-live: the below-scripts/ payload did NOT execute (rc $D2_RC, stderr: $(head -c 200 "$ERR" 2>/dev/null)) — the depth refusal below would prove nothing; fix the fixture before trusting it"
fi
mk_tree depth2-scan
mkdir -p "${TREE}/scripts/${d2dir}"
{ printf '%s\n' '#!/bin/bash' 'cat > "${1:-out.txt}" <<BODY' "$(payload_for '//')" 'BODY'
  printf '%s\n' 'echo fixture done'; } > "${TREE}/scripts/${d2file}"
chmod +x "${TREE}/scripts/${d2file}"
scan "$TREE"
expect_refused "depth-scan (unquoted heredoc with a live byte at $d2file — the region a one-level glob never entered)" "$d2file"
if grep -q "scanned ${d2file}" "$OUT" || grep -qE "^scanned +${d2file}" "$OUT"; then
  pok "depth-scan: the file appears in the scanned population by PATH, so a future surface file cannot hide one directory down"
else
  pfail "depth-scan: rc was $G_RC but $d2file never reached a scanned line — the walk did not enter the directory it claims to cover"
fi

# ---- enumeration edges: the two things '! -type d' buys, and what it must not --
# Every leg above already runs against a scripts/ tree made ENTIRELY of symlinks, so
# the moment the walk grows a '-type f' predicate every leg goes red at once — that
# property is structural, not asserted here. These two are not: a dangling symlink
# produces zero records, and two same-named files in different directories produce
# identical basenames. Both are silent-green shapes if the enumeration is wrong.
echo "enumeration edges: a dangling symlink refuses, and same-named files are told apart by path"
mk_tree link-edge
mkdir -p "${TREE}/scripts/zz_link_check"
ln -s "${IOSAPP}/scripts/build_check.sh" "${TREE}/scripts/zz_link_check/zz_clean_link.sh" 2>/dev/null
ln -s /nonexistent/definitely-not-here.sh "${TREE}/scripts/zz_link_check/zz_dangling.sh" 2>/dev/null
scan "$TREE"
if [ "$G_RC" -ne 0 ] && [ "$BANNERS" -eq 0 ] && grep -q 'zz_link_check/zz_dangling.sh' "$OUT" \
   && grep -q 'not readable' "$OUT"; then
  pok "link-edge: the dangling symlink is REFUSED by path as unreadable (rc $G_RC) — zero records from a listed entry is never reportable as clean"
else
  pfail "link-edge: rc=$G_RC banners=$BANNERS named-it=$(grep -c 'zz_link_check/zz_dangling.sh' "$OUT") — an unreadable entry scanned as if it were empty"
fi
if grep -q "^scanned zz_link_check/zz_clean_link.sh" "$OUT" || grep -qE "^scanned +zz_link_check/zz_clean_link.sh" "$OUT"; then
  pok "link-edge: the symlink to a real script has its own scanned line — a regular-file predicate would have dropped it, and the glob this gate replaced did not"
else
  pfail "link-edge: a symlinked .sh never reached a scanned line — the walk now excludes links, which the one-level glob it replaced included"
fi

mk_tree samename
mkdir -p "${TREE}/scripts/zz_same_a" "${TREE}/scripts/zz_same_b"
printf '%s\n' '#!/bin/bash' 'echo clean, no byte here' > "${TREE}/scripts/zz_same_a/common.sh"
{ printf '%s\n' '#!/bin/bash' 'cat > /dev/null <<BODY' "$(payload_for '//')" 'BODY'
  printf '%s\n' 'echo done'; } > "${TREE}/scripts/zz_same_b/common.sh"
chmod +x "${TREE}/scripts/zz_same_a/common.sh" "${TREE}/scripts/zz_same_b/common.sh"
scan "$TREE"
if [ "$G_RC" -ne 0 ] && [ "$BANNERS" -eq 0 ] \
   && grep -q 'zz_same_b/common.sh' "$OUT" && grep -q 'zz_same_a/common.sh' "$OUT" \
   && ! grep -qE 'FINDING  common.sh' "$OUT"; then
  pok "samename: the two common.sh files are reported as distinct paths (the clean one listed, the offending one refused) — a bare leaf name in a finding would conflate them"
else
  pfail "samename: rc=$G_RC banners=$BANNERS a=$(grep -c 'zz_same_a/common.sh' "$OUT") b=$(grep -c 'zz_same_b/common.sh' "$OUT") — findings are not addressing files uniquely"
fi

# ---- liveness --------------------------------------------------------------
echo "liveness fixtures: the same payload, EXECUTED, in each position"
mk_live() {   # mk_live <leg> <lead> <body|shell|comment>
  mk_tree "$1"
  local p; p="$(payload_for "$2")"
  case "$3" in
    body)    printf '%s\n' '#!/bin/bash' 'cat > "${1:-out.txt}" <<BODY' "$p" 'BODY' \
               > "${WORKROOT}/$1/in.txt" ;;
    shell)   printf '%s\n' '#!/bin/bash' "$p" 'echo fixture done' > "${WORKROOT}/$1/in.txt" ;;
    comment) printf '%s\n' '#!/bin/bash' "# pwn ${BT}touch \"${MARKER}\"${BT}" 'echo fixture done' \
               > "${WORKROOT}/$1/in.txt" ;;
  esac
  cp "${WORKROOT}/$1/in.txt" "${WORKROOT}/$1/fixture.sh"; chmod +x "${WORKROOT}/$1/fixture.sh"
  ( cd "${WORKROOT}/$1" && bash "${WORKROOT}/$1/fixture.sh" >"$OUT" 2>"$ERR" )
  L_RC=$?
}
mk_live live-body "//" body
if [ -e "$MARKER" ]; then
  pok "live-body: the payload inside an unquoted heredoc body EXECUTED — so refusing that position refuses something real"
else
  pfail "live-body: the heredoc-body payload did NOT execute (fixture exit $L_RC, stderr: $(head -c 200 "${WORKROOT}/live-body/err.txt" 2>/dev/null)) — every body leg below proves nothing; fix the fixture before trusting a single refusal"
fi
mk_live live-shell "//" shell
if [ -e "$MARKER" ]; then
  pok "live-shell: the payload in shell position EXECUTED"
else
  pfail "live-shell: the shell-position payload did NOT execute (fixture exit $L_RC, stderr: $(head -c 200 "${WORKROOT}/live-shell/err.txt" 2>/dev/null)) — the shell legs would be vacuous"
fi
mk_live live-comment "$BT" comment
if [ -e "$MARKER" ]; then
  pfail "live-comment: a '#' comment EXPANDED the payload — the inert-comment premise R3 rests on is false and the rules must be re-derived"
else
  pok "live-comment: a '#' comment did NOT expand (no marker, RC $L_RC) — measured, which is what lets R3 print those lines instead of refusing them"
fi

# ---- analyzer fixture table ------------------------------------------------
echo "analyzer fixtures: every grammar shape the gate claims to classify"
mk_fix() {   # mk_fix <tree> <name> <kind>
  local tree="$1" name="$2" kind="$3" p; p="$(payload_for '//')"
  local f="${tree}/scripts/zz_${name}.sh"
  case "$kind" in
    plain_body)      printf '%s\n' '#!/bin/bash' 'cat > /dev/null <<BODY' "$p" 'BODY' > "$f" ;;
    plain_shell)     printf '%s\n' '#!/bin/bash' "$p" 'cat > /dev/null <<BODY' 'ok' 'BODY' > "$f" ;;
    capable_comment) printf '%s\n' '#!/bin/bash' 'cat > /dev/null <<BODY' 'ok' 'BODY' > "$f"
                     awk -v p="# $p" 'NR == 2 { print; print p; next } { print }' "$f" > "$f.2" && mv "$f.2" "$f" ;;
    capable_escaped) printf '%s\n' '#!/bin/bash' 'cat > /dev/null <<BODY' \
                        "echo a \\${BT} escaped byte" 'BODY' > "$f" ;;
    unbound_plain)   # Assembled from pieces on purpose: this file would otherwise
                     # carry the very shape this gate refuses — an unquoted heredoc
                     # opener whose delimiter is followed by a redirection. This
                     # scanner does not track quotes, so even a comment naming that
                     # shape verbatim is refused. Splitting the token across a quoting
                     # boundary is the documented way to mention it (see the limits
                     # block the gate prints).
                     local unb='<<'"BODY > /dev/null"
                     printf '%s\n' '#!/bin/bash' "cat ${unb}" 'BODY' > "$f" ;;
    unbound_quoted)  printf '%s\n' '#!/bin/bash' "cat <<${QT}BODY${QT} > /dev/null" 'BODY' > "$f" ;;
    unclosed)        printf '%s\n' '#!/bin/bash' 'cat > /dev/null <<BODY' "$p" > "$f" ;;
    dash_tab)        printf '%s\n' '#!/bin/bash' 'cat <<-BODY' > "$f"
                     printf '\t%s\n' "$p" >> "$f"; printf '\t%s\n' 'BODY' >> "$f" ;;
    quoted_body)     printf '%s\n' '#!/bin/bash' "cat <<${QT}BODY${QT}" "$p" 'BODY' > "$f" ;;
    hash_comment)    printf '%s\n' '#!/bin/bash' "# pwn ${BT}touch \"${MARKER}\"${BT}" 'echo hi' > "$f" ;;
    shell_escaped)   printf '%s\n' '#!/bin/bash' "echo a \\${BT} escaped byte" 'echo hi' > "$f" ;;
    mention_only)    printf '%s\n' '#!/bin/bash' 'awk '"'"'{ i = index($0, "<<") }'"'"' f' \
                        'printf "%s\n" "cat > x <<BODY" > y' 'echo hi' > "$f" ;;
    *) pfail "mk_fix: unknown kind $kind"; return 1 ;;
  esac
  chmod +x "$f"
}
mk_tree fixtures-refuse
mk_fix "$TREE" plain_body      plain_body
mk_fix "$TREE" plain_shell     plain_shell
mk_fix "$TREE" capable_comment capable_comment
mk_fix "$TREE" capable_escaped capable_escaped
mk_fix "$TREE" unbound_plain   unbound_plain
mk_fix "$TREE" unbound_quoted  unbound_quoted
mk_fix "$TREE" unclosed        unclosed
mk_fix "$TREE" dash_tab        dash_tab
scan "$TREE"
for x in plain_body plain_shell capable_comment capable_escaped unbound_plain \
         unbound_quoted unclosed dash_tab; do
  expect_refused "fixture $x" "zz_${x}.sh"
done

mk_tree fixtures-pass
mk_fix "$TREE" quoted_body   quoted_body
mk_fix "$TREE" hash_comment  hash_comment
mk_fix "$TREE" shell_escaped shell_escaped
mk_fix "$TREE" mention_only  mention_only
scan "$TREE"
if [ "$G_RC" -eq 0 ] && [ "$BANNERS" -eq 1 ]; then
  pok "fixture pass-leg: all four inert/mention shapes pass (RC 0) — the scanner does not refuse what bash cannot expand"
else
  pfail "fixture pass-leg: refused an inert or mention-only shape (RC $G_RC) — the scanner is refusing text it does not understand"
fi
expect_note "fixture quoted_body (byte inside a QUOTED heredoc body)" 3
expect_note "fixture hash_comment (byte inside a '#' comment, no heredoc)" 2
expect_note "fixture shell_escaped (backslash-escaped byte in shell position)" 2
if grep -q -e 'never terminated' -e 'does not end the line' "$OUT"; then
  pfail "fixture mention_only: the scanner invented an unbounded region from text that only MENTIONS the operator — the exact false positive that once killed this gate"
else
  pok "fixture mention_only: lines carrying the operator inside a string opened no region"
fi

# ---- body legs: every unquoted heredoc of every real script ----------------
echo "planting inside every unquoted heredoc body of every real script"
body_legs=0
while IFS= read -r -d '' f; do
  name="$(rel "$f")"
  while IFS="$(printf '\t')" read -r oline delim; do
    [ -n "${oline:-}" ] || continue
    tline=$(awk -v s="$oline" -v d="$delim" 'NR > s && $0 == d { print NR; exit }' "$f")
    if [ -z "$tline" ]; then
      pfail "$name: unquoted heredoc opened at line $oline (delimiter $delim) never closes at column 0 — the gate cannot bound its body, so it cannot vouch for it"
      continue
    fi
    body_legs=$((body_legs + 1))
    mk_tree "body-$(legid "$name")-$oline"
    plant "$name" "$oline" "//"
    scan "$TREE"
    expect_refused "body-$name:$oline (inside unquoted <<$delim, region $oline..$tline)" "$name"
  done <<EOF
$(openers_of "$f")
EOF
done < <(all_sh)
if [ "$body_legs" -eq 0 ]; then
  pfail "no unquoted heredoc in the whole tree — the body legs pass vacuously; if that ever becomes true, delete this gate rather than keep a green that means nothing"
else
  pok "body: $body_legs unquoted heredoc region(s) enumerated at run time, all refused"
fi

# ---- shell legs: every real script ------------------------------------------
echo "planting in shell position of every real script"
shell_legs=0
while IFS= read -r -d '' f; do
  name="$(rel "$f")"
  lline=$(first_exec "$f")
  [ -n "$lline" ] || { pfail "$name: no executable line to plant in — the shell-position class is unproven here"; continue; }
  shell_legs=$((shell_legs + 1))
  mk_tree "shell-$(legid "$name")"
  plant "$name" "$lline" "//"
  scan "$TREE"
  expect_refused "shell-$name (comment-shaped line in shell position at line $lline)" "$name"
done < <(all_sh)
pok "shell: $shell_legs script(s) planted and scanned"

# ---- rename: content, not filename -----------------------------------------
echo "planting through a renamed copy with the canonical gate clean beside it"
src=$(subject_capable || true)
if [ -z "$src" ]; then
  pfail "no splice-capable script to rename — the rename leg has no subject"
else
  rename_as="zz_renamed_$(printf '%s' "$src" | tr '/' '_')"   # a path is not a leaf name
  mk_tree rename
  plant "$src" "$(first_exec "${SCRIPTS}/${src}")" "//"
  mv "${TREE}/scripts/${src}" "${TREE}/scripts/${rename_as}"
  ln -s "${SCRIPTS}/${src}" "${TREE}/scripts/${src}"   # canonical, clean, right beside it
  scan "$TREE"
  expect_refused "rename (mutated copy named $rename_as, unmodified $src symlinked next to it)" "$rename_as"
fi

# ---- R2 and the honest-skip legs -------------------------------------------
echo "R2: the same '#' payload inside a SPLICE-CAPABLE script"
cap=$(subject_capable || true)
if [ -z "$cap" ]; then
  pfail "no splice-capable script — the R2 comment leg has no subject"
else
  mk_tree comment-capable
  plant "$cap" 2 "#"
  scan "$TREE"
  expect_refused "comment-capable ($cap is splice-capable, so R2 holds it to the file-wide ban)" "$cap"
fi

echo "inert regions: the gate must pass AND disclose the planted line"
for spec in "build_check.sh:comment" "sf_symbol_check.sh:quoted" "check_theme.sh:escaped"; do
  name="${spec%%:*}"; kind="${spec##*:}"
  [ -f "${SCRIPTS}/${name}" ] || { pfail "$name: gone — the honest-skip leg has no subject"; continue; }
  if [ -n "$(openers_of "${SCRIPTS}/${name}")" ]; then
    pfail "$name: unexpectedly splice-capable — an inert-region leg on it would be an R2 refusal, not a note"
    continue
  fi
  mk_tree "skip-$kind-$name"
  case "$kind" in
    comment) plant "$name" 2 "#"
             scan "$TREE"
             # The payload is inserted AFTER line 2, so it sits on line 3.
             expect_note "comment-measured ($name: payload inside a '#' comment)" 3 ;;
    quoted)  ql=$(qopeners_of "${SCRIPTS}/${name}" | head -1)
             if [ -z "$ql" ]; then pfail "$name: no quoted-delimiter heredoc — the quoted skip leg cannot be proved here"; continue; fi
             plant "$name" "$ql" "//"
             scan "$TREE"
             expect_note "quoted-body ($name: payload inside a QUOTED heredoc opened at $ql)" "$((ql + 1))" ;;
    escaped) dest="${TREE}/scripts/${name}"
             rm -f "$dest"
             # The planted line carries ONE backslash before the byte, so bash would
             # not honour it. printf in double quotes gives that single backslash and
             # the byte comes from BT, so this probe never contains it. Built with
             # head/printf/tail rather than awk -v on purpose: awk processes escapes
             # in -v values and eats the backslash, which would plant a LIVE byte and
             # make the leg test the wrong thing (measured, not assumed).
             escline="echo a \\${BT} escaped byte in shell position"
             { head -1 "${SCRIPTS}/${name}"
               printf '%s\n' "$escline"
               tail -n +2 "${SCRIPTS}/${name}"; } > "$dest"
             chmod +x "$dest"
             scan "$TREE"
             expect_note "escaped-shell ($name: backslash-escaped byte in shell position)" 2 ;;
  esac
done

# ---- the shelltc verdict: refused, and described as what it is ------------------
# A code-bearing line whose TRAILING comment carries the byte. bash would not expand
# it, and the gate still refuses it, because deciding "no quote is open earlier on
# this line" is quote tracking and a payload can corrupt a quote tracker. Two things
# have to hold, and only the first is obvious: the shape is refused (fail closed), and
# the refusal does not claim the byte runs. A gate that labels an inert shape
# "executed" trains people to ignore its findings, so the wording itself is under test
# here — the discriminator is the phrase only the honest message carries.
tc="check_theme.sh"                 # a real gate that is NOT splice-capable
mk_tree "shelltc-$tc"               # bare: mk_tree publishes TREE/MARKER/OUT/ERR as globals
rm -f "${TREE}/scripts/$tc"
tcline="echo hello ${BT}in prose${BT}   # trailing comment carries the byte"
{ head -1 "${SCRIPTS}/${tc}"
  printf '%s\n' "$tcline"
  tail -n +2 "${SCRIPTS}/${tc}"; } > "${TREE}/scripts/$tc"
chmod +x "${TREE}/scripts/$tc"
scan "$TREE"
if [ "$G_RC" -ne 0 ] && [ "$BANNERS" -eq 0 ] \
   && grep -q "code-bearing line" "$OUT" \
   && ! grep -q "RUNS when this script runs" "$OUT"; then
  pok "shelltc ($tc): trailing-comment byte refused fail-closed AND described as a shape the scanner will not vouch for, not as code that runs"
else
  pfail "shelltc ($tc): expected a refusal that does NOT claim execution (RC $G_RC, banners $BANNERS); got: $(grep -m1 FINDING "$OUT" | head -c 200)"
fi

# ---- the residual this gate exists to close --------------------------------
echo "window above the guard: an in-file guard cannot see this high — REPORTED, closed by refusal"
wsrc=""
while IFS= read -r -d '' f; do
  b="$(rel "$f")"
  case "$b" in *probe*|heredoc_backtick_check*) continue ;; esac
  if grep -q '^# GUARD-SENTINEL' "$f" 2>/dev/null; then wsrc="$b"; break; fi
done < <(all_sh)
if [ -z "$wsrc" ]; then
  pnote "no script here carries an in-file guard sentinel — the window-above leg has no subject on this tree. It is the reason this meta-gate exists, so re-run it where the guard is (PR #345, merged as bc3c1674)."
else
  mk_tree window-above
  plant "$wsrc" 1 "//"          # line 1 is the shebang: this payload sits ABOVE the guard
  ( cd "$(dirname "$TREE")" && TMPDIR="$(dirname "$TREE")" bash "${TREE}/scripts/${wsrc}" >"$OUT" 2>"$ERR" )
  W_RC=$?
  if [ -e "$MARKER" ]; then
    # The reach boundary is the guard FIRST EXECUTABLE STATEMENT, never the comment
    # that names it: a marker token also matches the prose explaining the marker, and
    # exactly that mistake measured a guard reach 11x wrong on PR #345. So the matched
    # line must start with code, and when no line qualifies NO figure is printed —
    # these numbers get copied into fragments and other cards, and a derived number
    # that cannot be anchored is worth less than no number.
    gl=$(awk '/GUARD-FIRST-EXEC/ { t = $0; sub(/^[[:space:]]*/, "", t)
                                 if (t != "" && t !~ /^#/) { print NR; exit } }' \
             "${SCRIPTS}/${wsrc}")
    if [ -n "$gl" ]; then
      pnote "window-above: the payload ABOVE the guard of $wsrc EXECUTED (marker created, gate exited $W_RC). Its first executable statement is line $gl, so lines 2..$((gl - 1)) are outside its reach — measured every run on purpose. Whoever can write there can also delete the guard, which is why the closure is an EXTERNAL scanner and not a bigger in-file guard."
    else
      pnote "window-above: the payload ABOVE the guard of $wsrc EXECUTED (marker created, gate exited $W_RC), but no executable GUARD-FIRST-EXEC line was found in it, so no reach figure is printed. The execution is the measured fact; the span is not guessed."
    fi
  else
    pnote "window-above: the payload above the guard of $wsrc did not execute this run (gate exited $W_RC). Reported rather than counted as a pass: whether it executes depends on where the guard sits, and a guard's reach is never something this probe should rely on."
  fi
  scan "$TREE"
  expect_refused_only "window-above closure ($wsrc mutated above its own guard — the region the in-file guard cannot judge)" "$wsrc"
fi

# ---- integrity --------------------------------------------------------------
echo "integrity: the real tree, untouched"
after_list=$(all_sh | xargs -0 shasum -a 256 2>/dev/null | shasum -a 256 | cut -d' ' -f1)
if [ "$baseline_list" = "$after_list" ]; then
  pok "every real script under scripts/ is byte-identical after the run ($after_list)"
else
  pfail "the probe MUTATED the tree it was auditing — the combined digest moved"
fi
if [ "$have_worktree" = "1" ]; then
  after_dirty=$(git -C "$REPO" status --porcelain 2>/dev/null)
  # Scope the accusation to the probe's BLAST RADIUS. This leg exists to catch the probe
  # writing into the tree it audits (scripts/, Sources/, the golden) — it is not a monitor
  # for the rest of the checkout, and measuring one as the other produced a red run here
  # for a concurrent build's transient artifacts (measured: the digest leg stayed green,
  # porcelain went dirty, the tree was clean again afterwards). A false accusation on an
  # integrity leg is worse than a missing one: people start ignoring the leg that matters.
  new_paths=$(comm -13 <(printf '%s\n' "$baseline_dirty" | sed 's/^....//' | sort) \
                       <(printf '%s\n' "$after_dirty"    | sed 's/^....//' | sort) \
                      | grep -v '^$')
  blast=$(printf '%s\n' "$new_paths" | grep -E '^ios-app/(scripts|Sources)/' || true)
  if [ -n "$blast" ]; then
    pfail "the probe wrote inside the audited tree: $(printf '%s' "$blast" | tr '\n' ' ')"
  elif [ -n "$new_paths" ]; then
    pnote "porcelain gained path(s) OUTSIDE the audited tree: $(printf '%s' "$new_paths" | tr '\n' ' ') — not this probe's blast radius (another writer was in the checkout). Reported so it is visible; not blamed here, because the digest leg above is what proves the audited files are unchanged."
  else
    pok "the repo's porcelain status is unchanged — no marker, log or stray copy landed inside the checkout"
  fi
fi

echo "marker census"
allowed='live-body live-shell live-comment window-above depth2-live'
strays=""
for d in "$WORKROOT"/*/; do
  leg="$(basename "$d")"
  found=$(find "$d" -maxdepth 1 -name 'MARKER_*' 2>/dev/null | wc -l | tr -d ' ')
  case " $allowed " in
    *" $leg "*) ;;
    *) [ "$found" != "0" ] && strays="$strays $leg($found)" ;;
  esac
done
if [ -z "$strays" ]; then
  pok "no marker file was created by any scan leg — only the executed liveness/window legs wrote one, each inside its own leg dir"
else
  pfail "marker file(s) created by legs that must not execute anything:$strays"
fi

if [ "$probe_fail" -gt 0 ]; then
  echo "HEREDOC BACKTICK PROBE FAILED — $probe_fail leg(s) red. Work kept at $WORKROOT"
  exit 1
fi
echo "HEREDOC BACKTICK PROBE PASSED — control green and anti-vacuous, gate and probe" \
     "enumerate the same population path for path, every printed per-file count equals an" \
     "independent grep/tr measurement, the below-scripts/ heredoc executed" \
     "and was refused by path, $body_legs heredoc body leg(s) + $shell_legs shell leg(s)" \
     "+ rename + both fixture tables + the above-guard window all behaved, no marker" \
     "file, real tree byte-identical (work: $WORKROOT)"
exit 0
