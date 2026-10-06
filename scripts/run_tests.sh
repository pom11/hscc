#!/usr/bin/env bash
# Run the HSCC test suite.
#
# HSCC ships EIGHT INDEPENDENT plugins (hscc-bootstrap, hscc-commands,
# hscc-roles, hscc-cluster, hscc-project, hscc_daemon, sparkrun-hermes,
# hscc-api). Their dirs are deployed
# standalone into ~/.hermes/plugins, so several are hyphenated (not importable
# package names) and each tests/conftest.py puts its OWN dir on sys.path and
# imports its module bare (`import clusterlib`, `import __init__`, `import
# fleet`). Collected together in a single pytest process those bare names
# collide in sys.modules and leak sys.path between dirs — a handful of tests
# then fail purely on collection order. Each dir is green on its own, so we run
# one pytest process PER dir (true isolation, matching how they deploy) and
# aggregate. Exit non-zero if any dir fails.
#
# Usage:  scripts/run_tests.sh [extra pytest args...]
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${HSCC_TEST_PY:-$HOME/.hermes/hermes-agent/venv/bin/python}"
[ -x "$PY" ] || PY="python3"

DIRS=(hscc-bootstrap hscc-commands hscc-roles hscc-cluster hscc-project hscc_daemon sparkrun-hermes hscc-api memori_byodb)

# ━━━ SIGTERM forensics (t_6bb29d46) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RULE: NEVER stop a suite with a name-based sweep — `pkill -f "run_tests.sh"`,
# `pkill -f pytest`, or any pattern-kill of a generic script name. It matches
# by argv substring and kills OTHER CARDS' concurrent suite legs on this host,
# not just yours (three legs died this way on 2026-10-06). To stop your own
# run: the harness process tool's per-pid kill on the recorded background job,
# or `kill <pid>` on the pid from `pgrep -fl "<your full worktree path>"`.
#
# Three rc=143 "mystery reaper" aborts on 2026-10-06 turned out to be KILLS
# BY THE RUNNING AGENTS THEMSELVES (a `pkill -f "run_tests.sh"` issued to
# restart their own legs, and process.kill of a gate leg). A bare rc=143 at
# the end of a log reads like an unexplained reaper; this trap makes the log
# itself state it was killed, when, and mid-which-suite — so rc=143 can never
# be mistaken for a test failure again. A POSIX shell cannot learn the
# sender's PID from the signal; the killer's identity is recoverable from
# the session DB tool_calls / process-results termination_source (see
# docs/audits/rc143-suite-reaper-t_6bb29d46.md for the forensics recipe).
CURRENT_SUITE="(startup)"
on_sigterm() {
  echo
  echo "!! run_tests.sh RECEIVED SIGTERM at $(date '+%Y-%m-%d %H:%M:%S') — elapsed ${SECONDS}s, during suite: $CURRENT_SUITE"
  echo "   pid=$$ ppid=$PPID; parent: $(ps -o pid=,ppid=,command= -p "$PPID" 2>/dev/null | tr -s ' ')"
  echo "   This is an EXTERNAL KILL (shell rc=143), NOT a test failure."
  echo "   Prime suspect: an agent command 'pkill -f run_tests.sh' or a process.kill of"
  echo "   this suite's process — check the issuing session's transcript before believing"
  echo "   any 'reaper' theory (docs/audits/rc143-suite-reaper-t_6bb29d46.md)."
  exit 143
}
trap on_sigterm TERM

rc=0
declare -a summary
for d in "${DIRS[@]}"; do
  CURRENT_SUITE="$d"
  echo "━━━ $d ━━━"
  # Run each suite OUTSIDE the delegated-child execution context so a kanban
  # dispatcher worker invoking this script gets an isolated, non-read-only
  # fixture environment. When HERMES_DELEGATED_CHILD_CONTEXT is set (it is for
  # every dispatched worker), hermes_cli.kanban_db_connect.connect() opens the
  # kanban DB ?mode=ro and refuses to create/migrate a missing tmp-path board,
  # so any test that builds a fresh fixture board fails with "unable to open
  # database file". Tests run here against isolated tmp fixtures/boards, never
  # the operator's real board, so the worker-delegation read fence does not
  # apply and its absence cannot let a test touch real state. This is a harness
  # fix: a test that only fails inside the delegated worker context is a harness
  # bug, not a code failure — do not report it as "pre-existing".
  env -u HERMES_DELEGATED_CHILD_CONTEXT \
    "$PY" -m pytest -q "$ROOT/$d/tests" -p no:cacheprovider "$@"
  code=$?
  if [ $code -eq 0 ]; then
    summary+=("  ✓ $d")
  else
    summary+=("  ✗ $d (pytest exit $code)")
    rc=1
  fi
done

echo
echo "━━━ Summary ━━━"
printf '%s\n' "${summary[@]}"
[ $rc -eq 0 ] && echo "ALL GREEN" || echo "FAILURES ABOVE"
exit $rc
