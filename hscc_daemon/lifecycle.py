"""Agent lifecycle management for the HSCC daemon."""

import json
import os
import re
import shutil
import time
import datetime

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


# Post-launch serve verification (t_b72dce57 rule 4). sparkrun --no-follow
# returns the moment the container is CREATED — it never proves the engine
# inside serves anything. Incident proof: auto-restart #270 logged "success"
# for a container whose vllm never started (its port was already taken by the
# correct job); the hollow container sat for ~7h behind a lifetime counter
# nobody was watching. A verified probe is slow to arm (a model load takes
# minutes) — the daemon's 30s tick re-checks, and the load-grace window keeps
# the breaker quiet meanwhile.
VLLM_SERVE_VERIFY_TIMEOUT_S = float(os.environ.get("HSCC_VLLM_SERVE_VERIFY_TIMEOUT_S", "180"))
VLLM_SERVE_VERIFY_INTERVAL_S = float(os.environ.get("HSCC_VLLM_SERVE_VERIFY_INTERVAL_S", "5"))

# How long an unresolved launch (serve probe gave up) may sit unverified before
# the operator alarm fires — see _record_launch_verdict.
WATCHDOG_VERIFY_ALARM_AFTER = int(os.environ.get("HSCC_WATCHDOG_VERIFY_ALARM_AFTER", "2"))


def _registered_serving_unit(node, port):
    """The serving.json unit owning (node, port), or None when unregistered.

    serving.json IS the unit registry: `hscc cluster up` (fleet_up_plan) builds
    its whole launch plan from these units and `cluster down` stops exactly
    them — so a registered unit is cluster-up's EXCLUSIVE launch surface. The
    first orchestrator/unit whose own ``port`` matches wins; an orchestrator
    without an explicit unit port falls back to the serving-level port, then
    the VLLM_PORT default (the same precedence serving.orchestrator_endpoint
    uses, so the lookup cannot disagree with the endpoint the fleet advertises).
    """
    try:
        data = serving.load_serving()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    try:
        top_port = int(data.get("port") or serving.VLLM_PORT)
    except (TypeError, ValueError):
        top_port = serving.VLLM_PORT
    for u in (data.get("units") or []):
        if not isinstance(u, dict):
            continue
        if node not in (u.get("nodes") or []):
            continue
        try:
            uport = int(u.get("port") or top_port)
        except (TypeError, ValueError):
            uport = top_port
        if uport == port:
            return u
    return None


def _engine_serves(node, port, timeout):
    """True when the unit on (node, port) streams real generated text.

    THE same streaming probe engine-wedge uses — imported by NAME at call time
    (health does the same lazy dance against lifecycle at import, so a
    module-level import would deadlock on the cycle). A unit whose served model
    cannot be listed falls back to the probe's documented "x" placeholder.
    """
    try:
        from .health import _probe_unit_generation
    except ImportError:  # pragma: no cover — defensive, package imports cleanly
        return False
    res = _probe_unit_generation(node, port, timeout=timeout) or {}
    return bool(res.get("ok"))


def _verify_serves(node, port, probe_fn=None, timeout=None,
                   deadline_fn=None, sleep_fn=None):
    """Poll until the freshly-launched unit actually SERVES, or the budget ends.

    Returns True (serving — verified), False (deadline reached — hollow launch)
    or None (verification impossible: no probe available). False must never be
    counted as a successful restart; that gate is the whole point (rule 4).
    """
    timeout = VLLM_SERVE_VERIFY_TIMEOUT_S if timeout is None else float(timeout)
    if probe_fn is None:
        probe_fn = _engine_serves
    if deadline_fn is None:
        deadline_fn = lambda: time.monotonic() + timeout
    if sleep_fn is None:
        sleep_fn = time.sleep
    deadline = deadline_fn()
    while True:
        # Probe FIRST, even on a zero budget: a zero timeout means "one try,
        # no retrying", not "never look" (tests + degraded fast-fail).
        try:
            if probe_fn(node, port, timeout=max(1.0, VLLM_SERVE_VERIFY_INTERVAL_S)):
                return True
        except Exception as e:
            log(f"Watchdog: serve probe error for {node}:{port}: {e}", "WARN")
        if time.monotonic() >= deadline:
            return False
        sleep_fn(VLLM_SERVE_VERIFY_INTERVAL_S)


def _record_launch_verdict(block, verified, now_wall=None):
    """Fold a post-launch verification into the breaker block + decide the alarm.

    The counter was already incremented BEFORE the attempt, so an attempt is
    never lost — but an UNVERIFIED launch must not read as a success: it keeps
    `last_restart_verified` False and, after WATCHDOG_VERIFY_ALARM_AFTER
    consecutive unverified attempts, the caller alarms the operator ("hollow
    success" was invisible for ~7h in the 2026-10-18 incident). A verified
    launch resets the streak. Returns (verified_bool, alarm_bool).
    """
    now_wall = time.monotonic() if now_wall is None else now_wall
    block["last_restart_verified"] = bool(verified)
    block["last_restart_checked_at"] = now_wall
    if verified:
        block["unverified_restarts"] = 0
        return True, False
    streak = int(block.get("unverified_restarts", 0)) + 1
    block["unverified_restarts"] = streak
    return False, streak >= WATCHDOG_VERIFY_ALARM_AFTER


def restart_vllm(verify_fn=None):
    """Restart vLLM via sparkrun — TARGET'd stop, refuse cluster-up-owned
    units, abort on stop failure, verify the launch before calling it success.

    t_b72dce57 (the 2026-10-18 01:38 race) rewrote this function's contract;
    every rule below was separately violated in production:

      1. The heal lease (heal_lease.py — the SAME durable single-flight lease
         the proxy relaunch deferred on at 01:38:34) is consulted FIRST: while
         any repair is in flight this returns ``deferred`` and touches nothing.
         The watchdog fired its stop+launch 10 s after that WARN.
      2. ``sparkrun stop <TARGET>`` + launch is stop-then-launch — i.e. an
         independent restart — of a unit `hscc cluster up` owns (every unit
         registered in serving.json). That is refused outright: the correct
         relaunch surface is `hscc cluster up`, whose job (03fae933, with the
         served_model_name aliases) is what the watchdog raced.
      3. The stop MUST carry an explicit TARGET; a stop that fails ABORTS the
         restart. The old code logged "Error: Must specify TARGET or --all"
         and launched anyway, producing a second, hollow job on the port.
      4. A launch is only "success" when the unit actually serves: the same
         streaming probe engine-wedge uses gates the result. sparkrun's rc=0
         only means the container was created (rule 4).

    ``verify_fn(node, port, timeout)`` is injectable for tests/sandboxes.
    """
    from . import heal_lease
    from .serving import VLLM_START_CMD

    result = {"ok": False, "output": "", "deferred": False, "refused": False,
              "stop_failed": False, "verified": None, "registered_unit": None}

    # Rule 1 — the heal lease. Held => log the deferral, touch nothing.
    held = heal_lease.read_lease()
    if held is not None:
        msg = (f"vLLM restart deferred — heal in progress: "
               f"{heal_lease.describe(held)}")
        log(f"Restarting vLLM via sparkrun: {msg}", "WARN")
        result.update(deferred=True, output=msg)
        return result

    node = serving.PRIMARY_NODE
    port = serving.VLLM_PORT

    # Rules 2+3 resolve the stop target: the unit's recipe (same source as
    # VLLM_START_CMD's). A registered unit short-circuits — its target is only
    # resolved so the refusal log names the exact stop the watchdog is NOT
    # allowed to issue.
    recipe = ""
    try:
        cmd_recipe = VLLM_START_CMD[2]
        recipe = os.path.expanduser(cmd_recipe) if cmd_recipe else ""
    except (IndexError, TypeError):
        recipe = ""
    unit = _registered_serving_unit(node, port)
    stop_cmd = serving.vllm_stop_cmd(node=node, recipe=recipe, port=None)

    if unit is not None:
        msg = (f"vLLM restart refused: {node}:{port} is registered unit "
               f"{unit.get('id') or '?'} ({unit.get('role') or '?'}) — owned by "
               f"`hscc cluster up`; watchdog never launches cluster-up-owned "
               f"units (no stop issued: {stop_cmd or ['<no target — abort>']})")
        log(msg, "WARN")
        result.update(refused=True, output=msg, registered_unit=unit.get("id"))
        return result

    if stop_cmd is None:
        # Rule 3 without ever issuing the broken argv: no explicit TARGET can
        # be resolved, so there is nothing safe to stop — abort.
        msg = ("vLLM restart aborted: no explicit stop TARGET "
               "(no recipe in serving.json/cluster config); refusing "
               "'Must specify TARGET or --all' no-op stop")
        log(msg, "WARN")
        result.update(stop_failed=True, output=msg)
        return result

    # The lease is what makes stop-then-launch a *repair*, not a second launch
    # racing another repair: take it before the first mutating step and hold it
    # through verification, so engine-wedge recovery / auto-heal / proxy
    # relaunch back off against THIS restart exactly as they do against each
    # other. If a repair started between the check above and here, back off.
    lease_ok, holder = heal_lease.acquire("vllm-auto-restart", f"{node}:{port}")
    if not lease_ok:
        msg = (f"vLLM restart deferred — heal in progress: "
               f"{heal_lease.describe(holder)}")
        log(msg, "WARN")
        result.update(deferred=True, output=msg)
        return result
    try:
        log("Restarting vLLM via sparkrun...")
        try:
            # Rule 3 — the TARGET'd stop; a failed stop ABORTS. No stop means
            # no launch: launching onto a port the stop could not clear is
            # precisely the hollow duplicate job of the incident.
            stop_result = run_cmd(stop_cmd, timeout=30)
            if stop_result.get("ok"):
                log("vLLM stopped")
            else:
                msg = (f"vLLM stop failed ({' '.join(stop_cmd)}): "
                       f"{stop_result.get('output', '')} — aborting restart, "
                       f"no launch attempted")
                log(msg, "WARN")
                result.update(stop_failed=True, output=msg)
                return result

            start_result = run_cmd(VLLM_START_CMD, timeout=30)
            if not start_result.get("ok"):
                msg = f"vLLM start failed: {start_result.get('output', '')}"
                log(f"vLLM restart: failed — {msg}", "WARN")
                result.update(output=msg)
                return result

            # Rule 4 — rc=0 only proves the container was CREATED. Verify it
            # serves before anyone may call this a success.
            verified = _verify_serves(node, port, probe_fn=verify_fn)
            if verified:
                log(f"vLLM restart: success (unit serving on {node}:{port})")
                result.update(ok=True, verified=True,
                              output=start_result.get("output", ""))
            elif verified is None:
                msg = (f"vLLM launched on {node}:{port} but serve verification "
                       f"unavailable — not counted as success")
                log(msg, "WARN")
                result.update(output=msg)
            else:
                msg = (f"vLLM launched on {node}:{port} but never served "
                       f"(hollow launch) — not counted as success")
                log(msg, "WARN")
                result.update(verified=False, output=msg)
            return result
        except Exception as e:
            log(f"vLLM restart exception: {e}", "ERROR")
            result.update(output=str(e))
            return result
    finally:
        heal_lease.release("vllm-auto-restart")


def pipeline_watchdog(check_dgx_fn=None, check_gateway_fn=None,
                      restart_vllm_fn=None, send_macos_notification_fn=None):
    """Watchdog cycle — one per watchdog tick (cadence =
    ``PERIODIC_INTERVALS["watchdog"]``, the SOT since GAP-B-6): check
    DGX+gateway, auto-restart vLLM, block on 3 failures."""
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

    # Intentional-autodown fork (§5, C2): ``autodown.classify()`` is the single
    # decision-table for the serving layer; the watchdog consults its verdict
    # instead of re-deriving the rule. While an intentional autodown is in
    # effect the orchestrator unit is DOWN BY DESIGN — the watchdog must never
    # resurrect it (that is exactly the C2 mid-teardown-resurrection class this
    # file historically got wrong). Keepalive/other units are supervised by the
    # separate check_workers thread (health.py:791) which is intentionally NOT
    # here — so "don't resurrect the orchestrator" is NOT "do nothing".
    intentional = block.get("intentional") == "autodown"

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
            # Intentional-autodown gate on the STATE WRITE (§5 C2): a backoff
            # caused by an INTENTIONAL autodown must NOT write the watchdog
            # stream unhealthy-and-untagged, or `hscc verify` reads a RED fleet
            # during a deliberate power-save. Carry the same `intentional ==
            # "autodown"` marker the reader (verify.check_daemon_streams)
            # requires to excuse a down stream — writer and reader agree. A
            # backoff for any OTHER reason (a real circuit-breaker latch) still
            # writes plain ok:False and still fails verify — never suppressing
            # genuine faults. `intentional` was derived from the block at the
            # top of this fork (line 219).
            if intentional:
                write_state("watchdog", {
                    "ok": False, "blocked": True, "reason": block.get("reason", ""),
                    "intentional": "autodown",
                    "auto_restart_count": block.get("auto_restart_count", 0),
                    "last_check": now_iso(),
                    "message": f"Backing off (intentional autodown): "
                               f"{block.get('reason', '')}",
                })
            else:
                write_state("watchdog", {
                    "ok": False, "blocked": True, "reason": block.get("reason", ""),
                    "auto_restart_count": block.get("auto_restart_count", 0),
                    "last_check": now_iso(),
                    "message": f"Backing off: {block.get('reason', '')}",
                })
            return False
        # Backoff elapsed — clear the breaker and resume checking this cycle.
        if intentional:
            # Intentional autodown owns the block lifecycle: autoup clears it
            # after readiness (or autodown.disable releases it). Never let the
            # watchdog's backoff-elapsed path clear it, or the very next tick
            # would resurrect a deliberately-down orchestrator. The block stays
            # latched; keepalive supervision (check_workers) is unaffected.
            log("Watchdog: intentional autodown in effect — block NOT cleared, "
                "orchestrator left down")
            write_state("watchdog", {
                "ok": False, "blocked": True, "reason": block.get("reason", ""),
                "intentional": "autodown",
                "auto_restart_count": block.get("auto_restart_count", 0),
                "last_check": now_iso(),
                "message": f"Intentional autodown: {block.get('reason', '')}",
            })
            return False
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

    # 1-2 failures — try auto-restart vLLM via its sparkrun recipe.
    # Skipped while an intentional autodown is in effect (§5): the orchestrator
    # is down by design, so restarting it would resurrect a layer autodown
    # deliberately stopped — independent of whether the block is still latched.
    if not dgx_ok and not intentional:
        # t_b72dce57 rule 1 — the ATTEMPT itself is deferred while a heal is in
        # flight, not merely its notification. The incident: engine-wedge
        # recovery held the heal lease (it stopped 'orch' at 01:38:33 and
        # relaunched it via `hscc cluster up` at 01:38:40); the proxy stream
        # honoured that lease and deferred at 01:38:34; this watchdog ignored
        # it 10s later, stopped nothing (TARGET error) and launched a hollow
        # duplicate at 01:39:09. So the lease is checked HERE, before any
        # attempt — the same deferral machinery (heal_lease.read_lease), never
        # a second lock. While held: log + state the deferral, NO attempt, NO
        # counter increment, NO success/failed log, NO alarm notification.
        from . import heal_lease
        held = heal_lease.read_lease()
        if held is not None:
            deferral = (f"vLLM auto-restart deferred — heal in progress: "
                        f"{heal_lease.describe(held)}")
            log(f"Watchdog: {deferral}", "WARN")
            write_state("watchdog", {
                "ok": False,
                "blocked": False,
                "dgx": dgx_ok,
                "gateway": gw_ok,
                "auto_restart": False,
                "auto_restart_deferred": True,
                "heal_in_progress": heal_lease.describe(held),
                "auto_restart_count": block.get("auto_restart_count", 0),
                "last_check": now_iso(),
                "message": deferral,
            })
            return False
        # Attempt (rules 2-4 enforced inside restart_vllm, which is also where
        # the TARGET'd stop + registered-unit refusal live). The lifetime
        # counter advances ONLY on a VERIFIED serving restart (rule 4): the
        # incident's "#270: success" was a hollow launch that served nothing
        # for ~7h, so an unverified attempt must never consume a counter slot.
        # `unverified_restarts` is the streak that drives the hollow-launch
        # alarm; refusals/deferrals/stop-aborts make no launch and so neither
        # count nor reset anything.
        log("Watchdog: attempting vLLM auto-restart via sparkrun")
        restart_result = restart_vllm_fn() or {}
        restart_ok = bool(restart_result.get("ok"))
        refused = bool(restart_result.get("refused"))
        deferred_by_restart = bool(restart_result.get("deferred"))
        stop_failed = bool(restart_result.get("stop_failed"))
        outcome = ("success" if restart_ok else
                   "deferred" if deferred_by_restart else
                   "refused" if refused else
                   "stop-failed" if stop_failed else "hollow" if
                   restart_result.get("verified") is False else "failed")
        count = block.get("auto_restart_count", 0)
        verified = None
        alarm = False
        if restart_ok:
            count += 1
            block["auto_restart_count"] = count
            # Load-grace window ONLY on a verified serving restart (rule 4).
            block["restart_cooldown_until"] = (
                datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(minutes=VLLM_LOAD_GRACE_MINUTES)
            ).isoformat()
            verified = True
        if not (deferred_by_restart or refused or stop_failed):
            # A launch happened (success or hollow/failed) — fold the verdict
            # in and decide whether the hollow-launch alarm must fire.
            verified, alarm = _record_launch_verdict(block, restart_ok)
        block["last_restart"] = now_iso()
        save_watchdog_block(block)
        write_state("watchdog", {
            "ok": False,
            "blocked": False,
            "dgx": dgx_ok,
            "gateway": gw_ok,
            "auto_restart": True,
            "restart_result": restart_ok,
            "restart_verified": verified,
            "restart_refused": refused,
            "restart_deferred": deferred_by_restart,
            "restart_stop_failed": stop_failed,
            "restart_output": restart_result.get("output", "")[:200],
            "auto_restart_count": count,
            "last_check": now_iso(),
            "message": (f"Auto-restart #{count}: success" if restart_ok
                        else f"Auto-restart attempt: {outcome}"),
        })
        if not deferred_by_restart and not refused:
            # A real attempt happened: log + notify its REAL outcome — a
            # hollow launch is never logged "success", and a streak of them
            # escalates to the critical alarm (rule 4).
            if restart_ok:
                log(f"Watchdog: vLLM auto-restart #{count}: success")
            else:
                # Keep the `#N: failed` shape hscc-api/routes_history.py
                # greps for; N is the display attempt-number, and the text
                # says plainly that the lifetime counter did NOT move.
                log(f"Watchdog: vLLM auto-restart #{count + 1}: failed "
                    f"({outcome}; counter not advanced, stays #{count})", "WARN")
            if alarm:
                send_macos_notification_fn(
                    "HSCC vLLM Auto-Restart NOT SERVING",
                    f"{block.get('unverified_restarts', 0)} consecutive vLLM "
                    f"restart attempts produced no serving unit (last: "
                    f"{restart_result.get('output', '')[:120]}) — check "
                    f"{serving.PRIMARY_NODE}:{serving.VLLM_PORT}",
                    priority="critical",
                )
            else:
                send_macos_notification_fn(
                    "HSCC vLLM Auto-Restart",
                    f"Auto-restart of vLLM on {serving.PRIMARY_NODE}: "
                    f"{'OK' if restart_ok else 'NOT SERVING' if verified is False else outcome.upper()}",
                    priority="high",
                )
    else:
        if intentional:
            # Orchestrator down by design — report reality, do NOT restart.
            write_state("watchdog", {
                "ok": False,
                "blocked": False,
                "intentional": "autodown",
                "dgx": dgx_ok,
                "gateway": gw_ok,
                "last_check": now_iso(),
                "auto_restart_count": block.get("auto_restart_count", 0),
                "message": "Intentional autodown — orchestrator left down "
                           "(auto-restart suppressed)",
            })
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
