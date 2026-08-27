"""HSCC HTTP API — Phase C2: orchestrator chat (job-based).

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

Why job-based (Phase 1 — do NOT fake streaming):
``hermes chat -Q`` emits the reply ONCE, complete, when the underlying
``run_conversation`` returns (see cli.py:17858-17896 — quiet mode sets
``stream_delta_callback = None`` and ``print(response)`` once). There is NO
supported incremental/output-streaming interface on the ``hermes chat`` CLI
(no ``--stream`` / SSE / ndjson flag; the CLI's streaming is interactive
display chrome, gated on ``display.streaming`` which is off, and routes
through ANSI ``_cprint`` box drawing — not a machine-readable token stream).
So token streaming is NOT available, and faking it would mislead the operator.

The honest fix is a JOB API (option b):
  * ``POST /v1/orchestrator/chat`` VALIDATES + RESOLVES the project, spawns a
    background thread that actually invokes hermes, and returns IMMEDIATELY
    (202) with a ``job_id`` + ``status`` — no more dead-wait on the phone.
  * ``GET /v1/orchestrator/chat/{id}`` polls the job: ``queued`` / ``running``
    / ``done`` (with the reply) / a terminal error state, plus honest
    ``elapsed`` seconds.
This ALSO fixes a real failure mode: a dropped connection no longer loses a
90 s answer the server already computed — the background thread finishes and
the phone (or a backgrounded app) can pick it up by job_id later.

Timeout semantics (t_023d4c4c): because a job holds NO connection open, a short
"reply latency" timeout is pointless — the honest model is "report elapsed and
let the operator decide". The worker therefore runs the orchestrator under a
generous WEDGE-backstop :data:`_DEFAULT_TIMEOUT` (600s default, configurable as
``chat_timeout`` seconds in ``~/.hscc/api.json`` — see ``_chat_timeout``) that
only fires when the ``hermes chat`` process is genuinely hung, not merely slow.
Every terminal job (``done`` or a failure state) reports ``elapsed`` FROZEN at
its termination instant (``finished_at - submitted_at``), so status, message
and elapsed always agree (the original 180s-vs-2972.7s contradiction).
"""
from __future__ import annotations

import itertools
import re
import subprocess
import sys
import threading
import time
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
#
# WHY 600s and not 180s (t_023d4c4c): the orchestrator answers a trivial prompt
# in ~17s when the fleet is IDLE, but >165s while even three kanban workers are
# running — and "busy" is this cluster's NORMAL state, not the exception. The
# old 180s sat barely above loaded latency, so normal busy-cluster chats timed
# out (the reported 2972.7s / 180s contradiction came from poll-side elapsed
# drift, fixed separately — see _job_dict). A job does NOT hold a connection
# open (the POST returns in ms and the phone polls by job_id), so a short
# "reply latency" budget serves no purpose; the honest model is "report elapsed
# and let the operator decide". This value is therefore a pure WEDGE-backstop:
# it should only fire when the underlying `hermes chat` process is genuinely
# hung (deadlocked / wedged), never merely slow. 600s = 10 min, ~3.5x the
# measured 165s loaded case — room for "busy", yet still bounds a truly stuck
# process so an orphan can't burn GPU forever. Tune via ``chat_timeout`` (float
# seconds) in ``~/.hscc/api.json`` — see ``_chat_timeout``.
_DEFAULT_TIMEOUT = 600.0   # seconds (configurable backstop for a WEDGED process)


def _chat_timeout(ctx) -> float:
    """Resolve the orchestrator-chat timeout for this server's config.

    Precedence (lowest -> highest): :data:`_DEFAULT_TIMEOUT` -> ``chat_timeout``
    in ``~/.hscc/api.json`` -> ``chat_timeout`` on the resolved config dict.
    Mirrors the ``registry`` precedence in :func:`routes_project._registry_path`.
    A non-numeric / non-positive value is a user CONFIG error: raise (matching
    api_server's hard-error-on-malformed-config stance) rather than silently
    guess.
    """
    raw = None
    from_config = getattr(ctx, "config", None)
    if from_config and from_config.get("chat_timeout") is not None:
        raw = from_config["chat_timeout"]
    else:
        hscc_dir = getattr(ctx, "hscc_dir", None) or "~/.hscc"
        cfg_path = Path(hscc_dir).expanduser() / "api.json"
        if cfg_path.exists():
            try:
                import json
                data = json.loads(cfg_path.read_text())
                if isinstance(data, dict) and data.get("chat_timeout") is not None:
                    raw = data["chat_timeout"]
            except (OSError, ValueError):
                pass  # fall through to the default
        else:
            raw = None
    if raw is None:
        return _DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise RuntimeError(
            f"invalid 'chat_timeout' {raw!r} in api config — expected seconds"
        )
    if value <= 0:
        raise RuntimeError(
            f"invalid 'chat_timeout' {value!r} — must be a positive number of seconds"
        )
    return value


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
    # ``-Q`` still lets a couple of harmless one-line notices through to stdout
    # BEFORE the reply (observed empirically, t_bc242def Phase 1) — e.g. the
    # cwd-restore line ``↪ restored workspace dir: <path>``. Strip those so the
    # answer we parse is never polluted by a notice.
    reply = "\n".join(
        ln for ln in reply.splitlines() if not _is_notice_line(ln)
    ).strip()
    if not reply:
        raise _OrchestratorInvocationError(
            f"orchestrator {profile!r} returned an empty reply"
        )
    return reply, profile, session


def _is_notice_line(line: str) -> bool:
    """True when a stdout line is a channel notice, not part of the reply.

    ``hermes chat -Q`` can print a couple of harmless one-liners to stdout
    BEFORE the reply (observed, t_bc242def Phase 1). The one seen is the
    cwd-restore notice ``↪ restored workspace dir: <path>``. Matched
    defensively (substring) so future-notice drift still lands on the
    conservative side: unknown lines pass through untouched.
    """
    return "restored workspace dir:" in line.strip()


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
# Job store (in-memory, thread-safe). Lives as long as the server process, so
# a dropped POST connection does NOT lose a reply the background thread later
# completes — the phone can pick it up by job_id via GET.
# --------------------------------------------------------------------------- #

_jobs_lock = threading.Lock()
_jobs = {}                       # job_id -> _ChatJob
_job_ids = itertools.count(1)


class _ChatJob:
    """One asynchronous orchestrator chat invocation.

    Status transitions: ``queued`` -> ``running`` -> ``done``, or to a
    terminal error state (``timeout`` / ``unavailable`` / ``error``). ``reply``
    and the identity fields are set only on ``done``; ``error`` carries a clean
    code+message on the failure states. ``elapsed`` is honest wall-clock from
    submission.
    """

    # terminal error status -> (public error code, speak-safe headline)
    _ERROR_MAP = {
        "timeout": ("orchestrator_timeout",
                    "The orchestrator did not reply in time."),
        "unavailable": ("orchestrator_unavailable",
                        "The orchestrator is not available right now."),
        "error": ("orchestrator_error",
                  "The orchestrator call failed."),
    }

    def __init__(self, job_id, project, profile, session, prompt,
                 timeout=_DEFAULT_TIMEOUT):
        self.job_id = job_id
        self.project = project
        self.profile = profile
        self.session = session
        self.prompt = prompt
        self.timeout = timeout
        self.submitted_at = time.time()
        self.finished_at: float | None = None
        self.lock = threading.Lock()
        self.status = "queued"
        # done-state payload
        self.reply: str | None = None
        self.speak: str | None = None
        # error-state payload
        self.error: dict | None = None


def _new_job(project, profile, session, prompt, timeout=_DEFAULT_TIMEOUT) -> _ChatJob:
    """Create (and store) a queued job under the store lock."""
    with _jobs_lock:
        job_id = f"chat-{next(_job_ids)}"
        job = _ChatJob(job_id, project, profile, session, prompt, timeout=timeout)
        _jobs[job_id] = job
    return job


def _job_dict(job: _ChatJob) -> dict:
    """Snapshot a job as a JSON-serialisable dict (safe to hand the client).

    ``elapsed`` is honest wall-clock from submission. For ANY terminal state
    (``done`` or a failure state) it is FROZEN at ``finished_at - submitted_at``
    — the exact instant the job terminated — so the reported elapsed ALWAYS
    agrees with the status and error message: a job marked ``timeout`` at 180s
    reports ~180s, never the minutes you spent polling it afterward (that drift
    was the t_023d4c4c contradiction: status/error said 180s while elapsed said
    2972.7s). Only a live (``queued``/``running``) job shows a growing
    ``time.time() - submitted_at``.
    """
    with job.lock:
        base = {
            "job_id": job.job_id,
            "project": job.project,
            "status": job.status,
        }
        if job.finished_at is not None:
            # Terminal: elapsed frozen at the termination moment, so status,
            # error message and elapsed tell ONE coherent story.
            base["elapsed"] = round(job.finished_at - job.submitted_at, 3)
        else:
            # Live: elapsed grows as the operator polls.
            base["elapsed"] = round(time.time() - job.submitted_at, 3)
        if job.status == "done":
            base["reply"] = job.reply
            base["profile"] = job.profile
            base["session"] = job.session
            base["speak"] = job.speak
        if job.error is not None:
            base["error"] = job.error
        return base


def _run_job(job: _ChatJob):
    """Background worker: invoke the orchestrator and record the outcome.

    Runs in a daemon thread spawned by ``handle_orchestrator_chat``. Any
    transport failure maps to a terminal job error state with a clean
    code+message — never a raw exception, never a leaked detail.
    """
    with job.lock:
        job.status = "running"
    try:
        reply, profile, session = _backing_invoke(
            job.profile, job.session, job.prompt, timeout=job.timeout,
        )
    except _OrchestratorTimeout as exc:
        _finish_job_error(job, "timeout", str(exc))
        return
    except _OrchestratorUnavailable as exc:
        _finish_job_error(job, "unavailable", str(exc))
        return
    except _OrchestratorInvocationError as exc:
        _finish_job_error(job, "error", str(exc))
        return
    except Exception:  # never surface raw internals to the client
        _finish_job_error(job, "error",
                          "an unexpected orchestrator failure occurred — "
                          "check ~/.hscc/api.log for details")
        return

    with job.lock:
        job.status = "done"
        job.reply = reply
        job.speak = f"{profile} says: {_shorten(reply)}"
        job.finished_at = time.time()


def _finish_job_error(job: _ChatJob, status: str, message: str):
    """Land a job in a terminal error state with a clean, client-safe shape."""
    code, headline = _ChatJob._ERROR_MAP[status]
    with job.lock:
        job.status = status
        job.error = {"code": code, "message": message,
                     "speak": headline}
        job.finished_at = time.time()


# --------------------------------------------------------------------------- #
# Shared POST validation (confirm + prompt), used by handle_orchestrator_chat
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


def _validate_prompt(data: dict) -> str:
    """Pull + validate the required ``prompt``; 400 when missing/blank."""
    prompt = data.get("prompt")
    if prompt is None or not str(prompt).strip():
        raise ApiError(
            400, "bad_request", "missing required field 'prompt'",
            "Prompt is required.",
        )
    return str(prompt).strip()


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

def handle_orchestrator_chat(server, ctx, query, body):
    """POST /v1/orchestrator/chat — start an async orchestrator chat JOB.

    Body: ``{"project": "<name>"|null, "prompt": "...", "confirm": true}``.
      * ``project`` absent/null -> the ``general`` orchestrator;
      * unknown project -> 400;
      * ``prompt`` required -> 400 if missing/empty;
      * ``confirm: true`` REQUIRED -> 409 otherwise.

    The request VALIDATES + RESOLVES synchronously (so a bad project/prompt
    returns a clean 4xx immediately), then spawns a background thread that
    actually invokes the orchestrator, and returns **202 Accepted** with a
    ``job_id`` — NOT the reply. The phone polls
    ``GET /v1/orchestrator/chat/{id}`` for ``queued``/``running``/``done`` +
    honest ``elapsed`` and the reply when finished. This kills the 90 s dead
    wait: the POST returns in milliseconds, and a dropped connection no longer
    loses an answer the server is still computing.
    """
    data = _parse_body(body)
    _require_confirm(data)
    prompt = _validate_prompt(data)
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

    # Create the job and run the real invocation on a daemon background thread
    # (the server is ThreadingHTTPServer with daemon_threads, so this thread
    # lives as long as the process — the reply is NOT lost when the POST
    # connection closes). The timeout (configurable, default 600s) is captured
    # per-job at submission so every job reports elapsed at ITS termination.
    job = _new_job(project, profile, session, prompt, timeout=_chat_timeout(ctx))
    worker = threading.Thread(target=_run_job, args=(job,), daemon=True)
    worker.start()

    return 202, {
        "job_id": job.job_id,
        "project": project,
        "status": "queued",
        "elapsed": 0.0,
        "profile": profile,
        "session": session,
        "speak": f"Chat request accepted (job {job.job_id}).",
    }


def handle_orchestrator_chat_job(server, ctx, query, body):
    """GET /v1/orchestrator/chat/{id} — poll an async chat job.

    Read-only (the orchestrator was already messaged by the POST), so no
    ``confirm`` is required — only bearer auth (enforced by the dispatcher).

    Response:
      * ``queued``/``running`` -> ``{job_id, project, status, elapsed}``;
      * ``done`` -> plus ``reply``, ``profile``, ``session``, ``speak``;
      * terminal error state (``timeout``/``unavailable``/``error``) -> plus an
        ``error`` object ``{code, message, speak}`` mirroring the API's error
        shape so the client can surface each real condition distinctly.
    Unknown job id -> 404.
    """
    job_id = query.get("id")
    with _jobs_lock:
        job = _jobs.get(job_id)  # type: ignore[arg-type]
    if job is None:
        raise ApiError(404, "not_found", f"unknown chat job {job_id!r}",
                       "That chat job does not exist (it may have expired).")
    return 200, _job_dict(job)


def _shorten(text: str, limit: int = 120) -> str:
    """Trim a long reply for the one-line ``speak`` summary."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Route registration (import side-effect; loaded by api_server.py)
# --------------------------------------------------------------------------- #

ROUTES.append(("POST", re.compile(r"^/v1/orchestrator/chat$"),
               handle_orchestrator_chat))
ROUTES.append(("GET", re.compile(r"^/v1/orchestrator/chat/(?P<id>[^/]+)$"),
               handle_orchestrator_chat_job))
