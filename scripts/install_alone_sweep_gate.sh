#!/usr/bin/env bash
# install_alone_sweep_gate.sh — schedule the alone-vs-in-leg sweep gate (t_6cd17e1c).
#
# WHAT IT REGISTERS. A daily Hermes cron job running
# ``scripts/alone_sweep_gate.sh`` with ``--no-agent`` (the script IS the job, same
# shape as hscc-dep-watcher / hscc-escalate-watcher). The gate sweeps the leg
# once through the canonical runner, then every one of its test files alone, and
# exits non-zero when a file answers alone differently than inside the leg — a
# class the leg itself prints ALL GREEN for (twice, in hscc_daemon; see
# docs/audits/daemon-alone-run-order-dependence_t_572b4856.md §2 and §7).
#
# WHY A SCHEDULE AND NOT THE PR LANE. ~12 min per interpreter (131 pytest
# processes + a fresh leg) on the macOS runner pool — hscc_daemon is a macOS-only
# leg (leg_select.MACOS_LEGS), the smallest pool in CI at 10x the minute cost —
# i.e. slower than the suite it guards, which is the CI header's own criterion
# for a gate that gets clicked through. The full reasoning is in
# ``scripts/alone_sweep_gate.sh``'s header and in the audit's §9.
#
# THE HOST HAS TO HAVE BOTH INTERPRETERS. The gate sweeps the two interpreters
# the dual-interpreter stamp is taken on, and that is the point: the §7
# regression was invisible on the thin sibling and visible only on the canonical
# venv. A host with one of them is a half-measurement, which the gate says out
# loud (it prints the missing interpreter and refuses to call the pass green if
# NEITHER is available).
#
# DRIFT RULE, carried over from install_dep_watcher.sh (t_3aadd2c0): Hermes
# resolves a bare ``--script NAME`` ONLY against the BOUND home's ``scripts/``
# dir ($HERMES_HOME/scripts; profiles never share files). The dep watcher used to
# hard-code the shared ~/.hermes/scripts while the job bound the invoking
# profile's home, and failed 17 consecutive daily runs that way. Here the copy
# destination and the cron binding resolve from the SAME variable, and
# scripts/tests/test_alone_sweep_gate.py pins it with a stubbed `hermes`.
#
# The installed job is a GENERATED one-line wrapper, not a copy of the gate.
# ~/.hermes/scripts is not inside a checkout, so a copied script could not find
# the sweep tooling (alone_sweep.sh infers its repo from its own path) and a copy
# goes stale the moment the repo moves. The wrapper bakes the repo path at
# install time and execs the repo's gate, so the measured code is always the code
# in git and re-running this installer is the only way to change what runs.
#
# Usage:  scripts/install_alone_sweep_gate.sh [--uninstall | --status]
set -euo pipefail

JOB="hscc-alone-sweep-gate"
WRAPPER="hscc-alone-sweep-gate.sh"
# Nightly 04:30 local: quiet hours, ~25 min for the interpreter pair, and it
# finishes before the 08:00 dep watcher so two --no-agent jobs never overlap on
# one host. Override with HSCC_ALONE_SWEEP_SCHEDULE (a Hermes cron spec) — and if
# you do, move the doctor's staleness window with it: export
# HSCC_ALONE_SWEEP_CADENCE_S=<seconds> (hscc_daemon/cron_watchers.py reads that
# for the `alone_sweep_gate` row, so a weekly pass is not judged stale on Tuesday).
SCHEDULE="${HSCC_ALONE_SWEEP_SCHEDULE:-30 4 * * *}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
GATE="$SCRIPT_DIR/alone_sweep_gate.sh"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_SCRIPTS="$HERMES_HOME/scripts"
HERMES="$HOME/.hermes/hermes-agent"
HERMES_BIN="$HERMES/venv/bin/hermes"
[ -x "$HERMES_BIN" ] || HERMES_BIN="hermes"
OUT="${HSCC_ALONE_SWEEP_OUT:-$HOME/.hscc/alone-sweep}"

if [ "${1:-}" = "--uninstall" ]; then
  "$HERMES_BIN" cron remove "$JOB" 2>/dev/null || true
  rm -f "$HERMES_SCRIPTS/$WRAPPER"
  # The liveness marker AND the stamp go with the job (GAP-B-4): leaving the
  # stamp behind would leave the doctor ageing a deliberately-removed nightly
  # into a permanent `stale` warn for a job nobody wants any more.
  python3 - "$REPO" <<'PY' 2>/dev/null || true
import importlib.util, os, sys
repo = sys.argv[1]
p = os.path.join(repo, "hscc_daemon", "cron_watchers.py")
spec = importlib.util.spec_from_file_location("hscc_cron_watchers", p)
if spec is None or spec.loader is None:
    raise SystemExit(0)
cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cw)
cw.mark_removed("alone_sweep_gate")
cw.remove_stamp("alone_sweep_gate")
PY
  echo "removed cron job '$JOB' and $HERMES_SCRIPTS/$WRAPPER"
  echo "  also cleared its doctor liveness marker + stamp (bootstrap doctor -> 'cron watchers')"
  echo "reports under $OUT stay where they are (delete by hand if you want them gone)"
  exit 0
fi

# The bootstrap doctor's own verdict for this stream, printed where an operator
# looks first (t_13a70290). Read through hscc_daemon/cron_watchers.py so --status
# and `python3 hscc-bootstrap/doctor.py` cannot disagree about what "alive" means,
# and best-effort: a host without the module gets the paths and the report lines,
# not a crash.
_liveness_line() {
  python3 - "$REPO" <<'PY' 2>/dev/null
import importlib.util, os, sys
repo = sys.argv[1]
p = os.path.join(repo, "hscc_daemon", "cron_watchers.py")
spec = importlib.util.spec_from_file_location("hscc_cron_watchers", p)
if spec is None or spec.loader is None:
    raise SystemExit(0)
cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cw)
v = cw.verdict("alone_sweep_gate")
print(f'{v["status"]}: {v["detail"]}')
PY
}

if [ "${1:-}" = "--status" ]; then
  echo "job:      $JOB"
  "$HERMES_BIN" cron list 2>/dev/null | grep -F "$JOB" \
    || echo "          NOT REGISTERED — run: scripts/install_alone_sweep_gate.sh"
  echo "wrapper:  $HERMES_SCRIPTS/$WRAPPER"
  [ -f "$HERMES_SCRIPTS/$WRAPPER" ] || echo "          MISSING — the cron job would fail on its next tick"
  # Report where the JOB writes, not where this shell would default to: the
  # wrapper baked both resolved at install time and may name paths this shell's
  # $HOME would not reach (a bound profile home is exactly that case). `|| true`
  # inside the substitution because the wrapper may not exist yet and this script
  # runs under `set -euo pipefail` — a missing wrapper is a STATUS line, not a
  # crash (measured: without it, --status died at rc 1 on an uninstalled host
  # before printing the one thing an operator needs to see).
  reports="$(sed -n 's/^export HSCC_ALONE_SWEEP_OUT="\(.*\)"$/\1/p' "$HERMES_SCRIPTS/$WRAPPER" 2>/dev/null | tail -1 || true)"
  [ -n "$reports" ] || reports="$OUT"
  pythons="$(sed -n 's/^export HSCC_ALONE_SWEEP_PYTHONS="\(.*\)"$/\1/p' "$HERMES_SCRIPTS/$WRAPPER" 2>/dev/null | tail -1 || true)"
  echo "reports:  $reports"
  [ -n "$pythons" ] && echo "sweeps:   $pythons"
  liveness="$(_liveness_line || true)"
  if [ -n "$liveness" ]; then echo "liveness: $liveness"; fi
  if [ -f "$reports/latest.txt" ]; then
    latest="$(cat "$reports/latest.txt")"
    echo "last pass: $latest"
    cat "$latest/verdict.json" 2>/dev/null || sed -n '/^GATE /p' "$latest/sweep.log" 2>/dev/null || true
  else
    echo "last pass: NONE on record — that is not the same as a pass."
  fi
  exit 0
fi

[ -z "${1:-}" ] || { echo "install_alone_sweep_gate.sh: unknown argument '$1' (expected --uninstall or --status)" >&2; exit 2; }

# Fail before registering anything rather than installing a job that can only
# fail at 04:30 — a watcher nobody notices failing is the failure mode this
# installer's own header documents (17 consecutive missed runs, t_3aadd2c0).
[ -f "$GATE" ]            || { echo "no gate at $GATE — is this checkout complete?" >&2; exit 2; }
[ -f "$REPO/scripts/run_tests.sh" ] || { echo "'$REPO' is not an HSCC checkout — the gate would measure nothing" >&2; exit 2; }
[ -f "$REPO/docs/audits/support/t_572b4856/alone_sweep.sh" ] \
  || { echo "the sweep tooling is missing under docs/audits/support/t_572b4856/ — the gate has no measurement to route" >&2; exit 2; }

# Resolve what the nightly needs NOW, from the operator's own shell, and bake it.
# Hermes may bind a profile home for a job, and cron's HOME is not the operator's
# shell HOME — the two `$HOME`-relative interpreter defaults (and the report root)
# would then resolve somewhere else at 04:30 and the pass would refuse (rc 2,
# loudly) or report into a dir nobody looks at. `test_hermes_python.py` exists for
# exactly this HERMES_HOME-vs-root split on the resolver side; this is the same
# trap on the scheduling side.
PY_CANDIDATES="${HSCC_ALONE_SWEEP_PYTHONS:-$HOME/.hermes/hermes-agent/venv/bin/python $HOME/miniconda3/envs/p313/bin/python}"
PY_BAKED=""
for _cand in $PY_CANDIDATES; do
  [ -x "$_cand" ] && PY_BAKED="${PY_BAKED:+$PY_BAKED }$_cand"
done
unset _cand
[ -n "$PY_BAKED" ] || {
  echo "no stamp interpreter is executable from this shell — refusing to install a job" >&2
  echo "  looked for: $PY_CANDIDATES" >&2
  echo "  the gate sweeps every interpreter it can see and exits 2 when it sees none," >&2
  echo "  so installing now would produce a nightly that never measures anything." >&2
  exit 2; }
OUT_BAKED="${HSCC_ALONE_SWEEP_OUT:-$HOME/.hscc/alone-sweep}"

mkdir -p "$HERMES_SCRIPTS" "$OUT_BAKED" 2>/dev/null || true
# Generated wrapper: the resolved paths baked at install time, then exec the gate
# from git.
cat > "$HERMES_SCRIPTS/$WRAPPER" <<EOF
#!/bin/bash
# GENERATED by $REPO/scripts/install_alone_sweep_gate.sh — do not edit.
# Re-run that installer to change what this job runs. The gate itself (and the
# sweep it routes) live in git, so the nightly measures committed code.
export HSCC_ALONE_SWEEP_REPO="$REPO"
export HSCC_ALONE_SWEEP_PYTHONS="$PY_BAKED"
export HSCC_ALONE_SWEEP_OUT="$OUT_BAKED"
exec "$GATE" "\$@"
EOF
chmod 0755 "$HERMES_SCRIPTS/$WRAPPER"

# Idempotent: drop any existing job of this name, then (re)create it.
"$HERMES_BIN" cron remove "$JOB" 2>/dev/null || true
"$HERMES_BIN" cron create "$SCHEDULE" \
  --no-agent --script "$WRAPPER" \
  --name "$JOB"

# Liveness marker (GAP-B-4, same shape as install_dep_watcher.sh): without it the
# doctor cannot tell "registered but the job never completed a pass" — the
# 17-consecutive-failures incident class, t_3aadd2c0 — from "nobody ever
# installed this". Written through hscc_daemon/cron_watchers.py so the file schema
# stays in ONE place, and best-effort: a host without the module must still get a
# working install.
python3 - "$REPO" "$SCHEDULE" <<'PY' || echo "note: could not write the liveness install marker (the doctor will report this job as never-installed)"
import importlib.util, os, sys
repo, schedule = sys.argv[1], sys.argv[2]
p = os.path.join(repo, "hscc_daemon", "cron_watchers.py")
spec = importlib.util.spec_from_file_location("hscc_cron_watchers", p)
if spec is None or spec.loader is None:
    sys.exit(1)
cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cw)
sys.exit(0 if cw.mark_installed("alone_sweep_gate", schedule) else 1)
PY

echo "installed Hermes cron job '$JOB' (schedule: $SCHEDULE)"
echo "  wrapper : $HERMES_SCRIPTS/$WRAPPER  -> $GATE"
echo "  repo    : $REPO (baked; re-run this installer after moving the checkout)"
echo "  sweeps  : $PY_BAKED"
echo "  reports : $OUT_BAKED (latest.txt + a per-interpreter verdict.json)"
echo "  inspect : $HERMES_BIN cron list   |   scripts/install_alone_sweep_gate.sh --status"
echo "  run now : $HERMES_BIN cron run $JOB && $HERMES_BIN cron tick"
echo "  liveness: every pass stamps hscc_daemon/cron_watchers.py (stream alone_sweep_gate),"
echo "            so the bootstrap doctor (python3 hscc-bootstrap/doctor.py) reports"
echo "            'cron watchers' when this nightly stops firing OR fires and comes"
echo "            back RED. The detail is in"
echo "            '$HERMES_BIN cron runs', this --status, and $OUT_BAKED."
