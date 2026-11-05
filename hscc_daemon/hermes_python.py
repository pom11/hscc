"""Shared resolution of the Hermes venv interpreter (t_314eb13c).

Why this module exists
----------------------
``HERMES_HOME`` is NOT the Hermes root. Hermes sets it to the *profile* dir
(``<root>/profiles/<name>``) whenever it runs under a profile — the fleet's
normal run mode, not a misconfiguration (upstream ``hermes_constants.
get_hermes_home`` resolves config/SOUL/skills relative to the profile dir,
while ``get_default_hermes_root`` strips the ``profiles/<name>`` tail for
root-level ops). The ``hermes-agent`` checkout — and therefore its venv —
lives at the ROOT, one level above ``profiles/``.

So a resolver that does ``HERMES_HOME + "hermes-agent/venv/bin/python3"``
resolves a path that exists only when HERMES_HOME happens to be the root. Run
from a profile session — every dispatched worker, this CLI included — it
returns nothing, and the caller dead-ends. That is exactly how
``hscc api install-service --tailscale`` failed on 2026-11-04
(``error: no Hermes venv python found (HERMES_HOME/hermes-agent/venv/bin/python3)``);
the workaround was ``env -u HERMES_HOME``, which is a hint that the
resolution, not the install, was wrong.

The same ``profiles/``-grandparent rule is already used by
``hscc-bootstrap/doctor.py`` (``hermes_root_from_home``) and
``hscc-roles/rolelib.py``; this module applies it to the interpreter so the
fleet resolves the venv consistently in one place.

Contract
--------
:func:`hermes_venv_python` returns the first candidate that EXISTS, or
``None`` when none does (fail-closed: a caller must never write a launchd
unit or systemd unit pointing at an interpreter that isn't there). It does
NOT verify the interpreter can import the Hermes runtime — callers that need
that verify separately (``flightdeck.core.project_lifecycle.
resolve_hermes_python`` probes with a subprocess; this module is
stdlib-only, import-time-safe and process-cheap so the install paths can use
it without spawning anything).

The same profile-vs-root trap applies to the CHECKOUT itself, not only to the
interpreter inside it: ``ensure_review_feature.py`` used to bind
``$HERMES_HOME/hermes-agent`` at import, so under a profile session the runtime
looked absent, it returned ``skipped_missing``/ok, and bootstrap printed a
near-green line while the review feature stayed dropped (t_919ac971).
:func:`hermes_agent_dir` walks the same homes ladder for the checkout, so
callers that need the repository resolve it in the same one place.

All three inputs are injectable so the ladder is testable without touching
the operator's real ``~/.hermes`` — see
``hscc_daemon/tests/test_hermes_python.py``.
"""
from __future__ import annotations

import os
import sys

#: Directory names inside a Hermes install that carry the checkout.
AGENT_DIR = "hermes-agent"
#: The directory under the agent checkout that holds the venv.
VENV_BIN_REL = os.path.join(AGENT_DIR, "venv", "bin")
#: Interpreter names tried in order inside ``<home>/hermes-agent/venv/bin``.
#: ``python3`` first: it is the name the shipped units and docs use, and the
#: name the API launchd unit's ProgramArguments records.
VENV_PYTHONS = ("python3", "python")


def hermes_homes(env=None, *, home=None) -> list[str]:
    """Candidate Hermes ROOTS, in priority order, de-duplicated.

    1. ``$HERMES_HOME`` as given — it is the root whenever Hermes runs without
       a profile, and it is still the first place to look under a profile
       because a profile dir CAN carry its own ``hermes-agent`` (a
       profile-local checkout).
    2. the grandparent of ``$HERMES_HOME`` when its parent is named
       ``profiles`` — i.e. ``<root>/profiles/<name>`` → ``<root>``. This is the
       normal profile case and the one the old resolver missed.
    3. the platform default ``~/.hermes``.

    Homes are returned even when they do not exist; :func:`venv_python_candidates`
    turns them into paths and the picker checks existence. That keeps "where did
    it look" printable in an error message (see :func:`describe_search`).
    """
    env = os.environ if env is None else env
    home = home or os.path.expanduser("~")
    homes: list[str] = []

    def _push(path):
        if not path:
            return
        path = os.path.expanduser(str(path))
        if path not in homes:
            homes.append(path)

    _push(env.get("HERMES_HOME", "").strip())
    if homes:
        parent = os.path.dirname(homes[0])
        if parent and os.path.basename(parent) == "profiles":
            _push(os.path.dirname(parent))
    _push(os.path.join(os.path.expanduser(str(home)), ".hermes"))
    return homes


def _venv_python_candidates_for(home: str) -> list[str]:
    bindir = os.path.join(os.path.expanduser(str(home)), VENV_BIN_REL)
    return [os.path.join(bindir, name) for name in VENV_PYTHONS]


def _current_executable() -> str:
    """The interpreter this process runs under (module seam for tests).

    Indirection on purpose: :func:`running_hermes_venv_python` reads it, so a
    test that needs the fail-closed ``None`` path can pin it (e.g. to a brew
    python) without mutating the global ``sys`` module.
    """
    return sys.executable


def running_hermes_venv_python(sys_executable=None) -> str | None:
    """The venv interpreter the CURRENT process is running, when it is one.

    The shape test is the point: ``sys.executable`` is only a Hermes venv
    python when it sits at ``<...>/hermes-agent/venv/bin/python*``. A brew
    ``hscc`` console script, a CI python, or this suite's own interpreter under
    a fabricated layout must NOT be advertised as the Hermes venv — the API
    unit would then record an interpreter without the websockets/starlette deps.
    """
    exe = sys_executable if sys_executable is not None else _current_executable()
    if not exe:
        return None
    exe = os.path.expanduser(str(exe))
    if os.path.basename(os.path.dirname(exe)) != "bin":
        return None
    if os.path.basename(os.path.dirname(os.path.dirname(exe))) != "venv":
        return None
    if os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(exe)))) != AGENT_DIR:
        return None
    if os.path.basename(exe) not in VENV_PYTHONS:
        return None
    return exe


def venv_python_candidates(env=None, *, home=None, sys_executable=None) -> list[str]:
    """Every candidate interpreter path, in the order they should be tried.

    Order (t_314eb13c): the exported ``$HERMES_HOME`` layout first — it wins
    when it is real, so a profile-local or custom-root checkout is still
    honoured — then the ``profiles/``-grandparent and ``~/.hermes`` roots, then
    the venv the *running* process is inside. De-duplicated, first hit wins.
    """
    candidates: list[str] = []

    def _push(path):
        if path and path not in candidates:
            candidates.append(path)

    for h in hermes_homes(env, home=home):
        for cand in _venv_python_candidates_for(h):
            _push(cand)
    _push(running_hermes_venv_python(sys_executable))
    return candidates


def hermes_venv_python(env=None, *, home=None, sys_executable=None) -> str | None:
    """Absolute path of the Hermes venv python, or None when there is none.

    Returns the first candidate from :func:`venv_python_candidates` that is a
    real file. Existence only (not exec bit, not importability): the shipped
    tests fabricate non-executable stubs to pin the ladder, and the callers use
    the result to WRITE a service unit whose interpreter launchd/systemd execs
    later. ``None`` means no candidate exists anywhere searched — the caller
    must report it rather than write a unit that could never start.
    """
    for cand in venv_python_candidates(env, home=home, sys_executable=sys_executable):
        try:
            if os.path.isfile(cand):
                return cand
        except OSError:  # pragma: no cover - unreadable path is not a hit
            continue
    return None


def agent_dir_candidates(env=None, *, home=None) -> list[str]:
    """Every candidate ``hermes-agent`` CHECKOUT path, in the order they should be tried.

    Same homes ladder as :func:`hermes_homes`, joined to ``hermes-agent`` — the
    checkout itself, not the interpreter inside it. Candidates are returned even
    when they do not exist so a caller can print where it looked
    (:func:`describe_agent_search`).

    Deliberately NO running-interpreter fallback here, unlike
    :func:`venv_python_candidates`. That fallback exists because a venv this
    process is inside demonstrably carries Hermes' deps, which is the only
    property the API unit needs. A checkout is different: it is the repository a
    caller is about to WRITE to (``ensure_review_feature`` cherry-picks into
    it), and deriving the write target from whichever python happened to invoke
    the script could point it at a checkout unrelated to the configured home.
    Checkout resolution stays env/home-driven — which also keeps
    "nothing exists anywhere" a state a test can fabricate hermetically.
    """
    candidates: list[str] = []
    for h in hermes_homes(env, home=home):
        cand = os.path.join(os.path.expanduser(str(h)), AGENT_DIR)
        if cand not in candidates:
            candidates.append(cand)
    return candidates


def hermes_agent_dir(env=None, *, home=None) -> str | None:
    """Absolute path of the ``hermes-agent`` checkout, or None when there is none.

    The runtime checkout — where ``tools/kanban_tools.py`` and ``.git`` live.
    Returns the first candidate from :func:`agent_dir_candidates` that is a real
    DIRECTORY. ``None`` means no candidate exists anywhere searched: a caller
    must report that as "there is no runtime here", never as a silent no-op
    (that is exactly how t_919ac971's silent skip read as near-green).
    """
    for cand in agent_dir_candidates(env, home=home):
        try:
            if os.path.isdir(cand):
                return cand
        except OSError:  # pragma: no cover - unreadable path is not a hit
            continue
    return None


def describe_agent_search(env=None, *, home=None) -> str:
    """One-line "where did you look" for the no-checkout report."""
    cands = agent_dir_candidates(env, home=home)
    return ", ".join(cands) if cands else "no candidate paths"


def describe_search(env=None, *, home=None, sys_executable=None) -> str:
    """One-line "where did you look" for the fail-closed error message.

    The old message named only ``HERMES_HOME/hermes-agent/venv/bin/python3`` —
    the one path that could not exist under a profile — so an operator reading
    it had no way to see that the real venv had already been ruled out.
    """
    cands = venv_python_candidates(env, home=home, sys_executable=sys_executable)
    return ", ".join(cands) if cands else "no candidate paths"
