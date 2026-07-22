"""Agent lifecycle management for the HSCC daemon."""

import json
import os
import re
import datetime
import shutil

from .daemon_ops import log
from . import serving
from .state import now_iso, read_state
from .util import run_cmd


BRIDGE_FILE = os.path.expanduser("~/.hscc/bridge.json")
WORKER_MAX_RUNTIME_MINUTES = int(os.environ.get("HSCC_WORKER_MAX_RUNTIME_MINUTES", "360"))


def reconcile_lifecycle(agents):
    """Sync lifecycle.json to the authoritative agents.json status.

    Nothing transitions lifecycle running->idle on normal task completion, so
    lifecycle.json drifts to a stale 'running' while agents.json status (kept
    current by provision/heartbeat) already reads 'idle'. Converge the FSM to
    the authoritative field: when status is idle but lifecycle says running,
    write the idle transition. Only running->idle is reconciled — transitional
    states (spawning/ready/finished) are left untouched to avoid racing an
    in-flight spawn.
    """
    lc_file = os.path.expanduser("~/.hscc/lifecycle.json")
    if not os.path.exists(lc_file):
        return
    try:
        with open(lc_file) as f:
            lc_data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return

    states = lc_data.get("agents", {})
    status_by_id = {a.get("id"): a.get("status") for a in agents}
    changed = []
    for aid, entry in states.items():
        if entry.get("state") == "running" and status_by_id.get(aid) == "idle":
            entry["state"] = "idle"
            entry["updated_at"] = now_iso()
            entry["reconciled"] = True
            changed.append(aid)

    if changed:
        lc_data["agents"] = states
        tmp = lc_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(lc_data, f, indent=2, default=str)
        os.replace(tmp, lc_file)
        log(f"Reconciled lifecycle: {len(changed)} agents updated")


def find_hermes_bin():
    """Find the hermes executable (in venv or PATH)."""
    hmdir = os.path.expanduser("~")
    candidates = [
        os.path.join(hmdir, ".hermes/hermes-agent/venv/bin/hermes"),
        shutil.which("hermes"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _kanban_task_status(task_id):
    """Get the status of a kanban task."""
    kanban_file = os.path.expanduser("~/.hermes/kanban.json")
    try:
        with open(kanban_file) as f:
            data = json.load(f)
        for task in data.get("tasks", []):
            if task.get("id") == task_id:
                return task.get("status")
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return "unknown"


def refresh_live_workers():
    """Update workers.json with live worker status."""
    workers_file = os.path.expanduser("~/.hscc/workers.json")
    workers = []

    # Check each node
    for node in os.environ.get("HSCC_NODES", "").split(","):
        node = node.strip()
        if not node:
            continue
        try:
            result = run_cmd(f"curl -s http://{node}:8080/health", timeout=5, shell=True)
            if result and result.get("ok"):
                workers.append({
                    "node": node,
                    "status": "online",
                    "last_check": now_iso(),
                })
            else:
                workers.append({
                    "node": node,
                    "status": "offline",
                    "last_check": now_iso(),
                })
        except Exception:
            workers.append({
                "node": node,
                "status": "unknown",
                "last_check": now_iso(),
            })

    tmp = workers_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"workers": workers}, f, indent=2)
    os.replace(tmp, workers_file)


# ── Pipeline Watchdog ────────────────────────────────────────────────────

WATCHDOG_BLOCK_FILE = os.path.expanduser("~/.hscc/watchdog-block.json")
VLLM_LOAD_GRACE_MINUTES = int(os.environ.get("HSCC_VLLM_LOAD_GRACE_MINUTES", "20"))
# After the breaker trips, back off this long, then auto-clear and resume trying.
# "Relentless, not infinite": repeated failures slow retries instead of stopping
# them forever, so a transient outage self-heals once the cluster recovers.
WATCHDOG_BACKOFF_MINUTES = int(os.environ.get("HSCC_WATCHDOG_BACKOFF_MINUTES", "10"))


def load_watchdog_block():
    """Load the block state file."""
    try:
        with open(WATCHDOG_BLOCK_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"blocked": False, "reason": "", "blocked_at": None, "failures": [], "auto_restart_count": 0}


def save_watchdog_block(data):
    """Save the block state file."""
    tmp = WATCHDOG_BLOCK_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, WATCHDOG_BLOCK_FILE)


def cleanup_old_failures(failures, window_minutes=10):
    """Keep only failures within the last window_minutes."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=window_minutes)
    result = []
    for f in failures:
        ts = f.get("timestamp", "")
        if not ts:
            result.append(f)
            continue
        try:
            entry_time = datetime.datetime.fromisoformat(ts).replace(tzinfo=datetime.timezone.utc)
            if entry_time > cutoff:
                result.append(f)
        except (ValueError, TypeError):
            result.append(f)
    return result


def restart_vllm():
    """Restart vLLM via sparkrun."""
    from .serving import VLLM_STOP_CMD, VLLM_START_CMD
    from .state import write_state

    log("Restarting vLLM via sparkrun...")
    try:
        # Stop current instance
        stop_result = run_cmd(VLLM_STOP_CMD, timeout=30)
        if stop_result.get("ok"):
            log("vLLM stopped")
        else:
            log(f"vLLM stop failed: {stop_result.get('output', '')}", "WARN")

        # Start new instance
        start_result = run_cmd(VLLM_START_CMD, timeout=30)
        success = start_result.get("ok")
        log(f"vLLM restart: {'success' if success else 'failed'}")
        return {"ok": success, "output": start_result.get("output", "")}
    except Exception as e:
        log(f"vLLM restart exception: {e}", "ERROR")
        return {"ok": False, "output": str(e)}


def pipeline_watchdog(check_dgx_fn=None, check_gateway_fn=None,
                      restart_vllm_fn=None, send_macos_notification_fn=None):
    """Watchdog cycle (every 30s): check DGX+gateway, auto-restart vLLM, block on 3 failures."""
    from .health import check_dgx, check_gateway
    from .desktop import send_macos_notification
    from .state import write_state

    if not check_dgx_fn:
        check_dgx_fn = check_dgx
    if not check_gateway_fn:
        check_gateway_fn = check_gateway
    if not restart_vllm_fn:
        restart_vllm_fn = restart_vllm
    if not send_macos_notification_fn:
        send_macos_notification_fn = send_macos_notification

    log("Running PipelineWatchdog")

    block = load_watchdog_block()

    # If currently blocked, stay backed off until the backoff window elapses,
    # then auto-clear and resume — instead of giving up permanently. This makes
    # the breaker a backoff, not a dead-end: a transient outage self-heals once
    # the cluster recovers, with retries merely slowed while it's down.
    if block.get("blocked"):
        blocked_at = block.get("blocked_at")
        elapsed_ok = False
        if blocked_at:
            try:
                ba = datetime.datetime.fromisoformat(blocked_at).replace(
                    tzinfo=datetime.timezone.utc)
                elapsed_ok = datetime.datetime.now(datetime.timezone.utc) >= \
                    ba + datetime.timedelta(minutes=WATCHDOG_BACKOFF_MINUTES)
            except (ValueError, TypeError):
                elapsed_ok = True  # unparseable timestamp — don't latch forever
        if not elapsed_ok:
            log("Watchdog: backing off (blocked), will retry after cooldown")
            write_state("watchdog", {
                "ok": False, "blocked": True, "reason": block.get("reason", ""),
                "auto_restart_count": block.get("auto_restart_count", 0),
                "last_check": now_iso(),
                "message": f"Backing off: {block.get('reason', '')}",
            })
            return False
        # Backoff elapsed — clear the breaker and resume checking this cycle.
        log("Watchdog: backoff elapsed, clearing block and resuming checks")
        block["blocked"] = False
        block["failures"] = []
        block.pop("blocked_at", None)
        block.pop("reason", None)
        save_watchdog_block(block)

    # Run DGX + gateway checks
    dgx_ok = check_dgx_fn()
    gw_ok = check_gateway_fn()

    if dgx_ok and gw_ok:
        # Success — reset failure history if within window
        success_entry = {"timestamp": now_iso(), "dgx": True, "gateway": True}
        failures = block.get("failures", [])
        failures.append(success_entry)
        block["failures"] = cleanup_old_failures(failures, window_minutes=10)
        block["failed_count"] = 0
        block.pop("restart_cooldown_until", None)  # model loaded — end grace period
        save_watchdog_block(block)
        write_state("watchdog", {
            "ok": True,
            "blocked": False,
            "dgx": dgx_ok,
            "gateway": gw_ok,
            "last_check": now_iso(),
            "message": "Pipeline healthy",
            "auto_restart_count": block.get("auto_restart_count", 0),
        })
        log("Watchdog: pipeline healthy")
        return True

    # Failure detected. If a restart is still within its load-grace window, the
    # model is legitimately loading — don't count this toward the breaker.
    cooldown_until = block.get("restart_cooldown_until")
    if cooldown_until and now_iso() < cooldown_until:
        write_state("watchdog", {
            "ok": False,
            "blocked": False,
            "dgx": dgx_ok,
            "gateway": gw_ok,
            "last_check": now_iso(),
            "auto_restart_count": block.get("auto_restart_count", 0),
            "message": f"vLLM restarting — model loading (grace until {cooldown_until})",
        })
        log(f"Watchdog: in restart grace window until {cooldown_until}, model still loading — not counting failure")
        return False

    # Failure — record it
    failure_entry = {"timestamp": now_iso(), "dgx": dgx_ok, "gateway": gw_ok}
    failures = block.get("failures", [])
    failures.append(failure_entry)
    block["failures"] = cleanup_old_failures(failures, window_minutes=10)
    block["failed_count"] = len(block["failures"])

    # Count recent failures
    recent = [f for f in block["failures"] if not f.get("dgx", True) or not f.get("gateway", True)]

    if len(recent) >= 3:
        # BLOCK — don't do more checks or restarts
        block["blocked"] = True
        block["blocked_at"] = now_iso()
        reason = f"3 consecutive failures in 10min: DGX={'OK' if dgx_ok else 'FAIL'} GW={'OK' if gw_ok else 'FAIL'}"
        block["reason"] = reason
        log(f"Watchdog: BLOCKING pipeline — {reason}")
        save_watchdog_block(block)
        write_state("watchdog", {
            "ok": False,
            "blocked": True,
            "reason": reason,
            "last_check": now_iso(),
            "message": "PIPELINE BLOCKED — manual intervention required",
            "auto_restart_count": block.get("auto_restart_count", 0),
        })
        # Send desktop notification
        send_macos_notification_fn(
            "HSCC Pipeline Blocked",
            reason,
            priority="critical",
        )
        return False

    # 1-2 failures — try auto-restart vLLM via its sparkrun recipe
    if not dgx_ok:
        log("Watchdog: attempting vLLM auto-restart via sparkrun")
        restart_result = restart_vllm_fn()
        restart_ok = restart_result.get("ok", False)
        count = block.get("auto_restart_count", 0) + 1
        block["auto_restart_count"] = count
        block["last_restart"] = now_iso()
        # Open a load-grace window so the next checks don't latch the breaker
        # while the 35B model is still loading inside the container.
        if restart_ok:
            block["restart_cooldown_until"] = (
                datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(minutes=VLLM_LOAD_GRACE_MINUTES)
            ).isoformat()
        save_watchdog_block(block)
        write_state("watchdog", {
            "ok": False,
            "blocked": False,
            "dgx": dgx_ok,
            "gateway": gw_ok,
            "auto_restart": True,
            "restart_result": restart_result.get("ok", False),
            "restart_output": restart_result.get("output", "")[:200],
            "auto_restart_count": count,
            "last_check": now_iso(),
            "message": f"Auto-restart #{count} attempted",
        })
        log(f"Watchdog: vLLM auto-restart #{count}: {'success' if restart_ok else 'failed'}")
        send_macos_notification_fn(
            "HSCC vLLM Auto-Restart",
            f"Auto-restart #{count} of vLLM attempted on {serving.PRIMARY_NODE}: {'OK' if restart_ok else 'FAILED'}",
            priority="high",
        )
    else:
        write_state("watchdog", {
            "ok": False,
            "blocked": False,
            "dgx": dgx_ok,
            "gateway": gw_ok,
            "last_check": now_iso(),
            "auto_restart_count": block.get("auto_restart_count", 0),
            "message": "Degraded — gateway not reachable",
        })

    return dgx_ok and gw_ok
