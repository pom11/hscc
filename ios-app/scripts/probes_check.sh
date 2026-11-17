#!/bin/bash
# probes_check.sh — t_48ac8a98: run EVERY ios-app/scripts/*_probe.sh, found by glob.
#
# WHY THIS FILE EXISTS. ios-app/scripts/ holds injection probes whose whole job is to
# prove a gate cannot be walked past: widget_settings_check_backtick_probe.sh
# (t_9e4a731b, PR #345) and heredoc_backtick_check_probe.sh (t_4558fd32, PR #349).
# Nothing in the repo ran either one. Measured on main @ 38ce2372:
#   grep -rn '_probe' .github/workflows ios-app/scripts/build_check.sh
# returns only an unrelated comment in address-guard.yml. So the evidence for the
# backtick-splice class existed only when a worker happened to type the path. A gate
# whose negative proof is never executed is a gate one refactor away from being
# silently broken — which is exactly what happened to merge_base_check.sh's trip-wire,
# untested across four orchestrator attempts (t_cedc5f9e). This file is the thing that
# references the probes, and it references them BY GLOB, so a third probe is executed
# the day it is committed without any edit being made here.
#
# WHY IT IS NOT IN build_check.sh (decision, do not re-litigate). build_check.sh costs
# about 14 minutes of Swift compile; each probe costs 5-8 minutes on top of that. A
# probe must also never be renamed to *_check.sh, because that glob IS build_check's
# gate set — doing so adds a whole Swift compile per gate. Both halves of that decision
# are asserted here rather than trusted: this script refuses if build_check.sh ever
# starts naming a probe, and refuses any discovered leaf that also matches *_check.sh.
#
# WHY IT IS NOT A CI STEP (measured, not assumed). The repo has exactly two workflows,
# address-guard.yml and check-runtime-deps.yml, both runs-on ubuntu-latest, and
#   git log -S 'runs-on: macos' origin/main -- .github
# returns nothing — this repo has never had a macOS job. The probes need a Swift
# toolchain (widget_settings_check_backtick_probe.sh runs copies of
# widget_settings_check.sh, which compiles Swift), so a step bolted onto today's CI
# could only fail for the wrong reason. The smallest correct wiring is one step in a
# future macOS ios-app workflow; until such a runner exists this gate is the local
# entry point, and the per-probe seconds it prints are the number a CI-budget decision
# needs. The toolchain is preflighted below so a missing swiftc is reported as an
# environment fault instead of arriving as a probe failure.
#
# HOW A RUNNER PRINTS GREEN WITHOUT TESTING ANYTHING — three paths, each closed here
# (the family is documented in skill hscc-probe-harness-pitfalls):
#   1. zero probes discovered. A glob that matches nothing looks exactly like a clean
#      run, so zero is REFUSED (exit 2), never celebrated.
#   2. a borrowed exit code. Nothing here decides anything from a pipeline: a probe's
#      output goes to its own log file by redirection and the exit code comes from that
#      command alone. (gate | tail reports tail, always.)
#   3. a probe that exits 0 having done nothing. Both probes in this repo end with an
#      uppercase verdict banner containing PASSED or FAILED, so that banner is
#      REQUIRED: exit 0 with an empty log, or with a final line carrying neither word,
#      is a harness failure. Not hypothetical — heredoc_backtick_check_probe.sh prints
#      one line and exits 0 when HEREDOC_BACKTICK_PROBE_NESTED is set in its
#      environment (its own nesting guard, t_4558fd32). A runner that inherited that
#      variable would report a green probe that executed nothing, so the variable is
#      cleared for every probe invocation and the banner rule catches the same shape in
#      any future probe. Symmetrically, a probe that PRINTS a FAILED verdict and then
#      exits 0 is refused too: that is the shipped-defect shape this class descends
#      from — exit 0 while something red just happened.
#
# Exit codes:
#   0  every discovered probe exited 0 with a PASSED banner
#   1  a probe failed (named, with its own final line and a log tail)
#   2  harness error — nothing discovered, a probe exited 2 (its own tree was lost),
#      a probe exited 0 with no output or no verdict banner, a probe printed FAILED and
#      exited 0, or a naming rule was violated. 2 is never a finding of the class: it
#      means the evidence was not produced, and reading it as one would be a false
#      accusation against the code under test.
#
# Usage: bash ios-app/scripts/probes_check.sh
#        Documented from the repo root (how heredoc_backtick_check_probe.sh resolves
#        its own scripts dir); paths here resolve from BASH_SOURCE, so any cwd works.
# Logs:  full output of every probe goes OUTSIDE the checkout — a log written inside a
#        worktree gets swept up by someone's later git add -A (t_9664a1c8). Override
#        with PROBES_CHECK_LOGDIR.
set -uo pipefail
# No set -e on purpose: every probe runs even if an earlier one failed, so one run
# reports the state of the whole probe set instead of stopping at the first red.

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IOSAPP="$(cd "${SELF_DIR}/.." && pwd)"
SCRIPTS="${IOSAPP}/scripts"
REPO="$(cd "${IOSAPP}/.." && pwd)"
BUILD_CHECK="${SCRIPTS}/build_check.sh"

# Resolved BEFORE the cd, and deliberately outside the checkout.
LOGDIR="${PROBES_CHECK_LOGDIR:-${TMPDIR:-/tmp}/probes_check.$$}"

refuse() {   # a harness fault: the evidence was not produced, so nothing may be concluded
  printf 'PROBES CHECK REFUSED (harness) — %s\n' "$1" >&2
  printf '  logs: %s\n' "$LOGDIR" >&2
  exit 2
}

[ -d "$SCRIPTS" ] || refuse "no scripts dir at $SCRIPTS"
mkdir -p "$LOGDIR" 2>/dev/null || refuse "cannot create the log dir $LOGDIR"
cd "$REPO" 2>/dev/null || refuse "cannot cd to the repo root $REPO"

# The invocation heredoc_backtick_check_probe.sh documents, so the harness resolves the
# same scripts dir whether or not the caller obeyed the documented cwd.
printf 'probes_check.sh: repo %s @ %s\n' \
  "$REPO" "$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || printf 'not a work tree')"

if [ -f "$BUILD_CHECK" ] && grep -q '_probe' "$BUILD_CHECK"; then
  refuse "build_check.sh names a probe — probes are deliberately NOT in that gate, because it compiles Swift for every gate it globs"
fi
command -v swiftc >/dev/null 2>&1 ||
  refuse "no swiftc on PATH — the probes run copies of gates that compile Swift, so a failure now would be an environment fault reported as a finding"

# ---- discovery: a glob, so a probe nobody references cannot rot ------------------
# find, not ls, so a non-matching glob cannot be mistaken for an empty population by
# way of a shell error, and LC_ALL=C so the order is the same on every machine.
probe_list="$(find "$SCRIPTS" -maxdepth 1 -name '*_probe.sh' 2>/dev/null | LC_ALL=C sort)"

probes=()
while IFS= read -r line; do
  [ -n "$line" ] && probes+=("$line")
done < <(printf '%s\n' "$probe_list")

count=${#probes[@]}
if [ "$count" -eq 0 ]; then
  refuse "no *_probe.sh found in $SCRIPTS. An empty probe set is not a pass: it means the negative proofs are gone (deleted, renamed, or never committed) and nothing here can tell that apart from every probe passing. If probes were deliberately retired, delete this gate in the same commit."
fi

printf 'discovered %s probe(s) by glob *_probe.sh in %s:\n' "$count" "$SCRIPTS"
for p in "${probes[@]}"; do
  printf '  %s\n' "$(basename "$p")"
done

# A file whose name nearly matches the glob is a probe nothing will ever run — the same
# rot this gate exists to prevent, so it is disclosed rather than silently skipped.
while IFS= read -r line; do
  [ -n "$line" ] || continue
  case "$line" in
    *_probe.sh|*_check.sh) continue ;;   # run by this gate / run by build_check.sh
  esac
  printf '  note: %s names "probe" but matches neither glob — nothing runs it. Rename it to *_probe.sh (this gate) or *_check.sh (build_check), or say here why it is neither.\n' "$(basename "$line")"
done < <(find "$SCRIPTS" -maxdepth 1 -name '*probe*.sh' 2>/dev/null | LC_ALL=C sort)

# ---- run every probe, in name order ---------------------------------------------
run=0; passed=0; failed=0; harness=0
started_all=$SECONDS

for p in "${probes[@]}"; do
  base="$(basename "$p")"
  [ -f "$p" ] || refuse "discovered $base is not a regular file"
  [ -r "$p" ] || refuse "discovered $base is not readable"
  case "$base" in
    *_check.sh)
      refuse "discovered $base matches both *_probe.sh and *_check.sh — that second glob is build_check.sh's gate set, so this probe would add a full Swift compile to it"
      ;;
  esac

  out="${LOGDIR}/${base}.log"
  run=$((run + 1))
  started=$SECONDS
  # Redirection, never a pipe: the exit code must belong to the probe. The nesting
  # guard variable is cleared so an inherited value cannot make a probe no-op and then
  # report that as a pass.
  env -u HEREDOC_BACKTICK_PROBE_NESTED bash "$p" >"$out" 2>&1
  rc=$?
  took=$((SECONDS - started))
  last="$(tail -n 1 "$out")"

  if [ "$rc" -eq 2 ]; then
    printf 'PROBE %s: rc=2 (%ss) HARNESS ERROR — the probe could not resolve what it was supposed to scan. Not a finding of the class: no evidence was produced.\n' "$base" "$took"
    printf '  final line: %s\n' "${last:-<no output>}"
    harness=$((harness + 1))
    continue
  fi

  if [ "$rc" -ne 0 ]; then
    printf 'PROBE %s: rc=%s (%ss) FAILED — %s\n' "$base" "$rc" "$took" "${last:-<no output>}"
    printf '  last lines of %s:\n' "$out"
    tail -n 25 "$out" | sed 's/^/    /'
    failed=$((failed + 1))
    continue
  fi

  # rc == 0. Three ways that is still not evidence.
  if [ ! -s "$out" ]; then
    printf 'PROBE %s: rc=0 (%ss) REFUSED — it exited clean having written nothing at all. A probe that produced no output proved nothing.\n' "$base" "$took"
    harness=$((harness + 1))
    continue
  fi
  case "$last" in
    *FAILED*)
      printf 'PROBE %s: rc=0 (%ss) REFUSED — its final line reports FAILED but it exited 0, so the leg counter never reached its exit code. This is the shape of the original defect: exit 0 while something red happened.\n' "$base" "$took"
      printf '  final line: %s\n' "$last"
      harness=$((harness + 1))
      ;;
    *PASSED*)
      printf 'PROBE %s: rc=0 (%ss) — %s\n' "$base" "$took" "$last"
      passed=$((passed + 1))
      ;;
    *)
      printf 'PROBE %s: rc=0 (%ss) REFUSED — its final line carries no PASSED or FAILED banner, so an exit 0 here cannot be told apart from a probe that ran nothing (early exit, stripped body, a guard that silenced it). A probe must end with an uppercase PASSED or FAILED verdict line.\n' "$base" "$took"
      printf '  final line: %s\n' "$last"
      harness=$((harness + 1))
      ;;
  esac
done

total=$((SECONDS - started_all))
printf 'probes_check.sh: %s probe(s) executed in %ss — %s passed, %s failed, %s harness error(s). logs: %s\n' \
  "$run" "$total" "$passed" "$failed" "$harness" "$LOGDIR"

if [ "$harness" -gt 0 ]; then
  printf 'PROBES CHECK FAILED (harness) — %s of %s probe(s) did not produce usable evidence. Nothing has been shown about the class.\n' "$harness" "$run"
  exit 2
fi
if [ "$failed" -gt 0 ]; then
  printf 'PROBES CHECK FAILED — %s of %s probe(s) refused their gate or found a planted payload. See the named PROBE lines above.\n' "$failed" "$run"
  exit 1
fi
printf 'PROBES CHECK PASSED — %s probe(s) discovered by glob, every one exited 0 with a PASSED banner.\n' "$run"
exit 0
