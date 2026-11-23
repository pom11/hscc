"""Agent-tool surface for the HSCC preflight doctor (card t_7f570861).

ONE read-only tool — ``doctor`` — and deliberately NO repair tool.

The audit (docs/audits/2026-11-23-agent-surface.md) ruled on this surface twice,
and the two rulings are about two different doors:

* **D11 as asked in §10 was about an HTTP route**, and the answer was *no route*
  — ``doctor`` reads *this host's* trees and state, so a route serving it from
  elsewhere would answer a question the caller cannot ask. That is what
  §2.7's table records.
* **§12.2 (the operator verdict, folded in after the merge) closed the question
  explicitly**: "*No route.* It stays ``hscc project doctor`` (it reads this
  host's trees). The read-only agent *tool* is a different surface and stands —
  D11 was about the route." An agent tool runs on the host the doctor reads, so
  the reason the route was refused does not apply to it.

So the READ joins the agent surface and the WRITE does not. ``hscc-bootstrap/
doctor.py`` has exactly one mutating mode, ``--fix``, and it is a real write:
``run_doctor_fix()`` re-runs the checks and then calls
``enable_plugins.enable()`` on the operator's ``config.yaml`` (backup + rewrite
of model/alias keys, plus the Card-D alias migration with its own endpoint
safety gate). That is operator scope — the same class of decision the audit
closed for autodown (§2.4) and the merge queue (D8) — so there is no
``doctor_fix`` tool, and this module has no path to it. What an agent gets is
the verdict: every check with its ``ok`` and its one-line ``detail``, so it can
say *what* is wrong and *what the doctor would run to fix it* — and it stops
there.

## The door: the CLI's own command line, run the way the CLI runs it

``doctor.py`` is not an importable library and there is no ``hscc doctor`` verb
for it (the bare verb was retired in 2.12.0; ``hscc doctor`` in prose means this
script, which ``bootstrap.sh`` Stage 1 execs as
``"$PYBIN" "$BOOT_DIR/doctor.py"``). It is a standalone script whose own
module-level imports are FLAT siblings (``import _theme``, ``import backup_util``,
``import doctor_rows``, ``import doctor_node_recovery``), which resolve only when
``hscc-bootstrap`` is the script directory on ``sys.path`` — the reason
``hscc_daemon/gateway_probe.py`` loads the *one* self-contained bootstrap file it
needs with ``spec_from_file_location`` and states in its docstring why it must
not put that hyphenated dir on the daemon's path ("inserting it globally would
shadow generic module names for the whole daemon process"). ``doctor.py`` carries
about thirty such siblings (``detect``, ``main_board``, ``skill_guard``,
``install_gpu_clock_cap``, …), so an in-process import of it here would put all
of them in the agent host's global namespace for the life of the process —
exactly the hazard that docstring names, at thirty-fold scale. It would also be
*quietly wrong*: ``_load_node_pauses()`` and the ``install_gpu_clock_cap`` block
have no sys.path fallback of their own, so a bootstrap dir that is not reachable
at call time renders a parked node as "module not deployed" and the GPU-clock row
RED — findings the operator's own ``doctor.py`` run does not print.

So this handler runs the operator's own command line as a subprocess:
``[sys.executable, <root>/hscc-bootstrap/doctor.py, "--json"]``. That is
literally "the same way the CLI does", it reads live state at call time (no
second copy of any verdict, and no stale copy of ``~/.hscc`` or the API), and it
inherits the doctor's own posture for everything below the door: the
``fleet_checks`` auto-detect, the per-check subprocess timeouts, the
sparkrun-only transport, the feature-module loaders. The argv is a fixed
literal — a caller cannot add ``--fix``, ``--skills`` or ``--home`` and cannot
steer which tree is read; the door resolves HERMES_HOME from the environment
exactly as the operator's shell does.

## ``rc`` is a verdict here, not a read status

Unlike the other read tools in this plugin, a non-zero exit from this door does
NOT mean "could not read": ``main()`` prints the JSON payload and then exits 1
when a FATAL check failed — the preflight gate that lets ``bootstrap.sh``
hard-stop. So a host with a genuinely broken install produces a *perfectly good
payload* on a non-zero rc. Parsing first and checking rc second would throw the
findings away on exactly the host that needs them, so the order here is
parse-then-report: a parseable payload is returned verbatim (with its own
authoritative ``ok`` and the ``rc`` in the envelope), and only an unparseable
stdout — the door crashing, which is the one case where nothing was answered —
becomes ``ok: false`` plus the raw text (and the stderr, which is where a
traceback lands). Pinned by
``test_a_fatal_verdict_still_returns_the_findings_not_a_read_error``.

## Read-only is the shape of the call

The argv contains no mutating mode and no caller input; the script is invoked
read-only the way ``bootstrap.sh`` invokes it; and nothing in this module writes
anything — not the config, not ``~/.hscc``, not a lock. The doctor's own writes
live behind ``--fix`` (``run_doctor_fix`` → ``enable_plugins.enable`` →
``_write_yaml``), which this module neither names nor can reach.

One honest caveat, stated rather than hidden: the door is the operator's own
command, so it has the side effects that command already has. Its
``uv python find`` probe makes uv maintain a cache under ``$HOME`` — that is
what ``python3 doctor.py`` does on this host today, before and independently of
this tool. What is guaranteed here is that nothing on the HSCC side is written
and no repair path is reachable; the read of ``~/.hscc`` / ``config.yaml`` /
daemon state is inert, which is what the tests snapshot.
"""

import json
import os
import subprocess
import sys

#: No arguments. Every input the doctor has — the Hermes home, the ``~/.hscc``
#: state root, the fleet config — comes from the environment the agent host
#: already runs in, which is the point of the tool (an agent asking about *this*
#: host must not be able to ask about another one). ``additionalProperties:
#: False`` keeps a model from inventing ``fix``/``home``/``confirm``: ``fix``
#: would be the mutating mode this tool exists to withhold, and a ``confirm``
#: key must never become acceptable on a surface that is read-only by contract.
DOCTOR_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

#: The only keys this handler adds on top of the door's payload. The payload
#: already carries ``ok`` / ``checks`` / ``fatal_failures``, so none of them is
#: re-set here — see test_the_envelope_shadows_no_field_of_the_payload.
_ENVELOPE = ("rc", "report")

#: Where the door lives, relative to the shared plugin root: ``hscc_daemon`` and
#: ``hscc-bootstrap`` deploy as sibling plugin dirs (install_payload.
#: DEFAULT_PAYLOAD ships both), so ``<root>`` is two levels up from this file in
#: BOTH layouts — a repo checkout and ``~/.hermes/plugins``. Same relative-layout
#: reasoning as ``hscc_daemon/gateway_probe.py::LIVENESS_REL``.
DOOR_REL = ("hscc-bootstrap", "doctor.py")

#: The fixed argv. ``--json`` is the door's documented machine flag (the same one
#: ``doctor.main`` keys its payload print off, and the one the doctor's own suite
#: drives: ``doctor.main(["--json", "--home", …])``). Nothing else is ever passed
#: and nothing a caller sends reaches this list.
DOOR_ARGV = ("--json",)

#: Upper bound on one doctor run. The door bounds each probe itself (sparkrun
#: subprocesses at 15s, TCP probes at 5s); a healthy host answers in seconds and
#: the checks run in one pass, so this only fires when a probe is hanging on an
#: unreachable network — and a hung door must report as "nothing was answered",
#: never as the last payload someone might have had.
DOOR_TIMEOUT_SECONDS = 120


class DoctorDoorError(RuntimeError):
    """The shipped ``doctor.py`` could not be located — nothing was checked."""


def _candidate_paths(root=None, home=None):
    """Where ``doctor.py`` may live, best match first.

    ``root`` / ``home`` are injectable so a test can pin both deployment layouts
    without touching the caller's real filesystem. The default ``root`` is two
    levels up from this file, which is the repo root in a checkout and the
    plugins dir once deployed; ``home`` is the real home, for a deployment where
    ``hscc-cluster`` was installed without its sibling (reported, not guessed).
    """
    here = os.path.dirname(os.path.abspath(__file__))
    root = root if root is not None else os.path.dirname(here)
    yield os.path.join(root, *DOOR_REL)
    home = home if home is not None else os.path.expanduser("~")
    yield os.path.join(home, ".hermes", "plugins", *DOOR_REL)


def door_source(root=None, home=None):
    """Absolute path to the shipped ``doctor.py``, or None when it is not there."""
    for path in _candidate_paths(root=root, home=home):
        if os.path.isfile(path):
            return path
    return None


def _run_door(door, argv, timeout):
    """Run the door. -> ``(rc, stdout, stderr)``.

    One seam, one purpose: this is the only place in the plugin that spawns this
    process, and it spawns exactly the door script (never a fleet transport —
    ``sparkrun``/``ssh`` stay the doctor's own business, reached only through the
    doctor's own code). ``cwd`` is the door's directory because the script's
    flat sibling imports need their own dir importable, and ``close_fds`` +
    text mode keep the payload a single clean stream.
    """
    proc = subprocess.run(
        [sys.executable, door, *argv],
        cwd=os.path.dirname(door),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _report_findings():
    """Ask the door. -> ``(rc, stdout, stderr)`` with rc ``None`` on a timeout.

    A missing door is raised, not swallowed: ``DoctorDoorError`` names the paths
    it tried, and `doctor_findings` turns that into an explicit "nothing was
    checked" result. A tool that cannot find the doctor must not report a host as
    healthy — the same rule ``gateway_probe.check_gateway`` applies to the
    gateway.
    """
    door = door_source()
    if door is None:
        tried = ", ".join(_candidate_paths())
        raise DoctorDoorError(
            f"cannot find {os.sep.join(DOOR_REL)} (tried: {tried})")
    try:
        return _run_door(door, list(DOOR_ARGV), DOOR_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None, "", (f"doctor timed out after "
                          f"{DOOR_TIMEOUT_SECONDS}s (a probe is hanging; nothing "
                          f"was answered)")


def doctor_findings(args, **kwargs):
    """Every HSCC doctor check as data: name, ok, and the one-line cause.

    Returns the payload ``python3 hscc-bootstrap/doctor.py --json`` prints,
    verbatim — ``ok`` (True when no FATAL check failed), ``checks`` (one row per
    check: ``name``, ``ok``, ``detail`` — the one-line cause — ``fix`` and
    ``fatal``) and ``fatal_failures`` — plus this tool's ``rc`` envelope. The
    ``fix`` text is advice copied from the door's payload, never an action: the
    repair side (``--fix``, which rewrites ``config.yaml`` through
    ``enable_plugins.enable``) stays CLI-only, so an agent can tell the operator
    what to run and cannot run it. Read-only: same verdict as the operator's own
    doctor run, read from live daemon/API/host state at call time.
    """
    try:
        rc, report, err = _report_findings()
    except DoctorDoorError as exc:
        # Nothing was checked. Say so, in a shape that cannot be mistaken for a
        # clean bill of health (no ``checks: []`` — an empty list would read as
        # "all 27 rows green").
        return {"ok": False, "rc": None, "report": str(exc),
                "error": "doctor-unavailable"}
    text = (report or "").strip()
    payload = None
    if text:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed
    if payload is None:
        # The door ran but printed nothing parseable: it crashed, or its --json
        # contract broke. That IS a read failure, and stderr is the only
        # explanation there is — the traceback lands there, not in stdout.
        detail = text or (err or "").strip() or "the doctor printed no output"
        return {"ok": False, "rc": rc, "report": detail,
                "error": "doctor-no-payload"}
    out = dict(payload)
    out["rc"] = rc
    # The payload's own ``ok`` is the doctor's verdict (False on a FATAL row) and
    # stays authoritative; it is deliberately not defaulted to True, because a
    # fatal verdict must never be smoothed over by the tool's own optimism.
    out.setdefault("ok", False)
    return out
