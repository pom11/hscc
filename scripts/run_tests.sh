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
#         HSCC_TEST_PARALLEL=N  run N legs at a time. UNSET or 1 = the SERIAL
#         path, byte-identical to the pre-concurrency runner; N>=2 packs. See
#         the bounded leg concurrency block below for why the default is 1.
#         HSCC_TEST_LEGS="loadstress"  the STRESS leg: re-runs every case
#         pytest.ini marks `loadstress` N repeats deep under bounded CPU
#         pressure owned by scripts/load_burners.sh (scripts/loadstress_leg.py
#         owns the rest). It is selectable and never part of the default roster,
#         and every roster leg above deselects those cases with
#         `-m "not loadstress"` — so a normal gate never pays for the repeats,
#         and nothing that only fails under load is left to the queue gate to
#         discover. Measured load, budgets and how to raise the pressure:
#         docs/testing/loadstress-leg.md.
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
# WHY THE ROOT IS DECIDED BEFORE mktemp (t_9c1ff8e1). On a host whose login home IS
# `/tmp` — the Linux/CI default shape, and what `HSCC_TEST_LEGS=<bogus>` met on #606's
# own CI — the two rules above are mutually unsatisfiable: every dir `mktemp` can make
# under `/tmp` is inside the login home, so the refusal fired rc 3 BEFORE leg selection
# and the roster could not run at all, nor reject a typo'd leg with its contracted 64.
# So when `/tmp` is inside the login home the root becomes `/var/tmp` — the other
# literal, still never `$TMPDIR`, still subject to the same refusal (a host that
# contains BOTH has nowhere to seal and still gets rc 3). The decision reads ONLY
# `$SUITE_REAL_HOME`, and never what `mktemp` answered: a fallback triggered by the
# refusal would swallow the refusal itself, and the guard below would stop being a
# guard (pinned both ways in scripts/tests/test_run_tests_home_seal.py).
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
  # The root is decided from the LOGIN HOME alone, before mktemp runs — the two
  # literals below are the whole answer, never `$TMPDIR` and never "whatever mktemp
  # answered" (see WHY THE ROOT IS DECIDED BEFORE mktemp above). The literals stay
  # spelled out per branch rather than folded into one `$SUITE_HOME_ROOT/template`
  # argument on purpose: `mktemp -d /tmp/hscc-suite-home-XXXXXX` is the string
  # scripts/tests/test_run_tests_home_seal.py pins, and a rewrite that hides it makes
  # that guard parse a shape it no longer checks.
  SUITE_HOME_ROOT="/tmp"
  case "/tmp" in
    "$SUITE_REAL_HOME"|"$SUITE_REAL_HOME"/*)
      SUITE_HOME_ROOT="/var/tmp"
      echo "HOME seal: /tmp is inside the login home ($SUITE_REAL_HOME) — sealing under /var/tmp instead" ;;
  esac
  if [ "$SUITE_HOME_ROOT" = "/var/tmp" ]; then
    SUITE_HOME="$(mktemp -d /var/tmp/hscc-suite-home-XXXXXX 2>/dev/null || true)"
  else
    # Set here, not only above: the branch that runs THIS mktemp is the branch that
    # owns the root the banner names, so no third value can leave the log naming a
    # root the path does not live under.
    SUITE_HOME_ROOT="/tmp"
    SUITE_HOME="$(mktemp -d /tmp/hscc-suite-home-XXXXXX 2>/dev/null || true)"
  fi
  if [ -z "$SUITE_HOME" ] || [ ! -d "$SUITE_HOME" ]; then
    # Fail CLOSED: running the roster unsealed while claiming otherwise is the
    # worse outcome, so an inability to seal stops the run before leg 1.
    echo "run_tests.sh: cannot create the sealed HOME under $SUITE_HOME_ROOT — refusing to run the roster unsealed" >&2
    exit 3
  fi
  case "$SUITE_HOME" in
    "$SUITE_REAL_HOME"/*)
      echo "run_tests.sh: refusing a sealed HOME inside the login home: $SUITE_HOME" >&2
      exit 3 ;;
  esac
  export HOME="$SUITE_HOME"
  export USERPROFILE="$SUITE_HOME"   # pathlib/expanduser readers on a Windows host
  echo "HOME seal: HOME=USERPROFILE=$SUITE_HOME (empty dir under $SUITE_HOME_ROOT; HSCC_TEST_NO_SEAL=1 opts out)"
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
# ── selectable-only legs ────────────────────────────────────────────────────
# `loadstress` is a leg this runner can run and the default roster must never
# run: it re-runs the cases pytest.ini marks `loadstress` under bounded CPU
# pressure, which is the one thing no ordinary gate should pay for. It is NOT in
# DIRS for that reason (DIRS *is* the default roster), and it is declared here
# rather than beside DIRS because scripts/tests/test_run_tests_leg_selection.py
# executes THIS block under `set -u` against a stub roster — a name defined
# outside the block is unbound there, and a stress leg nobody can name by
# accident is a stress leg that rots.
OPTIONAL_LEGS="loadstress"
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
      # A selectable-only leg is known; it just is not in the default roster.
      case " $OPTIONAL_LEGS " in
        *" $_want "*) _known=1 ;;
      esac
    fi
    if [ "$_known" -eq 0 ]; then
      echo "run_tests.sh: HSCC_TEST_LEGS names an unknown leg: '$_want'" >&2
      echo "  valid legs: ${DIRS[*]} $OPTIONAL_LEGS" >&2
      exit 64
    fi
  done
  LEGS=()
  for _d in "${DIRS[@]}"; do
    for _want in $SELECTED_LEGS; do
      if [ "$_d" = "$_want" ]; then LEGS+=("$_d"); break; fi
    done
  done
  # Optional legs after the roster ones, so a run that asks for both has a
  # deterministic order regardless of how the caller listed them. The empty
  # check is deliberately AFTER this: `HSCC_TEST_LEGS=loadstress` selects one
  # leg that DIRS cannot ever produce.
  for _want in $OPTIONAL_LEGS; do
    for _sel in $SELECTED_LEGS; do
      if [ "$_sel" = "$_want" ]; then LEGS+=("$_want"); break; fi
    done
  done
  if [ "${#LEGS[@]}" -eq 0 ]; then
    echo "run_tests.sh: HSCC_TEST_LEGS='$HSCC_TEST_LEGS' selected zero legs" >&2
    echo "  valid legs: ${DIRS[*]} $OPTIONAL_LEGS" >&2
    exit 64
  fi
fi
unset _want _known _d _sel

# "FULL SUITE" means exactly one thing: nobody selected anything. A selection
# that happens to name all twelve roster legs is still a selection, and a log
# reader who cannot tell the two apart cannot tell a partial gate from a whole
# one (the same reason the count is printed as n/N).
if [ -z "$SELECTED_LEGS" ] && [ "${#LEGS[@]}" -eq "${#DIRS[@]}" ]; then
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
  # With a bounded pack (t_6c2123f9) a kill that only stops the shell leaves the
  # legs in flight: they would keep burning CPU on a host running the fleet
  # control plane and keep the log's write end open. Kill the ones we started,
  # by pid, in the range the cursors describe — never by name (the RULE above).
  # `set -m` in the pack gives every leg its own process group, so the group kill
  # reaches the pytest AND the children IT spawned (a leg that shells out to
  # bootstrap.sh leaves a grandchild behind otherwise).
  if [ "${PARALLEL_MODE:-0}" -eq 1 ] 2>/dev/null; then
    _k="${P_HEAD:-0}"
    while [ "$_k" -lt "${P_NEXT:-0}" ]; do
      kill -- "-${P_PID[$_k]}" 2>/dev/null
      kill "${P_PID[$_k]}" 2>/dev/null
      _k=$((_k+1))
    done
  fi
  exit 143
}
on_sigint() {
  # A pack puts each leg in its OWN process group (set -m), so a Ctrl-C at the
  # terminal no longer reaches pytest the way it does on the serial path. The
  # trap restores what the serial path gives for free.
  echo
  echo "!! run_tests.sh RECEIVED SIGINT at $(date '+%Y-%m-%d %H:%M:%S') — elapsed ${SECONDS}s, during suite: ${CURRENT_SUITE:-unknown}"
  if [ "${PARALLEL_MODE:-0}" -eq 1 ] 2>/dev/null; then
    _k="${P_HEAD:-0}"
    while [ "$_k" -lt "${P_NEXT:-0}" ]; do
      kill -- "-${P_PID[$_k]}" 2>/dev/null
      _k=$((_k+1))
    done
  fi
  exit 130
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

# ━━━ bounded leg concurrency (t_6c2123f9) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# WHY THIS EXISTS. A leg is one pytest process over one directory, and the box
# spends the whole gate with ONE of them running: a full roster is 1323 s of wall
# time during which the Mac Studio sits at load 2-3 of 14
# (docs/audits/merge-queue-gate-leg-timings_t_fdc2bff0.md §1, §5). Legs are
# independent BY CONSTRUCTION — the entire reason this runner exists is that the
# twelve dirs must never share a pytest process — so the idle cores are a pure
# loss. Measured through this same runner at the gate's own priority
# (`taskpolicy -b`, `ps -o pri` 4): five legs serial 188 s vs three-deep 96 s.
#
# WHY THE DEFAULT IS 1 AND NOT 2 — THE MEASUREMENT THAT DECIDED IT, AND THE
# MEASUREMENT THAT UNBLOCKED MOVING IT. The card set the bar at "a full-roster
# concurrent run must be green before the default goes above 1", so the roster was
# run at p=2. The first run was NOT green: 12 legs, 3 red — `hscc-api` (an ERROR at
# setup in `test_gateway_serve_watchdog.py:637`: `harness identity check broken on
# darwin: our own live parent pid 3578 did not match its own argv marker;
# _state_and_command answered ('Z', '<defunct>')`), plus `scripts` (1 failed) and
# `.github/scripts`. All three were then root-caused, and NONE of them was the
# mechanism this block used to blame: the zombie was the watchdog test's OWN child
# (the sandbox parent its `stop` mode instructs to exit, which no runner tick can
# reap — it is the pytest process's own child), the `.github/scripts` leg never ran
# at all because its log path spliced the leg NAME (a path) into a FILENAME, and
# `scripts` came back green. Both mechanisms this block used to name as the lead —
# deferred reaping and the pack's own process groups — are disproven for all three;
# `docs/audits/packed-roster-reds_t_4fc35c1f.md` carries the before/after numbers
# (0 red in 25 at ordinary priority, 8 red in 30 under bounded burners, 0/30 after
# the fixture fix — with HSCC_TEST_PARALLEL unset throughout, so no pack involved).
#
# THE BAR IS NOW MET: the full twelve-leg roster is green at p=2, 12/12, 713 s wall
# against 1396 s of summed leg work = **1.96x** (t_4fc35c1f, this host, one clean
# run). What keeps the default at 1 is no longer a red roster — it is that flipping
# it is a CONTRACT change, not a green question. The serial stream is what two
# readers were measured against (below), `test_the_default_is_the_serial_stream_and_
# prints_nothing_new` pins unset as silent and byte-identical to the pre-concurrency
# runner, and the narrowed `hscc_daemon scripts` gate has never been TIMED packed.
# The flip needs those re-pinned and that number measured; it is one small slice,
# unblocked, and deliberately not done half-verified here.
#
# CONTRACT (pinned by scripts/tests/test_run_tests_leg_parallelism.py):
#   HSCC_TEST_PARALLEL unset or empty -> the SERIAL loop below, UNTOUCHED: no
#       extra line, no new tmp dir, no job control. Two readers anchor on this
#       stream rather than reading it — the SIGTERM forensics'
#       `during suite: $CURRENT_SUITE` (t_6bb29d46) and merge_queue's
#       `^✗ <leg>` / `^FAILED <path>::<test>` digest anchors — and both were
#       measured against it.
#   HSCC_TEST_PARALLEL=1 (or 0)       -> the same serial loop, with one line
#       saying so.
#   HSCC_TEST_PARALLEL=N (N>=2)       -> at most N legs in flight, launched in
#       roster order, reaped as they FINISH (a slot is recycled the moment its
#       leg ends — waiting for the oldest instead measured 1.0x, i.e. nothing).
#   anything else (non-numeric, negative) -> SERIAL, with a printed note: a
#       workflow that builds the value from a list must degrade to the posture it
#       has always had, not to an unknown one.
#
# WHY THE PACK CAP IS MODEST WHEN IT IS USED. This host also runs the fleet
# control plane (the daemon, the gateway, the kanban dispatcher). Two is enough
# to remove the serial queueing for the gate the queue actually runs.
#
# WHY EACH LEG GETS ITS OWN FILE. With N legs writing to one stdout, their dot
# bands interleave and the two ANCHORED reads above break: `FAILED path::test -
# reason` only matches merge_queue._TEST_FAILURE_RE at LINE START, and a leg's
# short test summary would be split by another leg's progress dots. So a leg
# writes to its own file under a tmp dir, and the files are concatenated in
# ROSTER ORDER at the end: every block keeps the exact shape the serial runner
# produced (`━━━ <leg> ━━━`, its pytest output, then the `━━━ Summary ━━━`
# block), just with the blocks emitted late. `test_red_leg_digest_matches_the_
# serial_shape` asserts that against the REAL merge_queue regexes, not a copy.
#
# WHAT A LIVE READER SEES INSTEAD (the trade, stated, not hidden). A redirected
# log gets no pytest bytes until a leg finishes. That is why the two live signals
# stay in the foreground stream: the heartbeat (one watcher, one line per LIVE
# leg — see scripts/leg_heartbeat.sh) and one `parallel: done leg=… elapsed=…s
# rc=… pack=…s` line the moment each leg is reaped. Together they still answer
# the only question the heartbeat was written for — slow, or stopped.
PAR_RAW="${HSCC_TEST_PARALLEL:-}"
PAR=1
case "$PAR_RAW" in
  '') ;;                                    # unset/empty -> serial, silently
  *[!0-9]*)
    echo "parallel: HSCC_TEST_PARALLEL='$PAR_RAW' is not a non-negative integer — running SERIALLY"
    ;;
  *)
    if [ "$PAR_RAW" -lt 2 ]; then
      echo "parallel: HSCC_TEST_PARALLEL=$PAR_RAW — running SERIALLY (the pre-concurrency path)"
    else
      PAR="$PAR_RAW"
    fi
    ;;
esac
# A pack wider than the roster is the same run; clamping keeps the launch loop
# honest (it never waits for a leg it did not start).
if [ "$PAR" -gt "${#LEGS[@]}" ]; then PAR="${#LEGS[@]}"; fi
[ "$PAR" -lt 1 ] && PAR=1

PARALLEL_MODE=0
LEG_TMP=""
LEG_PIDFILE=""
# `loadstress` is a leg whose entire result depends on how much of the box the
# rest of the box is using: scripts/load_burners.sh applies BOUNDED CPU pressure
# and the leg's meaning is "these cases survive that". A concurrent pack is
# unbounded extra pressure from an unpredictable source, so a stress result taken
# inside one measures the pack, not the code. Serial whenever it is selected.
if [ "$PAR" -ge 2 ]; then
  for _chk in "${LEGS[@]}"; do
    if [ "$_chk" = "loadstress" ]; then
      PAR=1
      echo "parallel: the loadstress leg needs the box to itself — running SERIALLY"
      break
    fi
  done
  unset _chk
fi
if [ "$PAR" -ge 2 ] && [ "${#LEGS[@]}" -ge 2 ]; then
  # Never $TMPDIR (same reasoning as the sealed HOME above: a second answer to
  # "where does suite scratch live" is how a run stops being reproducible), and
  # never inside the sealed HOME. A tmp dir it cannot make is a reason to run
  # serially, not a reason to fail a gate.
  #
  # `HSCC_LEG_TMP_ROOT` moves the ROOT, never the mechanism: the default is the
  # literal `/tmp` and `$TMPDIR` is still not consulted (guarded by
  # test_the_default_leg_root_stays_tmp_even_with_a_tmpdir). The seam exists
  # because `/tmp/hscc-legs-XXXXXX` is a HOST-GLOBAL namespace and a gate no
  # longer has the box to itself — since #800/#801 two packed runs overlap on
  # one host (a queue gate plus CI-overlap, a gate plus a worker's suite), and
  # each creates and `rm -rf`s a dir there during the other's window. While the
  # root was unmovable, no test could tell "this run leaked" from "a sibling run
  # existed" — that is what red-gated PR #786's gate on 2026-11-25. Nothing in
  # production sets it; it is a test seam.
  LEG_TMP_ROOT="${HSCC_LEG_TMP_ROOT:-/tmp}"
  case "$LEG_TMP_ROOT" in
    /*) ;;
    *) echo "parallel: HSCC_LEG_TMP_ROOT='$LEG_TMP_ROOT' is not an absolute path — using /tmp"
       LEG_TMP_ROOT=/tmp ;;
  esac
  LEG_TMP="$(mktemp -d "$LEG_TMP_ROOT/hscc-legs-XXXXXX" 2>/dev/null || true)"
  if [ -n "$LEG_TMP" ] && [ -d "$LEG_TMP" ]; then
    LEG_PIDFILE="$LEG_TMP/legs.tsv"
    : > "$LEG_PIDFILE"
    PARALLEL_MODE=1
    trap 'rm -rf "$LEG_TMP" 2>/dev/null; true' EXIT
    echo "parallel: ${PAR} legs at a time (HSCC_TEST_PARALLEL=1 for the serial path); leg logs in $LEG_TMP"
  else
    LEG_TMP=""
    PAR=1
    echo "parallel: cannot create $LEG_TMP_ROOT/hscc-legs-XXXXXX — running SERIALLY"
  fi
fi

rc=0
declare -a summary

if [ "$PARALLEL_MODE" -eq 0 ]; then
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
  # ── the marker every ordinary leg deselects ────────────────────────────────
  # `-m "not loadstress"` runs on EVERY roster leg, so the load-sensitive cases
  # (pytest.ini's marker) execute in exactly one place: the loadstress leg,
  # under load. Two reasons they are not simply run twice: an idle-box pass over
  # a case that only fails loaded proves nothing new, and the repeats that make
  # the stress leg worth running are wall time no normal gate should pay for.
  # The operator's own `-m` (in "$@") comes last and therefore wins, so this is
  # a default posture, not a cage.
  if [ "$d" = "loadstress" ]; then
    # Not a `<dir>/tests` pytest run, so it does not go through the line below.
    # Extra argv is forwarded as pytest args — same contract, same surprise
    # budget, as every other leg.
    env "${ENV_UNSET_ARGS[@]}" \
      "$PY" "$ROOT/scripts/loadstress_leg.py" "$@"
  else
    env "${ENV_UNSET_ARGS[@]}" \
      "$PY" -m pytest -q -rs "$ROOT/$d/tests" -p no:cacheprovider \
      -m "not loadstress" "$@"
  fi
  code=$?
  [ -n "$HB_PID" ] && kill "$HB_PID" 2>/dev/null
  # ── the wall time of THIS leg, on its own summary row ─────────────────────
  # WHY. Until now the runner carried a per-leg clock (`LEG_T0`, started for the
  # heartbeat) and threw it away: the summary said only `✓ <leg>`, so the one
  # number a reader needs — which of the twelve legs costs what — existed nowhere
  # a human or the queue could read it. Measured consequence (t_fdc2bff0,
  # docs/audits/merge-queue-gate-leg-timings_t_fdc2bff0.md): the queue's local
  # gate was reported as a single ~75 min block, and the diagnosis (two legs are
  # 88 % of the cost; the rest of the roster is 7 s each) had to be rebuilt by
  # hand from twelve separate `HSCC_TEST_LEGS=<leg>` invocations.
  # WHY ON THE ROW, AND WHY HERE. The heartbeat already answers "is it slow or
  # stopped" for the leg in flight; this answers "what did it cost" once it is
  # over, in the same redirected file, for every leg — including legs that
  # passed, which no failure digest ever mentions.
  # COMPATIBILITY: `merge_queue._LEG_FAILURE_RES` matches the red row on `^✗\s`
  # only, so widening the parenthetical keeps the leg in the handback digest and
  # now carries its cost with it. Pinned by
  # scripts/tests/test_run_tests_leg_duration.py.
  LEG_SECS=$(( $(date +%s) - LEG_T0 ))
  if [ $code -eq 0 ]; then
    summary+=("  ✓ $d (${LEG_SECS}s)")
  else
    summary+=("  ✗ $d (pytest exit $code, ${LEG_SECS}s)")
    rc=1
  fi
done
else
  # ── the bounded pack (t_6c2123f9) ─────────────────────────────────────────
  # bash 3.2 is the interpreter this script must run under (/bin/bash on macOS is
  # 3.2.57), so there is no `wait -n` and no associative array.
  #
  # WHY A POLL AND NOT A FIFO WAIT. The first version of this loop waited for the
  # OLDEST leg in flight, which is the obvious way to bound a pool without
  # `wait -n`, and it is nearly useless here: measured on the real roster at
  # p=2, seven legs cost 627 s of work in 615 s of wall — **1.0x**. The reason is
  # head-of-line blocking: while `hscc_daemon` (320 s) was the oldest in flight,
  # `sparkrun-hermes` (3 s) had finished 313 s earlier and its slot stayed
  # counted as busy, so no new leg launched. A pack that cannot recycle a free
  # slot is not a pack.
  #
  # So completion is DETECTED rather than WAITED FOR: a zombie (`stat` starts
  # with Z) is a finished child, `wait <pid>` on it returns its code
  # immediately, and the slot is recycled the same tick. That is `wait -n`
  # written for bash 3.2. The 0.5 s tick costs two `ps` calls per live leg and
  # is invisible next to legs measured in minutes.
  #
  # Reaping order is therefore COMPLETION order, while the summary and the
  # concatenated blocks stay ROSTER order (index order) — which is the digest
  # contract, and is why everything is keyed by index and never by a cursor into
  # a shifting list.
  declare -a P_PID P_NAME P_LOG P_T0S P_CODE P_DONE P_END
  P_N="${#LEGS[@]}"
  P_NEXT=0; P_HEAD=0; P_LIVE=0; P_REAPED=0
  P_T0="$(date +%s)"
  P_I=0
  while [ "$P_I" -lt "$P_N" ]; do P_DONE[$P_I]=""; P_I=$((P_I+1)); done

  # ONE watcher for the whole run, told which legs exist by the file it re-reads
  # each tick (scripts/leg_heartbeat.sh's 5th argument). It is a child of THIS
  # runner, so the existing "a watcher must not outlive its runner" rule still
  # holds, and it prints one existing-format line per LIVE leg — the t_ecb84764
  # signal, generalised to N instead of dropped.
  HB_PID=""
  if [ "${HEARTBEAT_SECONDS:-0}" -gt 0 ] 2>/dev/null; then
    bash "$ROOT/scripts/leg_heartbeat.sh" "roster" "$$" "$P_T0" "$HEARTBEAT_SECONDS" "$LEG_PIDFILE" &
    HB_PID=$!
  fi

  # Job control for the pack only, and restored after it: every background leg
  # then runs in its OWN process group, which is what lets the TERM/INT handlers
  # stop a leg and everything that leg spawned, by pid. (On the serial path
  # pytest is in the runner's own group, which is why Ctrl-C reaches it there
  # without any of this.)
  set -m
  trap on_sigint INT

  # CURRENT_SUITE must keep naming what is running: the SIGTERM line reads it,
  # and with a pack "what is running" is a SET. Rebuilt on every launch and every
  # reap, in roster order, from the legs not yet reaped.
  _live_suite() {
    CURRENT_SUITE=""
    _j="$P_HEAD"
    while [ "$_j" -lt "$P_NEXT" ]; do
      [ -n "${P_DONE[$_j]}" ] || CURRENT_SUITE="${CURRENT_SUITE:+$CURRENT_SUITE }${P_NAME[$_j]}"
      _j=$((_j+1))
    done
    [ -n "$CURRENT_SUITE" ] || CURRENT_SUITE="(draining)"
  }

  # A finished child is one whose STAT starts with Z (a zombie: exited, not yet
  # reaped) or whose STAT is empty (already reaped by an earlier tick). `kill -0`
  # is NOT the test — it succeeds on a zombie, which is how a pool stops seeing
  # free slots.
  _finished() {
    _st="$(ps -o stat= -p "${P_PID[$1]}" 2>/dev/null | tr -d ' ')"
    [ -z "$_st" ] && return 0
    [ "${_st#Z}" != "$_st" ] && return 0
    return 1
  }

  while [ "$P_REAPED" -lt "$P_N" ]; do
    while [ "$P_NEXT" -lt "$P_N" ] && [ "$P_LIVE" -lt "$PAR" ]; do
      d="${LEGS[$P_NEXT]}"
      # A leg NAME is a PATH (`scripts`, `.github/scripts`), so it cannot be
      # spliced raw into one filename. `…/1-.github/scripts.log` names a file in
      # a directory that does not exist: the redirect fails before pytest is
      # ever execed, the wrapper records nothing, and the leg reads red from a
      # `wait` status — measured at p=2, the `.github/scripts` leg of the roster
      # was red on every packed run for this reason alone and no FAILED line ever
      # existed to capture (t_4fc35c1f). Flatten the `/`; the index prefix still
      # carries roster order, and the leg's real name stays in the heartbeat TSV
      # and the summary rows below, which is where a reader looks for it.
      logf="$LEG_TMP/$P_NEXT-${d//\//_}.log"
      P_NAME[$P_NEXT]="$d"
      P_LOG[$P_NEXT]="$logf"
      P_T0S[$P_NEXT]="$(date +%s)"
      # The subshell is what `pgrep -P` is asked about, exactly as the runner is
      # in serial mode: `env` execs into pytest, so the subshell's one child IS
      # the leg's pytest. A subshell is needed at all so the redirection belongs
      # to the leg and `"$@"` still means the RUNNER's extra pytest args.
      #
      # The command MUST be the serial branch's command, flag for flag — the
      # `-m "not loadstress"` deselect in particular. A pack that forgot it would
      # run the load-sensitive cases on every ordinary leg while the serial gate
      # does not, i.e. the two postures would silently test different things.
      # Pinned by test_pack_runs_the_same_pytest_argv_as_the_serial_leg.
      #
      # The wrapper records its OWN exit code and end clock before the subshell
      # exits. The runner cannot time a leg it has not reaped yet (with a bound
      # of N, a leg can sit finished for as long as its slowest neighbour runs —
      # measured: a 3 s leg reported 320 s), so the duration in the `parallel:
      # done` line comes from here, not from the reap.
      (
        if [ "$d" = "loadstress" ]; then
          env "${ENV_UNSET_ARGS[@]}" \
            "$PY" "$ROOT/scripts/loadstress_leg.py" "$@"
        else
          env "${ENV_UNSET_ARGS[@]}" \
            "$PY" -m pytest -q -rs "$ROOT/$d/tests" -p no:cacheprovider \
            -m "not loadstress" "$@"
        fi
        rc_inner=$?
        printf '%s %s\n' "$rc_inner" "$(date +%s)" > "$LEG_TMP/$P_NEXT.rc"
        exit "$rc_inner"
      ) > "$logf" 2>&1 &
      P_PID[$P_NEXT]=$!
      printf '%s\t%s\t%s\n' "$d" "$!" "${P_T0S[$P_NEXT]}" >> "$LEG_PIDFILE"
      P_NEXT=$((P_NEXT+1)); P_LIVE=$((P_LIVE+1))
      _live_suite
    done

    _k="$P_HEAD"
    while [ "$_k" -lt "$P_NEXT" ]; do
      if [ -z "${P_DONE[$_k]}" ] && _finished "$_k"; then
        wait "${P_PID[$_k]}" 2>/dev/null
        wait_rc=$?
        # The leg's own record is the authority on both numbers; `wait`'s status
        # is the fallback for a wrapper that died before it could write it (a
        # missing record must read as RED, never as the 0 of the test above).
        rc_inner=""; p_end=""
        if [ -f "$LEG_TMP/$_k.rc" ]; then
          read -r rc_inner p_end < "$LEG_TMP/$_k.rc" || true
        fi
        [ -n "$rc_inner" ] || rc_inner="$wait_rc"
        P_CODE[$_k]="$rc_inner"
        P_END[$_k]="${p_end:-$(date +%s)}"
        P_DONE[$_k]=1
        P_LIVE=$((P_LIVE-1)); P_REAPED=$((P_REAPED+1))
        # LIVE, into the runner's own stream: the one line a reader of a
        # redirected log can act on while the pack is still running. Prefixed
        # `parallel:` so it can never be mistaken for an anchored digest line.
        printf 'parallel: done leg=%s elapsed=%ss rc=%s pack=%ss\n' \
          "${P_NAME[$_k]}" "$(( ${P_END[$_k]} - ${P_T0S[$_k]} ))" \
          "${P_CODE[$_k]}" "$(( $(date +%s) - P_T0 ))"
      fi
      _k=$((_k+1))
    done
    while [ "$P_HEAD" -lt "$P_NEXT" ] && [ -n "${P_DONE[$P_HEAD]}" ]; do
      P_HEAD=$((P_HEAD+1))
    done
    _live_suite
    [ "$P_REAPED" -lt "$P_N" ] && sleep 0.5
  done

  [ -n "$HB_PID" ] && kill "$HB_PID" 2>/dev/null
  set +m
  trap - INT

  # Concatenated in ROSTER ORDER (index order, not completion order): this is
  # the stream merge_queue parses, and it has to look like the serial one.
  P_I=0
  while [ "$P_I" -lt "$P_N" ]; do
    echo "━━━ ${P_NAME[$P_I]} ━━━"
    cat "${P_LOG[$P_I]}" 2>/dev/null
    # Same per-leg wall time the serial row carries (t_fdc2bff0): a packed gate
    # is still a gate whose cost a reader needs to see, and the queue's digest
    # takes the row verbatim.
    LEG_SECS=$(( ${P_END[$P_I]} - ${P_T0S[$P_I]} ))
    if [ "${P_CODE[$P_I]}" -eq 0 ]; then
      summary+=("  ✓ ${P_NAME[$P_I]} (${LEG_SECS}s)")
    else
      summary+=("  ✗ ${P_NAME[$P_I]} (pytest exit ${P_CODE[$P_I]}, ${LEG_SECS}s)")
      rc=1
    fi
    P_I=$((P_I+1))
  done
fi

echo
echo "━━━ Summary ━━━"
printf '%s\n' "${summary[@]}"
[ $rc -eq 0 ] && echo "ALL GREEN" || echo "FAILURES ABOVE"
exit $rc
