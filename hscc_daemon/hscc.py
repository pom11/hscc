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
import json
from pathlib import Path

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
    _worker_recipe_for,
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


def on_state_change(stream: str) -> None:
    """State file changed — log and trigger re-evaluation of trigger rules.

    Used as a callback registered with the event bridge so that any state
    change automatically re-runs the trigger engine.
    """
    from .health import log
    from .trigger import trigger_engine
    log(f"Event-driven state change: {stream}")
    try:
        trigger_engine()
    except Exception as e:
        log(f"Post-state-change trigger eval error: {e}", "ERROR")


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

def _get_version():
    """Read VERSION from the project root, fallback to 1.0.0."""
    try:
        version_file = Path(__file__).resolve().parent.parent / "VERSION"
        if version_file.is_file():
            return version_file.read_text().strip() or "1.0.0"
    except Exception:
        pass
    return "1.0.0"


def _get_help_text():
    """Build the full grouped help text with live version."""
    version = _get_version()
    return f"""\
HSCC — Hermes Spark Cluster Control   v{version}
Turn a DGX Spark GPU cluster into a self-running fleet of AI agents.

Usage:  hscc <command> [args]        hscc help <command>   for details

Daemon control
  start                Start the monitoring daemon in the background
  stop                 Gracefully stop the running daemon
  status               Daemon status + last result of every health stream
  install              Install the launchd service (auto-start at login)
  uninstall            Remove the service and stop the daemon
  plist                Print the launchd plist (no install)
  log                  Show the daemon log output

Health & monitoring
  check [stream]       Run one check cycle now (default: all)
                         streams: dgx gateway local heartbeat nas watchdog triggers
  watch [stream]       Live-tail check results
  triggers             Show trigger-engine rules and recent firings
  verify               Run a full compatibility/health smoke-test of the cluster
  stats [days]         Fleet activity — completions & tool usage over N days (default 7)
  throughput           Aggregate vLLM throughput + queue depth across the fleet
  autoscale            Show autoscale decision from current queue depth (read-only)
  escalate             Show pending failure escalations (dry-run, no mutations)

Cluster & templates
  cluster status       Running workloads + idle hosts
  cluster hosts        All cluster hosts and saved sparkrun clusters
  cluster monitor      One CPU/RAM/GPU snapshot across the fleet
  cluster jobs         All sparkrun jobs currently running
  cluster info         Detailed resolved cluster configuration
  cluster stop <id>    Stop a running workload by container id
  template list        List available cluster templates (declared fleet layouts)
  template status      Which template is currently applied
  template preview <name>    Dry-run: what applying <name> would change
  template validate <name>   Preflight-check a template is deployable
  template apply <name> [--confirm]   Apply a template (--confirm to execute)
  profiles             Running kanban task counts per profile

Utility
  notify <message>     Send a desktop notification

Examples
  hscc status                          # is the daemon healthy?
  hscc check gateway                   # re-run just the gateway check
  hscc template list                   # see the declared fleet layouts
  hscc template preview 3node-coding
  hscc template apply 3node-coding --confirm
  hscc cluster status

Docs: ~/dev/hscc/README.md   Slash commands (in Hermes chat): /cluster /workers-up /cluster-restart /template

Run `hscc help advanced` for internal/service-manager commands."""


def _get_advanced_help():
    """Return help text for advanced/internal commands."""
    return """\
Advanced / internal commands (omitted from main help):

  start-daemon       Run the daemon loop in the foreground (used by launchd)
  ed-status          Event-driven mode status (placeholder, not yet available)
  ed-install         Event-driven mode install (placeholder, not yet available)
  ed-uninstall       Event-driven mode uninstall (placeholder, not yet available)
"""


COMMAND_HELP = {
    "start": "Start the monitoring daemon in the background",
    "stop": "Gracefully stop the running daemon",
    "status": "Daemon status + last result of every health stream",
    "install": "Install the launchd service (auto-start at login)",
    "uninstall": "Remove the service and stop the daemon",
    "plist": "Print the launchd plist (no install)",
    "log": "Show the daemon log output",
    "check": "Run one check cycle now. Usage: hscc check [stream]\n  streams: dgx gateway local heartbeat nas watchdog triggers",
    "watch": "Live-tail check results. Usage: hscc watch [stream]",
    "triggers": "Show trigger-engine rules and recent firings",
    "notify": "Send a desktop notification. Usage: hscc notify <message>",
    "cluster": "Cluster management commands.\n  Subcommands: status hosts monitor jobs info stop <id>\n  Usage: hscc cluster <subcommand> [args]",
    "template": "Cluster template commands.\n  Subcommands: list status preview <name> validate <name> apply <name> [--confirm]\n  Usage: hscc template <subcommand> [args]",
    "profiles": "Running kanban task counts per profile",
    "help": "Show help. Usage: hscc help [command]\n  Use 'hscc help advanced' for internal commands.",
    "verify": "Run a full compatibility/health smoke-test of the cluster.\n  Usage: hscc verify [--json]",
    "stats": "Fleet activity — completions & tool usage over N days (default 7).\n  Usage: hscc stats [days] [--json]",
    "throughput": "Aggregate vLLM throughput + queue depth across the fleet.\n  Usage: hscc throughput [--json]",
    "autoscale": "Show autoscale decision from current queue depth (read-only, never scales).\n  Usage: hscc autoscale [--json]",
    "escalate": "Show pending failure escalations (dry-run, no mutations).\n  Usage: hscc escalate [--json]",
    "advanced": "Internal/service-manager commands.\n  See 'hscc help advanced' for details.",
    "start-daemon": "Run the daemon loop in the foreground (used by launchd)",
    "ed-status": "Event-driven mode status (placeholder, not yet available)",
    "ed-install": "Event-driven mode install (placeholder, not yet available)",
    "ed-uninstall": "Event-driven mode uninstall (placeholder, not yet available)",
}


CLUSTER_SUBCOMMANDS = {"status", "hosts", "monitor", "jobs", "info", "stop"}
TEMPLATE_SUBCOMMANDS = {"list", "status", "preview", "validate", "apply"}


def _resolve_cluster_dir():
    """Locate the hscc-cluster plugin as a sibling of hscc_daemon."""
    cluster_dir = Path(__file__).resolve().parent.parent / "hscc-cluster"
    return cluster_dir


def _load_cluster_engine():
    """Load the hscc-cluster engine module directly as a library.

    The cluster engine lives in the hscc-cluster plugin (Hermes loads it as a
    toolset). We import its functions and call them directly — one CLI, one
    source of truth, no sub-process / argv rewriting. The sibling file is also
    named ``hscc.py`` so we load it under an alias to avoid colliding with
    ``hscc_daemon.hscc``. Its own submodules import each other by bare name, so
    the plugin dir must be on ``sys.path`` while we call into it.

    Returns the loaded module, or None (after printing an error) if missing.
    """
    cluster_dir = _resolve_cluster_dir()
    cluster_hscc = cluster_dir / "hscc.py"
    if not cluster_hscc.is_file():
        print(f"Error: hscc-cluster plugin not found at {cluster_dir}", file=sys.stderr)
        return None

    import importlib.util

    if str(cluster_dir) not in sys.path:
        sys.path.insert(0, str(cluster_dir))
    spec = importlib.util.spec_from_file_location("hscc_cluster_engine", str(cluster_hscc))
    if spec is None or spec.loader is None:
        print(f"Error: cannot load hscc-cluster engine from {cluster_hscc}", file=sys.stderr)
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _emit(result):
    """Print an engine result dict as pretty JSON (the cluster engine's format)."""
    print(json.dumps(result, indent=2, default=str))
    # A result carrying an ``error`` key is a failed command.
    return 1 if isinstance(result, dict) and result.get("error") else 0


def _handle_cluster():
    """Route 'cluster' subcommands to hscc-cluster plugin."""
    if len(sys.argv) < 3 or sys.argv[2] == "--help":
        print("Cluster management commands:")
        print()
        print("  hscc cluster status       Running workloads + idle hosts")
        print("  hscc cluster hosts        All cluster hosts and saved sparkrun clusters")
        print("  hscc cluster monitor      One CPU/RAM/GPU snapshot across the fleet")
        print("  hscc cluster jobs         All sparkrun jobs currently running")
        print("  hscc cluster info         Detailed resolved cluster configuration")
        print("  hscc cluster stop <id>    Stop a running workload by container id")
        if len(sys.argv) < 3:
            return 0
        else:
            return 0

    sub = sys.argv[2]
    if sub not in CLUSTER_SUBCOMMANDS:
        print(f"Error: unknown cluster subcommand: {sub}")
        print(f"Valid subcommands: {', '.join(sorted(CLUSTER_SUBCOMMANDS))}")
        return 1

    eng = _load_cluster_engine()
    if eng is None:
        return 1

    if sub == "stop":
        if len(sys.argv) < 4:
            print("Usage: hscc cluster stop <id>")
            return 1
        return _emit(eng.cmd_stop(sys.argv[3]))

    fn = {
        "status": eng.cmd_cluster_status,
        "hosts": eng.cmd_hosts,
        "monitor": eng.cmd_monitor,
        "jobs": eng.cmd_jobs,
        "info": eng.cmd_info,
    }[sub]
    return _emit(fn())


def _handle_template():
    """Route 'template' subcommands to hscc-cluster plugin."""
    if len(sys.argv) < 3 or sys.argv[2] == "--help":
        print("Cluster template commands:")
        print()
        print("  hscc template list                 List available cluster templates")
        print("  hscc template status               Which template is currently applied")
        print("  hscc template preview <name>       Dry-run: what applying <name> would change")
        print("  hscc template validate <name>      Preflight-check a template is deployable")
        print("  hscc template apply <name> [--confirm]  Apply a template")
        if len(sys.argv) < 3:
            return 0
        else:
            return 0

    sub = sys.argv[2]
    if sub not in TEMPLATE_SUBCOMMANDS:
        print(f"Error: unknown template subcommand: {sub}")
        print(f"Valid subcommands: {', '.join(sorted(TEMPLATE_SUBCOMMANDS))}")
        return 1

    # Reuse the template engine's own subcommand handler (returns a dict).
    if str(_resolve_cluster_dir()) not in sys.path:
        sys.path.insert(0, str(_resolve_cluster_dir()))
    try:
        from cluster_template_cli import cmd_cluster_template
    except ImportError:
        print(f"Error: hscc-cluster plugin not found at {_resolve_cluster_dir()}",
              file=sys.stderr)
        return 1
    return _emit(cmd_cluster_template([sub, *sys.argv[3:]]))


def _handle_verify():
    """Run verify.run_all() and print a per-check table (or JSON with --json)."""
    from hscc_daemon import verify as verify_mod

    json_mode = "--json" in sys.argv[1:]
    result = verify_mod.run_all()

    if json_mode:
        print(json.dumps(result))
    else:
        checks = result.get("checks", [])
        if checks:
            max_name = max(len(c.get("name", "")) for c in checks)
            max_name = max(max_name, 4)  # minimum padding
            for c in checks:
                glyph = "\u2713" if c.get("ok") else "\u2717"
                name = c.get("name", "").ljust(max_name)
                detail = c.get("detail", "")
                print(f"  {glyph} {name}  {detail}")
        overall = "\u2713 All checks passed" if result.get("ok") else "\u2717 Some checks failed"
        print(f"\n  {overall}")
    sys.exit(0 if result.get("ok") else 1)


def _handle_stats():
    """Run stats.compute_stats and print formatted output (or JSON with --json)."""
    from hscc_daemon import stats as stats_mod

    json_mode = "--json" in sys.argv[1:]
    days = 7
    rest = [a for a in sys.argv[1:] if a != "--json"]
    if len(rest) > 1:
        try:
            days = int(rest[1])
        except (ValueError, TypeError):
            days = 7

    result = stats_mod.compute_stats(since_days=days)

    if json_mode:
        print(json.dumps(result))
    else:
        print(stats_mod.format_stats(result))
    sys.exit(0)


def _handle_throughput():
    """Run throughput.compute_throughput() and print formatted output (or JSON with --json)."""
    from hscc_daemon import throughput

    json_mode = "--json" in sys.argv[1:]
    tp = throughput.compute_throughput()

    if json_mode:
        print(json.dumps(tp))
    else:
        print(throughput.format_throughput(tp))
    sys.exit(0)


def _handle_autoscale():
    """Run autoscale decision from throughput data and print it (read-only, never scales)."""
    from hscc_daemon import throughput
    from hscc_daemon import autoscale

    json_mode = "--json" in sys.argv[1:]
    tp = throughput.compute_throughput()
    current = tp.get("fleet", {}).get("nodes_ok", 0) or len(tp.get("by_node", {}))
    d = autoscale.decide_scale(tp, current_workers=current)

    if json_mode:
        print(json.dumps(d))
    else:
        print(f"autoscale: {d['action']} (target {d['target']}) — {d['reason']}")
    sys.exit(0)


def _handle_escalate():
    """Run escalate_watcher scan in dry-run mode (read-only, no mutations)."""
    from hscc_daemon import escalate_watcher

    json_mode = "--json" in sys.argv[1:]
    actions = escalate_watcher.scan_and_escalate(
        _reassign=lambda *a: None,
        _notify=lambda *a: None,
    )

    if json_mode:
        print(json.dumps(actions))
    else:
        if actions:
            for a in actions:
                task_id = a.get("task", "?")
                action = a.get("action", "?")
                to = a.get("to", a.get("category", "?"))
                print(f"card {task_id}: {action} -> {to}")
        else:
            print("no escalations pending")
    sys.exit(0)


def _handle_help():
    """Handle 'hscc help [command]' and 'hscc --help' / '-h'."""
    if len(sys.argv) < 3:
        print(_get_help_text())
        return 0

    topic = sys.argv[2]
    if topic == "advanced":
        print(_get_advanced_help())
        return 0

    help_text = COMMAND_HELP.get(topic)
    if help_text:
        print(f"hscc {topic}")
        print(f"  {help_text}")
        return 0

    print(f"Error: no help for '{topic}'")
    print("Use 'hscc help' to see all commands.")
    return 1


def _handle_profiles():
    """Route 'profiles' to the cluster engine's profile-status."""
    eng = _load_cluster_engine()
    if eng is None:
        return 1
    return _emit(eng.cmd_profile_status())


# Per-command --help support
DAEMON_COMMANDS = {
    "start", "stop", "status", "install", "uninstall", "plist",
    "log", "check", "watch", "triggers", "notify", "verify", "stats",
    "throughput", "autoscale", "escalate",
}


def main():
    args = sys.argv[1:]

    # No args or explicit help flags -> full help
    if not args or args[0] in ("--help", "-h"):
        print(_get_help_text())
        sys.exit(0)

    cmd = args[0]

    # 'help' subcommand
    if cmd == "help":
        rc = _handle_help()
        sys.exit(rc)

    # 'cluster' group
    if cmd == "cluster":
        rc = _handle_cluster()
        sys.exit(rc)

    # 'template' group
    if cmd == "template":
        rc = _handle_template()
        sys.exit(rc)

    # 'profiles'
    if cmd == "profiles":
        rc = _handle_profiles()
        sys.exit(rc)

    # Per-command --help (daemon commands)
    if cmd in DAEMON_COMMANDS and len(args) > 1 and args[1] == "--help":
        help_text = COMMAND_HELP.get(cmd, f"Run 'hscc help {cmd}' for details.")
        print(f"hscc {cmd}")
        print(f"  {help_text}")
        sys.exit(0)

    # 'verify'
    if cmd == "verify":
        _handle_verify()

    # 'stats'
    if cmd == "stats":
        _handle_stats()

    # 'throughput'
    if cmd == "throughput":
        _handle_throughput()

    # 'autoscale'
    if cmd == "autoscale":
        _handle_autoscale()

    # 'escalate'
    if cmd == "escalate":
        _handle_escalate()

    # Advanced commands (also support --help)
    advanced_cmds = {"start-daemon", "ed-status", "ed-install", "ed-uninstall"}
    if cmd in advanced_cmds and len(args) > 1 and args[1] == "--help":
        help_text = COMMAND_HELP.get(cmd, "")
        print(f"hscc {cmd}")
        print(f"  {help_text}")
        sys.exit(0)

    # Daemon command dispatch
    cmd_lower = cmd.lower()
    if cmd_lower == "start":
        from hscc_daemon.cli import cmd_start
        cmd_start()
    elif cmd_lower == "stop":
        from hscc_daemon.cli import cmd_stop
        cmd_stop()
    elif cmd_lower == "status":
        from hscc_daemon.cli import cmd_status
        cmd_status()
    elif cmd_lower == "check":
        from hscc_daemon.cli import cmd_check
        cmd_check(args[1] if len(args) > 1 else None)
    elif cmd_lower == "watch":
        from hscc_daemon.cli import cmd_watch
        cmd_watch(args[1] if len(args) > 1 else None)
    elif cmd_lower == "triggers":
        from hscc_daemon.cli import cmd_triggers
        cmd_triggers()
    elif cmd_lower == "notify":
        from hscc_daemon.cli import cmd_notify
        cmd_notify(" ".join(args[1:])) if len(args) > 1 else print("Usage: hscc notify <message>")
    elif cmd_lower == "plist":
        from hscc_daemon.install import cmd_plist
        cmd_plist()
    elif cmd_lower == "install":
        from hscc_daemon.install import cmd_install
        cmd_install()
    elif cmd_lower == "uninstall":
        from hscc_daemon.install import cmd_uninstall
        cmd_uninstall()
    elif cmd_lower == "log":
        from hscc_daemon.cli import cmd_log
        cmd_log()
    elif cmd_lower == "start-daemon":
        from hscc_daemon.cli import cmd_start_daemon
        cmd_start_daemon()
    elif cmd_lower == "ed-status":
        from hscc_daemon.cli import cmd_ed_status
        cmd_ed_status()
    elif cmd_lower == "ed-install":
        from hscc_daemon.cli import cmd_ed_install
        cmd_ed_install()
    elif cmd_lower == "ed-uninstall":
        from hscc_daemon.cli import cmd_ed_uninstall
        cmd_ed_uninstall()
    else:
        print(f"Unknown command: {cmd}")
        print(f"Available: start, stop, status, check, watch, triggers, notify, plist, install, uninstall, log, cluster, template, profiles, verify, stats, throughput, autoscale, escalate, start-daemon")
        sys.exit(1)


if __name__ == "__main__":
    main()
