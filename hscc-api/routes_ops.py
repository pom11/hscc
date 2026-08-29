"""HSCC HTTP API — Ops endpoints (verify, daemon/status, triggers, escalate,
profiles) + fleet control (cluster up/down).

Wraps the same ``hscc_daemon`` / ``hscc-cluster`` libraries the corresponding
``hscc`` CLI verbs call — never re-implements anything, never shells out and
parses text. Follows the existing API contract (see routes_cluster.py A2 for
reads, routes_actions.py A4 for mutations):

  * handlers are ``(server, ctx, query, body) -> (status, dict)``;
  * every READ carries a top-level ``speak`` (design §B);
  * every MUTATING endpoint requires ``confirm: true`` in the body (409
    ``confirm_required`` otherwise — same gate as ``routes_actions.py``);
  * read backing errors DEGRADE to a 200-with-honest-speak (never a crash,
    never fabricated data); mutating backing failures surface as a non-2xx
    error (never claim success for a change that didn't land).

Backing (libraries, never CLI text-parsing):
  * ``GET  /v1/verify``       -> ``hscc_daemon.verify.run_all()``
  * ``GET  /v1/daemon/status``-> ``daemon_ops.get_pid()`` + ``state.read_all_states()``
  * ``GET  /v1/triggers``     -> ``trigger.load_triggers()`` + ``state.read_state('triggers')`` + recent events
  * ``GET  /v1/escalate``     -> ``escalate_watcher.scan_and_escalate`` (read-only: no-op reassign/notify)
  * ``GET  /v1/profiles``     -> ``cluster_engine.cmd_profile_status()``
  * ``POST /v1/cluster/up``   -> ``cluster_engine.cmd_cluster_up(dry_run=False)``
  * ``POST /v1/cluster/down`` -> ``cluster_engine.cmd_cluster_down(dry_run=False)``

Test seam: every backing call goes through a ``_backing_*`` module function so
tests can monkeypatch them without running a real verify scan, reading a live
state dir, or issuing a real fleet up/down.
"""

from __future__ import annotations

import re

from api_server import ApiError, ROUTES
from routes_cluster import _load_cluster_engine, _is_error_dict


# --------------------------------------------------------------------------- #
# Backing-call seam (monkeypatch these in tests)
# --------------------------------------------------------------------------- #

def _backing_verify():
    from hscc_daemon import verify
    return verify.run_all()


def _backing_daemon_status():
    """Assemble daemon status: PID + every stream's last result (hscc status)."""
    from hscc_daemon import daemon_ops, state
    pid = daemon_ops.get_pid()
    alive = False
    if pid:
        try:
            import os
            os.kill(pid, 0)
            alive = True
        except (OSError, TypeError):
            pid, alive = None, False
    streams = state.read_all_states()
    return {
        "daemon_running": bool(pid and alive),
        "pid": pid,
        "state": "running" if (pid and alive) else "stopped",
        "streams": streams,
    }


def _backing_triggers():
    from hscc_daemon import trigger
    rules = trigger.load_triggers()
    from hscc_daemon import state
    last_run = state.read_state("triggers")
    recent = trigger.read_events_tail(limit=20)
    return {
        "rules": rules,
        "last_run": last_run,
        "recent_events": recent,
    }


def _backing_escalate():
    from hscc_daemon import escalate_watcher
    return escalate_watcher.scan_and_escalate(
        _reassign=lambda *a: None, _notify=lambda *a: None,
    )


def _backing_triggers_run():
    """Force the trigger engine to re-evaluate all rules now, firing any
    pending actions, then return the fresh read state.

    Mirrors ``hscc check triggers`` — an operator-initiated trigger-engine
    run that does NOT wait for the daemon's periodic cycle. Effectively a
    mutation: enabled rules may fire notify / emit_event / auto_restart /
    block_pipeline actions, so it is confirm-gated.
    """
    from hscc_daemon import trigger
    trigger.trigger_engine()
    rules = trigger.load_triggers()
    from hscc_daemon import state
    last_run = state.read_state("triggers")
    recent = trigger.read_events_tail(limit=20)
    return {
        "rules": rules,
        "last_run": last_run,
        "recent_events": recent,
    }


def _backing_escalate_run():
    """Actually perform pending failure escalations (reassign + notify)."""
    from hscc_daemon import escalate_watcher
    return escalate_watcher.scan_and_escalate()


def _backing_profiles():
    eng = _load_cluster_engine()
    if eng is None:
        return None
    return eng.cmd_profile_status()


def _backing_cluster_up():
    eng = _load_cluster_engine()
    if eng is None:
        return {"error": "hscc-cluster plugin not found"}
    return eng.cmd_cluster_up(dry_run=False)


def _backing_cluster_down():
    eng = _load_cluster_engine()
    if eng is None:
        return {"error": "hscc-cluster plugin not found"}
    return eng.cmd_cluster_down(dry_run=False)


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

def _speak_verify(data: dict) -> str:
    """§B: ok ? "All checks passed." : "{N} of {T} checks have problems."."""
    ok = data.get("ok")
    checks = data.get("checks", [])
    if ok:
        return "All checks passed."
    failed = [c for c in checks if not c.get("ok")]
    names = ", ".join(str(c.get("name", "?")) for c in failed)
    sentence = f"{len(failed)} of {len(checks)} checks have problems."
    if names:
        sentence += f" ({names})"
    return sentence


def _speak_daemon_status(data: dict) -> str:
    """§B: running ? "Daemon running." : "Daemon stopped." + stream count."""
    if data.get("daemon_running"):
        n = len(data.get("streams") or {})
        return f"Daemon is running with {n} health streams."
    return "Daemon is stopped."


def _speak_triggers(data: dict) -> str:
    """§B: "{n} trigger rules configured." + firing note."""
    rules = data.get("rules") or []
    last_run = data.get("last_run") or {}
    base = f"{len(rules)} trigger rules configured."
    fired = last_run.get("actions_fired")
    if fired:
        base += f" Last run fired {fired} action{'s' if fired != 1 else ''}."
    return base


def _speak_escalate(data) -> str:
    """§B: "{n} pending escalation(s)." / "no escalations pending."."""
    if not isinstance(data, list) or not data:
        return "No escalations pending."
    return f"{len(data)} pending escalation{'s' if len(data) != 1 else ''}."


def _speak_profiles(data: dict) -> str:
    """§B: "{total} profile(s) running tasks."."""
    counts = data.get("counts") or {}
    total = data.get("total_running") or sum(counts.values())
    n_profiles = len(counts)
    return (f"{n_profiles} profile{'s' if n_profiles != 1 else ''} "
            f"running {total} task{'s' if total != 1 else ''}.")


def _speak_fleet_up(data: dict) -> str:
    """§B: "Starting {units} unit(s)." / "Fleet control unavailable."."""
    if not isinstance(data, dict):
        return "Fleet control unavailable."
    units = data.get("units")
    if isinstance(units, int):
        return f"Bringing up {units} serving unit{'s' if units != 1 else ''}."
    return "Fleet control unavailable."


def _speak_fleet_down(data: dict) -> str:
    """§B: "Stopping all serving units." / "Fleet control unavailable."."""
    if not isinstance(data, dict) or data.get("error"):
        return "Fleet control unavailable."
    return "Stopping the serving fleet."


# --------------------------------------------------------------------------- #
# Handlers (reads)
# --------------------------------------------------------------------------- #

def handle_verify(server, ctx, query, body):
    """GET /v1/verify — full `hscc verify` result, per-check pass/fail."""
    try:
        data = _backing_verify()
    except Exception:
        return 200, {"speak": "Health check unavailable."}
    if not isinstance(data, dict):
        return 200, {"speak": "Health check unavailable."}
    return 200, {
        "ok": data.get("ok", False),
        "checks": data.get("checks", []),
        "speak": _speak_verify(data),
    }


def handle_daemon_status(server, ctx, query, body):
    """GET /v1/daemon/status — `hscc status` (daemon + every stream)."""
    try:
        data = _backing_daemon_status()
    except Exception:
        return 200, {"speak": "Daemon status unavailable."}
    if not isinstance(data, dict):
        return 200, {"speak": "Daemon status unavailable."}
    return 200, {**data, "speak": _speak_daemon_status(data)}


def handle_triggers(server, ctx, query, body):
    """GET /v1/triggers — trigger rules + recent firings."""
    try:
        data = _backing_triggers()
    except Exception:
        return 200, {"speak": "Trigger status unavailable."}
    if not isinstance(data, dict):
        return 200, {"speak": "Trigger status unavailable."}
    return 200, {**data, "speak": _speak_triggers(data)}


def handle_escalate(server, ctx, query, body):
    """GET /v1/escalate — pending escalations (read-only, never mutates)."""
    try:
        data = _backing_escalate()
    except Exception:
        data = []
    return 200, {"escalations": data, "count": len(data),
                 "speak": _speak_escalate(data)}


def handle_triggers_run(server, ctx, query, body):
    """POST /v1/triggers/run — force re-evaluate all trigger rules now.

    Confirm-gated: enabled rules may fire notify / emit_event / auto_restart
    / block_pipeline actions immediately. Returns the fresh read state so the
    operator sees the result in the same response.
    """
    data = _parse_body(body)
    _require_confirm(data, "run the trigger engine now")
    try:
        result = _backing_triggers_run()
    except Exception as exc:
        raise ApiError(502, "triggers_run_failed",
                       f"trigger run failed: {exc}", "Trigger run failed.")
    if not isinstance(result, dict):
        raise ApiError(502, "triggers_run_failed",
                       "trigger run produced no result", "Trigger run failed.")
    payload = dict(result)
    payload["message"] = "trigger engine run"
    payload["speak"] = _speak_triggers(result)
    return 200, payload


def handle_escalate_run(server, ctx, query, body):
    """POST /v1/escalate — perform pending failure escalations for real.

    Confirm-gated: actually reassigns repeatedly-failing tasks to the strong
    tier and notifies a human for those needing one (the escalation watcher's
    real action path, not the dry-run the GET uses). Returns the actions taken.
    """
    data = _parse_body(body)
    _require_confirm(data, "run pending escalations")
    try:
        actions = _backing_escalate_run()
    except Exception as exc:
        raise ApiError(502, "escalate_failed",
                       f"escalation run failed: {exc}", "Escalation run failed.")
    if not isinstance(actions, list):
        raise ApiError(502, "escalate_failed",
                       "escalation run produced no actions", "Escalation run failed.")
    return 200, {"escalations": actions, "count": len(actions),
                 "performed": True,
                 "speak": _speak_escalate(actions)}


def handle_profiles(server, ctx, query, body):
    """GET /v1/profiles — running kanban task counts per profile."""
    try:
        data = _backing_profiles()
    except Exception:
        return 200, {"speak": "Profile status unavailable."}
    if data is None or _is_error_dict(data):
        return 200, {"speak": "Profile status unavailable."}
    return 200, {**data, "speak": _speak_profiles(data)}


# --------------------------------------------------------------------------- #
# Handlers (mutations — confirm-gated)
# --------------------------------------------------------------------------- #

def handle_cluster_up(server, ctx, query, body):
    """POST /v1/cluster/up — start every unit in serving.json (confirm-gated)."""
    data = _parse_body(body)
    _require_confirm(data, "bring the fleet up")
    dry_run = bool(data.get("dry_run"))
    try:
        result = _backing_cluster_up()
    except Exception as exc:
        raise ApiError(502, "cluster_up_failed",
                       f"fleet up failed: {exc}",
                       "Fleet up failed.")
    if not isinstance(result, dict) or result.get("error"):
        reason = (result.get("error") if isinstance(result, dict) else
                  "fleet up could not be issued")
        raise ApiError(502, "cluster_up_failed", reason, "Fleet up failed.")
    payload = dict(result)
    payload["message"] = "cluster up issued"
    if not dry_run:
        payload["speak"] = _speak_fleet_up(result)
    return 200, payload


def handle_cluster_down(server, ctx, query, body):
    """POST /v1/cluster/down — stop ALL servings fleet-wide (confirm-gated)."""
    data = _parse_body(body)
    _require_confirm(data, "stop the entire fleet")
    try:
        result = _backing_cluster_down()
    except Exception as exc:
        raise ApiError(502, "cluster_down_failed",
                       f"fleet down failed: {exc}",
                       "Fleet down failed.")
    if not isinstance(result, dict) or result.get("error"):
        reason = (result.get("error") if isinstance(result, dict) else
                  "fleet down could not be issued")
        raise ApiError(502, "cluster_down_failed", reason, "Fleet down failed.")
    payload = dict(result)
    payload["message"] = "cluster down issued"
    payload["speak"] = _speak_fleet_down(result)
    return 200, payload


# --------------------------------------------------------------------------- #
# Route registration (import side-effect; loaded by api_server.py)
# --------------------------------------------------------------------------- #

ROUTES.append(("GET", re.compile(r"^/v1/verify$"), handle_verify))
ROUTES.append(("GET", re.compile(r"^/v1/daemon/status$"), handle_daemon_status))
ROUTES.append(("GET", re.compile(r"^/v1/triggers$"), handle_triggers))
ROUTES.append(("POST", re.compile(r"^/v1/triggers/run$"), handle_triggers_run))
ROUTES.append(("GET", re.compile(r"^/v1/escalate$"), handle_escalate))
ROUTES.append(("POST", re.compile(r"^/v1/escalate$"), handle_escalate_run))
ROUTES.append(("GET", re.compile(r"^/v1/profiles$"), handle_profiles))
ROUTES.append(("POST", re.compile(r"^/v1/cluster/up$"), handle_cluster_up))
ROUTES.append(("POST", re.compile(r"^/v1/cluster/down$"), handle_cluster_down))
