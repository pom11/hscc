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
  * ``GET  /v1/verify``       -> ``hscc_daemon.verify.run_all()`` (always inline;
    the verdict is also recorded into the health snapshot /v1/health serves)
  * ``GET  /v1/daemon/status``-> ``daemon_ops.get_pid()`` + ``state.read_all_states()``
  * ``GET  /v1/daemon/host``  -> local platform/psutil host metrics (daemon host's OWN machine)
  * ``GET  /v1/main/status``  -> ``main_cli.status()`` — the read ``hscc main
    status`` prints (identity through ``hscc-roles/main_orch.py``, pane through
    ``repl_inject``). No ``main start``/``main wakeups`` door exists here.
  * ``GET  /v1/orch/waker/status`` -> ``orch_waker.waker_status()`` — the read
    ``hscc orch waker status`` prints. ``enable``/``disable`` have NO route.
  * ``GET  /v1/triggers``     -> ``trigger.load_triggers()`` + ``state.read_state('triggers')`` + recent events
  * ``GET  /v1/escalate``     -> ``escalate_watcher.scan_and_escalate`` (read-only: no-op reassign/notify)
  * ``GET  /v1/profiles``     -> ``cluster_engine.cmd_profile_status()`` —
    the canonical ``hscc profiles`` answer (running counts). The profile
    ROSTER is a different question: ``GET /v1/profiles/list`` in
    ``routes_profiles.py``. See ``PROFILES_STATUS_ROUTE`` — audit F2.
  * ``POST /v1/cluster/up``   -> ``cluster_engine.cmd_cluster_up(dry_run=False)``
  * ``POST /v1/cluster/down`` -> ``cluster_engine.cmd_cluster_down(dry_run=False)``

Test seam: every backing call goes through a ``_backing_*`` module function so
tests can monkeypatch them without running a real verify scan, reading a live
state dir, or issuing a real fleet up/down.
"""

from __future__ import annotations

import re

import health_cache
from api_server import ApiError, ROUTES
from routes_cluster import _load_cluster_engine, _is_error_dict


# --------------------------------------------------------------------------- #
# The `hscc profiles` question has ONE canonical route (audit F2)
# --------------------------------------------------------------------------- #
#
# ``docs/audits/2026-11-23-agent-surface.md:186`` (F2) measured that `hscc
# profiles` "is answered by TWO routes" — this one and `GET /v1/profiles/list`.
# Measured against the payloads they answer two DIFFERENT questions:
#
#   * this route: "which profiles are running kanban cards, how many each"
#     -> ``counts`` / ``total_running`` / ``profiles`` = a list of NAMES;
#   * ``routes_profiles.handle_list``: "what profiles exist" (the roster)
#     -> ``count`` / ``profiles`` = a list of per-profile OBJECTS, and it is
#        the only door to the config whitelist.
#
# Neither is an alias of the other, and both have shipped clients (iOS
# ``ProfilesResponse`` reads the counts, ``ProfileListResponse`` — used by the
# create-card assignee picker — reads the roster). So the F2 fix is not a
# deletion: it is to name the canonical, point the roster at it, and pin the
# boundary so a later payload edit cannot quietly make the roster a second
# answer to this question. Pinned by
# ``hscc-api/tests/test_profiles_route_canonical.py``.

#: The ONE route that answers ``hscc profiles`` (running task counts). The CLI
#: verb renders the same single implementation this handler calls, so the two
#: doors cannot disagree — see :func:`handle_profiles`.
PROFILES_STATUS_ROUTE = "GET /v1/profiles"
#: The registration pattern for :data:`PROFILES_STATUS_ROUTE`. The
#: ``ROUTES.append`` at the bottom of this file compiles THIS constant, so the
#: name in prose and the name in the dispatch table cannot drift apart.
PROFILES_STATUS_PATTERN = r"^/v1/profiles$"
#: Where to send a reader who wanted the roster rather than the counts. A
#: literal on purpose (importing ``routes_profiles`` here would make this
#: module depend on the roster's module to describe its own naming); the test
#: asserts both modules' pointers agree and that this one resolves to a real
#: registered route.
PROFILES_ROSTER_POINTER = "GET /v1/profiles/list"


# --------------------------------------------------------------------------- #
# The dashboard pair (audit §2.6 / D9): two reads, each ONE route per CLI verb
# --------------------------------------------------------------------------- #
#
# ``docs/audits/2026-11-23-agent-surface.md`` measured that the two dashboard
# questions below had no HTTP surface at all (row ``main status [--json]`` and
# row ``orch waker status``), and the operator ruled **yes — reads only**
# (§12.2, D9). The rule that shapes them is audit §5 S1 (no second source of
# truth): each route calls the ONE function the CLI verb itself renders from,
# so the CLI answer and the route answer cannot drift, and each contributes
# nothing to the payload but ``speak``.
#
# What is deliberately NOT here is the other half of each CLI group:
# ``hscc main start`` (spawns/re-attaches the operator's tmux pane),
# ``hscc main wakeups`` (rewrites the wake schedule) and
# ``hscc orch waker enable|disable`` (moves when the operator gets woken).
# Those stay CLI/operator-facing; the two handlers below take no body, no
# ``confirm`` and no ``profile``, so there is no door to reach them from.

#: The ONE route answering ``hscc main status`` (who the configured main
#: orchestrator is and what its pane is doing).
MAIN_STATUS_ROUTE = "GET /v1/main/status"
#: Registration pattern for :data:`MAIN_STATUS_ROUTE`, compiled by the
#: ``ROUTES.append`` below so prose and dispatch table cannot drift.
MAIN_STATUS_PATTERN = r"^/v1/main/status$"
#: The ONE route answering ``hscc orch waker status`` (is the built-in waker
#: enabled, when did it last tick, what is it waiting on).
WAKER_STATUS_ROUTE = "GET /v1/orch/waker/status"
#: Registration pattern for :data:`WAKER_STATUS_ROUTE`.
WAKER_STATUS_PATTERN = r"^/v1/orch/waker/status$"
#: Where a reader who wants the waker turned on or off must go instead of a
#: route: there is none. A literal naming the CLI verb (not an import), so the
#: pointer can be tested against the handler's own docstring.
WAKER_MUTATION_POINTER = "hscc orch waker enable|disable"


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


def _gb(n):
    """Bytes -> GiB as float (rounded to 1 decimal)."""
    return round(n / (1024 ** 3), 1)


def _backing_daemon_host():
    """Assemble the DAEMON HOST machine's own metrics.

    The API server runs ON the daemon host (the operator's Mac, remote from
    the DGX cluster), so 'local' is correct — we do NOT route through
    sparkrun. psutil is imported lazily (never at module top level) so this
    module still imports on interpreters without psutil (tests monkeypatch
    this function and never trigger the import).
    """
    import os
    import platform
    import socket
    import time

    from hscc_daemon import daemon_ops

    _platform = {"Darwin": "macOS", "Linux": "Linux", "Windows": "Windows"}.get(
        platform.system(), platform.system()
    )

    try:
        import psutil
    except Exception:
        psutil = None

    if psutil is not None:
        boot = psutil.boot_time()
        uptime = max(0.0, time.time() - boot)
        try:
            cpu_percent = psutil.cpu_percent(interval=None)
        except Exception:
            cpu_percent = 0.0
        vm = psutil.virtual_memory()
        du = psutil.disk_usage("/")
        try:
            processes = len(psutil.pids())
        except Exception:
            processes = 0
        cpu_count = psutil.cpu_count() or 0
    else:
        # Minimal stdlib fallback so the route never hard-fails without
        # psutil — honest lower-fidelity values, still the right shape.
        uptime = 0.0
        try:
            with open("/proc/uptime") as f:
                uptime = float(f.read().split()[0])
        except Exception:
            try:
                import subprocess
                out = subprocess.run(
                    ["sysctl", "-n", "kern.boottime"], capture_output=True,
                    text=True, timeout=3,
                ).stdout
                import re
                m = re.search(r"sec = (\d+)", out)
                if m:
                    uptime = max(0.0, time.time() - int(m.group(1)))
            except Exception:
                uptime = 0.0
        cpu_percent = 0.0
        try:
            cpu_count = os.cpu_count() or 0
        except Exception:
            cpu_count = 0
        try:
            import shutil
            import types
            total, used, _ = shutil.disk_usage("/")
            vm = types.SimpleNamespace(
                total=total, used=used,
                percent=round(used / total * 100, 1) if total else 0.0,
            )
            du = types.SimpleNamespace(
                total=total, used=used,
                percent=round(used / total * 100, 1) if total else 0.0,
            )
        except Exception:
            import types
            vm = types.SimpleNamespace(total=0, used=0, percent=0.0)
            du = types.SimpleNamespace(total=0, used=0, percent=0.0)
        try:
            processes = len(os.listdir("/proc")) if os.path.isdir("/proc") else 0
        except Exception:
            processes = 0

    try:
        load_avg = [round(x, 1) for x in os.getloadavg()]
    except (OSError, AttributeError):
        load_avg = []

    daemon_pid = daemon_ops.get_pid()
    daemon_running = bool(daemon_pid)
    daemon_uptime = 0
    if daemon_running and psutil is not None:
        try:
            daemon_uptime = max(
                0, int(time.time() - psutil.Process(daemon_pid).create_time())
            )
        except Exception:
            daemon_uptime = 0

    return {
        "hostname": socket.gethostname(),
        "platform": _platform,
        "arch": platform.machine(),
        "uptime_seconds": int(uptime),
        "cpu": {
            "count": cpu_count,
            "percent": round(cpu_percent, 1),
            "load_avg": load_avg,
        },
        "memory": {
            "total_gb": _gb(vm.total),
            "used_gb": _gb(vm.used),
            "percent": round(vm.percent, 1),
        },
        "disk": {
            "total_gb": _gb(du.total),
            "used_gb": _gb(du.used),
            "percent": round(du.percent, 1),
        },
        "processes": processes,
        "daemon_running": daemon_running,
        "daemon_uptime_seconds": daemon_uptime,
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
    """The ONE door from this API to the profile-count implementation.

    ``cmd_profile_status`` is called nowhere else in ``hscc-api``, and that
    single call site is what makes ``GET /v1/profiles`` one implementation of
    the ``hscc profiles`` payload rather than one of two (audit F2). Tests
    monkeypatch THIS function; a second door would have to be added here and
    ``tests/test_profiles_route_canonical.py`` reddens on it.
    """
    eng = _load_cluster_engine()
    if eng is None:
        return None
    return eng.cmd_profile_status()


def _backing_main_status():
    """``hscc main status`` — the CLI's own read, called in-process.

    ``main_cli.status()`` is the function the verb renders (``cmd_main`` calls
    it for ``status`` and prints its dict), so this route and
    ``hscc main status --json`` have ONE implementation: identity through
    ``hscc-roles/main_orch.py`` (``main_name``/``main_profile``/``main_board``),
    pane discovery + busy marker through ``repl_inject`` — the same pair
    ``hscc orch inject`` uses. Nothing here resolves a name, guesses a pane or
    reads a waker.

    Deliberately no argument: ``status(profile=...)`` exists for the CLI's own
    tests, and accepting a caller-supplied profile would let a request ask about
    a profile other than the configured main (profile switching, which D9 ruled
    out). A literal ``status()`` call is the shape of that guarantee.
    """
    from hscc_daemon import main_cli
    return main_cli.status()


def _backing_waker_status():
    """``hscc orch waker status`` — the waker's own status function.

    ``orch_waker.waker_status()`` is what the CLI verb renders AND what the
    doctor row renders (both read that one function), and it is pure file
    reads — config block, the daemon's stream stamp, the waker's own cursor
    ledger and the audit tail. It never ticks the waker and never writes, which
    is why calling it per request is safe while the daemon's loop is mid-scan.

    The enable/disable half is NOT wrapped here (audit D9: reads only); the
    waker's own ``set_enabled`` stays reachable only from the CLI verb.
    """
    from hscc_daemon import orch_waker
    return orch_waker.waker_status()


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


def _speak_daemon_host(data: dict) -> str:
    """§B: host uptime + CPU% + RAM% summary line for the monitor widget."""
    cpu = data.get("cpu") or {}
    mem = data.get("memory") or {}
    up = int(data.get("uptime_seconds") or 0)
    days, rem = divmod(up, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        up_s = f"{days}d {hours}h"
    elif hours:
        up_s = f"{hours}h {mins}m"
    else:
        up_s = f"{mins}m"
    host = data.get("hostname") or "host"
    return (f"{host} up {up_s}, "
            f"{cpu.get('percent', 0.0):g}% CPU, "
            f"{mem.get('percent', 0.0):g}% RAM.")


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


#: The pane states ``main_cli.status()`` reports, each with its §B sentence.
#: ``error`` (the identity resolver itself failed) is NOT in this map: it takes
#: the same "unavailable" wording the other reads use, so a failed read can
#: never be misread as "the main orchestrator is not running".
_MAIN_STATE_SPEAK = {
    "busy": "{name} is running and busy.",
    "idle": "{name} is running and idle.",
    "not-running": "{name} is not running.",
    "ambiguous": "{name} has {panes} panes — the pane target is ambiguous.",
    "unknown": "{name}'s pane state is unknown.",
}


def _speak_main_status(data: dict) -> str:
    """§B: who the configured main orchestrator is, and what its pane is doing."""
    state = data.get("state")
    template = _MAIN_STATE_SPEAK.get(state) if isinstance(state, str) else None
    if template is None:
        return "Main orchestrator status unavailable."
    name = data.get("name") or "The main orchestrator"
    return template.format(name=name, panes=len(data.get("panes") or []))


def _speak_waker_status(data: dict) -> str:
    """§B: whether the built-in waker is enabled, and how fresh its last tick is."""
    if not data.get("enabled"):
        return "Orchestrator waker is disabled."
    stream = data.get("stream") or {}
    boards = data.get("boards") or {}
    n = len(boards)
    tail = f" {n} board{'s' if n != 1 else ''} tracked."
    age = stream.get("tick_age_s")
    if isinstance(age, (int, float)):
        return f"Orchestrator waker is enabled, last tick {int(age)}s ago." + tail
    return ("Orchestrator waker is enabled, but no tick has been recorded yet."
            + tail)


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
    """GET /v1/verify — full `hscc verify` result, per-check pass/fail.

    Inline semantics are deliberate (t_7babf634): this route IS the explicit
    "run the fleet check now" trigger, so it always runs a real verify and
    answers with its own result — it never serves the cache. What it adds is
    that the verdict is also recorded into the shared health snapshot, so a
    manual verify refreshes the health screen and the two routes cannot drift.
    """
    try:
        data = _backing_verify()
    except Exception:
        return 200, {"speak": "Health check unavailable."}
    if not isinstance(data, dict):
        return 200, {"speak": "Health check unavailable."}
    # Record only a verdict-shaped result; a non-verdict must not overwrite the
    # last good snapshot (health_cache.record returns None for it).
    health_cache.record(data)
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


def handle_daemon_host(server, ctx, query, body):
    """GET /v1/daemon/host — the daemon host machine's OWN metrics.

    Read-only. Returns CPU/memory/disk/uptime of the machine running the HSCC
    daemon (the operator's Mac, remote from the DGX cluster) — NOT the cluster
    node metrics that /v1/cluster/* return. The iOS MonitorWidget decodes
    exactly this shape.
    """
    try:
        data = _backing_daemon_host()
    except Exception:
        return 200, {"speak": "Daemon host metrics unavailable."}
    if not isinstance(data, dict):
        return 200, {"speak": "Daemon host metrics unavailable."}
    return 200, {**data, "speak": _speak_daemon_host(data)}


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
    """GET /v1/profiles — the canonical ``hscc profiles`` answer; the profile
    ROSTER is ``GET /v1/profiles/list`` (audit F2).

    Running kanban task counts per profile. The payload has ONE implementation:
    ``profile_status.get_profile_status`` reached through
    ``cluster_engine.cmd_profile_status`` — the same single call the CLI verb
    makes (``hscc_daemon/hscc.py::_handle_profiles`` renders
    ``eng.cmd_profile_status()``), so the CLI and this route cannot drift. This
    handler contributes nothing to the payload but ``speak``.

    NOT the profile roster: a caller wanting every profile plus its config
    whitelist wants ``GET /v1/profiles/list`` (``routes_profiles.handle_list``,
    pointed at by :data:`PROFILES_ROSTER_POINTER`). The two payloads share the
    key name ``profiles`` with DIFFERENT meanings — a list of profile NAMES
    here, a list of profile OBJECTS there — which is exactly how F2 read them
    as one question. That boundary is pinned by
    ``tests/test_profiles_route_canonical.py``; do not "unify" the two.
    """
    try:
        data = _backing_profiles()
    except Exception:
        return 200, {"speak": "Profile status unavailable."}
    if data is None or _is_error_dict(data):
        return 200, {"speak": "Profile status unavailable."}
    return 200, {**data, "speak": _speak_profiles(data)}


def handle_main_status(server, ctx, query, body):
    """GET /v1/main/status — the canonical ``hscc main status`` answer (D9).

    Who the configured main orchestrator is and what its pane is doing:
    ``name``/``profile``/``board`` from the ONE identity resolver
    (``hscc-roles/main_orch.py``), plus ``state``/``running``/``busy``/``pane``/
    ``target``/``panes``/``detail`` from ``repl_inject``'s discovery — the very
    payload ``hscc main status --json`` prints, reached through
    :func:`_backing_main_status`, the single door from this API to
    ``main_cli.status()``. This handler adds nothing to it but ``speak``.

    Read-only by shape, not by promise: no body, no ``confirm``, no
    ``profile`` — and the backing call is a literal ``status()``, so the sibling
    verbs (``main start`` spawns a pane, ``main wakeups`` rewrites the wake
    schedule) have no door here. A pane the CLI cannot see stays invisible here
    (its own honest ``not-running`` / ``ambiguous`` / ``unknown``), and a
    resolver that cannot be read degrades to a 200 with an ``unavailable``
    ``speak`` rather than a 500: dashboards poll this route.
    """
    try:
        data = _backing_main_status()
    except Exception:
        return 200, {"speak": "Main orchestrator status unavailable."}
    if not isinstance(data, dict):
        return 200, {"speak": "Main orchestrator status unavailable."}
    return 200, {**data, "speak": _speak_main_status(data)}


def handle_waker_status(server, ctx, query, body):
    """GET /v1/orch/waker/status — the canonical ``hscc orch waker status`` read (D9).

    Is the daemon-resident orchestrator waker enabled, when did it last tick,
    which boards is it waiting on and which has it stopped nudging: the payload
    ``orch_waker.waker_status()`` returns for the CLI verb and the doctor row
    alike (``enabled``, the whole ``config`` knob block, ``stream`` with
    ``last_tick``/``tick_age_s``, per-board ``boards``, ``paneless_skip``,
    ``audit``), reached through :func:`_backing_waker_status`. This handler adds
    nothing to it but ``speak``.

    Read-only, and the boundary is the ruling: ``enable``/``disable`` are
    deliberately NOT exposed here — turning the waker off silently stops the
    operator being woken, so that write stays on the CLI verb
    ``hscc orch waker enable|disable`` (see :data:`WAKER_MUTATION_POINTER`). The
    read itself is pure file reads and never ticks the waker, so calling it per
    request cannot change the thing it reports; an unreadable waker degrades to
    a 200 with an ``unavailable`` ``speak`` rather than a 500.
    """
    try:
        data = _backing_waker_status()
    except Exception:
        return 200, {"speak": "Orchestrator waker status unavailable."}
    if not isinstance(data, dict):
        return 200, {"speak": "Orchestrator waker status unavailable."}
    return 200, {**data, "speak": _speak_waker_status(data)}


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
ROUTES.append(("GET", re.compile(r"^/v1/daemon/host$"), handle_daemon_host))
ROUTES.append(("GET", re.compile(r"^/v1/triggers$"), handle_triggers))
ROUTES.append(("POST", re.compile(r"^/v1/triggers/run$"), handle_triggers_run))
ROUTES.append(("GET", re.compile(r"^/v1/escalate$"), handle_escalate))
ROUTES.append(("POST", re.compile(r"^/v1/escalate$"), handle_escalate_run))
ROUTES.append(("GET", re.compile(PROFILES_STATUS_PATTERN), handle_profiles))
# The D9 dashboard pair (audit §2.6): one GET per CLI verb, patterns compiled
# from the same constants the prose above names.
ROUTES.append(("GET", re.compile(MAIN_STATUS_PATTERN), handle_main_status))
ROUTES.append(("GET", re.compile(WAKER_STATUS_PATTERN), handle_waker_status))
ROUTES.append(("POST", re.compile(r"^/v1/cluster/up$"), handle_cluster_up))
ROUTES.append(("POST", re.compile(r"^/v1/cluster/down$"), handle_cluster_down))
