"""Per-checkout readout of the commit-time address guard for ``hscc check --repo``.

WHY THIS EXISTS
---------------
``core.hooksPath`` lives in the COMMON git config and its value is RELATIVE, so
the config value alone LIES per checkout. Measured during the t_ec2c2f95 review:
the primary checkout plus every pre-merge worktree reported "armed" while
resolving no hook at all — the commit-time guard was silently fail-open exactly
where it mattered (docs/audits/commit-time-address-guard-t_ec2c2f95.md §6c).

The per-checkout truth is already computed by ``posture()`` in
``hscc-bootstrap/install_hooks.py``. This module deliberately holds NO guard
logic of its own: it locates that shipped file, loads it, and calls
``posture()``. Same single-implementation rule that governs the detector itself
(``scripts/address_guard.py``, shared by the pytest gate and the pre-commit
hook) — one implementation of the rule, two triggers. A second implementation
here would be a second thing that can drift from the truth, which is precisely
the flaw §6c describes.

DEPLOYMENT LAYOUT (why the lookup is shaped like this)
------------------------------------------------------
``hscc_daemon`` and ``hscc-bootstrap`` deploy as SIBLING plugin dirs (see
``hscc-bootstrap/install_payload.py::DEFAULT_PAYLOAD``), exactly as they sit in
the repo checkout::

    <root>/hscc_daemon/guard_posture.py     <- this file
    <root>/hscc-bootstrap/install_hooks.py  <- the single implementation

so ``<root>`` is two levels up from this file in BOTH layouts, and the sibling
lookup works from a checkout and from the runtime plugin dir alike. This is the
same technique ``verify.py::check_plugin_payload`` uses to read
``install_payload.py`` rather than re-derive the payload list.

FAIL CLOSED
-----------
If the shipped module cannot be found or loaded, that is reported as an error
state (exit non-zero), never as a silent "armed" or a silent skip: a status
line that cannot see the guard must not advertise that it can.
"""

import importlib.util
import os
from pathlib import Path

# Relative layout of the single implementation, relative to <root>.
BOOTSTRAP_REL = ("hscc-bootstrap", "install_hooks.py")

# The four states posture() can answer with, and how an operator should read
# them. NAMES ONLY — the classification logic is posture()'s, not ours.
STATE_ARMED = "armed"
STATE_UNARMED = "unarmed"
STATE_ARMED_BUT_ABSENT = "armed-but-absent"
STATE_NOT_A_REPO = "not-a-repo"

# Exit-code contract for `hscc check --repo` (scripts/cron key off it):
# 0 only when the guard is actually armed HERE; every other state — including
# the fail-open `armed-but-absent` and a failed load — is non-zero.
EXIT_OK = 0
EXIT_NOT_ARMED = 1


class GuardSourceError(RuntimeError):
    """The shipped ``posture()`` implementation could not be located/loaded."""


def _candidate_paths():
    """Where ``install_hooks.py`` may live, best match first."""
    root = Path(__file__).resolve().parents[1]
    yield root.joinpath(*BOOTSTRAP_REL)
    # Last-resort runtime location, for a deployment where hscc_daemon was
    # installed without its sibling (should not happen; reported, not guessed).
    yield Path(os.path.expanduser("~")) / ".hermes" / "plugins" / Path(*BOOTSTRAP_REL)


def install_hooks_source():
    """Absolute path to the shipped ``install_hooks.py``, or None."""
    for p in _candidate_paths():
        if p.is_file():
            return p
    return None


# Cached by resolved path so repeated `hscc check --repo` calls in one process
# (tests, scripts) load the module once.
_MODULES = {}


def load_install_hooks():
    """Load the shipped ``hscc-bootstrap/install_hooks.py`` as a module.

    Raises ``GuardSourceError`` with the paths it tried — the caller renders
    that as an unverified/error line rather than pretending the guard is fine.
    """
    src = install_hooks_source()
    if src is None:
        tried = ", ".join(str(p) for p in _candidate_paths())
        raise GuardSourceError(
            f"cannot find hscc-bootstrap/install_hooks.py (tried: {tried})"
        )
    key = str(src)
    mod = _MODULES.get(key)
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location("hscc_install_hooks_single_impl", src)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise GuardSourceError(f"cannot load {src}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # pragma: no cover - broken shipped file
        raise GuardSourceError(f"loading {src} failed: {exc}") from exc
    if not callable(getattr(mod, "posture", None)):
        raise GuardSourceError(f"{src} exposes no posture()")
    _MODULES[key] = mod
    return mod


def default_repo():
    """Default target for ``--repo``: the current working directory.

    posture() itself resolves it to that checkout's top-level (and reports
    ``not-a-repo`` when cwd is not a git checkout), so cwd is enough here —
    no second git probe.
    """
    return os.getcwd()


def posture(repo=None):
    """The guard's true posture for ``repo`` (default cwd), straight from posture().

    Returns the dict EXACTLY as the shipped implementation produced it —
    ``state``, ``toplevel``, ``core_hooks_path``, ``hook_present``,
    ``hook_executable``, ``guard_present``, ``detail`` — so ``--json`` can pass
    it through verbatim. Raises ``GuardSourceError`` if unavailable.
    """
    mod = load_install_hooks()
    return mod.posture(repo if repo else default_repo())


def exit_code_for(p):
    """0 only for ``armed``; 1 for every other state (fail closed)."""
    return EXIT_OK if p.get("state") == STATE_ARMED else EXIT_NOT_ARMED


def status_role_for(state):
    """Theme role for the human view, by how the operator must read it.

    ``armed-but-absent`` is the ERROR the operator explicitly asked to see:
    the config advertises protection while this checkout resolves no runnable
    hook (fail-open). ``unarmed`` is honest but incomplete (warn); so is
    ``not-a-repo``.
    """
    return {
        STATE_ARMED: "ok",
        STATE_ARMED_BUT_ABSENT: "error",
        STATE_UNARMED: "warn",
        STATE_NOT_A_REPO: "warn",
    }.get(state, "warn")


def next_step_for(p):
    """Plain-language recovery, keyed on the state posture() reported."""
    state = p.get("state")
    if state == STATE_ARMED:
        return None
    if state == STATE_ARMED_BUT_ABSENT:
        return ("core.hooksPath is set in the COMMON config but this checkout has "
                "no runnable hook — commits here are NOT guarded while the config "
                "says they are. Rebase this checkout onto a revision that carries "
                ".githooks/pre-commit + scripts/address_guard.py (or run "
                "hscc-bootstrap/bootstrap.sh here after that) — the config alone "
                "cannot fix it.")
    if state == STATE_UNARMED:
        return ("run hscc-bootstrap/bootstrap.sh (or "
                "python3 hscc-bootstrap/install_hooks.py <repo>) from a checkout "
                "that carries .githooks/pre-commit AND scripts/address_guard.py")
    if state == STATE_NOT_A_REPO:
        return "point --repo at a git checkout (or run it from inside one)"
    return "inspect `hscc check --repo --json` for the raw posture"
