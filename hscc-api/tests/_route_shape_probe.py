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

Writes go only into a ``tempfile.mkdtemp`` sandbox. Output is one JSON line prefixed with
the sentinel the gate looks for, so an import-time warning can never be parsed as data.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

#: ``(METHOD, display path) -> query dict`` for every row THIS card documented and can
#: measure hermetically. Path groups merge into the query dict the way
#: ``api_server._dispatch`` does it. Rows whose payload is conditional are deliberately
#: absent -- see ``SHAPE_UNCHECKED`` in the gate.
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
    "GET /v1/profile/list": {},
}

SENTINEL = "@@ROUTE_SHAPE@@"


def _sandbox_home() -> str:
    root = tempfile.mkdtemp(prefix="hscc-route-shape-")
    for var in ("HOME", "HSCC_DIR", "HSCC_HOME", "HERMES_HOME"):
        os.environ[var] = root
    os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    return root


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


def measure() -> dict:
    root = _sandbox_home()
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
            status, payload = handler(None, None, dict(query), None)
        except Exception as exc:  # noqa: BLE001 - a failed measurement is data
            out["routes"][key] = {
                "error": f"{type(exc).__name__}: {exc}",
                "handler": f"{handler.__module__}.{handler.__name__}",
            }
            continue
        out["routes"][key] = {
            "handler": f"{handler.__module__}.{handler.__name__}",
            "status": status,
            "keys": sorted(payload) if isinstance(payload, dict) else None,
        }
    return out


if __name__ == "__main__":
    print(SENTINEL + json.dumps(measure()))
