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
# about 14 minutes of Swift compile; each probe costs seconds on top of that, but a
# probe is not a gate — it is the evidence that a gate cannot be walked past — and the
# *_check.sh leaf convention marks gates that a run-all loop runs by name, so putting a
# 5-8 minute harness in that convention makes it block every compile. (Measured
# correction to the card body, because the difference matters: NOTHING in this repo
# globs *_check.sh. build_check.sh loops over four hard-coded target names and parses
# project.yml; grep -rn '[*]_check[.]sh' finds only prose. So "naming a probe
# *_check.sh makes build_check compile Swift per gate", as the card puts it, is not
# what the code does. The guard below is therefore the reachable one: this script
# refuses if build_check.sh ever starts naming a probe, and discloses a *_check.sh
# file that carries a probe's marker-file signature, which is what a renamed probe
# actually looks like.)
#
# WHY IT IS NOT A CI STEP (measured, not assumed). The repo has exactly two workflows,
# address-guard.yml and check-runtime-deps.yml, both runs-on ubuntu-latest, and
#   git log -S 'runs-on: macos' origin/main -- .github
# returns nothing — this repo has never had a macOS job. widget_settings_check_backtick_probe.sh
# runs copies of widget_settings_check.sh, which compiles Swift at line 646, so a step
# bolted onto today's CI could only fail for the wrong reason. This is the part of the
# card's premise that did not survive measurement: the probe set is NOT ~15 minutes
# (a full clean run on main @ 38ce2372 took 76 s wall clock, printed per probe by this
# script on every run), so cost was never the real objection — the absence of any
# macOS runner is. The smallest correct wiring is one step in a future macOS ios-app
# workflow, and it would be nearly free when one exists. The toolchain is preflighted
# below so a missing swiftc is reported as an environment fault instead of arriving as
# a probe failure.
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
#   4. a probe that never finishes. Each probe gets PROBES_CHECK_TIMEOUT seconds
#      (default 900) and is then killed along with its process tree, reported as a
#      harness error naming the probe and its elapsed seconds. The seconds this script
#      prints per probe are the number to judge that limit against; 900 is headroom,
#      not a budget.
#   5. the runner itself not completing. The PASS banner is printed only after an
#      invariant holds: probes discovered == probes entered the loop == verdicts
#      counted. A future edit that adds a branch or a continue which forgets to count
#      therefore reddens instead of printing PASSED over probes it never looked at.
#      Report lines also go through one writer, so a runner whose stdout has been closed
#      (piped into something that exited) refuses rather than finishing green.
#
# Exit codes:
#   0  every discovered probe exited 0 with a PASSED banner
#   1  a probe failed (named, with its own final line and a log tail)
#   2  harness error — nothing discovered (the refusal names what it considered and what
#      nearly matched), a probe exited 2 (its own tree was lost), a probe timed out, a
#      probe exited 0 with no output or no verdict banner, a probe printed FAILED and
#      exited 0, the runner's own invariant failed, or a naming rule was violated.
#      Every non-zero exit names the runner rc on its own line; every probe that ran
#      got a PROBE line naming it with its RC before the runner exits. 2 is never a
#      finding of the class: it means the evidence was not produced, and reading it as
#      one would be a false accusation against the code under test.
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

say() {   # every line of the report goes through here. A report that cannot be written
          # (stdout is a pipe someone closed, an fd that went away) must never be followed
          # by a PASS banner: a green nobody received is still a false green.
  printf "$@" || {
    printf 'PROBES CHECK REFUSED (harness) — the report could not be written (stdout closed?) — runner rc=2\n' >&2
    exit 2
  }
}

refuse() {   # a harness fault: the evidence was not produced, so nothing may be concluded
  printf 'PROBES CHECK REFUSED (harness) — runner rc=2 — %s\n' "$1" >&2 || exit 2
  printf '  logs: %s\n' "$LOGDIR" >&2 || exit 2
  exit 2
}

kill_desc() {   # kill_desc <pid> <depth> <signal>: walk a process tree and signal it.
                # Depth is bounded on purpose. The measured shape here is four levels —
                # this runner's wrapper, the probe, the gate copy the probe runs, and the
                # compiler that copy spawns — so 4 covers a real hang and the residual
                # (something orphaned deeper still) is stated, not papered over: an
                # orphaned probe keeps planting marker files and reddens a later run for
                # a reason nobody can attribute.
  local top="$1" depth="$2" sig="$3" c
  [ "$depth" -le 0 ] && return 0
  for c in $(pgrep -P "$top" 2>/dev/null); do
    kill_desc "$c" $((depth - 1)) "$sig"
    kill "-$sig" "$c" 2>/dev/null
  done
  return 0
}

kill_tree() {   # TERM first so a probe can clean its own work dir, then KILL survivors.
  local pid="$1"
  kill_desc "$pid" 4 TERM
  kill -TERM "$pid" 2>/dev/null
  sleep 3
  kill_desc "$pid" 4 KILL
  kill -KILL "$pid" 2>/dev/null
}

[ -d "$SCRIPTS" ] || refuse "no scripts dir at $SCRIPTS"
mkdir -p "$LOGDIR" 2>/dev/null || refuse "cannot create the log dir $LOGDIR"
cd "$REPO" 2>/dev/null || refuse "cannot cd to the repo root $REPO"

# The invocation heredoc_backtick_check_probe.sh documents, so the harness resolves the
# same scripts dir whether or not the caller obeyed the documented cwd.
say 'probes_check.sh: repo %s @ %s\n' \
  "$REPO" "$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || printf 'not a work tree')"

if [ -f "$BUILD_CHECK" ] && grep -q '_probe' "$BUILD_CHECK"; then
  refuse "build_check.sh names a probe — the probes are deliberately not in that gate (measured on main: build_check.sh globs nothing, it compiles four hard-coded targets parsed from project.yml, so the only way a probe gets in there is someone adding it by hand — and a probe is not a gate, it is the evidence a gate cannot be walked past, so it belongs in this runner, whose per-probe seconds say what it actually costs)"
fi
command -v swiftc >/dev/null 2>&1 ||
  refuse "no swiftc on PATH — the probes run copies of gates that compile Swift, so a failure now would be an environment fault reported as a finding"

# A hanging probe must not become a hanging gate, and must never become a pass either.
# Portable background + poll + kill, deliberately not timeout(1): macOS ships no
# guarantee of that binary (gtimeout only via Homebrew), and a guard that depends on
# which coreutils the machine happens to have is a guard that silently disappears.
timeout_s="${PROBES_CHECK_TIMEOUT:-900}"
case "$timeout_s" in
  ''|*[!0-9]*) refuse "PROBES_CHECK_TIMEOUT must be a whole number of seconds, got: [$timeout_s]" ;;
esac
if [ "$timeout_s" -lt 30 ]; then
  refuse "PROBES_CHECK_TIMEOUT=$timeout_s — below 30 s every probe here would be killed before it could finish a single leg, which is a red run for the wrong reason"
fi

# ---- discovery: a glob, so a probe nobody references cannot rot ------------------
# find, not ls, so a non-matching glob cannot be mistaken for an empty population by
# way of a shell error, and LC_ALL=C so the order is the same on every machine.
probe_list="$(find "$SCRIPTS" -maxdepth 1 -name '*_probe.sh' 2>/dev/null | LC_ALL=C sort)"

probes=()
while IFS= read -r line; do
  [ -n "$line" ] && probes+=("$line")
done < <(printf '%s\n' "$probe_list")

count=${#probes[@]}

# Disclosed for every exit, including the zero-probe refusal: a runner that says only
# "found nothing" cannot be told apart from a runner looking in the wrong directory, so
# the reader is shown what WAS considered and what nearly matched. The near list ignores
# extension on purpose — the failure this gate exists to prevent is a probe renamed OUT
# of the glob, and a probe sitting beside the runner as .sh.disabled or .bak is exactly
# what that looks like.
candidates="$(find "$SCRIPTS" -maxdepth 1 -name '*.sh' 2>/dev/null | LC_ALL=C sort)"
near="$(find "$SCRIPTS" -maxdepth 1 -name '*probe*' ! -name '*_probe.sh' 2>/dev/null | LC_ALL=C sort)"

if [ "$count" -eq 0 ]; then
  printf 'PROBES CHECK REFUSED (harness) — runner rc=2 — no *_probe.sh found in %s\n' "$SCRIPTS" >&2 || exit 2
  printf '  an empty probe set is not a pass. It means the negative proofs are gone\n' >&2 || exit 2
  printf '  (deleted, renamed out of the glob, or never committed), and nothing here can\n' >&2 || exit 2
  printf '  tell that apart from every probe passing. If probes were deliberately\n' >&2 || exit 2
  printf '  retired, delete this gate in the same commit.\n' >&2 || exit 2
  printf '  considered %s shell script(s) in that directory; %s of them name "probe":\n' \
    "$(printf '%s\n' "$candidates" | grep -c . )" "$(printf '%s\n' "$near" | grep -c . )" >&2 || exit 2
  printf '%s\n' "$near" | while IFS= read -r line; do
    [ -n "$line" ] && printf '    %s\n' "$(basename "$line")" >&2
  done
  printf '  logs: %s\n' "$LOGDIR" >&2 || exit 2
  exit 2
fi

say 'discovered %s probe(s) by glob *_probe.sh in %s:\n' "$count" "$SCRIPTS"
for p in "${probes[@]}"; do
  say '  %s\n' "$(basename "$p")"
done

# Two ways a probe stops being run without anyone deciding that, both disclosed rather
# than silently skipped, because a probe nobody runs is the exact rot this gate exists
# to prevent: (1) a name that nearly matches the glob; (2) a planted-payload harness
# wearing the *_check.sh gate convention, which the glob cannot see. The signature is
# marker-file naming / mk_tree / pfail() — measured on main, ZERO *_check.sh files carry
# any of them (the gates that count assertions name it grepfail, not pfail), so this
# note is silent on a healthy tree and appears the moment a probe is renamed.
while IFS= read -r line; do
  [ -n "$line" ] || continue
  case "$line" in
    *_probe.sh) continue ;;               # run by this gate
    *_check.sh) continue ;;               # a gate, run by name — see the note below
  esac
  say '  note: %s names "probe" but matches no glob that runs anything — rename it to *_probe.sh (this gate) or say here why it is neither.\n' "$(basename "$line")"
done < <(find "$SCRIPTS" -maxdepth 1 -name '*probe*.sh' 2>/dev/null | LC_ALL=C sort)

while IFS= read -r line; do
  [ -n "$line" ] || continue
  # This file is the one exception, and it is a fact about wording rather than a pass:
  # the note below and the pattern above necessarily spell the tokens out, so the runner
  # would flag its own source on every single run — a permanent false alarm is exactly
  # how a disclosure gets ignored. Nothing else in the directory is exempt.
  [ "$(basename "$line")" = "probes_check.sh" ] && continue
  if grep -qE 'MARKER_|mk_tree|pfail\(\)' "$line" 2>/dev/null; then
    say '  note: %s carries a planted-payload signature (a MARKER_ file, mk_tree, or pfail()) but is named like a gate, so no glob runs it and it is invisible to build_check too — rename it to *_probe.sh so this gate executes it.\n' "$(basename "$line")"
  fi
done < <(find "$SCRIPTS" -maxdepth 1 -name '*_check.sh' 2>/dev/null | LC_ALL=C sort)

# ---- run every probe, in name order ---------------------------------------------
run=0; passed=0; failed=0; harness=0
started_all=$SECONDS

for p in "${probes[@]}"; do
  base="$(basename "$p")"
  [ -f "$p" ] || refuse "discovered $base is not a regular file"
  [ -r "$p" ] || refuse "discovered $base is not readable"

  out="${LOGDIR}/${base}.log"
  rcfile="${LOGDIR}/${base}.rc"
  rm -f "$rcfile"
  run=$((run + 1))
  started=$SECONDS
  # Redirection, never a pipe: the exit code must belong to the probe. The nesting
  # guard variable is cleared so an inherited value cannot make a probe no-op and then
  # report that as a pass. The probe runs in a background subshell that publishes the
  # code to a file, because a polled kill -0 on a background child succeeds while the
  # child is an unreaped ZOMBIE — a loop written that way never notices the probe
  # finished and reports every probe as a timeout.
  ( env -u HEREDOC_BACKTICK_PROBE_NESTED bash "$p" >"$out" 2>&1
    printf '%s' "$?" >"$rcfile" ) &
  pid=$!
  timed_out=0
  while [ ! -f "$rcfile" ]; do
    took=$((SECONDS - started))
    if [ "$took" -ge "$timeout_s" ]; then
      timed_out=1
      kill_tree "$pid"
      break
    fi
    sleep 5
  done
  wait "$pid" 2>/dev/null
  if [ -s "$rcfile" ]; then
    rc="$(cat "$rcfile")"
  else
    rc=143   # the wrapper died before publishing a code: a killed probe, never a pass
  fi
  took=$((SECONDS - started))
  last="$(tail -n 1 "$out")"

  if [ "$timed_out" -eq 1 ]; then
    say 'PROBE %s: TIMEOUT (%ss > %ss limit, PROBES_CHECK_TIMEOUT) — killed with its children. A probe that hangs is a harness error, never a pass and never a finding.\n' \
      "$base" "$took" "$timeout_s"
    say '  final line before the kill: %s\n' "${last:-<no output>}"
    say '  log: %s\n' "$out"
    harness=$((harness + 1))
    continue
  fi

  if [ "$rc" -eq 2 ]; then
    say 'PROBE %s: rc=2 (%ss) HARNESS ERROR — the probe could not resolve what it was supposed to scan. Not a finding of the class: no evidence was produced.\n' "$base" "$took"
    say '  final line: %s\n' "${last:-<no output>}"
    harness=$((harness + 1))
    continue
  fi

  if [ "$rc" -ne 0 ]; then
    say 'PROBE %s: rc=%s (%ss) FAILED — %s\n' "$base" "$rc" "$took" "${last:-<no output>}"
    say '  last lines of %s:\n' "$out"
    tail -n 25 "$out" | sed 's/^/    /'
    failed=$((failed + 1))
    continue
  fi

  # rc == 0. Three ways that is still not evidence.
  if [ ! -s "$out" ]; then
    say 'PROBE %s: rc=0 (%ss) REFUSED — it exited clean having written nothing at all. A probe that produced no output proved nothing.\n' "$base" "$took"
    harness=$((harness + 1))
    continue
  fi
  case "$last" in
    *FAILED*)
      say 'PROBE %s: rc=0 (%ss) REFUSED — its final line reports FAILED but it exited 0, so the leg counter never reached its exit code. This is the shape of the original defect: exit 0 while something red happened.\n' "$base" "$took"
      say '  final line: %s\n' "$last"
      harness=$((harness + 1))
      ;;
    *PASSED*)
      say 'PROBE %s: rc=0 (%ss) — %s\n' "$base" "$took" "$last"
      passed=$((passed + 1))
      ;;
    *)
      say 'PROBE %s: rc=0 (%ss) REFUSED — its final line carries no PASSED or FAILED banner, so an exit 0 here cannot be told apart from a probe that ran nothing (early exit, stripped body, a guard that silenced it). A probe must end with an uppercase PASSED or FAILED verdict line.\n' "$base" "$took"
      say '  final line: %s\n' "$last"
      harness=$((harness + 1))
      ;;
  esac
done

# The pass banner is earned, not assumed: one probe discovered must mean one verdict
# counted. Without this trip-wire, a future edit that adds a branch exiting the loop
# (or a continue that forgets to count) would print PASSED over a run that never looked
# at some probe at all — the same false green this gate exists to catch, moved up one
# level. It cannot fire on today (the loop body has exactly one non-counting exit, and
# refuse exits before the banner), which is why it is cheap to keep.
verdicts=$((passed + failed + harness))
if [ "$verdicts" != "$run" ] || [ "$run" != "$count" ]; then
  say 'PROBES CHECK FAILED (harness) — runner rc=2 — %s probe(s) discovered, %s entered the loop, %s produced a verdict. The runner did not complete, so no verdict about the class may be printed.\n' \
    "$count" "$run" "$verdicts"
  exit 2
fi

total=$((SECONDS - started_all))
say 'probes_check.sh: %s probe(s) executed in %ss — %s passed, %s failed, %s harness error(s). logs: %s\n' \
  "$run" "$total" "$passed" "$failed" "$harness" "$LOGDIR"

if [ "$harness" -gt 0 ]; then
  say 'PROBES CHECK FAILED (harness) — runner rc=2 — %s of %s probe(s) did not produce usable evidence. Nothing has been shown about the class.\n' "$harness" "$run"
  exit 2
fi
if [ "$failed" -gt 0 ]; then
  say 'PROBES CHECK FAILED — runner rc=1 — %s of %s probe(s) refused their gate or found a planted payload. See the named PROBE lines above.\n' "$failed" "$run"
  exit 1
fi
say 'PROBES CHECK PASSED — runner rc=0 — %s probe(s) discovered by glob, every one exited 0 with a PASSED banner.\n' "$run"
exit 0
