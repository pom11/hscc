#!/usr/bin/env bash
# Restart the hscc daemon through its SERVICE MANAGER and VERIFY the process
# actually turned over by comparing PIDs.
#
# Why: bootstrap's daemon-setup step (launchd/systemd-setup.sh) leaves an
# ALREADY-RUNNING daemon alone, so freshly-copied plugin code was never loaded.
# On the live host the daemon kept running the process started eight days
# earlier and the freshly-installed autodown loop silently never loaded — that
# cost real debugging time twice. This script fixes it: restart -> confirm
# the PID changed.
#
# ── THE OPERATOR RULE (2026-10-29, live P0) ────────────────────────────────
# When the service is loaded, a daemon restart goes through the supervisor. On
# macOS that means, and only means:
#
#     launchctl kickstart -k gui/$UID/com.hermes.hscc_daemon
#
# Restarting through the `hscc` console script is forbidden while the job is
# loaded, and the 2.14.0 deploy proved why twice over. The launchd job's real
# program is `~/.hermes/hermes-agent/venv/bin/python
# ~/.hermes/plugins/hscc_daemon/hscc.py start-daemon` (see the plist's
# ProgramArguments) and THAT interpreter carries the macOS Local Network (TCC)
# grant. The `/opt/homebrew/bin/hscc` console script is brew python, which has
# no such grant: the restart it performs leaves the daemon unable to reach any
# cluster node (errno 65 / "No route to host" on every stream), so it declares
# the whole fleet dead and its relaunch loop starts killing and re-creating
# healthy units. Worse, `hscc start` double-forks OUT of launchd (PPID 1), so
# the job label stays registered with no pid and supervision is silently lost —
# an orphan loop beside the service, exactly the twin class daemon_singleton.py
# exists for one level down.
#
# So: service loaded -> ask the supervisor to restart it (launchd `kickstart
# -k`, systemd `restart`). Both re-exec the unit's OWN program definition:
# right interpreter, right TCC context, right supervisor. The `hscc
# stop`/`hscc start` path below survives ONLY as the fallback for a machine
# where no service is loaded (fresh install, CI).
#
# ── ORPHAN GUARD (both paths) ───────────────────────────────────────────────
# Before touching anything we look for a live daemon process (`hscc start` /
# `hscc.py start-daemon`) that the supervisor does NOT own. Starting a second
# loop beside such a process is what produced the incident's double-tick, so we
# report it loudly with its pid and refuse to add a twin (rc 4). See
# hscc_daemon/daemon_singleton.py for the same class at the loop level; this is
# the supervisor-level twin check.
#
# Restart stays non-fatal to the caller (bootstrap): a failed restart must not
# abort an otherwise-good install, so we report a distinct exit code per
# failure mode instead of dying.
#
# How the daemon CLI is invoked on the FALLBACK path — the crux of an earlier
# fix. Running hscc_daemon/hscc.py by bare path fails instantly with
# `ModuleNotFoundError: No module named 'hscc_daemon'`, because the script
# imports its own package and a bare script path does not put its dir on
# sys.path. Both `stop` and `start` therefore died before doing anything, and a
# `>/dev/null 2>&1` hid the traceback. We now invoke the CLI like the daemon and
# operator actually do:
#   B (preferred) the installed `hscc` console script on PATH, used whenever we
#     are NOT under a test stub (no HSCC_CMD/HSCC_PYBIN env override) and `hscc`
#     resolves. NOTE: on a host whose service is loaded we never get here (see
#     the operator rule above); this path exists for service-less machines.
#   A (fallback)  $HSCC_PYBIN $HSCC_CMD with PYTHONPATH pointed at the package
#     root (parent of the dir holding HSCC_CMD) so a bare script path can still
#     import its own package. This honours the HSCC_CMD/HSCC_PYBIN env
#     overrides, which is how the tests stub the daemon.
#
# We also capture stop/start output instead of swallowing it: when the restart
# fails we surface the real reason (e.g. the ModuleNotFoundError traceback) in
# the outcome line rather than only "PID unchanged". Being loud is the whole
# point of this step.
#
# Safety / autodown: restarting briefly suspends watchdog supervision, but the
# new daemon's startup path (daemon_ops.run_daemon_loop) re-runs
# autodown.resume_from_restart, which re-asserts the intentional block when
# state=="down" and finishes the wake when state=="waking" — so a bootstrap-time
# restart can never strand a down/waking fleet. This script does not alter
# autodown's armed/disarmed state and never touches ~/.hscc/autodown.json or
# ~/.hscc/watchdog-block.json. It also never touches the plist or the unit file
# themselves, which are operator-owned runtime state (launchd-setup.sh /
# systemd-setup.sh / install.py own those).
#
# Usage: restart_daemon.sh [--skip]
#   --skip   do nothing (no restart, no PID check). bootstrap passes this
#            when it runs with --skip-daemon so no restart is attempted.
#
# Env (all optional; tests override them to stub the daemon AND both service
# managers, so no test can reach the operator's live service):
#   HSCC_CMD              daemon CLI script  (default
#                         ~/.hermes/plugins/hscc_daemon/hscc.py); when SET we
#                         always run it via $HSCC_PYBIN (path A) so tests can
#                         stub it
#   HSCC_PID_FILE         daemon pid file    (default ~/.hscc/daemon.pid)
#   HSCC_PYBIN            python interpreter (default hermes venv, else python3)
#   HSCC_LAUNCHD_LABEL    launchd job label  (default com.hermes.hscc_daemon)
#   HSCC_LAUNCHCTL        the launchctl binary to call (default: launchctl).
#                         Explicitly SET means "launchctl exists" even if it is
#                         not on PATH, so a stub can never be bypassed into the
#                         real service.
#   HSCC_SYSTEMCTL        the systemctl binary to call (default: systemctl);
#                         same "explicitly set means present" rule.
#   HSCC_SYSTEMD_UNIT     the unit to restart (default: probe
#                         hscc_daemon.service then com.hermes.hscc_daemon.service
#                         — see systemd_loaded for why both names).
#   HSCC_PGREP            the pgrep binary used by the orphan guard (default:
#                         pgrep). Tests stub it; when it is missing the guard
#                         finds nothing and fails open (documented below).
#   POLL_ATTEMPTS/POLL_INTERVAL                 poll budget, fallback path
#   LAUNCHD_POLL_ATTEMPTS/LAUNCHD_POLL_INTERVAL poll budget, supervised path
#                         (the supervisor may take a moment to report the new
#                         pid; launchd's ThrottleInterval makes this real)
#
# Prints a single outcome line (no ✓/⚠ prefix — the caller owns ok/warn) and
# exits:
#   0  restart verified (PID changed), OR fresh start (was not running), OR skipped
#   2  PID unchanged after restart — daemon left running is the OLD process
#   3  daemon did not come back after restart, the supervisor's restart command
#      failed, or the hscc command is missing
#   4  an unsupervised daemon (one the supervisor does not own) is running: the
#      fallback path refused to start a second loop beside it, or the supervised
#      restart succeeded but the orphan is still there. Either way a twin is
#      live and the operator has to reap it.
set -uo pipefail

SKIP=false
[ "${1:-}" = "--skip" ] && SKIP=true

# Record whether the user (or tests) explicitly set these, BEFORE defaulting
# them. ${X+x} is non-empty only if X is present in the environment.
HSCC_CMD_SET=0;   [ -n "${HSCC_CMD+x}" ]   && HSCC_CMD_SET=1
HSCC_PYBIN_SET=0; [ -n "${HSCC_PYBIN+x}" ] && HSCC_PYBIN_SET=1
HSCC_LAUNCHCTL_SET=0; [ -n "${HSCC_LAUNCHCTL+x}" ] && HSCC_LAUNCHCTL_SET=1
HSCC_SYSTEMCTL_SET=0; [ -n "${HSCC_SYSTEMCTL+x}" ] && HSCC_SYSTEMCTL_SET=1
HSCC_PGREP_SET=0;   [ -n "${HSCC_PGREP+x}" ]   && HSCC_PGREP_SET=1

HSCC_PYBIN="${HSCC_PYBIN:-$HOME/.hermes/hermes-agent/venv/bin/python}"
[ -x "$HSCC_PYBIN" ] || HSCC_PYBIN="python3"
HSCC_CMD="${HSCC_CMD:-$HOME/.hermes/plugins/hscc_daemon/hscc.py}"
HSCC_PID_FILE="${HSCC_PID_FILE:-$HOME/.hscc/daemon.pid}"
POLL_ATTEMPTS="${POLL_ATTEMPTS:-5}"
POLL_INTERVAL="${POLL_INTERVAL:-1}"

# launchd knobs. The label is the job the operator runs the daemon under (same
# constant as launchd-setup.sh / install.py); overridable so tests can never aim
# at the real service even if they forget to stub the binary.
LAUNCHD_LABEL="${HSCC_LAUNCHD_LABEL:-com.hermes.hscc_daemon}"
LAUNCHCTL="${HSCC_LAUNCHCTL:-launchctl}"
LAUNCHD_TARGET="gui/$(id -u)/${LAUNCHD_LABEL}"
LAUNCHD_POLL_ATTEMPTS="${LAUNCHD_POLL_ATTEMPTS:-45}"
LAUNCHD_POLL_INTERVAL="${LAUNCHD_POLL_INTERVAL:-1}"

# systemd --user knobs. Both unit names are probed because the two installers
# emit different ones (systemd-setup.sh writes hscc_daemon.service, install.py
# writes com.hermes.hscc_daemon.service). That drift is pre-existing — flagged
# as its own finding — but here we must not be fooled by it into calling a
# supervised daemon an orphan and refusing the restart.
SYSTEMCTL="${HSCC_SYSTEMCTL:-systemctl}"
SYSTEMD_UNIT="${HSCC_SYSTEMD_UNIT:-}"
SYSTEMD_UNIT_CANDIDATES="hscc_daemon.service com.hermes.hscc_daemon.service"

# The pgrep used by the orphan guard (stubbed by tests).
PGREP_BIN="${HSCC_PGREP:-pgrep}"

# Package root = parent of the dir holding HSCC_CMD. For the default this is
# ~/.hermes/plugins; for a test stub in a tmp dir it is that tmp dir. Putting it
# on PYTHONPATH is what lets a bare script path import its own package — the
# direct fix for the ModuleNotFoundError.
HSCC_PLUGIN_ROOT="$(CDPATH= cd -- "$(dirname -- "$HSCC_CMD")/.." 2>/dev/null && pwd)"

daemon_pid() {
  [ -f "$HSCC_PID_FILE" ] && cat "$HSCC_PID_FILE" 2>/dev/null || echo ""
}

# True when we can (and should) use the installed `hscc` console script (path
# B): no env override forcing a stub, and `hscc` resolves on PATH.
using_console_script() {
  [ "$HSCC_CMD_SET" -eq 0 ] && [ "$HSCC_PYBIN_SET" -eq 0 ] \
    && command -v hscc >/dev/null 2>&1
}

# Run one daemon CLI subcommand, capturing combined stdout+stderr so a failure
# can surface the real reason (never a silent /dev/null).
run_daemon_cmd() {
  local sub="${1:-}"
  if using_console_script; then
    hscc "$sub" 2>&1
  else
    PYTHONPATH="${HSCC_PLUGIN_ROOT}${PYTHONPATH:+:$PYTHONPATH}" \
      "$HSCC_PYBIN" "$HSCC_CMD" "$sub" 2>&1
  fi
}

# ── service-manager helpers ────────────────────────────────────────────────
# Two supervisors exist for this daemon: launchd (macOS — launchd-setup.sh /
# install.py) and systemd --user (Linux — systemd-setup.sh). Both are handled
# the same way: ask the supervisor whether it owns the service, restart THROUGH
# it, and verify the pid IT reports. Treating "no launchd job" as "no
# supervisor" (the obvious shortcut) would be wrong on every Linux node: the
# systemd-supervised loop would look like an orphan, the restart would be
# refused, and freshly-installed code would silently never load — the original
# bug this script exists to kill, reintroduced by the fix.

# True when we can talk to launchctl at all. No `uname` gate: on Linux there is
# no launchctl binary, `command -v` fails, and we fall through to the next
# supervisor — which is also what lets the tests drive this branch off-platform
# (they point HSCC_LAUNCHCTL at a stub). An explicitly SET HSCC_LAUNCHCTL means
# "launchctl exists" even when it is not on PATH, so a stub can never be
# silently bypassed into the real service.
have_launchctl() {
  if [ "$HSCC_LAUNCHCTL_SET" -eq 1 ]; then
    [ -x "$LAUNCHCTL" ] || command -v "$LAUNCHCTL" >/dev/null 2>&1
  else
    command -v "$LAUNCHCTL" >/dev/null 2>&1
  fi
}

# True when the job is BOOTSTRAPPED in this user's GUI domain. A loaded job with
# no pid still counts as loaded — that is the incident's shape (label alive,
# process orphaned elsewhere), and kickstart is the fix for it too.
launchd_loaded() {
  have_launchctl || return 1
  "$LAUNCHCTL" print "$LAUNCHD_TARGET" >/dev/null 2>&1
}

# The pid launchd currently reports for the job (empty when it has none). Reads
# the `pid = N` line of `launchctl print`, which is the only pid that launchd
# actually supervises — never the pid file, which is a shared label and can be
# written by an orphan fork (see daemon_singleton.py's rationale).
launchd_pid() {
  have_launchctl || return 0
  "$LAUNCHCTL" print "$LAUNCHD_TARGET" 2>/dev/null \
    | sed -n 's/^[[:space:]]*pid[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*$/\1/p' \
    | head -n 1
}

# True when a systemd --user unit for the daemon is active; sets SYSTEMD_UNIT to
# the one it found (and clears it when none is). Honours an explicit
# HSCC_SYSTEMD_UNIT instead of probing when the operator/tests name the unit.
systemd_loaded() {
  if [ -n "$SYSTEMD_UNIT" ]; then
    if [ "$HSCC_SYSTEMCTL_SET" -eq 1 ]; then
      : # an explicitly provided systemctl is trusted even off PATH
    elif ! command -v "$SYSTEMCTL" >/dev/null 2>&1; then
      SYSTEMD_UNIT=""
      return 1
    fi
    if "$SYSTEMCTL" --user is-active --quiet "$SYSTEMD_UNIT" 2>/dev/null; then
      return 0
    fi
    SYSTEMD_UNIT=""
    return 1
  fi
  command -v "$SYSTEMCTL" >/dev/null 2>&1 || return 1
  local u
  for u in $SYSTEMD_UNIT_CANDIDATES; do
    if "$SYSTEMCTL" --user is-active --quiet "$u" 2>/dev/null; then
      SYSTEMD_UNIT="$u"
      return 0
    fi
  done
  return 1
}

# The pid systemd reports as the unit's MainPID (empty when it has none) — the
# same "trust only the supervisor's pid" rule as launchd_pid().
systemd_pid() {
  [ -n "$SYSTEMD_UNIT" ] || return 0
  local v
  v="$("$SYSTEMCTL" --user show "$SYSTEMD_UNIT" -p MainPID --value 2>/dev/null | tr -d '[:space:]')"
  case "$v" in
    ''|0|0.0) printf '' ;;
    *) printf '%s' "$v" ;;
  esac
}

# Which supervisor owns the service right now: "launchd", "systemd", or "". The
# ownership probe is memoised (one probe run per invocation), so the poll loop
# re-reads only the pid. launchd wins if both somehow claim it: this script's
# incident is a macOS one and `kickstart -k` is the stronger of the two restart
# verbs.
SUPERVISOR=""
SUPERVISOR_PROBED=0
detect_supervisor() {
  [ "$SUPERVISOR_PROBED" -eq 1 ] && return 0
  SUPERVISOR_PROBED=1
  if launchd_loaded; then
    SUPERVISOR="launchd"
  elif systemd_loaded; then
    SUPERVISOR="systemd"
  fi
}

# The pid the owning supervisor reports ("" when it has none / no supervisor).
supervisor_pid() {
  case "$SUPERVISOR" in
    launchd) launchd_pid ;;
    systemd) systemd_pid ;;
    *) printf '' ;;
  esac
}

# Ask the owning supervisor to restart the service. Echoes its output; returns
# its exit code. `-k` kills the current instance first so a daemon that ignores
# SIGTERM still turns over; systemd's `restart` is that verb natively. Either
# way the supervisor re-execs the unit's own program definition, which is the
# whole point: we never choose the interpreter ourselves.
restart_via_supervisor() {
  case "$SUPERVISOR" in
    launchd) "$LAUNCHCTL" kickstart -k "$LAUNCHD_TARGET" 2>&1 ;;
    systemd) "$SYSTEMCTL" --user restart "$SYSTEMD_UNIT" 2>&1 ;;
    *) return 1 ;;
  esac
}

# How the restart was performed, for the outcome line.
supervisor_name() {
  case "$SUPERVISOR" in
    launchd) printf 'launchd kickstart' ;;
    systemd) printf 'systemd restart' ;;
    *) printf 'the service manager' ;;
  esac
}

# The restart command, spelled out for the failure/WARN lines so the operator
# can paste it.
supervisor_command() {
  case "$SUPERVISOR" in
    launchd) printf 'launchctl kickstart -k %s' "$LAUNCHD_TARGET" ;;
    systemd) printf 'systemctl --user restart %s' "$SYSTEMD_UNIT" ;;
    *) printf 'the service restart command' ;;
  esac
}

# Every live daemon-loop process the supervisor does NOT own. The pattern is
# deliberately narrow: `hscc start` / `hscc.py start-daemon` (with the word
# `hscc` at a path/word boundary) and nothing else, so `hscc api start`,
# `hscc project chat`, an editor's shell integration or this script's own argv
# never match. Must run AFTER detect_supervisor so the owned pid is excluded —
# the supervised daemon is always visible to pgrep and is never an orphan.
# Fails OPEN when pgrep is unavailable (CI container, exotic OS): a guard that
# refused to install on a machine without pgrep would be worse than the twin it
# hunts, and on such a machine there is no daemon to twin.
ORPHAN_PIDS=""
find_orphan_daemons() {
  ORPHAN_PIDS=""
  if [ "$HSCC_PGREP_SET" -eq 1 ]; then
    [ -x "$PGREP_BIN" ] || command -v "$PGREP_BIN" >/dev/null 2>&1 || return 0
  else
    command -v "$PGREP_BIN" >/dev/null 2>&1 || return 0
  fi
  local owned p
  owned=" $(supervisor_pid) "
  while read -r p; do
    [ -n "$p" ] || continue
    [ "$p" = "$$" ] && continue
    case "$owned" in *" $p "*) continue ;; esac
    ORPHAN_PIDS="${ORPHAN_PIDS:+$ORPHAN_PIDS }$p"
  done < <("$PGREP_BIN" -f 'hscc(\.py)?[[:space:]]+start(-daemon)?([[:space:]]|$)' 2>/dev/null)
}

# One readable list for the outcome line ("27723" / "32582 40984").
orphan_list() {
  [ -n "$ORPHAN_PIDS" ] && printf '%s' "$ORPHAN_PIDS" || printf 'unknown'
}

if $SKIP; then
  echo "daemon restart skipped (--skip-daemon)"
  exit 0
fi

# ── Path S: a supervisor owns the service -> restart through it ────────────
detect_supervisor
if [ -n "$SUPERVISOR" ]; then
  find_orphan_daemons
  PID0="$(supervisor_pid)"

  RESTART_OUT="$(restart_via_supervisor)"
  RESTART_RC=$?
  ERR="$(printf '%s\n' "$RESTART_OUT" | sed '/^[[:space:]]*$/d' | head -n 4 | tr '\n' ' ' | sed 's/[[:space:]]*$//')"

  if [ "$RESTART_RC" -ne 0 ]; then
    echo "daemon restart failed: $(supervisor_command) exited $RESTART_RC — new code may not be loaded${ERR:+ (${ERR})}"
    exit 3
  fi

  # Poll for the new pid rather than racing it. Two supervisor behaviours make
  # a single read unreliable:
  #   * immediately after `-k` launchd can STILL report the old pid (the process
  #     is mid-SIGTERM), so one read could bless a pid that is about to die;
  #   * the plist sets ThrottleInterval=30, so after the kill launchd may report
  #     NO pid for up to ~30s before re-spawning.
  # So we keep reading until the supervisor reports a pid that is not the old
  # one, and only fall back to "unchanged"/"no pid" once the budget is spent.
  # The budget covers the throttle window: 45 x 1s worst case, reached only when
  # the daemon is slow or gone (the normal case turns over within ~2s).
  PID1=""
  for _ in $(seq 1 "$LAUNCHD_POLL_ATTEMPTS"); do
    CAND="$(supervisor_pid)"
    if [ -n "$CAND" ] && { [ -z "$PID0" ] || [ "$CAND" != "$PID0" ]; }; then
      PID1="$CAND"
      break
    fi
    [ -n "$CAND" ] && PID1="$CAND"   # remembered for the unchanged verdict
    sleep "$LAUNCHD_POLL_INTERVAL"
  done

  if [ -z "$PID1" ]; then
    echo "daemon did not come back after restart — $SUPERVISOR reports no pid for the service; new code may not be loaded${ERR:+ (${ERR})}"
    exit 3
  fi

  if [ -n "$PID0" ] && [ "$PID0" = "$PID1" ]; then
    echo "daemon PID unchanged after restart via $(supervisor_name) (${PID0}) — new code may not be loaded${ERR:+ (${ERR})}"
    exit 2
  fi

  if [ -n "$PID0" ]; then
    OUT="daemon restarted via $(supervisor_name) (pid ${PID0} -> ${PID1})"
  else
    OUT="daemon started via $(supervisor_name) (pid ${PID1})"
  fi

  if [ -n "$ORPHAN_PIDS" ]; then
    # The supervised daemon IS on the new code, but an unsupervised twin is
    # still ticking beside it — say so and fail the step (caller warns) so this
    # never passes as a clean restart.
    echo "WARN unsupervised daemon process (pid $(orphan_list)) is running outside $SUPERVISOR — refusing to treat this as clean: kill it ($(supervisor_command) supervises only pid ${PID1}). ${OUT}"
    exit 4
  fi

  echo "$OUT"
  exit 0
fi

# ── Path A/B: no service loaded (fresh machine, CI) -> the daemon's own CLI ─
if ! using_console_script && [ ! -f "$HSCC_CMD" ]; then
  echo "daemon restart: hscc command not found ($HSCC_CMD) — new code may not be loaded; re-run hscc_daemon setup manually"
  exit 3
fi

find_orphan_daemons
if [ -n "$ORPHAN_PIDS" ]; then
  # A daemon is live and NOTHING supervises it. `hscc stop`/`hscc start` here
  # would either leave it running (start refuses to double-start) or add a
  # second loop beside it, and on a Mac it would start that loop under the
  # console script's interpreter — the TCC-blind orphan from the 2.14.0
  # incident. Refuse, name the pid, and hand the operator the exact fix.
  echo "WARN daemon restart blocked: unsupervised daemon process (pid $(orphan_list)) is running and no daemon service is loaded — refusing to start a second loop beside it. Install/load the service and restart it supervised: launchctl kickstart -k $LAUNCHD_TARGET (macOS; or bootout+bootstrap ~/Library/LaunchAgents/${LAUNCHD_LABEL}.plist) or systemctl --user restart hscc_daemon.service (Linux), or kill $(orphan_list) first"
  exit 4
fi

PID0="$(daemon_pid)"

STOP_OUT="$(run_daemon_cmd stop)"
# `start` MUST NOT be captured via $(...): `hscc start` double-forks and the
# daemon grandchild inherits stdout, so the command-substitution pipe never
# reaches EOF while the daemon runs — the subshell blocks forever, which is
# exactly the bootstrap hang (>10 min, no summary) this script fixes. So we
# DETACH the daemon instead: run start with stdin from /dev/null and stdout/
# stderr redirected to a temp regular file. The forked daemon inherits fd 1/2
# pointing at a regular file, not a pipe (so no EOF-wait), the parent `hscc
# start` writes its banner there and returns immediately, and we read the file
# back afterwards so real errors still surface in the outcome line. We drop the
# tmp after reading; the daemon keeps a harmless fd to the unlinked file.
START_LOG="$(mktemp "${TMPDIR:-/tmp}/hscc-restart-start.XXXXXX")"
run_daemon_cmd start </dev/null >"$START_LOG" 2>&1
START_OUT="$(cat "$START_LOG")"
rm -f "$START_LOG"
# Real reason a restart may have failed, trimmed to a readable single line
# (first non-blank lines of captured stop/start output).
ERR="$(printf '%s\n' "$STOP_OUT" "$START_OUT" | sed '/^[[:space:]]*$/d' | head -n 4 | tr '\n' ' ' | sed 's/[[:space:]]*$//')"

# The freshly-forked daemon writes its pid file slightly asynchronously; poll a
# few times so we read the real new PID rather than racing it.
PID1=""
for _ in $(seq 1 "$POLL_ATTEMPTS"); do
  PID1="$(daemon_pid)"
  [ -n "$PID1" ] && break
  sleep "$POLL_INTERVAL"
done

if [ -z "$PID1" ]; then
  echo "daemon did not come back after restart — new code may not be loaded${ERR:+ (${ERR})}"
  exit 3
fi

if [ -n "$PID0" ] && [ "$PID0" != "$PID1" ]; then
  echo "daemon restarted (pid ${PID0} -> ${PID1})"
  exit 0
fi

if [ -z "$PID0" ]; then
  # was not running before (fresh install) — nothing to turn over; new code is
  # loaded and running, which is the whole point
  echo "daemon started (pid ${PID1})"
  exit 0
fi

echo "daemon PID unchanged after restart (${PID0}) — new code may not be loaded${ERR:+ (${ERR})}"
exit 2
