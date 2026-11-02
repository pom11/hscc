"""Single implementation of "is the Hermes gateway alive?" (GAP-F-3, t_c14d18cb).

WHY THIS FILE EXISTS
--------------------
The gateway-liveness verdict used to be computed twice in this repo: once by
the bootstrap doctor row (``doctor._gateway_running``, rewritten in
t_f766ddd5 / PR #148 after it was MEASURED wrong on the operator machine) and
once by the daemon gateway stream (``hscc_daemon.health._gateway_job_alive``),
which still carried the transport that bug came from —
``pgrep -f "hermes.*gateway"`` — as its last-resort leg. The transports now
live HERE and both consumers call into them, so a third gateway-liveness
implementation cannot appear by copying a check.

WHY NOT pgrep (measured, not theoretical)
-----------------------------------------
On the operator machine macOS ``pgrep`` is BLIND to the gateway process: it
never receives the pid, so no pattern can match it. Measured 2026-11-01 with
the gateway live as launchd pid 74857:

    pgrep -f "hermes_cli.main gateway"  -> rc 1
    pgrep -f "gateway"                  -> rc 1
    pgrep -f "run --replace"            -> rc 1
    pgrep -f .                          -> 1210 pids, and 74857 is NOT one of
                                          them (``ps -axww`` lists 1216; the
                                          six pgrep skips are pid 1
                                          /sbin/launchd, the gateway, and four
                                          argv-unreadable helpers)

A pattern cannot match a pid the matcher never receives, so re-calibrating the
pattern could never fix a pgrep-based row: on this box it is structurally
pinned to "no match" while the gateway is up. Every pgrep rc 1 therefore proves
NOTHING about gateway liveness here. pgrep is not invoked anywhere in this
module, and both consumers pin that (``test_no_longer_uses_pgrep`` in the
doctor suite, ``TestGatewayJobAlive`` in the daemon suite).

TRANSPORTS, IN TRUST ORDER
--------------------------
  1. the SUPERVISOR the gateway is installed under — ``launchctl list
     ai.hermes.gateway`` on macOS, ``systemctl --user is-active
     hermes-gateway`` on Linux. The only transport that answers the real
     question ("is the job the fleet launched alive?") rather than "does some
     argv happen to resemble mine?", and the one
     ``hscc-cluster/gateway_restart.py`` already trusts before it kicks the job.
  2. a TOKEN SCAN of ``ps`` argv lines, for the cases with no usable supervisor:
     bootstrap runs before the launchd job is installed, a Linux box may have
     no user session, and a supervisor probe can simply fail. ``ps -axww`` DOES
     show the full argv pgrep cannot read, so this path sees what pgrep cannot.

UNKNOWN IS NOT "NOT RUNNING"
----------------------------
Every probe returns ``None`` when it COULD NOT RUN (binary missing, timeout,
exec error). Callers must treat ``None`` as UNKNOWN and degrade to the next
transport. Collapsing "the probe failed" into "not running" is how GAP-F-3's
false negative stayed green through two real doctor runs.

STDRLIB-ONLY ON PURPOSE
-----------------------
No sibling imports (``_theme``, ``backup_util``, …) so a consumer outside this
plugin dir — ``hscc_daemon/gateway_probe.py`` — can load this file with
``importlib.spec_from_file_location`` without putting ``hscc-bootstrap`` on the
global ``sys.path`` and shadowing generic module names for the whole process.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

_GATEWAY_LAUNCHD_LABEL = "ai.hermes.gateway"
_GATEWAY_SYSTEMD_UNIT = "hermes-gateway"
# The gateway's command form as this fleet launches it (read back from the
# launchd job's ProgramArguments on the operator machine):
#     <venv>/bin/python -m hermes_cli.main gateway run --replace
# and the CLI-wrapper shape `<...>/bin/hermes gateway run --replace`.
# We match TOKENS, not a full-line literal: flags and their position are free
# (`-p qa`, `--replace`, `--foreground` may land before or after the
# subcommand), which is exactly what a literal substring match gets wrong. The
# two subcommand tokens must stay ADJACENT — argparse subparsers cannot parse
# `gateway` / `run` any other way — and adjacency is what keeps a prompt-
# carrying invocation that merely mentions the words from matching.
_GATEWAY_SUBCOMMANDS = ("run", "serve", "start")
# Tokens only a prompt-carrying / non-server invocation has. The gateway is a
# server: it never takes a chat prompt, so `hermes chat -q "why did the gateway
# run twice"` — which contains every token above, adjacent — is not it.
_GATEWAY_NON_GATEWAY_TOKENS = frozenset(
    {"chat", "-q", "--query", "--continue", "--create-if-missing"})
# A shell or a text tool running `grep "gateway run"` is an argv line that
# describes the gateway without being it.
_GATEWAY_NOT_A_SERVER = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "csh", "fish", "grep", "egrep", "ag",
     "awk", "sed", "ps", "pgrep", "pkill", "xargs", "tail", "less", "vim"})
_GATEWAY_LAUNCHD_PID_RE = re.compile(r'^\s*"PID"\s*=\s*(\d+)\s*;', re.M)

#: Public spellings of the two supervisor identities, for consumers that build
#: an operator-facing fix hint (a caller must not reach into an underscore name).
GATEWAY_LAUNCHD_LABEL = _GATEWAY_LAUNCHD_LABEL
GATEWAY_SYSTEMD_UNIT = _GATEWAY_SYSTEMD_UNIT


def gateway_probe_run(argv, *, timeout=5, _run=None):
    """Run a liveness probe; return ``(returncode, stdout)`` or None.

    None means the probe COULD NOT RUN (binary missing, timeout, exec error) —
    never a verdict. Callers must treat it as UNKNOWN, not as "not running":
    a probe that fails open is how GAP-F-3's false negative hid.
    """
    runner = _run or (
        lambda a, t: subprocess.run(a, capture_output=True, text=True, timeout=t))
    try:
        r = runner(list(argv), timeout)
    except (subprocess.SubprocessError, FileNotFoundError, OSError, ValueError):
        return None
    except Exception:                       # a probe that ERRORS is UNKNOWN
        return None                         # — never a "not running" verdict
    if r is None:
        return None
    return getattr(r, "returncode", 1), (getattr(r, "stdout", "") or "")


def gateway_launchd_state(*, _run=None):
    """``(loaded, pid)`` for the gateway's launchd job, on macOS.

    ``(None, None)`` — UNKNOWN, launchd is not usable here (not macOS, or the
    probe could not run); ``(False, None)`` — job not loaded; ``(True, None)``
    — job loaded with no process right now (an OnDemand job between
    invocations); ``(True, pid)`` — the job's process is up.
    """
    if sys.platform != "darwin":
        return (None, None)
    out = gateway_probe_run(["launchctl", "list", _GATEWAY_LAUNCHD_LABEL],
                            _run=_run)
    if out is None:
        return (None, None)
    rc, stdout = out
    if rc != 0:                      # "Could not find service" -> not loaded
        return (False, None)
    m = _GATEWAY_LAUNCHD_PID_RE.search(stdout)
    return (True, int(m.group(1)) if m else None)


def gateway_systemd_state(*, _run=None):
    """``(loaded, pid)`` for the gateway's user service, on Linux.

    Same UNKNOWN convention as `gateway_launchd_state`; pid is never known
    from `is-active`, so a live service reports ``(True, None)``.
    """
    if sys.platform == "darwin" or not shutil.which("systemctl"):
        return (None, None)
    out = gateway_probe_run(["systemctl", "--user", "is-active",
                             _GATEWAY_SYSTEMD_UNIT], _run=_run)
    if out is None:
        return (None, None)
    rc, stdout = out
    state = stdout.strip()
    if state == "active":
        return (True, None)
    if state in ("inactive", "failed"):
        return (False, None)
    return (None, None)              # activating / unknown / unit missing


def gateway_ps_lines(*, _run=None):
    """``ps`` argv lines as ``["<pid> <command>", ...]``, or None if unusable."""
    out = gateway_probe_run(["ps", "-axww", "-o", "pid=,command="], _run=_run)
    if out is None or out[0] != 0:
        return None
    return (out[1] or "").splitlines()


def argv_is_gateway(line: str) -> bool:
    """True if a command line is the Hermes gateway server process.

    Token-based, tolerant of flag order (see `_GATEWAY_SUBCOMMANDS`). Never
    matches: a shell/text tool that merely quotes the pattern, or a
    prompt-carrying Hermes invocation whose prompt happens to contain the
    words (both are excluded by construction, and pinned by tests).
    """
    toks = (line or "").split()
    if not toks:
        return False
    if toks[0].rsplit("/", 1)[-1] in _GATEWAY_NOT_A_SERVER:
        return False
    if _GATEWAY_NON_GATEWAY_TOKENS.intersection(toks):
        return False
    # Identity: the Hermes CLI itself, as a module (`-m hermes_cli.main`) or as
    # the wrapper script (`/…/bin/hermes`). A checker running this probe has
    # neither, so it can never match its own process.
    if not any("hermes_cli" in t or t.rsplit("/", 1)[-1] == "hermes"
               for t in toks):
        return False
    return any(a == "gateway" and b in _GATEWAY_SUBCOMMANDS
               for a, b in zip(toks, toks[1:]))


def gateway_argv_pid(*, _lines=None, _run=None):
    """First pid whose argv looks like the gateway, or None. None-safe."""
    lines = gateway_ps_lines(_run=_run) if _lines is None else _lines
    if lines is None:
        return None
    mine = os.getpid()
    for line in lines:
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        if pid <= 1 or pid == mine:      # never launchd, never ourselves
            continue
        if argv_is_gateway(parts[1]):
            return pid
    return None


class GatewayStates:
    """What the transports actually said — the ONE place they are consulted.

    ``via``      "launchd" | "systemd" | None (no supervisor could answer)
    ``loaded``   True/False from that supervisor, None when UNKNOWN
    ``pid``      the supervisor's pid when it names one, else None
    ``argv_pid`` pid from the `ps` argv token scan, or None
    ``answered`` True when at least one transport RETURNED AN ANSWER (as opposed
                 to being unable to run). This is the difference between
                 "we looked and nothing is there" and "we could not look", and
                 collapsing the two is exactly how GAP-F-3 hid: a probe that
                 fails open keeps reporting a healthy thing as dead forever.

    The two renderers below decide what these fields MEAN; because they both
    read the same object, the doctor row and the daemon stream can no longer
    disagree about what the probes said.
    """

    __slots__ = ("via", "loaded", "pid", "argv_pid", "answered")

    def __init__(self, via, loaded, pid, argv_pid, answered):
        self.via = via
        self.loaded = loaded
        self.pid = pid
        self.argv_pid = argv_pid
        self.answered = answered

    @property
    def supervisor_says_running(self) -> bool:
        """Did a supervisor answer "yes, the job is up"?"""
        return self.pid is not None or self.loaded is True

    @property
    def running(self) -> bool:
        """Is there a gateway process (supervisor pid, or an argv match)?"""
        return self.pid is not None or self.argv_pid is not None


def gateway_states(*, _run=None, _ps_lines=None) -> GatewayStates:
    """Run the transports in trust order and return their raw answers.

    The argv scan is skipped when a supervisor already named a pid — that is
    the authority, and a second opinion would only add a `ps` shell-out per
    check cycle for nothing.
    """
    loaded, pid = gateway_launchd_state(_run=_run)
    via = "launchd"
    if loaded is None:
        loaded, pid = gateway_systemd_state(_run=_run)
        via = "systemd"
    supervisor_answered = loaded is not None
    if not supervisor_answered:               # no usable supervisor at all
        via, loaded = None, None

    argv_pid, ps_answered = None, False
    # Scan argv when the supervisor gave no pid to trust — EXCEPT when systemd
    # says the unit is active: `is-active` answers the question outright and
    # names no pid, so a `ps` shell-out every cycle would only add cost (and the
    # doctor row's contract short-circuits on that state too).
    if pid is None and not (via == "systemd" and loaded is True):
        lines = gateway_ps_lines(_run=_run) if _ps_lines is None else _ps_lines
        ps_answered = lines is not None
        argv_pid = gateway_argv_pid(_lines=lines, _run=_run)
    return GatewayStates(via, loaded, pid, argv_pid,
                         supervisor_answered or ps_answered)


def gateway_verdict(*, _run=None, _ps_lines=None):
    """``(alive, detail, fix_hint)`` for an operator-facing row (doctor).

    Strict reading: a supervisor job that is loaded but has no process is NOT
    running — it gets its own verdict and a kickstart/restart hint instead of
    collapsing into "not running". When no transport could answer at all the
    row prints "not running" because the doctor row is informational and its
    contract is one of exactly two verdicts (as #148 shipped it). The daemon's
    stream has no such constraint and keeps UNKNOWN as a third state — see
    `job_is_alive`.
    """
    st = gateway_states(_run=_run, _ps_lines=_ps_lines)
    if st.pid is not None:
        return True, f"running ({st.via} pid {st.pid})", ""
    if st.loaded and st.via == "systemd":
        # `is-active` answers the question directly and never names a pid, so
        # "active" IS the verdict — do not second-guess it with an argv scan.
        return True, "running (systemd unit active)", ""
    if st.argv_pid is not None:
        return True, (f"running (pid {st.argv_pid}, argv match; "
                      f"{'job loaded, no pid' if st.loaded else 'supervisor not confirmed'})"), ""
    if st.loaded:
        restart = (f"launchctl kickstart -k gui/$UID/{_GATEWAY_LAUNCHD_LABEL}"
                   if st.via == "launchd" else
                   f"systemctl --user restart {_GATEWAY_SYSTEMD_UNIT}")
        return (False, "job loaded but no running process", f"Start it: {restart}")
    return False, "not running", "Start it after bootstrap."


def job_is_alive(*, _run=None, _ps_lines=None):
    """``(alive, detail)`` for the daemon's gateway stream.

    ``alive`` is True / False, or **None when the state is UNKNOWN** — no
    transport could run. None is not a verdict: nothing was read. The consumer
    (``hscc_daemon.health.check_gateway``) treats UNKNOWN as NOT healthy, the
    way ``check_dgx`` treats an unverifiable workload list — a liveness check
    that fails open can never print red, which is how GAP-F-3 hid.

    The job question this answers is "should the fleet consider the gateway job
    present?", and it deliberately matches what ``_gateway_job_alive`` has
    always meant: a supervisor that reports the job as loaded answers "yes"
    (that job is what ``hscc-cluster/gateway_restart.py`` kickstarts to bring
    the process back), and when no supervisor can answer, a `ps` argv match
    answers "yes". The stricter "loaded but no process" reading lives in
    `gateway_verdict`, which is the one that prints the operator a fix hint.

    Detail text is included so a stream that goes not-ok, or that could not be
    verified at all, can say WHICH transport said so instead of guessing.
    """
    st = gateway_states(_run=_run, _ps_lines=_ps_lines)
    if st.pid is not None:
        return True, f"{st.via} pid {st.pid}"
    if st.loaded:
        return True, f"{st.via} job loaded"
    if st.argv_pid is not None:
        return True, f"pid {st.argv_pid} (ps argv match; supervisor not confirmed)"
    if not st.answered:
        return None, "no liveness transport could answer (launchctl/systemctl/ps all failed)"
    return False, "no supervisor job and no matching argv"
