"""Hermes Spark Cluster Control (HSCC) — Monitoring Daemon & Watchdog.

This is the monolithic daemon refactored into modular components.
Each submodule imports cleanly and the main entry point dispatches to commands.

Modules:
  serving     — Cluster topology resolution (cluster.json/serving.json)
  state       — State directory management (thread-safe reads/writes)
  util        — Utility functions (run_cmd, ISO helpers)
  health      — Check functions (dgx, gateway, local, heartbeat, nas, workers)
  lifecycle   — Agent lifecycle (reconciliation, relaunch, pipeline_watchdog)
  trigger     — Trigger engine (rule evaluation, cooldowns, event firing)
  desktop     — Notifications (macOS/Linux/Desktop, event emitter)
  daemon_ops  — Daemon lifecycle (PID, log, stream watcher)
  install     — Service management (launchd/systemd plist/unit)
  cli         — CLI commands and main entry point
"""

import sys
import os
import re

# ── Re-exports for backward compatibility (tests load this file directly) ──

from hscc_daemon.serving import (
    load_serving,
    compute_base_url_change,
    resolve_cluster_config,
    orchestrator_nodes,
    orchestrator_head,
    orchestrator_endpoint,
    orchestrator_recipe,
    serving_port,
    _serving_warn,
    _endpoint_healthy,
)
from hscc_daemon.health import (
    check_dgx,
    check_gateway,
    check_local,
    check_heartbeat,
    check_nas,
    check_idle_monitor,
    check_workers,
    _gateway_job_alive,
)
from hscc_daemon.lifecycle import (
    pipeline_watchdog,
    save_watchdog_block,
)
from hscc_daemon.serving import (
    update_orchestrator_followers,
    _read_prev_orch_endpoint,
    _write_prev_orch_endpoint,
)
from hscc_daemon.state import (
    read_state,
    read_all_states,
    write_state,
    now_iso,
)
from hscc_daemon.util import run_cmd

# Constants (migrated from old monolithic hscc.py)
BRIDGE_FILE = os.path.expanduser("~/.hscc/bridge.json")
PROFILES_DIR = os.path.expanduser("~/.hermes/profiles")
ORCH_ENDPOINT_STATE = os.path.expanduser("~/.hscc/orch-endpoint")


# ── Legacy kanban/task helpers (migrated from old monolithic hscc.py) ──


def _kanban_task_status(board, kanban_id, timeout=20):
    """Return (status, started_at) for a kanban task via the CLI, or (None, None)."""
    from .util import run_cmd
    from .lifecycle import find_hermes_bin
    r = run_cmd(
        [find_hermes_bin(), "kanban", "--board", board, "show", kanban_id, "--json"],
        timeout=timeout, as_json=True,
    )
    j = r.get("json")
    if not isinstance(j, dict):
        return None, None
    return j.get("status"), j.get("started_at")


def live_dispatch_hosts():
    """Worker hosts running a UNIT-ROUTED dispatched task, exempt from reaping."""
    import json
    try:
        with open(BRIDGE_FILE) as f:
            bridge = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()
    hosts = set()
    for e in bridge.get("tasks", {}).values():
        if not e.get("unit_id"):
            continue
        host = e.get("worker_host")
        if not host:
            continue
        status = e.get("status")
        if status == "held":
            hosts.add(host)
        elif status == "released":
            board, kid = e.get("board"), e.get("kanban_id")
            kstatus = (_kanban_task_status(board, kid)[0]
                       if board and kid else None)
            if kstatus not in ("done", "review", "archived", "blocked"):
                hosts.add(host)
    return hosts


# ── Orchestrator follower helpers (for test patching) ──────────────────────


def _read_prev_orch_endpoint():
    """Read the previously-applied orchestrator endpoint from disk."""
    try:
        with open(ORCH_ENDPOINT_STATE) as f:
            return f.read().strip() or None
    except (FileNotFoundError, OSError):
        return None


def _write_prev_orch_endpoint(endpoint):
    """Persist orchestrator endpoint, writing atomically via tmp+rename."""
    try:
        tmp = ORCH_ENDPOINT_STATE + ".tmp"
        with open(tmp, "w") as f:
            f.write(endpoint)
        os.replace(tmp, ORCH_ENDPOINT_STATE)
    except OSError as e:
        log(f"base_url follower: could not persist orch endpoint: {e}", "WARN")


def update_orchestrator_followers():
    """Repoint managed profiles that track the OLD orchestrator endpoint to the
    NEW one when serving.json re-maps the orchestrator.

    Only profiles whose base_url == the previously-applied orchestrator endpoint
    are rewritten. Worker profiles point at their own node and never match,
    so the model split is preserved. The new endpoint is health-validated
    before any file is touched. No-op on first run.
    """
    from .health import log  # circular import guard
    
    serving = load_serving()
    new_endpoint = orchestrator_endpoint(serving)
    if not new_endpoint:
        return
    old_endpoint = _read_prev_orch_endpoint()
    if old_endpoint == new_endpoint:
        return
    if old_endpoint is None:
        _write_prev_orch_endpoint(new_endpoint)
        return
    
    if not _endpoint_healthy(new_endpoint):
        log(f"base_url follower: new orchestrator {new_endpoint} not healthy "
            f"(/models != 200); deferring profile rewrites", "WARN")
        return

    if not os.path.isdir(PROFILES_DIR):
        _write_prev_orch_endpoint(new_endpoint)
        return

    changed = 0
    had_failure = False
    for name in sorted(os.listdir(PROFILES_DIR)):
        cfg = os.path.join(PROFILES_DIR, name, "config.yaml")
        if not os.path.isfile(cfg):
            continue
        try:
            with open(cfg) as f:
                lines = f.readlines()
        except OSError as e:
            log(f"base_url follower: cannot read {cfg}: {e}", "WARN")
            had_failure = True
            continue
        dirty = False
        for i, line in enumerate(lines):
            m = re.match(
                r'^(?P<indent>\s*)base_url:\s*(?P<q>["\']?)(?P<url>\S+?)(?P=q)\s*$',
                line.rstrip("\n"),
            )
            if not m:
                continue
            repl = compute_base_url_change(m.group("url"), old_endpoint,
                                           new_endpoint)
            if repl:
                lines[i] = f'{m.group("indent")}base_url: {repl}\n'
                dirty = True
        if not dirty:
            continue
        try:
            tmp = cfg + ".tmp"
            with open(tmp, "w") as f:
                f.writelines(lines)
            os.replace(tmp, cfg)
            changed += 1
            log(f"base_url follower: {name} -> {new_endpoint}")
        except OSError as e:
            log(f"base_url follower: cannot write {cfg}: {e}", "WARN")
            had_failure = True

    if had_failure:
        log(f"base_url follower: {old_endpoint} -> {new_endpoint}, "
            f"{changed} updated but some profiles FAILED; endpoint state NOT "
            f"advanced — will retry next tick", "ERROR")
        return
    log(f"base_url follower: {old_endpoint} -> {new_endpoint}, "
        f"{changed} profile(s) updated")
    _write_prev_orch_endpoint(new_endpoint)


from hscc_daemon.daemon_ops import (
    log,
    ensure_state_dir,
    run_daemon_loop,
)

# Backward compat: log and now_iso are in multiple modules; keep top-level names
# (already imported above)

# ── CLI Entry Point ────────────────────────────────────────────────────────

USAGE = """
Hermes Spark Cluster Control (HSCC) — Monitoring Daemon & Watchdog

Usage: hscc_daemon <command> [args]

Commands:
  start              Start the daemon in the background
  stop               Gracefully stop a running daemon
  status             Show daemon status and last check results
  check [stream]     Run a single check cycle (dgx|gateway|local|heartbeat|nas|watchdog|triggers|all)
  watch [stream]     Tail check results in real-time
  triggers           Show trigger engine status
  notify <msg>       Send a manual desktop notification (macOS/Linux)
  plist              Generate Launchd plist for auto-start
  install            Install Launchd plist and start daemon
  uninstall          Remove Launchd plist and stop daemon
  log                Show daemon log output

Internal (called by Launchd):
  start-daemon       Start daemon loop directly
"""


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h", "help"):
        print(USAGE.strip())
        sys.exit(0)

    cmd = sys.argv[1].lower()

    # Import commands on-demand to avoid circular imports
    if cmd == "start":
        from hscc_daemon.cli import cmd_start
        cmd_start()
    elif cmd == "stop":
        from hscc_daemon.cli import cmd_stop
        cmd_stop()
    elif cmd == "status":
        from hscc_daemon.cli import cmd_status
        cmd_status()
    elif cmd == "check":
        from hscc_daemon.cli import cmd_check
        cmd_check(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "watch":
        from hscc_daemon.cli import cmd_watch
        cmd_watch(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "triggers":
        from hscc_daemon.cli import cmd_triggers
        cmd_triggers()
    elif cmd == "notify":
        from hscc_daemon.cli import cmd_notify
        cmd_notify(" ".join(sys.argv[2:])) if len(sys.argv) > 2 else print("Usage: hscc_daemon notify <message>")
    elif cmd == "plist":
        from hscc_daemon.install import cmd_plist
        cmd_plist()
    elif cmd == "install":
        from hscc_daemon.install import cmd_install
        cmd_install()
    elif cmd == "uninstall":
        from hscc_daemon.install import cmd_uninstall
        cmd_uninstall()
    elif cmd == "log":
        from hscc_daemon.cli import cmd_log
        cmd_log()
    elif cmd == "start-daemon":
        from hscc_daemon.cli import cmd_start_daemon
        cmd_start_daemon()
    elif cmd == "ed-status":
        from hscc_daemon.cli import cmd_ed_status
        cmd_ed_status()
    elif cmd == "ed-install":
        from hscc_daemon.cli import cmd_ed_install
        cmd_ed_install()
    elif cmd == "ed-uninstall":
        from hscc_daemon.cli import cmd_ed_uninstall
        cmd_ed_uninstall()
    else:
        print(f"Unknown command: {cmd}")
        print(f"Available: start, stop, status, check, watch, triggers, notify, plist, install, uninstall, log, start-daemon")
        sys.exit(1)


if __name__ == "__main__":
    main()
