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

# ━━━ no mid-run interpreter swap (t_82fa20f5) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# A pytest process must never replace its own image mid-suite. It can: a
# deferred import chain inside hermes_state's write path
# (append_messages_batch -> _display_dedupe_key -> context_compressor
# ._is_synthetic_compression_user_turn -> `from agent.conversation_loop import`
# -> agent.process_bootstrap -> hermes_bootstrap) reaches hermes_bootstrap's
# MODULE-LEVEL launch preparation, which calls
# `os.execv(selected_python, relaunch_command(...))` (hermes_bootstrap.py:599)
# when the running interpreter is not the selected dependency generation.
# execv discards the running session: no summary, no FAILED lines, no traceback
# on either stream — just rc=1. Measured on this host at origin/main 882b40cf:
# `pytest hscc-api/tests` died inside test_gateway_backfill.py::
# test_backfill_seeds_full_history with 0 bytes on stdout AND stderr, and
# `pytest hscc-bootstrap/tests` died at its 1161st of 1164 tests inside
# test_video_gen_openai_compat_e2e.py — the two "legs that never finish".
# `prepare_launch` honours HERMES_DISABLE_LAZY_INSTALLS (venv_sync.py:456) and
# returns None, so the guard below removes the hazard instead of hiding it: if
# the selected generation really is stale, an import now fails loudly with an
# ImportError naming the package, which is what a test harness should report.
export HERMES_DISABLE_LAZY_INSTALLS="${HERMES_DISABLE_LAZY_INSTALLS:-1}"

# ── no per-test 5 s compaction probe (t_ecb84764) ───────────────────────────
# enable_plugins.enable() ends with a REAL `urlopen(timeout=5)` sanity-probe of
# the fleet compaction endpoint. That is right for bootstrap on a real machine.
# It is not right for a test suite: hscc-bootstrap/tests/conftest.py pins
# HSCC_SERVING_JSON at a missing file for determinism, which selects the
# FALLBACK COMPACT_URL, and that address is unreachable rather than refused
# (refused returns instantly, unreachable burns the whole timeout). So every
# test calling enable() paid 5.04 s of dead wall time — 66 cases = 333 s, 48 %
# of the 700 s hscc-bootstrap leg — and tests that shell out to the real
# bootstrap.sh paid it again per stage. Full measurement: changelog.d entry.
# Set in the RUNNER (not just an autouse fixture) because the children need it
# too: a monkeypatch only reaches this process, and half the cost is in
# subprocess-spawning tests. Read per call in _skip_compaction_probe(); the
# operator path still probes, because nothing here exports it outside the suite.
export HSCC_SKIP_COMPACT_PROBE="${HSCC_SKIP_COMPACT_PROBE:-1}"

# ── leg heartbeat: make "slow" readable as distinct from "stopped" ──────────
# Every leg's consumer reads a REDIRECTED file, and pytest's -q band carries no
# elapsed time anywhere, while its [ NN%] is a test-COUNT percent printed once
# per 72-dot line — on a leg whose wall time is 48 % one file, "43 %" tells a
# dispatcher nothing about whether to keep waiting. scripts/leg_heartbeat.sh
# carries the full reason (including why PYTHONUNBUFFERED and -rA were measured
# and rejected); this runner only sets the cadence and starts the watcher.
# 30 s costs ~24 lines per full suite. 0 disables.
HEARTBEAT_SECONDS="${HSCC_LEG_HEARTBEAT_SECONDS:-30}"

# `scripts` is the one entry that is NOT a deployed plugin (it is absent from
# install_payload.DEFAULT_PAYLOAD): it is the repo's own tooling dir, and
# scripts/tests holds the suites for scripts/address_guard.py,
# changelog_fragments.py and dep_pr_watcher.py. It was added because those tests
# were unreachable from the canonical runner, so every `ALL GREEN` stamp silently
# excluded the commit-time address guard's own suite (t_95d864fc). The isolation
# property comes from the loop — one pytest process per entry — not from the kind
# of dir, so a non-plugin dir needs no special leg. It must stay registered here
# for the same reason the plugins do: scripts/tests does `sys.path.insert` of
# scripts/ and imports `changelog_fragments` / `dep_pr_watcher` bare.
# `.github/scripts` is registered for the same reason and by the same argument
# (t_9a4b7687): it is CI tooling, not a deployed plugin, and its tests are the
# only thing that executes the address-guard workflow's real step script.
DIRS=(hscc-bootstrap hscc-commands hscc-roles hscc-cluster hscc-project hscc_daemon sparkrun-hermes hscc-api memori_byodb scripts .github/scripts comfyui-sparkrun)

# ━━━ SIGTERM forensics (t_6bb29d46) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RULE: NEVER stop a suite with a name-based sweep — `pkill -f "run_tests.sh"`,
# `pkill -f pytest`, or any pattern-kill of a generic script name. It matches
# by argv substring and kills OTHER CARDS' concurrent suite legs on this host,
# not just yours (two legs died this way on 2026-10-06; a third rc=143 abort
# that day was a harness-mediated process.kill). To stop your own
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

# ━━━ no worker/dispatcher Kanban pin may reach a leg (t_7e143e04) ━──────────
# Every leg runs with the vars in SUITE_ENV_UNSET REMOVED from its environment.
# The marker was already dropped (see the in-loop comment); the four board pins
# were not, and they are the worse half of the same leak.
#
# WHY THE PINS MATTER. The dispatcher injects them into every worker it spawns
# (hermes_cli/kanban_db_dispatch.py:2811-2851) so the worker's kanban paths keep
# matching after `hermes -p` rewrites HERMES_HOME. hermes then resolves them
# AHEAD of caller intent: `_board_path()` (hermes_cli/kanban_db.py:540-560)
# returns the env pin for ANY board whenever `_explicit_board_intent_pinned()`
# holds, and that predicate (agent/delegation_context.py:76-93) holds for every
# dispatched worker — i.e. for the shell that runs this script. So a test that
# asks hermes' REAL resolver for a throwaway board (rather than injecting a fake
# path) gets the operator's live board, and inside a fence it cannot even see
# the difference.
#
# MEASURED in a dispatched worker shell at 5c718718, before this change:
#   kanban_db_path(board="fixture-board")  -> <kanban-home>/kanban/boards/<live-board>/kanban.db
#   workspaces_root(board="fixture-board") -> <kanban-home>/kanban/boards/<live-board>/workspaces
# The leak also survives the per-suite defence, so "the fixtures already handle
# it" is not true: hscc_daemon/tests/conftest.py:482-500 pins
# HERMES_KANBAN_HOME *and* deletes HERMES_KANBAN_DB, and `workspaces_root()`
# STILL returned the live root — `_board_path` checks the env pin BEFORE any
# root resolution, so HERMES_KANBAN_HOME cannot neutralise it. (BOARD is the one
# the fixture root does neutralise: `get_current_board()` requires
# `board_exists()` under the pinned home, so a stray slug falls through to
# `default`. It is scrubbed anyway — a leg must not depend on that accident.)
#
# WHY HERE AND NOT IN A FIXTURE: same reason as HSCC_SKIP_COMPACT_PROBE above —
# monkeypatch does not reach child processes, and a large share of these tests
# spawn python/`hermes` children that resolve their own board. A runner-level
# scrub covers the parent and every child it spawns.
#
# THE LIST IS MEASURED, NOT GUESSED. The dispatcher writes DB (2847),
# WORKSPACES_ROOT (2848) and BOARD (2851). ATTACHMENTS_ROOT is NOT
# dispatcher-injected — grep shows it only ever read (hermes_cli/kanban_db.py:575-578,
# gateway/platforms/base.py:836) — and it is scrubbed anyway: it is the same kind
# of location pin, HOME cannot neutralise it either, and `env -u` on a var that
# is not set costs nothing. scripts/tests/test_run_tests_harness_env.py pins
# this list and fails if a future dispatcher pin is added without a decision.
#
# DELIBERATELY NOT SCRUBBED: the worker IDENTITY vars (HERMES_KANBAN_TASK,
# _RUN_ID, _CLAIM_LOCK, _GOAL_MODE, _GOAL_MAX_TURNS, _WORKSPACE, _BRANCH). They
# name no board location, and the suite legitimately goes BOTH ways about them —
# hscc-cluster/tests/test_budget_watch.py:46 *sets* TASK to get worker
# behaviour, five cli-help suites *strip* it to get a clean subprocess env. A
# runner-level scrub would silently flip every worker-mode branch to its
# non-worker side, i.e. manufacture tests that pass by no longer exercising the
# thing they claim to test. That failure mode is worse than the one fixed here.
# The board pins have no such defence: no test can legitimately want the
# dispatching worker's live board.
#
# CONSEQUENCE, per the rule in the in-loop comment: a leg that newly FAILS with
# these removed was passing while reading the operator's board. That is a
# harness bug and a finding to report — not something to paper over by putting
# the pin back.
SUITE_ENV_UNSET=(
  HERMES_DELEGATED_CHILD_CONTEXT   # the read fence (in-loop comment)
  HERMES_KANBAN_DB                 # the board location pins (block above)
  HERMES_KANBAN_BOARD
  HERMES_KANBAN_WORKSPACES_ROOT
  HERMES_KANBAN_ATTACHMENTS_ROOT
)
ENV_UNSET_ARGS=()
for _pin in "${SUITE_ENV_UNSET[@]}"; do
  ENV_UNSET_ARGS+=(-u "$_pin")
done
unset _pin

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
  #
  # The same reasoning covers the BOARD PINS the dispatcher also injects into
  # this shell (HERMES_KANBAN_DB / _BOARD / _WORKSPACES_ROOT /
  # _ATTACHMENTS_ROOT), which outrank an explicit board= inside the fence — see
  # the SUITE_ENV_UNSET block above for the measurement, the list's provenance,
  # and why the worker IDENTITY vars are deliberately left in place (t_7e143e04).
  #
  # `-rs` prints every skip REASON, not just the count (GAP-D-1, t_b82ad0fe):
  # the httpx-keg guard used to hide a blind keg venv behind a soft-skip that
  # pytest could not report at all, and audit D read the leg as green. Once a
  # leg genuinely skips, the next reader has to be able to see WHY from the
  # canonical runner's output without re-running anything — a bare "N skipped"
  # does not tell an operator whether the guard ran or never got a tap.
  # Heartbeat per leg (see the HSCC_LEG_HEARTBEAT_SECONDS block above for the
  # why). Started BEFORE pytest and reaped right after, so its elapsed clock is
  # the leg's own, and the runner's $$ is passed rather than a pytest pid so
  # pytest stays in the foreground (a backgrounded job in a non-interactive
  # shell gets SIG_IGN on SIGINT, which would break Ctrl-C for everyone).
  LEG_T0="$(date +%s)"
  HB_PID=""
  if [ "${HEARTBEAT_SECONDS:-0}" -gt 0 ] 2>/dev/null; then
    bash "$ROOT/scripts/leg_heartbeat.sh" "$d" "$$" "$LEG_T0" "$HEARTBEAT_SECONDS" &
    HB_PID=$!
  fi
  env "${ENV_UNSET_ARGS[@]}" \
    "$PY" -m pytest -q -rs "$ROOT/$d/tests" -p no:cacheprovider "$@"
  code=$?
  [ -n "$HB_PID" ] && kill "$HB_PID" 2>/dev/null
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
