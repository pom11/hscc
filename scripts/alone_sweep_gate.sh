#!/bin/bash
# alone_sweep_gate.sh — the ROUTED form of the alone-vs-in-leg sweep (t_6cd17e1c).
#
# WHAT IT MEASURES. `scripts/run_tests.sh` runs a leg as ONE pytest process over
# the whole dir, so a leg can print ALL GREEN while a file inside it FAILS (or
# skips for a different reason) when run by itself. When that happens the leg is
# answering a question about test ORDER, not about the interpreter — and the
# dual-interpreter stamp's whole promise ("a FAILED in a sibling leg is a
# regression") rests on it being the latter. It has happened twice in
# `hscc_daemon`, and the leg was green both times:
# docs/audits/daemon-alone-run-order-dependence_t_572b4856.md (§2 the leg, §7 a
# bug in the code written to fix §2). Nothing else running today sees either class.
#
# WHAT THIS SCRIPT ADDS to that audit's tooling. The measurement itself is
# already shipped and correct — `docs/audits/support/t_572b4856/alone_sweep.sh`
# (+ its `diff_alone_vs_leg.py`) runs the leg once through the canonical runner,
# then every test file alone with the SAME argv and the SAME SUITE_ENV_UNSET
# scrub, and diffs per nodeid and per skip reason. What was missing was ROUTING:
# a manual 131-process loop is a document, not a gate. This file is the routing
# — one command, both interpreters, a verdict on disk, a lock that keeps the run
# sequential, and a refusal to report green when nothing ran. It deliberately
# contains NO pytest invocation of its own: the measurement lives in one place.
#
# WHY NIGHTLY AND NOT PER-PR (the decision this card asked for). The sweep is
# ~131 pytest processes plus a fresh leg, measured at 7-9 min per interpreter,
# ~12 min with the leg — ~25 min for the interpreter pair, on the macOS runner
# pool (the only one that can collect `hscc_daemon`: its tests annotate
# `select.kqueue` at class scope, and ubuntu died collecting it — see
# leg_select.MACOS_LEGS), which is also the smallest pool in CI. Per-PR that is
# slower than the suite it guards, on the runner where minutes cost 10x; a gate
# slower than the suite gets clicked through, which the CI header calls out as
# worse than no gate. So the sweep runs on a schedule on a host that has BOTH
# interpreters the stamp cares about, and the cheap always-on half — the wiring
# and the teeth of the tooling — is pinned inside the `scripts` leg by
# `scripts/tests/test_alone_sweep_gate.py`, which spends milliseconds and never
# runs pytest over the checkout. Install it with
# `scripts/install_alone_sweep_gate.sh`.
#
# Usage:
#   scripts/alone_sweep_gate.sh                     # both shipped interpreters
#   scripts/alone_sweep_gate.sh --preflight         # wiring only, runs nothing
#   scripts/alone_sweep_gate.sh --python PATH ...    # a specific interpreter(s)
# Options:
#   --repo DIR     checkout to measure (default: this script's own checkout,
#                  or $HSCC_ALONE_SWEEP_REPO — the installed cron wrapper needs
#                  this, because ~/.hermes/scripts is not inside a checkout)
#   --out DIR      report root (default $HSCC_ALONE_SWEEP_OUT or
#                  ~/.hscc/alone-sweep). Per-interpreter dirs + `latest.txt`.
#   --leg DIR      leg dir to sweep (default $HSCC_ALONE_SWEEP_LEG, hscc_daemon)
#   --python PATH  interpreter to sweep under; repeatable, overrides the defaults
#   --resume       keep any per-file results already in the out dir (for
#                  restarting an interrupted pass). NEVER valid after the tree
#                  changed: carried-over rc/xml would then be a measurement of a
#                  tree that no longer exists. Fresh is the default for that
#                  reason, and cron uses the default.
#   --preflight    check the wiring and print the plan, run nothing
# Environment (also baked by the installer, see install_alone_sweep_gate.sh):
#   HSCC_ALONE_SWEEP_REPO      checkout to measure (--repo wins)
#   HSCC_ALONE_SWEEP_LEG       leg dir (--leg wins, default hscc_daemon)
#   HSCC_ALONE_SWEEP_OUT       report root (--out wins)
#   HSCC_ALONE_SWEEP_PYTHONS   space-separated interpreter list (--python wins).
#                  The defaults are `$HOME`-relative, and a Hermes cron job may run
#                  with a bound home that is not the operator's shell HOME, so the
#                  installer resolves and bakes this rather than leaving it to 04:30.
# Liveness: every non-preflight pass stamps through
#   hscc_daemon/cron_watchers.py (stream `alone_sweep_gate`, roster row nightly
#   86400s), so the bootstrap doctor's "cron watchers" row sees a nightly that
#   stops firing — and sees one that fires and comes back RED, which stamps
#   ok=False. The stamp is best-effort: losing it costs the pass its liveness
#   signal, never its verdict.
#
# Exit codes — the distinction the CI header insists on ("a leg that could not
# run at all fails EITHER way"):
#   0  every interpreter swept and every file answered alone exactly as in-leg
#   1  at least one file answers alone differently than in-leg, or the leg
#      itself is red (which is the same claim, one step earlier)
#   2  NOT MEASURED — wiring broken, no interpreter available, or another pass
#      holds the lock. Never green, never advisory-green.

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${HSCC_ALONE_SWEEP_REPO:-$(cd "$HERE/.." && pwd)}"
LEG="${HSCC_ALONE_SWEEP_LEG:-hscc_daemon}"
OUT="${HSCC_ALONE_SWEEP_OUT:-$HOME/.hscc/alone-sweep}"
PREFLIGHT=0
RESUME=0
CLI_PYTHONS=""
# The two interpreters the dual-interpreter stamp is taken on. Both matter: the
# §7 regression was invisible on the thin sibling and visible only on the
# canonical venv (agent-tree eviction of rich/typer/_pytest), so a gate that
# swept one interpreter would have missed it. Both are `$HOME`-relative, which is
# why the installed cron job bakes the resolved list at install time — Hermes may
# bind a profile home, and cron's HOME is not the operator's shell HOME.
DEFAULT_PYTHONS="$HOME/.hermes/hermes-agent/venv/bin/python $HOME/miniconda3/envs/p313/bin/python"

while [ $# -gt 0 ]; do
  case "$1" in
    --repo)     REPO="${2:?--repo needs a directory}"; shift 2 ;;
    --out)      OUT="${2:?--out needs a directory}"; shift 2 ;;
    --leg)      LEG="${2:?--leg needs a directory}"; shift 2 ;;
    --python)   CLI_PYTHONS="$CLI_PYTHONS ${2:?--python needs an interpreter path}"; shift 2 ;;
    --resume)   RESUME=1; shift ;;
    --preflight) PREFLIGHT=1; shift ;;
    -h|--help)  awk '/^# Usage:/{f=1} /^set -u/{exit} f' "$0"; exit 0 ;;
    *) echo "alone_sweep_gate.sh: unknown argument '$1' (see --help)" >&2; exit 2 ;;
  esac
done
# Explicit --python beats the environment beats the shipped defaults.
PYTHONS="$CLI_PYTHONS${HSCC_ALONE_SWEEP_PYTHONS:+ $HSCC_ALONE_SWEEP_PYTHONS}"
[ -n "$PYTHONS" ] || PYTHONS="$DEFAULT_PYTHONS"

SWEEP="$REPO/docs/audits/support/t_572b4856/alone_sweep.sh"

# ── liveness stamp ──────────────────────────────────────────────────────────
# This is a Hermes cron job whose verdict otherwise lives only in $OUT, so a
# nightly that stops firing is discovered by nobody — the exact shape the cron
# watchers' roster exists for (the dep watcher failed 17 consecutive daily runs,
# t_3aadd2c0). Every pass that is not a preflight stamps through
# ``hscc_daemon/cron_watchers.py``, the ONE liveness stamp format on this host:
# loaded BY PATH from this checkout (a deployed plugin copy must not stamp
# through older code — t_77db1513), read back by the doctor's "cron watchers"
# row. Cadence/window live in that module's WATCHERS roster, not here.
#
# THE VERDICT RIDES INSIDE THE STAMP; it does not replace it. The row is a
# LIVENESS row ("did the job fire?"), and a pass that was not clean stamps
# ok=False — so `stale`/`never-ran` mean "the nightly stopped firing" and
# `failing` means "it fired and the sweep said RED / NOT MEASURED". A red sweep
# can never render as a healthy watcher (t_13a70290), and a green pass advances
# the stamp exactly like the watchers' idle passes do.
#
# Best-effort by contract: a stamp failure costs the pass its liveness signal,
# never its verdict — printed, never swallowed (same rule as the two python
# watchers). A preflight measured nothing, so it stamps nothing.
STAMP_STREAM="alone_sweep_gate"
GATE_VERDICT="not-measured"

_stamp() {   # _stamp <gate-rc> <note-for-the-warn-line>
  [ "$PREFLIGHT" = 1 ] && return 0
  local rc="${1:-2}" note="${2:-}" py="" mod="" cand
  for cand in ${AVAIL:-}; do py="$cand"; break; done
  [ -n "$py" ] || py="$(command -v python3 2>/dev/null || true)"
  for cand in "$HERE/../hscc_daemon/cron_watchers.py" \
              "$REPO/hscc_daemon/cron_watchers.py" \
              "$HOME/.hermes/plugins/hscc_daemon/cron_watchers.py"; do
    [ -f "$cand" ] && { mod="$cand"; break; }
  done
  if [ -z "$py" ] || [ -z "$mod" ]; then
    echo "   (liveness stamp SKIPPED: no interpreter or no hscc_daemon/cron_watchers.py — the doctor cannot see this pass)" >&2
    return 0
  fi
  # Fields are argv pairs, never a hand-built JSON string: a report path with a
  # quote in it must not corrupt the stamp.
  if ! "$py" - "$mod" "$STAMP_STREAM" "$rc" "$note" \
        gate_rc "$rc" verdict "$GATE_VERDICT" interpreters "${RAN:-0}" \
        head "${SHA:-NO-GIT}" report "$OUT" <<'PY'
import importlib.util
import sys

mod_path, stream, rc, note = (sys.argv[1], sys.argv[2], int(sys.argv[3]),
                              sys.argv[4])
spec = importlib.util.spec_from_file_location("hscc_cron_watchers", mod_path)
if spec is None or spec.loader is None:
    raise SystemExit(1)
cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cw)
pairs = sys.argv[5:]
fields = dict(zip(pairs[::2], pairs[1::2]))
for key in ("gate_rc", "interpreters"):
    try:
        fields[key] = int(fields[key])
    except (KeyError, ValueError):
        pass
if rc == 0:
    landed = cw.stamp(stream, **fields)
else:
    landed = cw.stamp_failure(stream, note, **fields)
sys.exit(0 if landed else 1)
PY
  then
    echo "   (liveness stamp WRITE FAILED — the doctor's cron-watchers row will not see this pass)" >&2
  else
    echo "   liveness: stamped $STAMP_STREAM verdict=$GATE_VERDICT (bootstrap doctor -> 'cron watchers')"
  fi
  return 0
}

# ── preflight: the wiring that would otherwise fail as a silent green ───────
# Every one of these is a way the gate could report nothing and look fine: wrong
# repo, sweep tooling renamed away, a leg dir with no tests, interpreters that
# are all missing. Checked before the lock so a broken install costs nothing.
fail2() { _stamp 2 "GATE NOT MEASURED: $*"; echo "GATE NOT MEASURED: $*" >&2; exit 2; }

[ -f "$REPO/scripts/run_tests.sh" ] || fail2 "'$REPO' has no scripts/run_tests.sh — pass --repo (or set HSCC_ALONE_SWEEP_REPO) with the checkout to measure"
[ -x "$SWEEP" ] || [ -f "$SWEEP" ] || fail2 "the sweep tooling is missing at docs/audits/support/t_572b4856/alone_sweep.sh — the gate has no measurement to route"
[ -d "$REPO/$LEG/tests" ] || fail2 "'$LEG/tests' is not a directory under '$REPO' — is '$LEG' a leg dir?"

SHA="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo NO-GIT)"
DIRTY="$(git -C "$REPO" status --porcelain 2>/dev/null | head -1)"
[ -n "$DIRTY" ] && DIRTY="dirty" || DIRTY="clean"

AVAIL=""
MISSING=""
for py in $PYTHONS; do
  if [ -x "$py" ]; then AVAIL="$AVAIL $py"; else MISSING="$MISSING $py"; fi
done

echo "== alone-vs-in-leg gate ($LEG) — repo $REPO @ $SHA ($DIRTY)"
for py in $AVAIL;  do echo "   interpreter present : $py"; done
for py in $MISSING; do echo "   interpreter MISSING: $py (its verdict will not be measured)" >&2; done

if [ "$PREFLIGHT" = "1" ]; then
  [ -n "$AVAIL" ] || fail2 "no interpreter is executable ($PYTHONS)"
  echo "PREFLIGHT OK — would sweep $(ls "$REPO/$LEG/tests"/test_*.py 2>/dev/null | wc -l | tr -d ' ') files per interpreter across $(echo $AVAIL | wc -w | tr -d ' ') interpreter(s)"
  exit 0
fi

# ── one pass at a time ──────────────────────────────────────────────────────
# The measurement's own precondition: one pytest process at a time over this
# checkout. Two concurrent passes trip the port/lock fixtures, and a flake from
# that reads exactly like an order effect — the thing this gate is looking for.
mkdir -p "$OUT" 2>/dev/null || fail2 "cannot create the report root '$OUT'"
LOCK="$OUT/.gate.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  HOLDER="$(cat "$LOCK/pid" 2>/dev/null || echo '?')"
  if [ "$HOLDER" != "?" ] && kill -0 "$HOLDER" 2>/dev/null; then
    fail2 "another pass (pid $HOLDER) holds $LOCK — concurrent passes would break the sequential precondition of the measurement"
  fi
  echo "   (stale lock from pid ${HOLDER:-?} — taking over)"
  echo $$ > "$LOCK/pid"
else
  echo $$ > "$LOCK/pid"
fi
trap 'rm -f "$LOCK/pid"; rmdir "$LOCK" 2>/dev/null' EXIT

RAN=0
WORST=0
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
declare -a VERDICTS

for py in $AVAIL; do
  # venv/bin/python -> "venv"; envs/p313/bin/python -> "p313"; plain python3 -> "python3"
  base="$(basename "$(dirname "$(dirname "${py%/}")")")"
  case "$base" in bin|""|".") base="$(basename "${py%/}")" ;; esac
  dir="$OUT/$base-$STAMP"
  if [ "$RESUME" = "0" ]; then rm -rf "$dir"; fi
  mkdir -p "$dir" || fail2 "cannot create '$dir'"

  echo
  echo "== sweep $base — $(ls "$REPO/$LEG/tests"/test_*.py 2>/dev/null | wc -l | tr -d ' ') files, one process each (this takes minutes)"
  # alone_sweep.sh owns the argv, the scrub and the diff; its log is the record.
  bash "$SWEEP" "$py" "$LEG" "$dir" > "$dir/sweep.log" 2>&1
  rc=$?
  RAN=$((RAN + 1))
  tail -6 "$dir/sweep.log" | sed 's/^/   /'

  case "$rc" in
    0) what="alone == in-leg" ;;
    1) what="$(grep -q 'LEG ITSELF is red' "$dir/sweep.log" && echo 'the leg itself is red' || echo 'at least one file answers alone differently')" ;;
    *) what="sweep could not measure (sweep rc=$rc)" ;;
  esac
  case "$rc" in
    0) ;; 1) [ "$WORST" -lt 1 ] && WORST=1 ;; *) [ "$WORST" -lt 2 ] && WORST=2 ;;
  esac
  printf '{\n  "leg": "%s",\n  "repo": "%s",\n  "head": "%s",\n  "tree": "%s",\n  "interpreter": "%s",\n  "sweep_rc": %s,\n  "verdict": "%s",\n  "utc": "%s"\n}\n' \
    "$LEG" "$REPO" "$SHA" "$DIRTY" "$py" "$rc" "$what" "$STAMP" > "$dir/verdict.json"
  VERDICTS+=("  $([ "$rc" -eq 0 ] && echo '✓' || echo '✗') $base: rc=$rc — $what")
  echo "   verdict: $dir/verdict.json"
done

echo
if [ "$RAN" = "0" ]; then
  # The failure this whole script exists to make impossible: green because
  # nothing ran. A nightly whose interpreters both went missing must be RED.
  fail2 "no interpreter was executable — NOTHING WAS SWEPT. Refusing to report green."
fi

printf '%s\n' "${VERDICTS[@]}"
[ "$RAN" -gt 1 ] && echo "  ($RAN interpreters swept)"
echo "$dir" > "$OUT/latest.txt"          # last pass, whatever the verdict
echo
if [ "$WORST" = "0" ]; then
  echo "GATE GREEN: every file answers alone exactly as it does inside the leg."
  GATE_VERDICT="green"
  STAMP_NOTE=""
elif [ "$WORST" = "1" ]; then
  STAMP_NOTE="GATE RED: at least one file answers alone differently than inside the leg (or its leg is red). See the logs above and $OUT/latest.txt"
  echo "$STAMP_NOTE"
  GATE_VERDICT="red"
else
  STAMP_NOTE="GATE NOT MEASURED: see the lines above — this is not a pass. Report dir: $OUT/latest.txt"
  echo "$STAMP_NOTE"
  GATE_VERDICT="not-measured"
fi
# The stamp is the LAST thing a pass does, so it reports what the whole pass
# concluded — including the sweep-side failures that left WORST at 2.
_stamp "$WORST" "$STAMP_NOTE"
exit "$WORST"
