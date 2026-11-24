"""HSCC HTTP API — Autodown endpoints (status / enable / disable / wake / cancel).

Wraps ``hscc_daemon.autodown`` + ``hscc_daemon.lifecycle`` exactly as the
``hscc autodown`` CLI verb does (see ``autodown_cli.py``) but through the API
contract: handlers are ``(server, ctx, query, body) -> (status, dict)``, reads
carry a top-level ``speak`` (design §B), and every MUTATING endpoint requires
``confirm: true`` in the body (409 ``confirm_required`` otherwise — the same
gate as ``routes_actions.py``).

Backing (libraries, never CLI text-parsing):
  * ``GET  /v1/autodown/status``  -> ``autodown.status_snapshot()`` — the
      exact builder ``autodown_cli._cmd_status`` uses (config, watchdog block,
      blocked_by/kanban/PR-CI classifiers, and the read-only state reconcile),
      plus ``speak``. No handler-local copy of the status logic.
  * ``POST /v1/autodown/enable``  -> the same ``record_activity`` +
      ``load_config``/``save_config`` writes as ``_cmd_enable`` (incl. the
      cron-guard split + force_armed bookkeeping).
  * ``POST /v1/autodown/disable`` -> ``record_activity`` + ``save_config`` +
      ``_clear_intentional_block(...)`` (mirrors ``_cmd_disable``).
  * ``POST /v1/autodown/wake``    -> ``record_activity`` + ``autodown.autoup()``
      run on a BACKGROUND THREAD so the request returns promptly with a
      ``waking`` status (autoup can block ~9 min polling readiness — the card
      forbids holding the connection that long). The client polls
      ``/v1/autodown/status`` for the outcome.
  * ``POST /v1/autodown/cancel``  -> ``record_activity`` + ``save_config``
      (``cancel_requested: true``), mirroring ``_cmd_cancel``.

Test seam: every backing call goes through a ``_backing_*`` module function so
tests can monkeypatch them without ever writing the operator's live
``~/.hscc/autodown.json`` or invoking a real ``autoup()``/teardown.

Safe-by-construction for testing: NO handler here calls the destructive
``autodown.teardown()`` directly — ``wake`` is the only acting verb exposed,
and its backing is fully stubbable.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from api_server import ApiError, ROUTES

# Make hscc_daemon importable (sibling of hscc-api/) — same seam as
# routes_cluster.py::_ensure_repo_root_on_path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_REPO_ROOT))


# --------------------------------------------------------------------------- #
# Backing-call seam (monkeypatch these in tests)
# --------------------------------------------------------------------------- #

def _backing_load_config():
    from hscc_daemon import autodown
    return autodown.load_config()


def _backing_save_config(cfg):
    from hscc_daemon import autodown
    autodown.save_config(cfg)


def _backing_record_activity(source):
    from hscc_daemon import autodown
    return autodown.record_activity(source)


def _backing_load_watchdog_block():
    from hscc_daemon import lifecycle
    return lifecycle.load_watchdog_block()


def _backing_clear_intentional_block(reason=None):
    from hscc_daemon import autodown
    autodown._clear_intentional_block(reason=reason)


def _backing_status_snapshot():
    """The SAME status payload ``hscc autodown status --json`` builds.

    Imports ``hscc_daemon.autodown.status_snapshot`` — one builder for the
    CLI and this route, including the read-only reconcile of a stale
    ``state: down`` against the live serving layer (t_8743f98c). No copied
    classifier logic lives here.

    The call runs inside ``autodown.silent_read()`` (t_85875291): this is a
    documented read, and the builder's unreachable-Hermes-library path would
    otherwise append its kanban WARN to ``$HSCC_HOME/daemon.log`` — the
    DAEMON'S log, written by the API process (the route answers the same
    question in its own body, ``kanban_ok``/``kanban_reason``). Verdict and
    evidence: ``docs/audits/verdict-api-autodown-status-read_t_85875291.md``;
    the guard is ``TestTheRouteWritesNoDaemonLog`` in this surface's own suite
    (the #704 lesson — the CLI door's guards in ``hscc_daemon`` prove nothing
    here, because this route reaches the builder directly).
    NOT a wider mute: only the kanban leaf is borrowed, never the module
    ``log`` name — the PR/CI WARN stays audible (t_18318fd9).
    """
    from hscc_daemon import autodown
    with autodown.silent_read():
        return autodown.status_snapshot()


def _backing_list_active_cron_jobs():
    from hscc_daemon import autodown
    return autodown.list_active_cron_jobs()


def _backing_autoup():
    """Run a full wake (autoup) — will block up to ~9 min polling readiness."""
    from hscc_daemon import autodown
    return autodown.autoup()


# --------------------------------------------------------------------------- #
# Body helpers (confirm gate mirrors routes_actions)
# --------------------------------------------------------------------------- #

def _parse_body(body: bytes) -> dict:
    import json
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "bad_request", "request body must be JSON")
    if not isinstance(data, dict):
        raise ApiError(400, "bad_request",
                       "request body must be a JSON object")
    return data


def _require_confirm(data: dict, what: str) -> None:
    if data.get("confirm") is True:
        return
    raise ApiError(
        409, "confirm_required",
        f"this action affects the shared cluster and requires "
        f"\"confirm\": true in the request body to {what}",
        f"Confirmation required to {what}.",
    )


# --------------------------------------------------------------------------- #
# speak helpers (design §B) — pure
# --------------------------------------------------------------------------- #

def _speak_status(status: dict) -> str:
    """§B: e.g. "Armed, 10 minute idle. Status up." / "Disabled."."""
    if not status.get("enabled"):
        if status.get("last_activity_iso") is None and status["state"] == "up":
            return "Autodown is disabled and has never been enabled."
        return "Autodown is disabled."
    minutes = status.get("idle_minutes")
    if status.get("state") == "down":
        base = f"Autodown armed, idle limit {minutes} minutes, fleet is down."
    else:
        base = f"Autodown armed, idle limit {minutes} minutes, status {status.get('state')}."
    if status.get("blocked_by"):
        base += f" Blocked by {status['blocked_by']}."
    res = status.get("wake_source") or status.get("reason") or ""
    if res:
        base += f" {res}."
    return base


def _speak_wake_started(result: dict) -> str:
    return (
        f"Wake initiated, bringing the serving layer up. "
        f"Current state: {result.get('state', 'waking')}."
    )


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

def handle_autodown_status(server, ctx, query, body):
    """GET /v1/autodown/status — read-only report (mirrors `hscc autodown status`)."""
    try:
        payload = dict(_backing_status_snapshot())
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        return 200, {
            "error": detail,
            "speak": f"Autodown status failed: {detail}",
        }
    payload["speak"] = _speak_status(payload)
    return 200, payload


def handle_autodown_enable(server, ctx, query, body):
    """POST /v1/autodown/enable — arm idle autodown (confirm-gated).

    Mirrors ``_cmd_enable``: enforce the cron-guard (fail-closed on
    model-requiring active jobs unless ``force``), then ``record_activity`` +
    persist ``enabled: true`` (+ idle_minutes, force_armed bookkeeping).
    NON-ACTING: never starts the serving layer.
    """
    data = _parse_body(body)
    _require_confirm(data, "enable autodown")
    idle_minutes = data.get("idle_minutes", 10)
    try:
        idle_minutes = int(idle_minutes)
    except (TypeError, ValueError):
        raise ApiError(
            400, "bad_request",
            "idle_minutes must be a non-negative integer",
            "Idle minutes must be a non-negative integer.",
        )
    if idle_minutes < 0:
        raise ApiError(
            400, "bad_request",
            "idle_minutes must be a non-negative integer",
            "Idle minutes must be a non-negative integer.",
        )
    force = bool(data.get("force"))

    active_jobs = _backing_list_active_cron_jobs()
    model_jobs = ([j for j in active_jobs if not j.get("cpu_only")]
                  if isinstance(active_jobs, list) else [])
    if not force and (active_jobs is None or model_jobs):
        # Fail-closed, mirroring _cmd_enable: unreadable cron config OR any
        # model-requiring active job blocks arming.
        labels = [j.get("name") or j.get("id") for j in model_jobs]
        raise ApiError(
            409, "cron_conflict",
            "active model-requiring Hermes cron jobs present — autodown may "
            "power the cluster down when they are due"
            + (f": {', '.join(labels)}" if labels else ""),
            "Active scheduled jobs block autodown. Confirm force to override.",
        )

    cfg = _backing_record_activity("api")
    cfg["enabled"] = True
    cfg["idle_minutes"] = idle_minutes
    if force and model_jobs:
        cfg["force_armed"] = True
        cfg["force_armed_overrides"] = [
            j.get("name") or j.get("id") for j in model_jobs]
    else:
        cfg["force_armed"] = False
        cfg["force_armed_overrides"] = []
    _backing_save_config(cfg)
    return 200, {
        "enabled": True,
        "idle_minutes": idle_minutes,
        "state": cfg.get("state"),
        "last_activity_iso": cfg.get("last_activity_iso"),
        "force_armed": bool(cfg.get("force_armed")),
        "force_armed_overrides": cfg.get("force_armed_overrides") or [],
        "message": f"autodown enabled (idle_minutes={idle_minutes})",
    }


def handle_autodown_disable(server, ctx, query, body):
    """POST /v1/autodown/disable — disarm + release intentional block (confirm-gated)."""
    data = _parse_body(body)
    _require_confirm(data, "disable autodown")
    _backing_record_activity("api")
    cfg = _backing_load_config()
    cfg["enabled"] = False
    _backing_save_config(cfg)
    _backing_clear_intentional_block(reason="autodown disabled by operator")
    return 200, {
        "enabled": False,
        "state": cfg.get("state"),
        "message": "autodown disabled; intentional watchdog block cleared. "
                   "Serving layer not restarted (use wake to bring it up).",
    }


def handle_autodown_wake(server, ctx, query, body):
    """POST /v1/autodown/wake — force autoup (confirm-gated).

    autoup() can BLOCK ~9 minutes polling readiness. We deliberately do NOT
    hold the HTTP connection that long (the card forbids it). Instead we run
    ``record_activity`` + ``autoup()`` on a daemon background thread and return
    promptly with a ``state: waking`` payload; the client polls
    ``/v1/autodown/status`` for the eventual outcome. This mirrors exactly the
    idempotent semantics of ``autoup()``: a second wake while one is in flight
    returns ``already-waking`` without starting a parallel wake.
    """
    data = _parse_body(body)
    _require_confirm(data, "wake the serving layer")

    def _run_wake():
        try:
            _backing_record_activity("api")
            _backing_autoup()
        except Exception:
            pass  # autoup records its own failure; status reflects reality

    threading.Thread(target=_run_wake, daemon=True).start()
    return 200, {
        "result": "waking",
        "state": "waking",
        "wake_source": "api",
        "message": (
            "wake initiated in the background — this can take up to ~9 "
            "minutes. Poll GET /v1/autodown/status for the outcome."
        ),
        "speak": _speak_wake_started({"state": "waking"}),
    }


def handle_autodown_cancel(server, ctx, query, body):
    """POST /v1/autodown/cancel — abort an in-progress teardown (confirm-gated)."""
    data = _parse_body(body)
    _require_confirm(data, "cancel teardown")
    _backing_record_activity("api")
    cfg = _backing_load_config()
    cfg["cancel_requested"] = True
    _backing_save_config(cfg)
    return 200, {
        "cancel_requested": True,
        "message": "cancel requested — an in-progress teardown will abort "
                   "between stops.",
    }


# --------------------------------------------------------------------------- #
# Route registration (import side-effect; loaded by api_server.py)
# --------------------------------------------------------------------------- #

ROUTES.append(("GET", re.compile(r"^/v1/autodown/status$"),
               handle_autodown_status))
ROUTES.append(("POST", re.compile(r"^/v1/autodown/enable$"),
               handle_autodown_enable))
ROUTES.append(("POST", re.compile(r"^/v1/autodown/disable$"),
               handle_autodown_disable))
ROUTES.append(("POST", re.compile(r"^/v1/autodown/wake$"),
               handle_autodown_wake))
ROUTES.append(("POST", re.compile(r"^/v1/autodown/cancel$"),
               handle_autodown_cancel))
