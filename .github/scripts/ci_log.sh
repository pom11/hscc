#!/usr/bin/env bash
# ci_log.sh — the sanctioned way to read a CI log on this repo (t_914b8db5).
#
# Why this exists: scripts/address_guard.py reports each offender as
# ``rel:line: <matched address>``, and pom11/hscc is PUBLIC — so anything that
# publishes a report verbatim publishes the leak. The shipped address-guard
# job's own logs are clean (its step redirects both streams to files and echoes
# only redacted output; measured on run 37845289259), but a raw
# `gh run view --log` on any job that ran the guard WITHOUT stream
# redirection — a manual workflow_dispatch, an ad-hoc step in another
# workflow, any pre-fix run — prints the live address into scrollback, a
# paste, or an attachment. This wrapper is the path workers should use.
#
# Contract:
#   * the log dump goes to ONE mode-0600 file in a mode-0700 temp dir, never
#     to a terminal;
#   * stdout carries ONLY what .github/scripts/redact_guard_report.py emits;
#   * gh's STDERR shares the dump and therefore the SAME redactor — it is
#     never sent to /dev/null, because those bytes cannot be proven to be
#     non-job content, and swallowing them would also hide real gh errors
#     (explicit instruction from the parent card, t_9a4b7687 §9);
#   * fail closed: if the redactor cannot load the guard's pattern this
#     script prints NOTHING on stdout and exits non-zero — it never falls
#     through to the raw dump;
#   * the raw dump is deleted on exit unless --keep, and when kept the caller
#     is told in stderr that the file is UNREDACTED.
#
# The rule this enforces: never paste, attach, or comment a raw CI or suite
# log on this repo without running it through redact_guard_report.py first.
#
# Usage: ci_log.sh <run-id> [--job <name>] [--keep]
# Env:   HSCC_CI_LOG_PY   interpreter for the redactor (default: python3)
# Exit:  gh's exit code on success, 2 usage, 3 redactor unavailable (closed),
#        127 gh missing
set -u -o pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REDACTOR="$SCRIPT_DIR/redact_guard_report.py"
PY="${HSCC_CI_LOG_PY:-python3}"

usage() {
  echo "usage: ci_log.sh <run-id> [--job <name>] [--keep]" >&2
}

run=""
job=""
keep=0
while [ $# -gt 0 ]; do
  case "$1" in
    --job)
      [ $# -ge 2 ] || { usage; exit 2; }
      job="$2"; shift 2 ;;
    --keep)  keep=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) usage; exit 2 ;;
    *)
      [ -z "$run" ] || { usage; exit 2; }
      run="$1"; shift ;;
  esac
done
if [ -z "$run" ]; then usage; exit 2; fi

# The dir is 0700 (mktemp -d) and every file inside is 0600 (umask 077). The
# cleanup trap is armed BEFORE anything can fail, so no error path can strand
# a raw dump outside --keep.
WORK="$(mktemp -d "${TMPDIR:-/tmp}/hscc-ci-log.XXXXXX")" || {
  echo "ci_log: cannot create a private temp dir" >&2; exit 3; }
RAW="$WORK/raw.log"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

# ── fail closed BEFORE fetching ──────────────────────────────────────────────
# A redactor that cannot load the guard's pattern must stop this script while
# NOTHING has been printed and nothing has been fetched. `</dev/null` is a
# real end-to-end probe of the exact invocation used below — pattern load in
# the child process included — not a file-exists guess.
if [ ! -f "$REDACTOR" ]; then
  echo "ci_log: redactor missing at $REDACTOR — refusing to fetch a log it cannot redact" >&2
  exit 3
fi
# The redactor writes its own diagnostics to stderr; stdout must stay empty in
# preflight, and stderr here is provably fixed diagnostic strings (see the
# redactor's trust-boundary docstring), so forwarding it is safe.
"$PY" -W ignore -E "$REDACTOR" </dev/null >"$WORK/preflight.out" 2>"$WORK/preflight.err"
if [ $? -ne 0 ]; then
  [ -s "$WORK/preflight.err" ] && cat "$WORK/preflight.err" >&2
  echo "ci_log: REDACTOR UNAVAILABLE — raw log bytes withheld (fail closed)." >&2
  exit 3
fi
rm -f "$WORK/preflight.out" "$WORK/preflight.err"

if ! command -v gh >/dev/null 2>&1; then
  echo "ci_log: gh is not on PATH" >&2
  exit 127
fi

# ── fetch: BOTH streams into one 0600 dump; raw bytes never hit a terminal ──
# Merging with 2>&1 is what keeps gh's stderr inside the redaction path
# instead of /dev/null: whatever gh prints, it reaches the log only after the
# redactor has had it, and real gh errors stay visible.
if [ -n "$job" ]; then
  gh run view "$run" --job "$job" --log >"$RAW" 2>&1
else
  gh run view "$run" --log >"$RAW" 2>&1
fi
gh_rc=$?
chmod 600 "$RAW" 2>/dev/null || true

# ── redact the dump; publish only if the redactor said OK ───────────────────
# Same interpreter flags the workflow step uses: -W ignore -E, so no warning
# can render a source line at the interpreter level either. The dump goes in
# as a FILE argument (t_914b8db5) — clean bytes come out byte-identical.
"$PY" -W ignore -E "$REDACTOR" "$RAW" >"$WORK/red.out" 2>"$WORK/red.err"
red_rc=$?
if [ "$red_rc" -ne 0 ]; then
  # Fixed redactor diagnostics only; the raw dump is NOT published and the
  # cleanup trap takes it with us.
  [ -s "$WORK/red.err" ] && cat "$WORK/red.err" >&2
  echo "ci_log: REDACTOR FAILED — raw log bytes withheld (fail closed)." >&2
  exit 3
fi

cat "$WORK/red.out"

# ── raw-dump retention policy ────────────────────────────────────────────────
if [ "$keep" -eq 1 ]; then
  # Leave exactly ONE file behind: the dump, already at mode 0600 (umask 077,
  # re-chmod'd above), and say loud in stderr what it is. The derived redacted
  # copy is not worth keeping; then disarm the trap so the dir survives.
  rm -f "$WORK/red.out" "$WORK/red.err"
  trap - EXIT
  echo "ci_log: WARNING — raw dump KEPT at $WORK/raw.log (mode 0600). It is UNREDACTED: scan it with .github/scripts/redact_guard_report.py before pasting, attaching, or commenting any of it." >&2
fi

exit "$gh_rc"
