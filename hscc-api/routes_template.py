"""HSCC HTTP API — Cluster template READ endpoints (list / status / preview / validate).

Wraps the ``hscc-cluster`` template engine (``cluster_template_cli``) exactly
as ``hscc_daemon/hscc.py:_handle_template`` loads it — never re-implements
template logic. All four endpoints here are READ-ONLY (the mutating
``template/apply`` already lives in ``routes_actions.py``);

  * ``GET /v1/template/list``            -> ``cmd_cluster_template(['list'])``
  * ``GET /v1/template/status``          -> ``cmd_cluster_template(['status'])``
  * ``GET /v1/template/preview/{name}``  -> ``cmd_cluster_template(['preview', name])``
  * ``GET /v1/template/validate/{name}`` -> ``cmd_cluster_template(['validate', name])``

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


def _backing_template_validate(name, structural_only=False):
    """Preflight a template — reuse the CLI's own ``validate`` verb verbatim.

    Builds the exact argv ``hscc template validate`` builds and lets the CLI
    parse it, so the route answer IS the CLI verdict (audit §5: no second
    implementation). ``--structural-only`` is forwarded through the CLI flag,
    never by calling ``validate_template`` here.
    """
    cmd = _load_template_cli()
    if cmd is None:
        return {"error": "hscc-cluster plugin not found"}
    argv = ["validate", name]
    if structural_only:
        argv.append("--structural-only")
    return cmd(argv)


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


def _speak_template_validate(data: dict) -> str:
    """§B: the verdict ``hscc template validate`` prints, in one sentence.

    The CLI's two-layer verdict is ``{template, structural: {ok, errors,
    warnings}, placement: {ok, errors, warnings}, ok}`` (the top-level ``ok``
    is the AND of both layers). A missing template is NOT an error dict — the
    CLI returns it as a ``ok: false`` verdict whose structural errors say
    ``Template not found``; that is a real verdict to speak, so it flows here
    as "is not deployable" (mirroring the CLI), not a degraded "unavailable".
    Only a genuine read failure (the seam's own ``error`` key / non-dict)
    speaks "unavailable".
    """
    if not isinstance(data, dict) or data.get("error"):
        return "Template validation unavailable."
    name = data.get("template") or "template"
    if data.get("ok"):
        return f"Template {name} is deployable."
    problems = 0
    for layer in ("structural", "placement"):
        block = data.get(layer)
        if isinstance(block, dict):
            problems += len(block.get("errors") or [])
    if problems:
        return (f"Template {name} is not deployable: "
                f"{problems} error{'s' if problems != 1 else ''}.")
    return f"Template {name} is not deployable."


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


def handle_template_validate(server, ctx, query, body):
    """GET /v1/template/validate/{name} — preflight a template (deployable?).

    Returns the SAME verdict ``hscc template validate`` prints, verbatim
    (``{template, structural, placement, ok}`` + ``speak``). It is a pure read:
    the two-layer check touches no live cluster and mutates nothing, and —
    unlike ``apply`` — it never restarts or recreates a unit. ``structural_only``
    is opt-in via the ``structural-only`` (or ``structural_only``) query flag so
    the placement layer, which resolves against the live fleet, can be skipped
    (matching the CLI's ``--structural-only``).

    Deliberately NOT a 404 on a missing template (unlike ``preview``): the CLI
    answers a missing name with a ``ok: false`` verdict whose structural errors
    read ``Template not found`` — that IS the verdict the card asks us to
    return, so it passes through as a 200, not an HTTP error.
    """
    name = query.get("name")
    if name is None or not str(name).strip():
        raise ApiError(400, "bad_request", "missing template name")
    structural_only = (query.get("structural-only") is not None
                       or query.get("structural_only") is not None)
    try:
        data = _backing_template_validate(name, structural_only=structural_only)
    except Exception:
        return 200, {"speak": "Template validation unavailable."}
    if not isinstance(data, dict) or data.get("error"):
        return 200, {"speak": "Template validation unavailable."}
    return 200, {**data, "speak": _speak_template_validate(data)}


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
ROUTES.append(
    ("GET", re.compile(r"^/v1/template/validate/(?P<name>[^/]+)$"),
     handle_template_validate)
)
