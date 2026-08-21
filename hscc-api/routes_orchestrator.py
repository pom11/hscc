"""HSCC HTTP API — Phase C2: ``POST /v1/orchestrator/chat``.

The conversational endpoint: talk to a project's orchestrator DIRECTLY,
bypassing Telegram. Unlike the deterministic structured ops (A2-A4), this is
the one endpoint where an operator can say "go build X" and the orchestrator
decomposes it and dispatches real work onto its board. Because it can cause an
orchestrator to dispatch real work, it is a *mutation* and therefore REQUIRES
``"confirm": true`` (409 otherwise) — the same gate as every other mutating
endpoint.

Project → orchestrator resolution reuses C1's ``hscc-roles/orchestrators.py``
resolver (``resolve_orchestrator``), which maps project → ``<project>-orch`` /
``<project>`` session / board / repo and the catch-all ``general`` →
``general-orch`` / ``general`` / ``default``. We load that sibling plugin via
the same sys.path pattern the API uses everywhere (routes_cluster loads the
cluster engine, routes_project loads hscc-project). C1 shipped on an unmerged
branch, so ``orchestrators.py`` is vendored into ``hscc-roles/`` verbatim.

Transport (documented decision — see module docstring of the backing fn): we
shell Hermes directly, ``hermes -p <profile> chat -Q --continue <session>
-q <prompt>``, passing argv as a LIST (never string-interpolating the user's
prompt into a shell command) with a hard timeout. ``chat --continue`` resolves
the named session by title from the profile's state.db and persists the
exchange into it — the Telegram-topic analog. The localhost:4000 proxy is
litellm, a stateless OpenAI-compatible inference relay with no Hermes session
store / profile / memory; it cannot bind a named session, so it cannot satisfy
the session-continuity requirement.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from api_server import ApiError, ROUTES

# --------------------------------------------------------------------------- #
# C1 resolver loading (same sys.path pattern as every other sibling plugin)
# --------------------------------------------------------------------------- #

_ROLES_DIR = None
for _candidate in (
    Path(__file__).resolve().parent.parent / "hscc-roles",  # live checkout
):
    if _candidate.is_dir():
        _ROLES_DIR = _candidate
        break
if _ROLES_DIR is not None and str(_ROLES_DIR) not in sys.path:
    sys.path.insert(0, str(_ROLES_DIR))

from orchestrators import (                          # noqa: E402
    OrchestratorError,        # base error (e.g. project has no board)
    UnknownProjectError,      # project neither in registry nor 'general'
    resolve_orchestrator,     # project -> {profile, session, board, repo}
)

# Registry path is shared with routes_project (config-driven).
from routes_project import _registry_path             # noqa: E402

# Default: how long we wait for the orchestrator to reply before giving up.
_DEFAULT_TIMEOUT = 180.0   # seconds; an orchestrator can take a while


# --------------------------------------------------------------------------- #
# Backing-call seam (monkeypatch these in tests — no test spawns a real agent)
# --------------------------------------------------------------------------- #

def _backing_resolve(project, registry_path):
    """Resolve project -> orchestrator identity via C1's resolver.

    ``project`` may be None -> the ``general`` orchestrator. Raises
    :class:`UnknownProjectError` / :class:`OrchestratorError` for unresolvable
    inputs (the handler maps them to 400).
    """
    return resolve_orchestrator(project, path=registry_path)


def _backing_invoke(profile, session, prompt, timeout=_DEFAULT_TIMEOUT):
    """Send a prompt to an orchestrator and return its reply.

    The transport: shell Hermes headlessly as the orchestrator profile, in the
    profile's NAMED session, quiet mode so the reply is the only thing on
    stdout:

        hermes -p <profile> chat -Q --continue <session> -q <prompt>

    argv is passed as a LIST — the user's prompt is a plain element, never
    interpolated into a shell string (no shell-injection). ``--continue
    <session>`` resolves the session by title from the profile's state.db and
    persists this exchange into it, preserving the thread (the Telegram-topic
    analog). Quiet mode (``-Q``) keeps status/banner lines on stderr so stdout
    is machine-readable.

    Returns ``(reply_text, profile, session)``. Raises:
      * ``_OrchestratorTimeout`` when the reply exceeds ``timeout``;
      * ``_OrchestratorUnavailable`` when the profile/session cannot be
        reached (e.g. no matching session yet, or hermes not installed).
      * ``_OrchestratorInvocationError`` on any other failed invocation
        (nonzero exit / unparsable output).
    """
    argv = ["hermes", "-p", profile, "chat", "-Q", "--continue", session,
            "-q", prompt]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise _OrchestratorTimeout(
            f"orchestrator {profile!r} did not reply within {timeout:.0f}s"
        )
    except (FileNotFoundError, OSError) as exc:
        raise _OrchestratorUnavailable(
            f"cannot invoke `hermes`: {exc!r}"
        )

    err = (proc.stderr or "").strip()
    # A clean "no such session yet" failure — the orchestrator's named session
    # must exist before it can be continued (created by provisioning / first
    # Telegram topic). Surface it honestly rather than synthesising a reply.
    if proc.returncode != 0 or "Session not found" in err:
        raise _OrchestratorUnavailable(
            f"orchestrator session {session!r} not ready "
            f"(create it first, then re-send)".strip()
        )

    reply = (proc.stdout or "").strip()
    if not reply:
        raise _OrchestratorInvocationError(
            f"orchestrator {profile!r} returned an empty reply"
        )
    return reply, profile, session


# --------------------------------------------------------------------------- #
# Orchestrator-invocation error types (mapped to clean HTTP errors, no leaks)
# --------------------------------------------------------------------------- #

class _OrchestratorError(Exception):
    """Base for transport-level orchestrator failures (never leaked verbatim)."""


class _OrchestratorTimeout(_OrchestratorError):
    """The orchestrator did not reply within the timeout."""


class _OrchestratorUnavailable(_OrchestratorError):
    """The orchestrator profile/session could not be reached."""


class _OrchestratorInvocationError(_OrchestratorError):
    """A failed invocation with no clean session message."""


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #

def handle_orchestrator_chat(server, ctx, query, body):
    """POST /v1/orchestrator/chat — send a prompt to a project's orchestrator.

    Body: ``{"project": "<name>"|null, "prompt": "...", "confirm": true}``.
      * ``project`` absent/null -> the ``general`` orchestrator;
      * unknown project -> 400;
      * ``prompt`` required -> 400 if missing/empty;
      * ``confirm: true`` REQUIRED -> 409 otherwise.

    Response: the orchestrator's reply text (``reply``), the ``profile`` and
    ``session`` used, and a short ``speak`` summary (the iOS client may speak
    it). Transport or timeout failures surface as clean 5xx — the route never
    crashes the server and never leaks a traceback or token.
    """
    data = _parse_body(body)
    _require_confirm(data)
    prompt = data.get("prompt")
    if prompt is None or not str(prompt).strip():
        raise ApiError(
            400, "bad_request", "missing required field 'prompt'",
            "Prompt is required.",
        )

    project = data.get("project")
    registry_path = _registry_path(ctx)

    # Resolve project -> orchestrator identity (general when absent/null).
    try:
        resolved = _backing_resolve(project, registry_path)
    except UnknownProjectError as exc:
        raise ApiError(
            400, "unknown_project", str(exc),
            f"Unknown project {project!r}.",
        )
    except OrchestratorError as exc:
        # e.g. a registry project with no board — cannot route an orchestrator.
        raise ApiError(
            400, "bad_request", str(exc),
            f"Project {project!r} cannot resolve to an orchestrator.",
        )

    profile = resolved["profile"]
    session = resolved["session"]

    # Send the prompt. A pathological orchestrator could take a while; the
    # backing call enforces the timeout and maps it to a clean error.
    try:
        reply, profile, session = _backing_invoke(
            profile, session, str(prompt).strip(), timeout=_DEFAULT_TIMEOUT,
        )
    except _OrchestratorTimeout as exc:
        raise ApiError(504, "orchestrator_timeout", str(exc),
                       "The orchestrator did not reply in time.")
    except _OrchestratorUnavailable as exc:
        raise ApiError(503, "orchestrator_unavailable", str(exc),
                       "The orchestrator is not available right now.")
    except _OrchestratorInvocationError as exc:
        raise ApiError(502, "orchestrator_error", str(exc),
                       "The orchestrator call failed.")

    # Keep the human-facing summary short (the iOS client may speak it).
    return 200, {
        "reply": reply,
        "profile": profile,
        "session": session,
        "speak": f"{profile} says: {_shorten(reply)}",
    }


def _shorten(text: str, limit: int = 120) -> str:
    """Trim a long reply for the one-line ``speak`` summary."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Body helpers (mirror routes_actions)
# --------------------------------------------------------------------------- #

def _parse_body(body: bytes) -> dict:
    """Decode the request body as JSON; 400 on empty or malformed."""
    if not body:
        return {}
    import json
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "bad_request", "request body must be JSON")
    if not isinstance(data, dict):
        raise ApiError(400, "bad_request", "request body must be a JSON object")
    return data


def _require_confirm(data: dict) -> None:
    """Require ``confirm: true``; raise 409 otherwise (the mutation gate)."""
    if data.get("confirm") is True:
        return
    raise ApiError(
        409, "confirm_required",
        "this action is destructive and requires \"confirm\": true in the "
        "request body to send a message to an orchestrator",
        "Confirmation required to message the orchestrator.",
    )


# --------------------------------------------------------------------------- #
# Route registration (import side-effect; loaded by api_server.py)
# --------------------------------------------------------------------------- #

ROUTES.append(("POST", re.compile(r"^/v1/orchestrator/chat$"),
               handle_orchestrator_chat))
