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
        from .cli import cmd_start
        cmd_start()
    elif cmd == "stop":
        from .cli import cmd_stop
        cmd_stop()
    elif cmd == "status":
        from .cli import cmd_status
        cmd_status()
    elif cmd == "check":
        from .cli import cmd_check
        cmd_check(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "watch":
        from .cli import cmd_watch
        cmd_watch(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "triggers":
        from .cli import cmd_triggers
        cmd_triggers()
    elif cmd == "notify":
        from .cli import cmd_notify
        cmd_notify(" ".join(sys.argv[2:])) if len(sys.argv) > 2 else print("Usage: hscc_daemon notify <message>")
    elif cmd == "plist":
        from .install import cmd_plist
        cmd_plist()
    elif cmd == "install":
        from .install import cmd_install
        cmd_install()
    elif cmd == "uninstall":
        from .install import cmd_uninstall
        cmd_uninstall()
    elif cmd == "log":
        from .cli import cmd_log
        cmd_log()
    elif cmd == "start-daemon":
        from .cli import cmd_start_daemon
        cmd_start_daemon()
    elif cmd == "ed-status":
        from .cli import cmd_ed_status
        cmd_ed_status()
    elif cmd == "ed-install":
        from .cli import cmd_ed_install
        cmd_ed_install()
    elif cmd == "ed-uninstall":
        from .cli import cmd_ed_uninstall
        cmd_ed_uninstall()
    else:
        print(f"Unknown command: {cmd}")
        print(f"Available: start, stop, status, check, watch, triggers, notify, plist, install, uninstall, log, start-daemon")
        sys.exit(1)


if __name__ == "__main__":
    main()
