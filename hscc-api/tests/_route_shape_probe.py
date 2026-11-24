"""Child process for the route-coverage gate's payload measurement.

Run as a SCRIPT, never collected as a test (the name does not start with ``test_``).

WHY A CHILD PROCESS AND NOT AN IN-PROCESS CALL. The gate compares a docs/API.md row's
promised key set with the key set the handler actually returns. Calling the handler
in-process would measure it under whatever ``HOME`` / ``HSCC_DIR`` / ``HERMES_HOME`` the
test process inherited -- and several route modules fold those paths into module-level
constants AT IMPORT, which the test process cannot move after the fact. A fresh
interpreter gets the sandboxed environment *before* ``api_server`` is imported, so no
handler can be holding a path into the operator's real ``~/.hscc``.

NO-LIVE: before any handler runs, every subprocess entry point and every socket connect
in the child raises. A route that reaches for the fleet fails the measurement instead of
doing real cluster work, which is the whole reason this file refuses to guess shapes.

SEAM STUBS (``SEAM_STUBS``) exist for one reason: a handler whose backing cannot answer
in a sandbox takes its DEGRADED branch, and pinning that branch as the row's contract
would publish a fiction (``GET /v1/sessions`` on an unreachable profile answers three
keys, its real answer has five). A stub therefore only ever selects the branch -- it
never supplies a top-level payload key. Each entry says which branch it pins, and the
stub value is empty/absent wherever the handler builds the keys itself.
``GET /v1/profile/editor/{profile}`` deliberately has NO stub: its key set IS its
backing's return literal, so the sandbox gets a real profile directory and the real
backing runs.

Writes go only into a ``tempfile.mkdtemp`` sandbox. Output is one JSON line prefixed with
the sentinel the gate looks for, so an import-time warning can never be parsed as data.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from types import SimpleNamespace

#: ``(METHOD, display path) -> query dict`` for every row this lane documented and can
#: measure hermetically. Path groups merge into the query dict the way
#: ``api_server._dispatch`` does it, so a value here is a SCALAR string -- the dispatcher
#: collapses ``parse_qs`` lists to ``v[-1]`` (``_parse_path``). Rows whose payload is
#: conditional are deliberately absent -- see ``SHAPE_UNCHECKED`` in the gate.
SHAPE_CHECKED: dict[str, dict] = {
    "GET /v1/fleet/usage": {},
    "GET /v1/projects": {},
    "GET /v1/kanban/blocked": {},
    "GET /v1/kanban/stale": {},
    "GET /v1/kanban/running": {},
    "GET /v1/merge-queue": {},
    "GET /v1/daemon/status": {},
    "GET /v1/daemon/host": {},
    "GET /v1/daemon/history": {},
    "GET /v1/triggers": {},
    "GET /v1/escalate": {},
    "GET /v1/autodown/status": {},
    "GET /v1/activity/feed": {},
    "GET /v1/commands": {},
    "GET /v1/alerts": {},
    "GET /v1/template/list": {},
    "GET /v1/template/status": {},
    # --- t_f95bcaad: measured key sets ---------------------------------------------- #
    # The CLI's two-layer verdict. Measured with a name that matches no template on
    # disk, which is the branch the row documents; `validate_template` returns the
    # SAME top-level key set on all four of its return paths
    # (hscc-cluster/cluster_template.py:2325-2341, :2356-2358), so the pin is not
    # a missing-template artefact. No stub: the missing-name path never reaches
    # _resolve, so nothing here can touch the fleet.
    "GET /v1/template/validate/{name}": {"name": "demo"},
    # The D9 dashboard pair (a79990a7). NO stub on either: both handlers spread their
    # backing's own dict and add only `speak`, and both backings answered for real in
    # the sandbox, so the key sets measured are the handlers' own.
    # main/status measured 12 keys -- and the row's set is not branch-luck: every
    # pane-state return in main_cli.status() (:449 discovery-failed, :455
    # not-running, :460 ambiguous, :475 busy/idle/screen-unknown) builds the same
    # receipt + state/running/busy/pane/target/panes/detail/ok. The two smaller
    # answers (:440 resolver unloadable, and the handler's own {speak}-only catch)
    # are documented in the row as the exception, exactly as /v1/sessions' does.
    "GET /v1/main/status": {},
    # waker/status measured 7 keys == the row's 7, with no stub.
    # orch_waker.waker_status() is pure file reads under the sandbox HOME.
    "GET /v1/orch/waker/status": {},
    "GET /v1/profile/list": {},
    # --- t_b9808146: the iOS-facing reads the shipped client calls ------------------ #
    "GET /v1/projects/{name}": {"name": "demo"},
    # No stub: this route's key set IS its backing's return literal, so the sandbox
    # seeds a real profile directory and the real _backing_read answers.
    "GET /v1/profile/editor/{profile}": {"profile": "demo"},
    "GET /v1/sessions": {"profile": "demo"},
}

#: ``(METHOD, display path) -> [(module, attribute, replacement)]``, applied to the
#: handler's own module immediately before that handler is called. Every replacement here
#: pins the NON-degraded branch and supplies no payload keys of its own.
_SEAM_ProjectNotFound = type("ProjectNotFoundError", (Exception,), {})
_SEAM_project = SimpleNamespace(name="demo", repo=None, board=None, topic=None)

SEAM_STUBS: dict[str, list[tuple[str, str, object]]] = {
    # The registry module is this module's documented seam
    # (``hscc-api/tests/test_routes_project.py`` replaces the same names). One project,
    # no repo and no board, so ``_project_detail`` degrades inside ``board_counts``/``git``
    # exactly as it does live -- the keys measured are the handler's own.
    "GET /v1/projects/{name}": [
        ("routes_project", "_registry", SimpleNamespace(
            ProjectNotFoundError=_SEAM_ProjectNotFound,
            load_registry=lambda path=None: [_SEAM_project],
            get_project=lambda name, path=None: _SEAM_project)),
    ],
    # An EMPTY session list is a real answer ("reachable, nothing there"), not a
    # fabricated row: it keeps ``_backing_open_profile_db`` out of the measurement
    # (it needs the profile's own ``state.db``) while still taking the full branch,
    # which is the one that carries ``count`` and ``bloated_count``.
    "GET /v1/sessions": [
        ("routes_sessions", "_backing_list_sessions",
         lambda profile, context_window: []),
    ],
}

#: A profile directory the REAL ``_backing_read`` can open, so the profile-editor row's
#: key set is that backing's own return literal rather than a stub's.
_SEED_PROFILE = "demo"
_SEED_CONFIG_YAML = """\
model:
  default: demo-model
  provider: demo-provider
toolsets:
  - dev
skills:
  preload: []
compression:
  threshold: 0.8
  threshold_tokens: 120000
"""
_SEED_PROFILE_YAML = "description: A seeded profile for a docs measurement.\n"

SENTINEL = "@@ROUTE_SHAPE@@"


def _sandbox_home() -> str:
    root = tempfile.mkdtemp(prefix="hscc-route-shape-")
    for var in ("HOME", "HSCC_DIR", "HSCC_HOME", "HERMES_HOME"):
        os.environ[var] = root
    os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    return root


def _seed_profile_dir(root: str) -> None:
    """Seed one profile directory under every plausible hermes-profiles root.

    ``routes_profile_editor._hermes_profiles_dir()`` decides the layout; seeding both
    ``<HOME>/.hermes/profiles`` and ``<HOME>/profiles`` keeps the measurement honest
    (the real backing reads real YAML) without the probe guessing which resolver won.
    """
    for rel in ((".hermes", "profiles"), ("profiles",)):
        pdir = os.path.join(root, *rel, _SEED_PROFILE)
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, "config.yaml"), "w", encoding="utf-8") as fh:
            fh.write(_SEED_CONFIG_YAML)
        with open(os.path.join(pdir, "profile.yaml"), "w", encoding="utf-8") as fh:
            fh.write(_SEED_PROFILE_YAML)


def _refuse(*_a, **_k):
    raise RuntimeError("NO-LIVE: fleet contact attempted during a docs measurement")


def _no_fleet_contact() -> None:
    import socket
    import subprocess

    for name in ("run", "Popen", "check_output", "check_call", "call"):
        setattr(subprocess, name, _refuse)
    os.system = _refuse
    os.popen = _refuse
    socket.socket.connect = _refuse
    socket.socket.connect_ex = _refuse
    socket.create_connection = _refuse


def _apply_stubs(key: str) -> list[str]:
    """Install this route's seam stubs; return the names actually replaced."""
    import importlib

    applied = []
    for mod_name, attr, value in SEAM_STUBS.get(key, ()):  # noqa: B007
        mod = importlib.import_module(mod_name)
        if not hasattr(mod, attr):
            raise RuntimeError(f"{key}: seam {mod_name}.{attr} does not exist")
        setattr(mod, attr, value)
        applied.append(f"{mod_name}.{attr}")
    return applied


def measure() -> dict:
    root = _sandbox_home()
    _seed_profile_dir(root)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import api_server  # noqa: E402  (the whole routes_* chain registers at import)

    _no_fleet_contact()
    out: dict = {"sandbox": root, "routes": {}}
    for key, query in SHAPE_CHECKED.items():
        method, display = key.split(" ", 1)
        hits = [(m, p, h) for (m, p, h) in api_server.ROUTES
                if m == method and p.match(display)]
        if not hits:
            out["routes"][key] = {"error": "not registered"}
            continue
        handler = hits[0][2]
        try:
            stubs = _apply_stubs(key)
        except Exception as exc:  # noqa: BLE001 - a bad stub is data, not a crash
            out["routes"][key] = {"error": f"stub: {exc}",
                                  "handler": f"{handler.__module__}.{handler.__name__}"}
            continue
        try:
            status, payload = handler(None, None, dict(query), None)
        except Exception as exc:  # noqa: BLE001 - a failed measurement is data
            out["routes"][key] = {
                "error": f"{type(exc).__name__}: {exc}",
                "handler": f"{handler.__module__}.{handler.__name__}",
                "stubs": stubs,
            }
            continue
        out["routes"][key] = {
            "handler": f"{handler.__module__}.{handler.__name__}",
            "status": status,
            "stubs": stubs,
            "keys": sorted(payload) if isinstance(payload, dict) else None,
        }
    return out


if __name__ == "__main__":
    print(SENTINEL + json.dumps(measure()))
