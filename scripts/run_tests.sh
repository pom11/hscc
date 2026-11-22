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
#         HSCC_TEST_LEGS="hscc-api scripts"  run ONLY those legs (see the leg
#         selection block below for the full contract; unknown names exit 64
#         rather than running a subset silently).
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${HSCC_TEST_PY:-$HOME/.hermes/hermes-agent/venv/bin/python}"
[ -x "$PY" ] || PY="python3"

# ━━━ the sibling interpreter is THIN on purpose (t_02c98915) ━━━━━━━━━━━━━━━
# HSCC_TEST_PY exists so the suite can be run under a second CPython (the
# dual-interpreter stamp: hermes' own venv, then e.g. ~/miniconda3/envs/p313).
# That sibling is a THIN pytest env — pytest, and not hermes' runtime deps. It
# is thin by design: hermes resolves its own dependency generation at runtime
# (see the execv guard below, t_82fa20f5), so a hand-maintained second copy of
# those deps drifts, and drift shows up as wrong-green rather than red.
#
# Some tests therefore cannot run there AT ALL: they build a real
# ``hermes_state.SessionDB``, or drive ``hermes_cli.kanban_db`` / hermes' profile
# predicates through the production resolvers, and hermes' own imports need
# ruamel.yaml (hermes_yaml), which the thin env lacks. Those tests SKIP, with the
# import error itself as the reason, through ONE probe module per leg — one
# function per capability there, so two capabilities cannot drift to two
# postures (t_02c98915, and t_2cc3d482 for the second hscc-api capability):
#
#   hscc_daemon   tests/real_hermes.py   require_real_hermes /
#                                        real_predicate_or_skip
#   hscc-project  tests/test_session_discovery.py::_require_hermes_state,
#                 tests/test_mcp_server.py::_hermes_kanban_error
#   hscc-roles    tests/hermes_gap.py    require_hermes_cli_profiles (hermes'
#                                        own profile resolver — what
#                                        rolelib.native_profiles_dir resolves
#                                        through) / require_hermes_cli_config_writer
#                                        (``hermes_cli.config``: the round-trip
#                                        writer generator.py delegates to)
#   hscc-api      tests/hermes_gap.py    require_hermes_state (SessionDB) /
#                                        require_hermes_profile_library
#                                        (``hermes_cli.profiles``: resolver +
#                                        writer, test_profile_root.py)
#   hscc-cluster  tests/hermes_gap.py    require_hermes_kanban_board (the board
#                                        trio: schema + ``connect`` + dispatcher) /
#                                        require_hermes_cli_plugins (VALID_HOOKS +
#                                        invoke_hook) /
#                                        require_hermes_cli_plugin_dispatcher
#                                        (``plugins_dispatch``: hermes' own hook
#                                        binder) / require_hermes_cli_profiles
#                                        (the active-profile resolver) /
#                                        require_child_ran_hermes (the same board
#                                        capability asked of the CHILD a case
#                                        spawns — not of this process)
#
# Enforced, not asserted: one guard per leg that has a probe —
# hscc-api/tests/test_hermes_gap_single_posture.py,
# hscc-roles/tests/test_hermes_gap_single_posture.py and
# hscc-cluster/tests/test_hermes_gap_single_posture.py each sweep THEIR OWN leg for
# a private copy of a probe (the shape this block exists to prevent was measured
# in each of those legs) and fail if this roster and the probe modules disagree in
# either direction. THIS BLOCK is the live authority for which probes exist; the
# audit page below is the dated measurement of record.
#
# THE STAMP RULE. Under a sibling interpreter a leg is GREEN when the only
# non-passing lines are skips whose reason names the missing hermes import
# (`-rs`, below, is what makes them visible). So:
#   * a FAILED test in a sibling leg is a REGRESSION — or one of the gated
#     tests failing for a reason that is not "hermes not importable", which is
#     also a finding, not an environment artefact;
#   * a NEW skip that does not name that reason is a test that quietly stopped
#     running: make the same gate decision deliberately, do not let a bare
#     ``skip()`` become the default answer;
#   * never "fix" a red sibling leg by installing hermes' deps into the sibling
#     env. That is the option this repo considered and rejected; the measurement,
#     the 21-test roster and the reasoning are in
#     docs/audits/sibling-interpreter-hermes-gap_t_02c98915.md — a dated record
#     of why, with an amendment block naming what changed since it was measured
#     (two families added, one private posture folded in). Which probes exist
#     TODAY is the block above, not that page.
# Compare legs one-dir-per-process, exactly as this loop does — a merged-dir
# baseline counts cross-file isolation failures as environment failures and gives
# a wrong answer (see the header above, and
# hscc_daemon/tests/test_hermes_cli_import_isolation.py).
#
# What the sibling leg loses is stated plainly: it does not exercise
# hermes-integration code paths. It never did on a thin env — that test simply
# used to fail instead of saying so.

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

# ━━━ one fastembed model cache per host (t_317a0f30) ────────────────────────
# The memori SDK vendors the Rust `fastembed` crate, whose model cache defaults
# to the RELATIVE path `.fastembed_cache` — the cwd of whichever process loaded
# it. Measured on this host before the fix: nine such roots, 88,984 KB each
# (782 MB), one model (`Qdrant/all-MiniLM-L6-v2-onnx`, sha256-identical blobs),
# scattered over agent workspaces, `gateclerk` worktrees, `hermes-agent`, and
# the ROOT OF THIS REPO — that last one is a leg: `run_tests.sh` runs pytest
# with the repo root as cwd, so every full-suite run could mint another 87 MB
# here. The provider pins this itself (`memori_byodb/_cache_env.py`, the durable
# fix — see docs/audits/fastembed-cache-dir-t_317a0f30.md); the runner pins it
# too because a leg must not have to import the provider to keep 87 MB out of
# the tree it is testing, and legs reach the SDK directly (e.g. anything calling
# `memori.embed_texts`). `:-` keeps an operator's own value.
#
# Pinned by scripts/tests/test_run_tests_fastembed_cache.py, which extracts this
# exact block and runs it under bash against a tmp HOME.
if [ -n "${HOME:-}" ]; then
  export FASTEMBED_CACHE_DIR="${FASTEMBED_CACHE_DIR:-$HOME/.cache/fastembed}"
  mkdir -p "$FASTEMBED_CACHE_DIR" 2>/dev/null || true
fi

# ━━━ roster-wide HOME seal for every leg (t_2b2a6c3b) ━━━━━━━━━━━━━━━━━━━━━━━
# WHY. A leg that resolves a default path through ``~`` reads the OPERATOR's
# machine, passes locally, and is red (or silently skipped) on a CI runner whose
# HOME is empty. That class hid #593 and then produced this card's two measured
# deltas: `hscc-project` red (`UnknownProjectError: unknown project 'hscc'`,
# #593's resolver reached from a test) and `hscc-cluster`'s two source
# cross-checks turning into SKIPS whose reason named `<sealed-home>/sparkrun/…`.
# `hscc-roles` is sealed in its own conftest (PR #595, `ca2aa8d2`) — necessary but
# not sufficient: the seal belongs in the runner because ONLY the runner reaches
# every leg child (the same argument that puts the kanban-pin scrub and
# HSCC_SKIP_COMPACT_PROBE here rather than in a fixture), and because the other
# eleven legs have no seal of their own.
#
# WHY HERE IN THE FILE. Two lines above derive a value from HOME — `PY` from
# `${HSCC_TEST_PY:-$HOME/.hermes/…}` and the FASTEMBED_CACHE_DIR block — so the
# seal goes AFTER both and before any leg child is spawned: the runner keeps the
# values it needs, every leg and every child a leg spawns sees one HOME.
#
# WHY /tmp AND WHY IT IS REFUSED INSIDE THE LOGIN HOME. The empty home is created
# under `/tmp` explicitly, not `$TMPDIR` (macOS puts TMPDIR under /var/folders/…,
# and a second answer to "where does a CI-shaped home live" is how an earlier
# roster run lied: a HOME containing the token `/Users` keeps a
# `'/Users' in argv[2]`-style escape hatch green). A home inside the login home is
# refused outright — hermes resolves a home under `~/.hermes` back to the live
# root (t_d39c720e), so a "sealed" home there is not sealed and would read as one.
#
# WHY THE RUNNER PRINTS THE PATH. A roster log has to prove its own posture: the
# `sealed HOME=` line is what makes a `1164 passed, 5 skipped` line attributable
# to the seal rather than to whatever else the machine looked like.
#
# The seal is a posture, not a sandbox: `HSCC_TEST_NO_SEAL=1` restores the
# operator's HOME in one env var (a developer debugging a HOME-dependent case
# should not have to edit the runner), and the pin test runs both arms.
SUITE_REAL_HOME="${HOME:-}"
if [ "${HSCC_TEST_NO_SEAL:-0}" = "1" ]; then
  echo "HOME seal: DISABLED (HSCC_TEST_NO_SEAL=1) — legs run with the operator's HOME"
else
  SUITE_HOME="$(mktemp -d /tmp/hscc-suite-home-XXXXXX 2>/dev/null || true)"
  if [ -z "$SUITE_HOME" ] || [ ! -d "$SUITE_HOME" ]; then
    # Fail CLOSED: running the roster unsealed while claiming otherwise is the
    # worse outcome, so an inability to seal stops the run before leg 1.
    echo "run_tests.sh: cannot create the sealed HOME under /tmp — refusing to run the roster unsealed" >&2
    exit 3
  fi
  case "$SUITE_HOME" in
    "$SUITE_REAL_HOME"/*)
      echo "run_tests.sh: refusing a sealed HOME inside the login home: $SUITE_HOME" >&2
      exit 3 ;;
  esac
  export HOME="$SUITE_HOME"
  export USERPROFILE="$SUITE_HOME"   # pathlib/expanduser readers on a Windows host
  echo "HOME seal: HOME=USERPROFILE=$SUITE_HOME (empty dir under /tmp; HSCC_TEST_NO_SEAL=1 opts out)"
  # Declared, not discovered. `hscc-cluster`'s two source cross-checks compare the
  # staged media recipes against sparkrun SOURCE, which a deployed plugin dir
  # cannot import and CI does not have; their seam is `SPARKRUN_SRC`
  # (t_2b2a6c3b, PR #604). Declaring the machine's own checkout keeps that
  # coverage running under the seal instead of retiring it as a skip — and the
  # runner exports ONLY that path, never the operator's home, so the seal stays a
  # seal. An operator's own `SPARKRUN_SRC` always wins (``${VAR:-}`` test below).
  if [ -z "${SPARKRUN_SRC:-}" ] && [ -n "$SUITE_REAL_HOME" ] &&
     [ -d "$SUITE_REAL_HOME/sparkrun/src/sparkrun" ]; then
    export SPARKRUN_SRC="$SUITE_REAL_HOME/sparkrun/src/sparkrun"
    echo "HOME seal: declared SPARKRUN_SRC=$SPARKRUN_SRC (this machine's sparkrun checkout)"
  fi
  # The Hermes AGENT SOURCE tree, same rule and same reasoning as SPARKRUN_SRC above.
  # Cases that consult the live `hermes_cli` / kanban board resolve it from
  # `~/.hermes/hermes-agent` by default, and their own skip reason says "Set
  # HERMES_AGENT_PATH if it lives elsewhere" — the seam already exists and is honoured in
  # production (hscc-roles/hscc.py:398, hscc-cluster/workflow.py:594,
  # hscc_daemon/autodown.py:875). What a seal removes is the DEFAULT, not the capability,
  # so the runner declares the machine's tree. Measured at 96a9f38a without this line:
  # `hscc-project/tests/test_mcp_server.py:176` (standup's watermark read) and three
  # `hscc_daemon` cases retire as skips under the seal. The path, never the home.
  if [ -z "${HERMES_AGENT_PATH:-}" ] && [ -n "$SUITE_REAL_HOME" ]; then
    for _agent_candidate in "$SUITE_REAL_HOME/.hermes/hermes-agent" "$SUITE_REAL_HOME/src/hermes-agent"; do
      if [ -d "$_agent_candidate/hermes_cli" ]; then
        export HERMES_AGENT_PATH="$_agent_candidate"
        echo "HOME seal: declared HERMES_AGENT_PATH=$HERMES_AGENT_PATH (this machine's Hermes agent source)"
        break
      fi
    done
  fi
  # The homebrew TAP checkout, third instance of the same rule. `scripts`'
  # formula-drift guard reads it through `bump_formula.py`'s documented seam
  # (`--tap`, else $HSCC_TAP_DIR, else ~/dev/homebrew-hscc — scripts/bump_formula.py:529),
  # so under a seal it loses its default and FOUR cases retire: measured at 96a9f38a,
  # `HSCC_TEST_LEGS=scripts` went 601 passed / 5 skipped -> 598 passed / 8 skipped with
  # the reason naming the two candidates it looked at and "Set HSCC_TAP_DIR to a
  # pom11/homebrew-hscc checkout to arm this guard". Declaring the machine's own tap
  # re-arms the guard; a host without one still skips honestly (and the guard does not
  # clone — a repo test that needs the network to satisfy itself is not a guard).
  if [ -z "${HSCC_TAP_DIR:-}" ] && [ -n "$SUITE_REAL_HOME" ]; then
    for _tap_candidate in "$SUITE_REAL_HOME/dev/homebrew-hscc" "$SUITE_REAL_HOME/.cache/hscc/taps/homebrew-hscc"; do
      if [ -f "$_tap_candidate/Formula/hscc.rb" ]; then
        export HSCC_TAP_DIR="$_tap_candidate"
        echo "HOME seal: declared HSCC_TAP_DIR=$HSCC_TAP_DIR (this machine's tap checkout)"
        break
      fi
    done
  fi
fi

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

# ━━━ leg selection — HSCC_TEST_LEGS (t_23bf61f3) ━───────────────────────────
# WHY THIS EXISTS. The roster above is the repo's only test gate, and until this
# block it had NO selection mechanism at all: extra argv goes to pytest (see the
# Usage line), so `run_tests.sh hscc-api` ran TWELVE legs and handed "hscc-api"
# to pytest as a path argument. That is why CI could not carry the gate: a job
# that must run the whole 12-leg roster on every push is ~25 min on a loaded
# host and makes every flake of an unrelated leg a merge blocker, so the honest
# options were "slow" or "not run", and the repo picked "not run" by omission.
#
# CONTRACT (pinned by scripts/tests/test_run_tests_leg_selection.py):
#   HSCC_TEST_LEGS unset or empty  -> every leg in DIRS, exactly as before.
#   HSCC_TEST_LEGS="a b" or "a,b"  -> ONLY those legs, run in DIRS order,
#                                     duplicates collapsed.
#   a name that is not in DIRS     -> exit 64 BEFORE any leg runs. Selection is
#                                     fail-CLOSED on purpose: a typo'd leg name
#                                     that silently ran nothing would report
#                                     green, and a green report from a job that
#                                     executed zero tests is the exact failure
#                                     mode this whole gate exists to close. Same
#                                     rule for an empty selection.
#   the selection is always printed, so a reader of a redirected log can tell
#     "3 legs, selected" from "12 legs, full suite" without re-running anything
#     (the same reasoning that put `-rs` on the pytest invocation).
#
# Nothing here decides WHICH legs a change should run — that map lives in
# .github/scripts/leg_select.py and is a CI concern. This block only makes
# "run exactly these legs" expressible at all.
SELECTED_LEGS="${HSCC_TEST_LEGS:-}"
SELECTED_LEGS="${SELECTED_LEGS//,/ }"
# Collapse whitespace before anything else: `set -f` so a stray glob char in the
# value cannot pathname-expand into whatever happens to be in the cwd, and a
# whitespace-only value has to read as "unset" (full suite) rather than as a
# zero-leg run. Both shapes are one `join` away in a workflow that built the
# string from a list.
set -f
_NORM=""
for _tok in $SELECTED_LEGS; do _norm="$_tok"; _NORM="${_NORM:+$_NORM }$_norm"; done
SELECTED_LEGS="$_NORM"
set +f
unset _NORM _norm _tok
declare -a LEGS
if [ -z "$SELECTED_LEGS" ]; then
  LEGS=("${DIRS[@]}")
else
  for _want in $SELECTED_LEGS; do
    _known=0
    for _d in "${DIRS[@]}"; do
      if [ "$_d" = "$_want" ]; then _known=1; break; fi
    done
    if [ "$_known" -eq 0 ]; then
      echo "run_tests.sh: HSCC_TEST_LEGS names an unknown leg: '$_want'" >&2
      echo "  valid legs: ${DIRS[*]}" >&2
      exit 64
    fi
  done
  LEGS=()
  for _d in "${DIRS[@]}"; do
    for _want in $SELECTED_LEGS; do
      if [ "$_d" = "$_want" ]; then LEGS+=("$_d"); break; fi
    done
  done
  if [ "${#LEGS[@]}" -eq 0 ]; then
    echo "run_tests.sh: HSCC_TEST_LEGS='$HSCC_TEST_LEGS' selected zero legs" >&2
    echo "  valid legs: ${DIRS[*]}" >&2
    exit 64
  fi
fi
unset _want _known _d

if [ "${#LEGS[@]}" -eq "${#DIRS[@]}" ]; then
  echo "legs: FULL SUITE (${#LEGS[@]} legs)"
else
  echo "legs: SELECTED ${#LEGS[@]}/${#DIRS[@]} -> ${LEGS[*]}"
  echo "      (HSCC_TEST_LEGS='$SELECTED_LEGS' — this is NOT the full suite)"
fi

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
#
# HSCC_REGISTRY joins the list, and it is a DIFFERENT kind of pin from the five
# above: none of the leg conftests re-pins it (the five above are), so the runner
# is the only place a suite-wide scrub can also reach the leg children —
# monkeypatch cannot reach children, and hermes_gap's _child_env drops it only for
# the gated cases' own children. It is read AT CALL TIME by two resolvers
# (orchestrators._registry_path, main_orch._registry_projects), so an export from
# provisioning outranks the documented default in any process, including one that
# imported first. PR #593 (t_4b70e8d4) was exactly that shape: the operator's live
# ~/.flightdeck/registry.yaml answered a case on a workstation, nothing answered it
# on the runner, and the leg was red only there. Verified nothing needs it armed:
# the three suite files that reference it — both bootstrap main_orch files and the
# whole hscc-roles leg — are green with a value armed AND with it scrubbed. The
# same consequence rule applies: a leg that reddens here was reading the operator's
# registry.
SUITE_ENV_UNSET=(
  HERMES_DELEGATED_CHILD_CONTEXT   # the read fence (in-loop comment)
  HERMES_KANBAN_DB                 # the board location pins (block above)
  HERMES_KANBAN_BOARD
  HERMES_KANBAN_WORKSPACES_ROOT
  HERMES_KANBAN_ATTACHMENTS_ROOT
  HSCC_REGISTRY                    # the registry location pin (block above)
)
ENV_UNSET_ARGS=()
for _pin in "${SUITE_ENV_UNSET[@]}"; do
  ENV_UNSET_ARGS+=(-u "$_pin")
done
unset _pin

rc=0
declare -a summary
for d in "${LEGS[@]}"; do
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
