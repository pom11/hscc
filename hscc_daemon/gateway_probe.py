"""Daemon-side handle on the gateway-liveness transports (GAP-F-3, t_c14d18cb).

WHY A LOADER INSTEAD OF AN IMPORT
---------------------------------
The one implementation of "is the Hermes gateway alive?" ships in the sibling
plugin dir as ``hscc-bootstrap/gateway_liveness.py`` (it lives there because
the bootstrap doctor is its primary consumer and it must stay stdlib-only so
bootstrap runs before anything else is installed). ``hscc_daemon`` and
``hscc-bootstrap`` deploy as SIBLING plugin dirs — see
``hscc-bootstrap/install_payload.py::DEFAULT_PAYLOAD``, which ships both —
exactly as they sit in a repo checkout::

    <root>/hscc_daemon/gateway_probe.py       <- this file
    <root>/hscc-bootstrap/gateway_liveness.py <- the single implementation

so ``<root>`` is two levels up from either file in BOTH layouts. This is the
same technique ``hscc_daemon/guard_posture.py`` uses to call
``hscc-bootstrap/install_hooks.py::posture()`` rather than recompute the guard,
and for the same reason: a second implementation of the probe here would be a
second thing that can drift from the truth. It already happened once —
``doctor._gateway_running`` was fixed in #148 and the copy in
``health._gateway_job_alive`` kept the pgrep leg #148 proved blind on this box.

The file is loaded with ``importlib.spec_from_file_location`` (again like
``guard_posture.load_install_hooks``) rather than by putting ``hscc-bootstrap``
on ``sys.path``: that dir is hyphenated, so it is not an importable package,
and inserting it globally would shadow generic module names for the whole
daemon process.

FAILS LOUD, NOT SILENT
----------------------
If the sibling file cannot be found or loaded, `load_gateway_liveness()` raises
``GatewayLivenessSourceError`` naming the paths it tried. `check_gateway()`
catches that, publishes the reason in the state entry and does NOT claim the
gateway is healthy — a check that cannot see the gateway must not advertise
that it can. The stream goes not-ok with `gateway_job: null` (nothing was
verified) rather than a bare "gateway down" that would blame the gateway for a
broken probe.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

# Relative layout of the single implementation, relative to <root>.
LIVENESS_REL = ("hscc-bootstrap", "gateway_liveness.py")

# The three names the daemon consumes. Names only — the probe logic is theirs.
_REQUIRED = ("job_is_alive", "gateway_verdict", "gateway_states")


class GatewayLivenessSourceError(RuntimeError):
    """The shipped ``gateway_liveness.py`` could not be located/loaded."""


def _candidate_paths(root=None, home=None):
    """Where ``gateway_liveness.py`` may live, best match first.

    ``root`` / ``home`` are injectable so a test can pin BOTH deployment
    layouts (repo checkout and deployed plugin tree) without touching the
    caller's real filesystem. Defaults are the real layouts, not a guess:
    ``root`` is two levels up from this file, which is ``<repo>`` in a checkout
    and ``<plugins_dir>`` once installed.
    """
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    yield root.joinpath(*LIVENESS_REL)
    # Last-resort runtime location, for a deployment where hscc_daemon was
    # installed without its sibling (should not happen; reported, not guessed).
    home = Path(home) if home is not None else Path(os.path.expanduser("~"))
    yield home / ".hermes" / "plugins" / Path(*LIVENESS_REL)


def liveness_source(root=None, home=None):
    """Absolute path to the shipped ``gateway_liveness.py``, or None."""
    for p in _candidate_paths(root=root, home=home):
        if p.is_file():
            return p
    return None


# Cached by resolved path so the daemon's repeated check cycles load once.
_MODULES = {}


def load_gateway_liveness():
    """Load ``hscc-bootstrap/gateway_liveness.py`` as a module.

    Raises ``GatewayLivenessSourceError`` with the paths it tried —
    ``check_gateway`` renders that as a DEGRADED stream rather than a verdict.
    """
    src = liveness_source()
    if src is None:
        tried = ", ".join(str(p) for p in _candidate_paths())
        raise GatewayLivenessSourceError(
            f"cannot find {os.sep.join(LIVENESS_REL)} (tried: {tried})"
        )
    key = str(src)
    mod = _MODULES.get(key)
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(
        "hscc_gateway_liveness_single_impl", src)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise GatewayLivenessSourceError(f"cannot load {src}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # pragma: no cover - broken shipped file
        raise GatewayLivenessSourceError(f"loading {src} failed: {exc}") from exc
    missing = [n for n in _REQUIRED if not callable(getattr(mod, n, None))]
    if missing:
        raise GatewayLivenessSourceError(f"{src} exposes no {', '.join(missing)}")
    _MODULES[key] = mod
    return mod
