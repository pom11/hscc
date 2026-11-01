"""Doctor coverage for daemon SUPERVISION (GAP-F-5, audit F).

The question this answers — the one the t_61457fbd incident rule exists to
catch and that NOTHING runtime-side asks today: **is the daemon process the
pid file names actually the process the service manager owns?**

The incident class: ``hscc start`` (or a TCC-blind bootstrap) leaves an
unsupervised orphan daemon-loop running with PPID 1 beside — or instead of —
the launchd/systemd job. Stream state stays fresh (the orphan ticks!), so
every existing doctor/verify row reads green: ``check_daemon_streams`` sees
recent stamps, and the twin scan in ``daemon_singleton.py:521`` counts LOOP
PROCESSES, is wired only into ``hscc status`` (cli.py:478), and does not ask
who OWNS them. A machine where nothing supervises the daemon — no restart on
crash, no kickstart target for bootstrap — looked healthy.

So this check does the one comparison nobody does: the pid in
``~/.hscc/daemon.pid`` versus the pid the owning supervisor reports, using the
EXACT probes ``restart_daemon.sh`` already trusts for its pid-turnover poll
(``launchctl print gui/<uid>/com.hermes.hscc_daemon`` → ``pid = N``;
``systemctl --user is-active`` + ``show -p MainPID --value``). Reusing those
verbs — and the same label/unit candidates (``com.hermes.hscc_daemon``,
``hscc_daemon.service``) — means the check and the restart script can never
disagree about who owns the service.

VERDICTS (ok/warn/skip like the existing rows; fatal=False on every non-skip:
doctor is bootstrap's Stage-1 gate and the daemon is legitimately not running
before the install has started the job — fatal would stop an install over a
service that has not had a chance to exist, the same reasoning as
``orch_waker.doctor_check``)
--------------------------------------------------------------------------------
* ``supervised``     — supervisor pid == pid-file pid. ok.
* ``orphan``         — the pid-file pid is ALIVE while its supervisor does NOT
  report it (no job loaded, or the loaded job has another/no pid). THE
  t_61457fbd class: a loop the service manager cannot restart. warn.
* ``mismatch``       — supervisor and pid file both report a pid and they
  differ: whatever the pid file names is NOT the supervised daemon. warn.
* ``stale pid file`` — supervised daemon runs, but the pid file's pid is dead
  / unreadable: the file drifted behind reality. warn.
* ``not running``    — neither supervisor nor pid file shows a live loop. warn
  (not fatal): the honest verdict is "nothing is running", NOT "supervision is
  broken" — and doctor runs before the daemon exists by design.
* ``skipped``        — no service manager to ask at all (no launchctl and no
  systemctl: CI container, exotic OS). ok, says skipped — a guard that refused
  to run where there is no service manager would be worse than the orphan it
  hunts (same fail-open rule as restart_daemon.sh's pgrep probe).

READ-ONLY BY CONSTRUCTION: ``launchctl print`` / ``systemctl is-active`` /
``systemctl show`` and ``kill(pid, 0)`` are all pure probes — no kickstart, no
restart, no write path in this file. In a pytest process the whole check is
refused (rendered as an ERROR naming the seam) unless a verdict is injected:
its default targets are the OPERATOR's live launchd job and ``~/.hscc/
daemon.pid``.

Address discipline: no LAN/tailnet address appears here.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Optional

CHECK_NAME = "daemon supervision (service manager owns the pid)"

#: The job identifiers, identical to restart_daemon.sh's defaults so the
#: check and the restart script cannot point at different services.
LAUNCHD_LABEL = os.environ.get("HSCC_LAUNCHD_LABEL", "com.hermes.hscc_daemon")
SYSTEMD_UNIT_CANDIDATES = ("com.hermes.hscc_daemon.service",
                           "hscc_daemon.service")

#: ``launchctl print`` reports the current pid as an indented ``pid = 12345``
#: property line. Same shape restart_daemon.sh's sed matches.
_LAUNCHCTL_PID_RE = re.compile(r"^\s*pid\s*=\s*(\d+)\s*$", re.MULTILINE)


def _pid_file_path(hscc_dir=None) -> str:
    """The daemon pid file, honouring the HSCC_DIR sandbox convention.

    Same rule as ``orch_waker.doctor_check(hscc_dir=...)``: a sandboxed
    bootstrap keeps HOME real and relocates only HSCC_DIR, so every read must
    resolve under the sandbox or doctor fires verdicts over the OPERATOR's
    live daemon while looking at the sandbox's absent state. Resolved at CALL
    time via expanduser so the suite's tmp redirect moves it.
    """
    if hscc_dir:
        return os.path.join(os.path.expanduser(hscc_dir), "daemon.pid")
    return os.path.expanduser("~/.hscc/daemon.pid")


def read_pid_file_pid(pid_file) -> Optional[int]:
    """The pid recorded in the daemon pid file, or None (absent/unparseable)."""
    try:
        with open(pid_file) as f:
            raw = f.read().strip()
    except OSError:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def pid_alive(pid) -> bool:
    """Pure liveness probe (POSIX signal 0). Never raises."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, different owner — alive enough to supervise
    except (OSError, ValueError, TypeError):
        return False
    return True


def _run(argv, timeout=10):
    """Run a read-only probe; return (returncode, output) or (None, error).

    ``returncode is None`` means the probe itself could not run (binary
    missing, timeout) — an environment fault the caller must not read as a
    statement about the service.
    """
    try:
        cp = subprocess.run([str(a) for a in argv], capture_output=True,
                            text=True, timeout=timeout)
        return cp.returncode, (cp.stdout or "") + (cp.stderr or "")
    except FileNotFoundError:
        return None, f"command not found: {argv[0]}"
    except (subprocess.SubprocessError, OSError) as exc:
        return None, str(exc)


def _launchd_probe(_run_probe=None):
    """(supervisor_kind, pid|None, note) from launchd, per restart_daemon.sh.

    rc 0 from ``launchctl print`` = the job is LOADED (the ownership claim);
    the ``pid = N`` line inside it = the pid it currently runs. A loaded job
    mid-throttle legitimately has no pid line yet.
    """
    runner = _run_probe or _run
    target = f"gui/{os.getuid()}/{LAUNCHD_LABEL}"
    rc, out = runner(["launchctl", "print", target])
    if rc is None:
        return None, None, f"launchctl probe unavailable: {out}"
    if rc != 0:
        return "launchd", None, "job not loaded"
    m = _LAUNCHCTL_PID_RE.search(out)
    return "launchd", (int(m.group(1)) if m else None), "job loaded"


def _systemd_probe(_run_probe=None):
    """(supervisor_kind, pid|None, note) from systemd --user, same candidates
    and verbs as restart_daemon.sh (is-active to claim, show -p MainPID)."""
    runner = _run_probe or _run
    rc, out = runner(["systemctl", "--version"])
    if rc is None:
        return None, None, f"systemctl probe unavailable: {out}"
    for unit in SYSTEMD_UNIT_CANDIDATES:
        rc, _ = runner(["systemctl", "--user", "is-active", "--quiet", unit])
        if rc == 0:
            rc2, out2 = runner(
                ["systemctl", "--user", "show", unit, "-p", "MainPID",
                 "--value"])
            pid = None
            if rc2 == 0:
                v = (out2 or "").strip()
                if v.isdigit() and int(v) > 0:
                    pid = int(v)
            return "systemd", pid, f"unit {unit} active"
    return "systemd", None, "no daemon unit active"


def check(hscc_dir=None, pid_file=None, _launchd=None, _systemd=None,
          _pid_alive=None) -> dict:
    """The supervision verdict as a Check-shaped dict (five doctor fields).

    ``_launchd`` / ``_systemd`` inject the supervisor probes (tests never ask
    the real service manager); ``_pid_alive`` injects liveness; ``pid_file``
    relocates the pid file. With none injected these read the live machine —
    which is exactly what a human ``hscc doctor`` run must do, and what a
    pytest process must never do (doctor.py fences that at the seam).
    """
    alive = _pid_alive or pid_alive
    path = pid_file or _pid_file_path(hscc_dir)
    file_pid = read_pid_file_pid(path)

    def _safe(fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - a probe fault is data, not ok
            return (None, None, f"probe error: {exc}")

    launchd = _safe(_launchd or _launchd_probe)
    systemd = _safe(_systemd or _systemd_probe)

    kind = None
    sup_pid = None
    notes = []
    probe_faults = 0
    for label, (k, p, note) in (("launchd", launchd), ("systemd", systemd)):
        notes.append(f"{label}: {note}")
        if k is None:
            # Manager absent or its probe could not run at all.
            probe_faults += 1
            continue
        if p is not None:
            # launchd wins if both claim the service (same precedence as
            # restart_daemon.sh's detect_supervisor); otherwise the manager
            # that reports a pid is the owner.
            if sup_pid is None or label == "launchd":
                kind, sup_pid = label, p
        elif kind is None:
            kind = label

    def _fix(cmd_hint):
        return (f"restart it through the service manager: "
                f"`launchctl kickstart -k gui/$(id -u)/{LAUNCHD_LABEL}` "
                f"(macOS) or `systemctl --user restart "
                f"{SYSTEMD_UNIT_CANDIDATES[0]}` (Linux), or re-run bootstrap "
                f"({cmd_hint})")

    if file_pid is not None and alive(file_pid):
        if sup_pid is not None and sup_pid == file_pid:
            return {"name": CHECK_NAME, "ok": True, "fatal": False,
                    "detail": f"supervised — {kind} pid {sup_pid} == "
                              f"pid-file pid ({path})", "fix": ""}
        # A live loop the supervisor does not own (different pid or none at
        # all): the t_61457fbd orphan class. The ONLY escape is a machine with
        # no service manager at all (both probes unavailable — CI container,
        # exotic OS): there supervision is not a thing that can be armed, and
        # a guard that failed there would be worse than the orphan it hunts.
        # A manager that EXISTS but does not own this pid is never skipped —
        # that is precisely the incident.
        if probe_faults == 2:
            return {"name": CHECK_NAME, "ok": True, "fatal": False,
                    "detail": "skipped — no service manager to ask ("
                              + "; ".join(notes) + f"); daemon pid {file_pid} "
                              "runs unsupervised by necessity (CI/container)",
                    "fix": ""}
        who = (f"supervisor reports pid {sup_pid}" if sup_pid is not None
               else "no supervisor owns the job")
        return {"name": CHECK_NAME, "ok": False, "fatal": False,
                "detail": f"ORPHAN DAEMON — pid-file pid {file_pid} "
                          f"({path}) is alive but {who}: nothing supervises "
                          "or restarts that process (the t_61457fbd class; "
                          "stream state can look fresh while it is dead on "
                          "crash)",
                "fix": _fix("hscc-bootstrap/bootstrap.sh")}
    if sup_pid is not None and alive(sup_pid):
        # Supervised daemon runs; the pid file is absent, dead or different.
        if file_pid is None:
            return {"name": CHECK_NAME, "ok": False, "fatal": False,
                    "detail": f"{kind} runs the daemon at pid {sup_pid} but "
                              f"the pid file ({path}) is absent or "
                              "unparseable — readers that key off daemon.pid "
                              "(orch-waker, restart) are blind",
                    "fix": _fix("restart the daemon so it rewrites the pid "
                                "file")}
        return {"name": CHECK_NAME, "ok": False, "fatal": False,
                "detail": f"pid MISMATCH — {kind} owns pid {sup_pid}, the pid "
                          f"file ({path}) names dead/foreign pid {file_pid}",
                "fix": _fix("restart the daemon so it rewrites the pid file")}
    if probe_faults == 2:
        return {"name": CHECK_NAME, "ok": True, "fatal": False,
                "detail": "skipped — no service manager to ask ("
                          + "; ".join(notes) + ")", "fix": ""}
    return {"name": CHECK_NAME, "ok": False, "fatal": False,
            "detail": "not running — neither the service manager nor "
                      f"the pid file ({path}) shows a live daemon loop; "
                      "nothing needs supervising YET, but nothing is running "
                      "either",
            "fix": _fix("hscc start is NOT the answer here — install/load "
                        "the service first")}
