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
#   control          the unmutated symlinked tree must PASS, must scan the same
#                    NUMBER of scripts as the real tree, and must report at least
#                    as many splice-capable scripts as this probe independently
#                    enumerates. Three anti-vacuity checks: a harness that scans an
#                    empty or misparsed population makes every leg below
#                    meaningless, and looks exactly like success.
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

# ---- baseline, measured before any leg runs --------------------------------
baseline_list=$( (cd "$SCRIPTS" && shasum -a 256 ./*.sh 2>/dev/null) | shasum -a 256 | cut -d' ' -f1)
real_scripts=$(ls -1 "$SCRIPTS"/*.sh 2>/dev/null | wc -l | tr -d ' ')
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

# ---- harness ---------------------------------------------------------------
mk_tree() {   # mk_tree <leg>: private ios-app dir; scripts/ and Sources/ symlinked
  TREE="${WORKROOT}/$1/ios-app"
  mkdir -p "${TREE}/scripts"
  ln -s "${IOSAPP}/Sources" "${TREE}/Sources" 2>/dev/null
  local f b
  for f in "${SCRIPTS}"/*; do
    b="$(basename "$f")"
    ln -s "$f" "${TREE}/scripts/${b}" 2>/dev/null
  done
  MARKER="${WORKROOT}/$1/MARKER_$1"
  OUT="${WORKROOT}/$1/out.txt"
  ERR="${WORKROOT}/$1/err.txt"
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
  awk -v n="$after" -v p="$p" '{ print; if (NR == n) print p }' \
      "${SCRIPTS}/${name}" > "$dest"
  chmod +x "$dest"
}

# A splice-capable real script to use as a subject, skipping this gate's own files:
# the scanner under test must not be its own subject.
subject_capable() {
  local f b
  for f in "${SCRIPTS}"/*.sh; do
    b="$(basename "$f")"
    case "$b" in heredoc_backtick_check*) continue ;; esac
    [ -n "$(openers_of "$f")" ] && { printf '%s' "$b"; return 0; }
  done
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
for f in "${SCRIPTS}"/*.sh; do [ -n "$(openers_of "$f")" ] && probe_capable=$((probe_capable + 1)); done
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
for f in "${SCRIPTS}"/*.sh; do
  name="$(basename "$f")"
  while IFS="$(printf '\t')" read -r oline delim; do
    [ -n "${oline:-}" ] || continue
    tline=$(awk -v s="$oline" -v d="$delim" 'NR > s && $0 == d { print NR; exit }' "$f")
    if [ -z "$tline" ]; then
      pfail "$name: unquoted heredoc opened at line $oline (delimiter $delim) never closes at column 0 — the gate cannot bound its body, so it cannot vouch for it"
      continue
    fi
    body_legs=$((body_legs + 1))
    mk_tree "body-$name-$oline"
    plant "$name" "$oline" "//"
    scan "$TREE"
    expect_refused "body-$name:$oline (inside unquoted <<$delim, region $oline..$tline)" "$name"
  done <<EOF
$(openers_of "$f")
EOF
done
if [ "$body_legs" -eq 0 ]; then
  pfail "no unquoted heredoc in the whole tree — the body legs pass vacuously; if that ever becomes true, delete this gate rather than keep a green that means nothing"
else
  pok "body: $body_legs unquoted heredoc region(s) enumerated at run time, all refused"
fi

# ---- shell legs: every real script ------------------------------------------
echo "planting in shell position of every real script"
shell_legs=0
for f in "${SCRIPTS}"/*.sh; do
  name="$(basename "$f")"
  lline=$(first_exec "$f")
  [ -n "$lline" ] || { pfail "$name: no executable line to plant in — the shell-position class is unproven here"; continue; }
  shell_legs=$((shell_legs + 1))
  mk_tree "shell-$name"
  plant "$name" "$lline" "//"
  scan "$TREE"
  expect_refused "shell-$name (comment-shaped line in shell position at line $lline)" "$name"
done
pok "shell: $shell_legs script(s) planted and scanned"

# ---- rename: content, not filename -----------------------------------------
echo "planting through a renamed copy with the canonical gate clean beside it"
src=$(subject_capable || true)
if [ -z "$src" ]; then
  pfail "no splice-capable script to rename — the rename leg has no subject"
else
  rename_as="zz_renamed_${src}"
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
for f in "${SCRIPTS}"/*.sh; do
  b="$(basename "$f")"
  case "$b" in *probe*|heredoc_backtick_check*) continue ;; esac
  if grep -q '^# GUARD-SENTINEL' "$f" 2>/dev/null; then wsrc="$b"; break; fi
done
if [ -z "$wsrc" ]; then
  pnote "no script here carries an in-file guard sentinel — the window-above leg has no subject on this tree. It is the reason this meta-gate exists, so re-run it where the guard is (PR #345, merged as bc3c1674)."
else
  mk_tree window-above
  plant "$wsrc" 1 "//"          # line 1 is the shebang: this payload sits ABOVE the guard
  ( cd "$(dirname "$TREE")" && TMPDIR="$(dirname "$TREE")" bash "${TREE}/scripts/${wsrc}" >"$OUT" 2>"$ERR" )
  W_RC=$?
  if [ -e "$MARKER" ]; then
    gl=$(awk '/GUARD-SENTINEL/ { print NR; exit }' "${SCRIPTS}/${wsrc}")
    pnote "window-above: the payload ABOVE the guard of $wsrc EXECUTED (marker created, gate exited $W_RC). Its guard's first line is $gl, so that much prologue is outside its reach — measured every run on purpose. Whoever can write there can also delete the guard, which is why the closure is an EXTERNAL scanner and not a bigger in-file guard."
  else
    pnote "window-above: the payload above the guard of $wsrc did not execute this run (gate exited $W_RC). Reported rather than counted as a pass: whether it executes depends on where the guard sits, and a guard's reach is never something this probe should rely on."
  fi
  scan "$TREE"
  expect_refused_only "window-above closure ($wsrc mutated above its own guard — the region the in-file guard cannot judge)" "$wsrc"
fi

# ---- integrity --------------------------------------------------------------
echo "integrity: the real tree, untouched"
after_list=$( (cd "$SCRIPTS" && shasum -a 256 ./*.sh 2>/dev/null) | shasum -a 256 | cut -d' ' -f1)
if [ "$baseline_list" = "$after_list" ]; then
  pok "every real script in scripts/ is byte-identical after the run ($after_list)"
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
allowed='live-body live-shell live-comment window-above'
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
echo "HEREDOC BACKTICK PROBE PASSED — control green and anti-vacuous, liveness proven," \
     "$body_legs heredoc body leg(s) + $shell_legs shell leg(s) + rename + both fixture" \
     "tables + the above-guard window all behaved, no marker file, real tree byte-identical" \
     "(work: $WORKROOT)"
exit 0
