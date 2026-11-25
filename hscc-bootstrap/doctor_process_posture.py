"""Doctor coverage for the daemon's SCHEDULING POSTURE (card t_8b17cb40).

The question this answers — the one PR #791 left open: **is the INSTALLED job
actually running the daemon in the Standard posture?** The three plist emitters
are pinned by ``hscc_daemon/tests/test_daemon_process_type_standard_t_ae87b783.py``,
but a pin on an emitter says nothing about the file that sits on the node. The
fleet ran ``ProcessType => Background`` until 2026-11-25 and every node keeps
that posture until it re-bootstraps, and while it does every merge-queue gate
pays ~7.6x silently.

Measured on this host, throwaway probe jobs bootstrapped into ``gui/<uid>`` each
running a fixed CPU loop, then booted out again (``launchctl print`` + ``ps -o
pri``, 2026-11-25):

  plist ProcessType   ps -o pri   launchctl "spawn type"
  Standard              20        daemon (3)
  (key absent)          20        daemon (3)
  Adaptive               4        adaptive (6)
  Background             4        background (5)
  Interactive           31        interactive (4)

WHICH SOURCE THE ROW READS, AND WHY (the card asks for this explicitly)
-----------------------------------------------------------------------
*The plist file, with ``plistlib``, as the authority* — ``launchctl print`` has
no process-type line at all (its ``jetsam priority = 40`` is identical for every
posture above, so it cannot be the signal; the card measured the same). The
plist is also the only source that survives the case this row exists for: a job
that is not currently loaded still has a posture, and it is the posture the next
bootstrap will apply.

*plus the loaded job's ``spawn type`` as a corroborator.* ``launchctl print``
does not echo ProcessType, but the ``spawn type`` line is derived from it and the
table above shows it discriminates all five postures. Reading it buys the one
case a plist-only row would read green on: the operator (or a bootstrap) has
already written ``Standard`` into the file, but the job launchd LOADED is still
the old Background one — the node keeps paying the tax until someone re-bootstraps.
That mismatch gets its own verdict below.

VERDICTS (five doctor fields, fatal=False on every non-skip: doctor is
bootstrap's Stage-1 gate, and a node may legitimately run another posture)
--------------------------------------------------------------------------
* ``Standard``       — plist says Standard and the loaded job agrees. ok.
* ``deprioritised``  — plist says Background/Adaptive (measured pri 4): the
  ~7.6x gate tax. warn.
* ``over-prioritised`` — plist says Interactive (measured pri 31), the
  over-reach t_ae87b783 removed from ``hscc_daemon/install.py``. warn.
* ``unpinned``       — no ProcessType key at all. Measured identical to
  Standard today (pri 20, spawn type daemon), so nothing is being taxed; warn
  because no emitter writes a keyless plist, so the posture is one hand-edit
  away from Background with nothing pinning it.
* ``stale bootstrap`` — the file says one posture and the LOADED job runs
  another: the fix is a re-bootstrap, not an edit. warn.
* ``unreadable``     — the job is loaded (launchd says so) but no plist could
  be read to say what it declares. warn, and name the probe that said "loaded".
* ``skipped``        — nothing to check: no launchctl and no systemctl, or no
  installed job and no loaded job. ok, says skipped (same fail-open rule as
  ``doctor_supervision.check``: a guard that refused where no service manager
  exists is worse than the drift it hunts).

Linux
-----
systemd --user has no ``ProcessType``; the comparable knobs are ``Nice=`` and
``CPUWeight=``, and NOTHING in this repo writes either (grep for ``Nice=`` /
``CPUWeight=`` / ``CPUQuota=`` across the emitters returns nothing), so the
fleet's expected answer is systemd's own default: ``Nice=0`` / ``CPUWeight=100``.
The row therefore asks ``systemctl --user show <unit> -p Nice -p CPUWeight`` for
the unit ``doctor_supervision`` already probes and warns when either knob says
the daemon is being given LESS cpu than the default. This half is pinned by
injected stubs only — there is no systemd on this host — and the tests say so.

READ-ONLY BY CONSTRUCTION: ``plistlib.load`` on the installed file,
``launchctl print`` and ``systemctl show``. No bootout, no bootstrap, no
kickstart, no write path in this file. In a pytest process the check refuses
(rendered as an ERROR naming the seams) unless the path/probes are injected: its
default targets are the OPERATOR's live launchd job and their real plist.

Address discipline: no LAN/tailnet address appears here.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from typing import Optional

# The in-suite fence is ONE predicate, shared with the row registry: see
# ``doctor_rows.under_pytest`` for why the module-local test is not enough.
import doctor_rows  # flat sibling, next to this module (hscc-bootstrap/)

#: The row's printed name. Kept here (not only in doctor.py) so the verdict and
#: the row cannot drift; doctor.py carries a fallback constant for the case where
#: this module itself is not importable, and a test pins the two equal.
CHECK_NAME = "daemon scheduling posture (launchd ProcessType=Standard)"

#: The posture the fleet runs. t_ae87b783 (operator decision 2026-11-25) made
#: all three plist emitters emit this value; this row is the half that checks
#: the node agrees. Deliberately a literal here, not a re-read of an emitter:
#: an emitter that drifted would otherwise define its own expected value.
STANDARD_PROCESS_TYPE = "Standard"

#: ProcessType values that give the daemon LESS cpu than Standard, with the
#: measured ``ps -o pri`` of a child of such a job (see the module docstring).
DEPRIORITISED = {"Background": 4, "Adaptive": 4}

#: ProcessType values that give it MORE than the fleet decided (measured pri 31
#: for Interactive): the over-reach, not the tax.
OVER_PRIORITISED = {"Interactive": 31}

#: The plist key, spelled once.
PROCESS_TYPE_KEY = "ProcessType"

#: ``launchctl print``'s derived line, e.g. ``spawn type = background (5)``.
_SPAWN_TYPE_RE = re.compile(r"^\s*spawn type\s*=\s*([a-zA-Z]+)", re.MULTILINE)

#: ``launchctl print`` reports the plist it loaded the job FROM — the path this
#: row reads when the conventional one is not the one in use.
_PLIST_PATH_RE = re.compile(r"^\s*path\s*=\s*(\S+\.plist)\s*$", re.MULTILINE)

#: ``systemctl show -p Nice -p CPUWeight`` answers ``Key=value`` lines.
_SYSTEMD_PROP_RE = re.compile(r"^(Nice|CPUWeight)=(\d+)\s*$", re.MULTILINE)


def _launchd_label() -> str:
    """The job label — the SAME value the supervision row probes.

    Imported from ``doctor_supervision`` (flat sibling) rather than re-typed, so
    the two rows can never ask about different jobs; that module is also the one
    ``restart_daemon.sh``'s label is pinned against.
    """
    try:
        import doctor_supervision  # noqa: PLC0415 - flat sibling
        return doctor_supervision.LAUNCHD_LABEL
    except Exception:  # noqa: BLE001 - an absent sibling must not blind the row
        return os.environ.get("HSCC_LAUNCHD_LABEL", "com.hermes.hscc_daemon")


def installed_plist_path(label=None) -> str:
    """The conventional installed plist for the daemon job.

    Resolved at CALL time through ``expanduser`` (so the suite's tmp redirect
    moves it, the same discipline as ``doctor_supervision._pid_file_path``) and
    named from the label rather than re-typed.
    """
    return os.path.join(os.path.expanduser("~/Library/LaunchAgents"),
                        f"{label or _launchd_label()}.plist")


def read_process_type(path):
    """(declared|None, note) — the plist's ProcessType, or None with the reason.

    ``(None, "absent")`` means the file parsed and simply has no key; any other
    note means the file could not be read or parsed at all, which the caller must
    never report as "the posture is X". The except is deliberately broad: a
    truncated or hand-mangled plist raises through ``plistlib`` as an
    ``ExpatError`` (measured, python 3.14), and a doctor row that propagates it
    goes quiet on exactly the box that has the problem.
    """
    try:
        with open(path, "rb") as f:
            data = plistlib.load(f)
    except FileNotFoundError:
        return None, "no such file"
    except Exception as exc:  # noqa: BLE001 - a broken plist is data, not a crash
        return None, f"unreadable: {type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return None, "not a property list dict"
    if PROCESS_TYPE_KEY not in data:
        return None, "absent"
    value = data[PROCESS_TYPE_KEY]
    if not isinstance(value, str):
        return None, f"not a string ({value!r})"
    return value, "read"


def _run(argv, timeout=10):
    """Run a read-only probe; return (returncode, output) or (None, error).

    ``returncode is None`` means the probe could not run (binary missing,
    timeout) — an environment fault the caller must not read as a statement
    about the job. Same contract as ``doctor_supervision._run``.
    """
    try:
        cp = subprocess.run([str(a) for a in argv], capture_output=True,
                            text=True, timeout=timeout)
        return cp.returncode, (cp.stdout or "") + (cp.stderr or "")
    except FileNotFoundError:
        return None, f"command not found: {argv[0]}"
    except (subprocess.SubprocessError, OSError) as exc:
        return None, str(exc)


def launchd_probe(_run_probe=None):
    """(available, loaded, spawn_type, plist_path, note) from launchd.

    One ``launchctl print gui/<uid>/<label>`` buys three facts, all read-only:
    rc 0 means the job is LOADED, ``spawn type`` is the posture launchd applied,
    and ``path`` is the plist it read it from.
    """
    runner = _run_probe or _run
    target = f"gui/{os.getuid()}/{_launchd_label()}"
    rc, out = runner(["launchctl", "print", target])
    if rc is None:
        return False, False, None, None, f"probe unavailable: {out}"
    if rc != 0:
        return True, False, None, None, "job not loaded"
    m = _SPAWN_TYPE_RE.search(out)
    spawn = m.group(1).lower() if m else None
    p = _PLIST_PATH_RE.search(out)
    return True, True, spawn, (p.group(1) if p else None), "job loaded"


def systemd_probe(_run_probe=None):
    """(available, active, {Nice, CPUWeight}, note) for the daemon's unit.

    Same unit candidates as ``doctor_supervision`` so the two rows cannot ask
    about different services. ``--user`` because the fleet installs the daemon
    as a user unit.
    """
    runner = _run_probe or _run
    try:
        import doctor_supervision  # noqa: PLC0415 - flat sibling
        units = doctor_supervision.SYSTEMD_UNIT_CANDIDATES
    except Exception:  # noqa: BLE001
        units = ("com.hermes.hscc_daemon.service", "hscc_daemon.service")
    rc, out = runner(["systemctl", "--version"])
    if rc is None:
        return False, False, {}, f"probe unavailable: {out}"
    for unit in units:
        rc, _ = runner(["systemctl", "--user", "is-active", "--quiet", unit])
        if rc != 0:
            continue
        rc2, out2 = runner(["systemctl", "--user", "show", unit,
                            "-p", "Nice", "-p", "CPUWeight"])
        props = {}
        if rc2 == 0:
            for m in _SYSTEMD_PROP_RE.finditer(out2 or ""):
                props[m.group(1)] = int(m.group(2))
        return True, True, props, f"unit {unit} active"
    return True, False, {}, "no daemon unit active"


def verdict(declared, declared_note, loaded, spawn, plist_path,
            systemd_props=None, systemd_active=False, manager="launchd"):
    """The posture verdict from the facts the probes collected.

    Pure: every input is a value, so the ladder is testable without a plist, a
    launchd or a systemd. ``declared`` is the plist's ProcessType (None with
    ``declared_note`` saying why), ``spawn`` the loaded job's launchd spawn type.
    """
    def row(ok, detail, fix=""):
        return {"name": CHECK_NAME, "ok": ok, "fatal": False,
                "detail": detail, "fix": fix}

    rebootstrap = (f"re-bootstrap the job so launchd re-reads the file: "
                   f"`launchctl bootout gui/$(id -u)/{_launchd_label()} && "
                   f"launchctl bootstrap gui/$(id -u) {plist_path}` "
                   f"(or re-run hscc-bootstrap/bootstrap.sh)")

    if manager == "systemd":
        systemd_props = systemd_props or {}
        if not systemd_active:
            return row(True, "skipped — no active systemd --user unit for the "
                             "daemon, so there is no scheduling posture to read")
        nice = systemd_props.get("Nice")
        weight = systemd_props.get("CPUWeight")
        if nice is None and weight is None:
            return row(True, "skipped — systemctl show reported neither Nice= "
                             "nor CPUWeight= for the daemon unit")
        low = []
        if nice is not None and nice > 0:
            low.append(f"Nice={nice} (default 0)")
        if weight is not None and weight < 100:
            low.append(f"CPUWeight={weight} (default 100)")
        shown = ", ".join(f"{k}={v}" for k, v in sorted(systemd_props.items()))
        if low:
            return row(False,
                       f"DEPRIORITISED — the daemon unit runs with "
                       f"{'; '.join(low)}, so the merge-queue gate legs it "
                       f"spawns inherit less cpu than the fleet decided "
                       f"({shown})",
                       "remove the Nice=/CPUWeight= override from the unit (no "
                       "emitter writes either) and `systemctl --user "
                       "daemon-reload && systemctl --user restart <unit>`")
        return row(True, f"posture ok — {shown} at systemd's default "
                         "scheduling knobs (no emitter writes Nice=/CPUWeight=)")

    # ── launchd ───────────────────────────────────────────────────────────
    if declared is None and declared_note != "absent":
        # No readable plist. Only a fault if there is a job to be wrong about.
        if not loaded:
            return row(True, "skipped — no installed launchd job to read "
                             f"({plist_path}: {declared_note}) and none loaded")
        return row(False, f"UNREADABLE — launchd has {_launchd_label()} loaded "
                          f"but the plist it names ({plist_path}) cannot be "
                          f"read ({declared_note}), so the posture cannot be "
                          "verified",
                   "restore the installed plist from a plist emitter "
                   "(hscc_daemon/install.py or launchd-setup.sh) and re-bootstrap")

    if declared == STANDARD_PROCESS_TYPE:
        if loaded and spawn is not None and spawn != "daemon":
            return row(False,
                       f"STALE BOOTSTRAP — {plist_path} declares "
                       f"{STANDARD_PROCESS_TYPE} but the LOADED job runs "
                       f"'spawn type = {spawn}', i.e. launchd is still running "
                       "an older file: the node pays the scheduling tax until "
                       "it re-bootstraps", rebootstrap)
        pri = "20"
        return row(True, f"{STANDARD_PROCESS_TYPE} — ProcessType="
                         f"{STANDARD_PROCESS_TYPE} in {plist_path} (measured "
                         f"child `ps -o pri` {pri}, launchd spawn type daemon; "
                         "the Background/Adaptive band measures 4)")

    if declared in DEPRIORITISED:
        return row(False,
                   f"DEPRIORITISED — {plist_path} declares "
                   f"ProcessType={declared}, which measured `ps -o pri` "
                   f"{DEPRIORITISED[declared]} against Standard's 20: the "
                   "~7.6x tax every merge-queue gate leg pays silently",
                   f"set ProcessType to {STANDARD_PROCESS_TYPE} in {plist_path} "
                   f"(t_ae87b783) and " + rebootstrap)

    if declared in OVER_PRIORITISED:
        return row(False,
                   f"OVER-PRIORITISED — {plist_path} declares "
                   f"ProcessType={declared} (measured `ps -o pri` "
                   f"{OVER_PRIORITISED[declared]}): foreground scheduling for a "
                   "headless supervisor, the over-reach t_ae87b783 removed from "
                   "hscc_daemon/install.py",
                   f"set ProcessType to {STANDARD_PROCESS_TYPE} in "
                   f"{plist_path} and " + rebootstrap)

    if declared is None:      # declared_note == "absent"
        return row(False,
                   f"UNPINNED — {plist_path} carries no {PROCESS_TYPE_KEY} "
                   "key, so the posture is launchd's default (measured equal "
                   "to Standard today: pri 20, spawn type daemon) rather than "
                   "pinned: no emitter writes a keyless plist, and one "
                   "hand-edit to Background costs every gate 3-7x with nothing "
                   "left pinning it",
                   f"add <key>{PROCESS_TYPE_KEY}</key><string>"
                   f"{STANDARD_PROCESS_TYPE}</string> to {plist_path} and " +
                   rebootstrap)

    return row(False, f"UNKNOWN posture — {plist_path} declares "
                      f"ProcessType={declared!r}, which is not a launchd value "
                      f"this row knows (Standard / Adaptive / Background / "
                      f"Interactive)",
               f"set ProcessType to {STANDARD_PROCESS_TYPE} in {plist_path}")


def _probe_value(injected, default, fault) -> tuple:
    """Call an injected probe, or take an injected answer as-is.

    A seam may be a callable (the way ``doctor_supervision`` injects its probes)
    or a plain tuple (the way a table-driven test states the answer). Both are
    data to the caller; the only ambient thing that may be *run* is the default,
    and it is never run when a seam was handed. A probe that raises is a fault
    to report, not a crash to propagate — same rule as ``doctor_supervision``'s
    ``_safe``: the row must never raise, or it goes quiet on the operator's box.
    ``fault`` is the probe's own tuple shape for that case.
    """
    probe = default if injected is None else injected
    try:
        return probe() if callable(probe) else probe
    except Exception as exc:  # noqa: BLE001 - a probe fault is data, not ok
        return fault(f"probe error: {exc}")


def doctor_check(plist_path=None, hscc_dir=None, *, _launchd=None,
                 _systemd=None, _read_plist=None, _platform=None) -> dict:
    """The row's verdict dict (the five doctor ``Check`` fields).

    Everything ambient is injectable — ``plist_path`` relocates the file,
    ``_launchd`` / ``_systemd`` replace the service-manager probes,
    ``_read_plist`` replaces ``plistlib`` — and inside a pytest process the
    check REFUSES unless they are, because its default targets are the
    operator's live job and their real ``~/Library/LaunchAgents`` (see
    ``hscc-daemon-ambient-seam-stub``).

    ``row_kwargs`` for this row is ``()``: the plist is written under HOME by
    ``install.py``/``daemon_install.py``/``launchd-setup.sh``, none of which
    takes ``HSCC_DIR``, so a root lifted from the environment would be a
    directory no emitter can reach and a loaded job would read as "not
    installed" (t_be690204). ``hscc_dir`` is accepted and ignored for the same
    reason the supervision row's is not: it must never decide what is read.
    """
    if _platform is None:
        platform = sys.platform
    else:
        platform = _platform
    if doctor_rows.under_pytest() and (
            plist_path is None and _launchd is None and _systemd is None
            and _platform is None):
        return refused()

    read = _read_plist or read_process_type
    label = _launchd_label()

    if platform == "darwin":
        available, loaded, spawn, launchd_plist, _note = _probe_value(
            _launchd, launchd_probe,
            lambda why: (False, False, None, None, why))
        path = plist_path or launchd_plist or installed_plist_path(label)
        if not available and not loaded:
            return verdict(None, "no launchctl", False, None, path)
        declared, note = read(path)
        return verdict(declared, note, loaded, spawn, path)

    # Not macOS: the systemd knobs are the comparable surface.
    available, active, props, _note = _probe_value(
        _systemd, systemd_probe,
        lambda why: (False, False, {}, why))
    if not available:
        return {"name": CHECK_NAME, "ok": True, "fatal": False,
                "detail": "skipped — no service manager to ask (no launchctl, "
                          f"no systemctl); {os.environ.get('PYTEST_CURRENT_TEST', 'CI/container')}",
                "fix": ""}
    return verdict(None, "systemd", active, None, None,
                   systemd_props=props, systemd_active=active,
                   manager="systemd")


def refused() -> dict:
    """In-suite stand-in when nothing was injected: an ERROR naming the seams."""
    return {"name": CHECK_NAME, "ok": False, "fatal": False,
            "detail": "scheduling-posture check refused inside a pytest "
                      "process — inject plist_path= plus _launchd / _systemd",
            "fix": "Pass plist_path=<tmp plist> and _launchd=lambda: (available, "
                   "loaded, spawn_type, plist_path, note) built from fixtures; a "
                   "suite must never read the operator's ~/Library/LaunchAgents."}
