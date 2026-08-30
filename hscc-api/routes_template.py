"""HSCC HTTP API — Cluster template READ endpoints (list / status / preview).

Wraps the ``hscc-cluster`` template engine (``cluster_template_cli``) exactly
as ``hscc_daemon/hscc.py:_handle_template`` loads it — never re-implements
template logic. All three endpoints here are READ-ONLY (the mutating
``template/apply`` already lives in ``routes_actions.py``);

  * ``GET /v1/template/list``            -> ``cmd_cluster_template(['list'])``
  * ``GET /v1/template/status``          -> ``cmd_cluster_template(['status'])``
  * ``GET /v1/template/preview/{name}``  -> ``cmd_cluster_template(['preview', name])``

Every response carries a top-level ``speak`` (design §B). Degrades to a 200
with an honest ``speak`` on a backing failure (never a crash, never a
fabricated value).

Test seam: every backing call goes through a ``_backing_*`` module function so
tests can monkeypatch them without reading the live template store.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from api_server import ApiError, ROUTES

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_template_cli():
    """Load ``cluster_template_cli`` with hscc-cluster/ on sys.path (read-only).

    Returns the module, or None if the plugin is missing. Mirrors
    ``routes_actions._backing_template_apply``'s loading path.
    """
    cluster_dir = _REPO_ROOT / "hscc-cluster"
    if not (cluster_dir / "cluster_template_cli.py").is_file():
        return None
    if str(cluster_dir) not in sys.path:
        sys.path.insert(0, str(cluster_dir))
    from cluster_template_cli import cmd_cluster_template
    return cmd_cluster_template


# --------------------------------------------------------------------------- #
# Backing-call seam (monkeypatch these in tests)
# --------------------------------------------------------------------------- #

def _backing_template_list():
    cmd = _load_template_cli()
    if cmd is None:
        return {"error": "hscc-cluster plugin not found"}
    return cmd(["list"])


def _backing_template_status():
    cmd = _load_template_cli()
    if cmd is None:
        return {"error": "hscc-cluster plugin not found"}
    return cmd(["status"])


def _backing_template_preview(name):
    cmd = _load_template_cli()
    if cmd is None:
        return {"error": "hscc-cluster plugin not found"}
    return cmd(["preview", name])


# --------------------------------------------------------------------------- #
# speak helpers (design §B) — pure
# --------------------------------------------------------------------------- #

def _speak_template_list(data: dict) -> str:
    """§B: "({count}) templates available." / degraded."""
    if not isinstance(data, dict) or data.get("error"):
        return "Template list unavailable."
    templates = data.get("templates") or []
    if isinstance(templates, list):
        return f"{len(templates)} template{'s' if len(templates) != 1 else ''} available."
    return "Template list available."


def _speak_template_status(data: dict) -> str:
    """§B: "Template <name> applied." / "no template applied."."""
    if not isinstance(data, dict) or data.get("error"):
        return "Template status unavailable."
    name = data.get("applied") or data.get("template") or data.get("name")
    if name:
        return f"Template {name} is applied."
    return "No template is currently applied."


def _speak_template_preview(data: dict) -> str:
    """§B: "Preview: {n} change(s)." / explicit no-op."""
    if not isinstance(data, dict) or data.get("error"):
        return "Template preview unavailable."
    changes = data.get("changes") or data.get("steps") or []
    if isinstance(changes, list):
        return f"Preview: {len(changes)} change{'s' if len(changes) != 1 else ''}."
    return "Template preview ready."


# --------------------------------------------------------------------------- #
# Handlers (all read-only)
# --------------------------------------------------------------------------- #

def handle_template_list(server, ctx, query, body):
    """GET /v1/template/list — list available cluster templates."""
    try:
        data = _backing_template_list()
    except Exception:
        return 200, {"speak": "Template list unavailable."}
    if not isinstance(data, dict) or data.get("error"):
        return 200, {"speak": "Template list unavailable."}
    return 200, {**data, "speak": _speak_template_list(data)}


def handle_template_status(server, ctx, query, body):
    """GET /v1/template/status — which template is currently applied."""
    try:
        data = _backing_template_status()
    except Exception:
        return 200, {"speak": "Template status unavailable."}
    if not isinstance(data, dict) or data.get("error"):
        return 200, {"speak": "Template status unavailable."}
    return 200, {**data, "speak": _speak_template_status(data)}


def handle_template_preview(server, ctx, query, body):
    """GET /v1/template/preview/{name} — dry-run, what applying would change."""
    name = query.get("name")
    if name is None or not str(name).strip():
        raise ApiError(400, "bad_request", "missing template name")
    try:
        data = _backing_template_preview(name)
    except Exception:
        return 200, {"speak": "Template preview unavailable."}
    if not isinstance(data, dict):
        return 200, {"speak": "Template preview unavailable."}
    if data.get("error"):
        # An unknown template name -> 404 (contract precision over degraded).
        raise ApiError(
            404, "not_found", f"no template named {name!r}",
            f"Template {name} was not found.",
        )
    return 200, {**data, "speak": _speak_template_preview(data)}


# --------------------------------------------------------------------------- #
# Route registration (import side-effect; loaded by api_server.py)
# --------------------------------------------------------------------------- #

ROUTES.append(("GET", re.compile(r"^/v1/template/list$"),
               handle_template_list))
ROUTES.append(("GET", re.compile(r"^/v1/template/status$"),
               handle_template_status))
ROUTES.append(
    ("GET", re.compile(r"^/v1/template/preview/(?P<name>[^/]+)$"),
     handle_template_preview)
)
