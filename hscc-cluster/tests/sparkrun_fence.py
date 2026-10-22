"""Sparkrun argv fence — shared test harness for the plugin suites.

WHY (live incident, t_be4b94ca, 2026-10-22): the operator's LIVE inference
proxy on :4000 received a graceful SIGTERM twice while ``scripts/run_tests.sh``
was running out of this workspace. Graceful SIGTERM to the stored PID is exactly
what ``sparkrun proxy stop`` does.

The daemon suite was already fenced by construction: every daemon call path
resolves the CLI through ``hscc_daemon.sparkrun_bin``, and
``hscc_daemon/tests/conftest.py::_hermetic_sparkrun_resolution`` (autouse) pins
``HSCC_SPARKRUN_BIN`` to a per-test fake script. ``hscc-cluster/`` (and
``hscc-commands/``) bypass that seam ENTIRELY — they use literal argv
(``subprocess.run(["sparkrun", "proxy", "stop"], ...)`` in cluster_template.py,
``SPARKRUN = "sparkrun"`` in clusterlib.py / hscc.py / discovery.py /
recipe_cost.py / hscc-commands/cmdlib.py) with raw ``subprocess.run``, invisible
to ``HSCC_SPARKRUN_BIN``. Fencing there was per-test
(``patch.object(subprocess, "run", ...)``), so any test, helper, or teardown
that reached one of those sites WITHOUT its own patch exec'd the operator's
REAL sparkrun — which is on PATH on this host.

WHAT THIS DOES
--------------
``install()`` wraps ``subprocess.run`` and ``subprocess.Popen`` for the duration
of one test and raises :class:`SparkrunFenceViolation` — instead of exec'ing —
when a call would run the operator's real sparkrun CLI:

  * the program (argv[0], or the first token of a ``shell=True`` string) is
    named ``sparkrun``, AND
  * what it resolves to — argv[0] itself when it is a path, otherwise the PATH
    lookup — is the live CLI (``realpath(which("sparkrun"))``, captured once).

  Plus: a call whose argv smuggles one of hscc-cluster's inline sparkrun scripts
  (the ``sparkrun_bridge`` remote-exec / proxy.yaml payloads) is blocked too —
  argv[0] there is sparkrun's venv *python*, so the name test above misses it,
  yet the effect (ssh into a fleet node / rewrite the live proxy.yaml) is worse.

A per-test FAKE sparkrun (a script the test wrote under its own ``tmp_path``)
resolves somewhere else, so it passes: the fence blocks the operator's CLI, not
the notion of a sparkrun command. Tests that patch ``subprocess.run``/``Popen``
themselves intercept before this wrapper and are unaffected — exactly like the
daemon suite's own fakes.

RESIDUAL RISK, stated plainly: a test that patches ``subprocess`` with a mock and
asserts on argv never reaches the fence at all (that is the intent — it execs
nothing), and a generic ``python -c "import sparkrun; ..."`` is not a
sparkrun-named argv. ``test_sparkrun_fence.py`` pins both directions of the
detection, and ``report()`` prints what the fence saw at the end of every suite
run, so a fence that silently did nothing stays visible.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys

#: Calls that reached the real exec layer, per-test (conftest clears it):
#: {"argv": [...], "resolved": str|None, "blocked": bool, "why": str}.
SEEN: list[dict] = []

#: The same events for the WHOLE session (never cleared) — what report() shows.
EVENTS: list[dict] = []

#: Every BLOCKED call for the whole session, never cleared. A test that
#: deliberately exercises the fence sets ``EXPECT_BLOCK`` so its own event is
#: tagged expected; the guard test asserts there are no unexpected ones.
BLOCKED: list[dict] = []

#: Set by the fence's own tests: records their blocked events as intentional.
EXPECT_BLOCK = False

#: Marker substrings identifying hscc-cluster's inline sparkrun payloads. A
#: subprocess argv carrying one of these drives sparkrun's own API against the
#: LIVE fleet/config even though argv[0] is a python interpreter.
_SPARKRUN_PAYLOAD_MARKERS = (
    "sparkrun.orchestration.ssh",
    "sparkrun.orchestration.primitives",
    "sparkrun.proxy.config",
)

_FENCE_TAG = "SPARKRUN FENCE"

_LIVE: str | None = None
_LIVE_RESOLVED = False


class SparkrunFenceViolation(RuntimeError):
    """Raised instead of exec'ing the operator's real sparkrun CLI."""


def live_sparkrun() -> str | None:
    """realpath of the sparkrun CLI on PATH, or None when not installed.

    Resolved ONCE per process and cached: the fence compares against this, so a
    test that mangles PATH mid-test cannot make the live CLI look like a fake.
    """
    global _LIVE, _LIVE_RESOLVED
    if not _LIVE_RESOLVED:
        found = shutil.which("sparkrun")
        _LIVE = os.path.realpath(found) if found else None
        _LIVE_RESOLVED = True
    return _LIVE


_EXECUTABLE_SUFFIXES = (".exe", ".bat", ".cmd")


def _is_sparkrun_name(program: str) -> bool:
    """True for a token that IS the sparkrun executable, not a recipe named
    `sparkrun.yaml`.

    Stripping any extension (the first attempt) matched recipe paths like
    `~/recipes/sparkrun.yaml` — a false positive that would block a legitimate
    `sparkrun show -- <recipe>`. Only Windows executable suffixes are stripped;
    on POSIX the name must be exactly `sparkrun`.
    """
    name = os.path.basename(program).lower()
    if name == "sparkrun":
        return True
    base, ext = os.path.splitext(name)
    return base == "sparkrun" and ext in _EXECUTABLE_SUFFIXES


def _under_pytest_tmp(program: str) -> bool:
    """True only when the candidate sits inside a pytest PER-TEST tmp dir.

    Requires BOTH markers in the path: a ``pytest-of-<user>`` ancestor AND a
    ``test_<name><n>`` component. The second one is what makes this narrow — a
    file written into the shared ``pytest-of-<user>/pytest-<n>`` root (not a
    test's own dir) is still treated as the machine's CLI.
    """
    try:
        real = os.path.realpath(program)
    except OSError:
        return False
    parts = real.split(os.sep)
    return (any(p.startswith("pytest-of-") for p in parts)
            and any(p.startswith("test_") for p in parts))


def _sparkrun_candidates(args, shell: bool) -> list:
    """Every element of argv that could BE the sparkrun CLI.

    argv[0] alone is not enough: `sparkrun_bin.argv()` rewrites argv[0] to the
    resolved absolute CLI path, and a `timeout N <cli> ...` shell string puts
    the CLI at index 2. A recipe path (`/Users/x/recipes/a.yaml`) is never a
    sparkrun executable, so scanning every element costs no false positives —
    only elements NAMED sparkrun are candidates.
    """
    raw = []
    if isinstance(args, (list, tuple)):
        raw = [a for a in args if isinstance(a, str)]
    elif isinstance(args, (bytes, bytearray)):
        raw = [args.decode("utf-8", "replace")]
    elif isinstance(args, str):
        raw = args.split() if shell else [args]
    return [e for e in raw if e and _is_sparkrun_name(e)]


def _violates(candidate: str) -> str | None:
    """Why this candidate must not exec, or None when it may."""
    resolved = _resolves_to_live(candidate)
    if resolved:
        return (f"an argv element {candidate!r} resolves to the operator's REAL "
                f"sparkrun CLI at {resolved}")
    is_path = os.sep in candidate or (os.altsep and os.altsep in candidate)
    if is_path and os.path.exists(candidate) and not _under_pytest_tmp(candidate):
        # A sparkrun-named executable we cannot attribute to a per-test fake is
        # the machine's own CLI, even when a hermetic override means PATH
        # resolution does not match it. Fail closed. A path that does not exist
        # cannot exec anything (the loud [Errno 2] the code promises), so it is
        # not a violation.
        return (f"an argv element is the sparkrun executable {candidate!r}, "
                f"which exists outside a test temp dir (so it is not a per-test "
                f"fake) — the live CLI")
    return None


def _payload_in(args) -> str | None:
    """Marker of an inline sparkrun payload anywhere in argv, else None."""
    if isinstance(args, (list, tuple)):
        parts = [a for a in args if isinstance(a, str)]
    elif isinstance(args, (bytes, bytearray)):
        parts = [args.decode("utf-8", "replace")]
    elif isinstance(args, str):
        parts = [args]
    else:
        parts = []
    for part in parts:
        for marker in _SPARKRUN_PAYLOAD_MARKERS:
            if marker in part:
                return marker
    return None


def _resolves_to_live(program: str) -> str | None:
    """Return the live CLI path when *program* would exec it, else None."""
    live = live_sparkrun()
    if not live:
        return None
    if os.sep in program or (os.altsep and os.altsep in program):
        target = program
    else:
        target = shutil.which(program)
    if not target:
        return None
    try:
        real = os.path.realpath(target)
    except OSError:
        return None
    return real if real == live else None


def _argv_snapshot(args) -> list:
    if isinstance(args, (list, tuple)):
        return [a if isinstance(a, str) else repr(a)[:80] for a in args][:8]
    if isinstance(args, (bytes, bytearray)):
        return [args.decode("utf-8", "replace")[:120]]
    if isinstance(args, str):
        return [args[:120]]
    return [repr(args)[:120]]


def _record(event: dict) -> None:
    event["expected"] = bool(EXPECT_BLOCK)
    SEEN.append(event)
    EVENTS.append(event)
    if event["blocked"]:
        BLOCKED.append(event)


def check(args, shell: bool = False, *, record: bool = True) -> str | None:
    """Return a human-readable violation for this call, or None when allowed.

    Pure inspection — no process is spawned. Exposed for the guard test, which
    asserts both directions without exec'ing anything.
    """
    payload = _payload_in(args)
    if payload:
        _record({"argv": _argv_snapshot(args), "resolved": None, "blocked": True,
                 "why": f"inline payload {payload!r}"})
        return (f"argv smuggles one of hscc-cluster's inline sparkrun payloads "
                f"({payload!r}) — that drives sparkrun's own API against the live "
                f"fleet/config; patch sparkrun_bridge.remote_cmd / "
                f"sparkrun_bridge.proxy_config_write instead")
    for candidate in _sparkrun_candidates(args, shell=shell):
        reason = _violates(candidate)
        resolved = _resolves_to_live(candidate)
        if record:
            _record({"argv": _argv_snapshot(args), "resolved": resolved,
                     "blocked": reason is not None, "why": "sparkrun argv"})
        if reason:
            return reason
    return None


def install(monkeypatch) -> None:
    """Fence ``subprocess.run`` and ``subprocess.Popen`` for this test."""
    monkeypatch.setattr(subprocess, "run", _wrap("run"), raising=True)
    monkeypatch.setattr(subprocess, "Popen", _wrap("Popen"), raising=True)


def _wrap(fn_name: str):
    fn = getattr(subprocess, fn_name)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        argv = (args[0] if args else
                kwargs.get("args") or kwargs.get("cmd") or kwargs.get("popenargs"))
        try:
            reason = check(argv, shell=bool(kwargs.get("shell")))
        except Exception as e:  # the fence must never break an unrelated test
            print(f"{_FENCE_TAG}: internal error inspecting argv "
                  f"({type(e).__name__}: {e}) — passing call through",
                  file=sys.stderr)
            reason = None
        if reason:
            raise SparkrunFenceViolation(
                f"{_FENCE_TAG}: blocked a test subprocess call. {reason}\n"
                f"  argv={_argv_snapshot(argv)}\n"
                f"No test may exec the operator's live sparkrun (`proxy stop` "
                f"SIGTERMs the live :4000 balancer). Write a fake script under "
                f"tmp_path and point argv at it, or patch subprocess.run / "
                f"sparkrun_bridge.remote_cmd, the way "
                f"hscc_daemon/tests/conftest.py pins HSCC_SPARKRUN_BIN.")
        return fn(*args, **kwargs)

    return wrapper


def unexpected_blocked() -> list:
    """Every blocked call this session that no test asked for."""
    return [e for e in BLOCKED if not e.get("expected")]


def report() -> str:
    """Session-finish summary of what the fence saw. Never silent."""
    live = live_sparkrun()
    unexpected = unexpected_blocked()
    if not EVENTS and not BLOCKED:
        return (f"{_FENCE_TAG}: no sparkrun-shaped subprocess call reached a real "
                f"exec in this run (live CLI: {live or 'not on PATH'}).")
    lines = [f"{_FENCE_TAG}: {len(EVENTS)} sparkrun-shaped call(s) reached the real "
             f"exec layer; {len(BLOCKED)} resolved to the live CLI at {live}; "
             f"{len(unexpected)} of them unexpected."]
    shown = BLOCKED if unexpected else EVENTS
    for e in shown[:20]:
        tag = "BLOCKED" if e["blocked"] else "allowed"
        lines.append(f"  {tag} expected={e.get('expected')} "
                     f"resolved={e['resolved']} argv={e['argv']}")
    return "\n".join(lines)
