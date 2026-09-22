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

# ── CLI rendering theme (Rich, Hermes-default gold palette; dark + light) ──
from hscc_daemon import cli_theme as theme

# ── Human-view renderers for cluster/template/profiles (replaces _emit) ──
from hscc_daemon import cluster_render

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
    """Build the full grouped help as a themed Rich renderable (Group).
    The output is a single scrolling Group of section blocks in the
    Hermes-default gold palette: gold section headings, plain cornsilk command
    lines, dim footer. ``main()`` prints it through a themed Console.
    """
    from rich.console import Group
    from rich.text import Text

    version = _get_version()

    title = Text()
    title.append("HSCC — Hermes Spark Cluster Control   ", style="title")
    title.append(f"v{version}", style="label")
    title.append("\nTurn a DGX Spark GPU cluster into a self-running fleet of AI agents.", style="dim")
    title.append("\nUsage:  hscc <command> [args]        hscc help <command>   for details", style="text")

    def _section(heading, lines):
        block = [Text(heading, style="label")]
        for line in lines:
            block.append(Text(f"  {line}", style="text"))
        block.append(Text(""))
        return block

    daemon = _section("Daemon control", [
        "start                Start the monitoring daemon in the background",
        "stop                 Gracefully stop the running daemon",
        "status               Daemon status + last result of every health stream",
        "install              Install the launchd service (auto-start at login)",
        "uninstall            Remove the service and stop the daemon",
        "plist                Print the launchd plist (no install)",
        "log                  Show the daemon log output",
    ])

    health = _section("Health & monitoring", [
        "check [stream]       Run one check cycle now (default: all)",
        "                       streams: dgx gateway local heartbeat nas watchdog triggers",
        "watch [stream]       Live-tail check results",
        "triggers             Show trigger-engine rules and recent firings",
        "verify               Run a full compatibility/health smoke-test of the cluster",
        "stats [days]         Fleet activity — completions & tool usage over N days (default 7)",
        "throughput           Aggregate vLLM throughput + queue depth across the fleet",
        "autoscale            Show autoscale decision from current queue depth (read-only)",
        "escalate             Show pending failure escalations (dry-run, no mutations)",
    ])

    cluster = _section("Cluster & templates", [
        "cluster status       Running workloads + idle hosts",
        "cluster hosts        All cluster hosts and saved sparkrun clusters",
        "cluster monitor      One CPU/RAM/GPU snapshot across the fleet",
        "cluster jobs         All sparkrun jobs currently running",
        "cluster info         Detailed resolved cluster configuration",
        "cluster stop <id>    Stop a running workload by container id",
        "cluster down [--dry-run]   Stop ALL sparkrun workloads fleet-wide",
        "cluster up   [--dry-run]   Start every unit in serving.json (orch + keepalive)",
        "template list        List available cluster templates (declared fleet layouts)",
        "template status      Which template is currently applied",
        "template preview <name>    Dry-run: what applying <name> would change",
        "template validate <name> [--structural-only] [--json]   Validate a template: structural (offline) layer always; placement (live) layer unless --structural-only. Exit non-zero if either layer fails.",
        "template apply <name> [--confirm] [--force-recreate]   Apply a template (--confirm executes; --force-recreate stops+reruns units so changed serve flags reach vLLM)",
        "profiles             Running kanban task counts per profile",
        "project <cmd>        Project-portfolio (flightdeck) commands: standup verify doctor ...",
        "api <cmd>            HSCC HTTP API server: start stop status (external apps)",
        "autodown <cmd>       Idle autodown/autoup: status enable disable wake cancel",
        "kanban <cmd>         Board hygiene: blocked (show/recover), stale (list/archive)",
    ])

    util = _section("Utility", [
        "notify <message>     Send a desktop notification",
    ])

    examples = _section("Examples", [
        "hscc status                          # is the daemon healthy?",
        "hscc check gateway                   # re-run just the gateway check",
        "hscc template list                   # see the declared fleet layouts",
        "hscc template preview 3node-coding",
        "hscc template apply 3node-coding --confirm",
        "hscc cluster status",
    ])

    footer = Text("\nDocs: ~/dev/hscc/README.md   Slash commands (in Hermes chat): /cluster /workers-up /cluster-restart /template", style="dim")
    footer.append("\nRun `hscc help advanced` for internal/service-manager commands.", style="dim")
    footer.append("\nTheme: add `--theme dark|light` after any command to pick a palette (default: auto).", style="dim")

    return Group(title, *daemon, *health, *cluster, *util, *examples, footer)


def _get_advanced_help():
    """Return advanced/internal help as a themed Rich Panel/Group."""
    from rich.console import Group
    from rich.text import Text

    heading = Text("Advanced / internal commands (omitted from main help):", style="label")
    lines = Text("", style="text")
    lines.append("", style="text")
    for cmd, desc in [
        ("start-daemon", "Run the daemon loop in the foreground (used by launchd)"),
        ("ed-status", "Event-driven mode status (placeholder, not yet available)"),
        ("ed-install", "Event-driven mode install (placeholder, not yet available)"),
        ("ed-uninstall", "Event-driven mode uninstall (placeholder, not yet available)"),
    ]:
        lines.append(f"  {cmd:<16}{desc}", style="text")
        lines.append("", style="text")
    return Group(heading, lines)


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
    "cluster": "Cluster management commands.\n  Subcommands: status hosts monitor jobs info stop <id> down [--dry-run] up [--dry-run]\n  Usage: hscc cluster <subcommand> [args]",
    "template": "Cluster template commands.\n  Subcommands: list status preview <name> validate <name> [--structural-only] [--json] apply <name> [--confirm]\n  Usage: hscc template <subcommand> [args]",
    "profiles": "Running kanban task counts per profile",
    "project": "Project-portfolio (flightdeck) commands.\n  Delegates to the relocated flightdeck CLI under hscc-project/. Try 'hscc project --help' for the full subcommand surface.\n  Usage: hscc project <subcommand> [args]",
    "api": "HSCC HTTP API server lifecycle.\n  Subcommands: start [--tailscale] [--bind <ip>] [--port <n>] stop status\n  Usage: hscc api <subcommand> [args]",
    "autodown": "Idle autodown/autoup for the GPU serving layer.\n  Subcommands: status enable [--idle-minutes <n>] [--force] disable wake cancel\n  Usage: hscc autodown <subcommand> [args]",
    "kanban": "Board hygiene for autodown.\n  blocked [--json]: list BLOCKED cards with why; blocked --recover <id> [--reason <t>]: recover one blocked card to ready\n  stale [--older-than <days>] [--json]: list non-terminal cards; stale --archive <id>: archive one (t_e751e652)\n  Usage: hscc kanban <subcommand> [args]",
    "help": "Show help. Usage: hscc help [command]\n  Use 'hscc help advanced' for internal commands.",
    "verify": "Run a full compatibility/health smoke-test of the cluster.\n  Usage: hscc verify [--json] [--chat]\n  --chat  also run a deep end-to-end chat round trip against the live\n          orchestrator (real POST, poll, assert reply, assert model tokens\n          moved). Opt-in: it fires a real prompt and can take ~600s.",
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


CLUSTER_SUBCOMMANDS = {"status", "hosts", "monitor", "jobs", "info", "stop",
                       "down", "up"}
TEMPLATE_SUBCOMMANDS = {"list", "status", "preview", "validate", "apply"}


def _resolve_cluster_dir():
    """Locate the hscc-cluster plugin as a sibling of hscc_daemon."""
    cluster_dir = Path(__file__).resolve().parent.parent / "hscc-cluster"
    return cluster_dir


def _resolve_project_dir():
    """Locate the relocated flightdeck package as a sibling of hscc_daemon.

    Phase 1 relocated flightdeck's source into ``hscc-project/`` at the repo
    root (sibling of ``hscc_daemon/``). That is the parent dir whose
    ``flightdeck/`` package is imported by :func:`_handle_project`.
    """
    return Path(__file__).resolve().parent.parent / "hscc-project"


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
    """Print an engine result dict as pretty JSON (the cluster engine's format).

    Kept for the machine ``--json`` path and any callers that still need a raw
    dump; the human path of cluster/template/profiles now routes through
    :mod:`hscc_daemon.cluster_render` instead.
    """
    print(json.dumps(result, indent=2, default=str))
    # A result carrying an ``error`` key is a failed command.
    return 1 if isinstance(result, dict) and result.get("error") else 0


def _handle_cluster():
    """Route 'cluster' subcommands to hscc-cluster plugin, render human view."""
    if len(sys.argv) < 3 or sys.argv[2] == "--help":
        print("Cluster management commands:")
        print()
        print("  hscc cluster status       Running workloads + idle hosts")
        print("  hscc cluster hosts        All cluster hosts and saved sparkrun clusters")
        print("  hscc cluster monitor      One CPU/RAM/GPU snapshot across the fleet")
        print("  hscc cluster jobs         All sparkrun jobs currently running")
        print("  hscc cluster info         Detailed resolved cluster configuration")
        print("  hscc cluster stop <id>    Stop a running workload by container id")
        print("  hscc cluster down [--dry-run]  Stop ALL sparkrun workloads fleet-wide")
        print("  hscc cluster up   [--dry-run]  Start every unit in serving.json (orch + keepalive)")
        return 0

    sub = sys.argv[2]
    if sub not in CLUSTER_SUBCOMMANDS:
        print(f"Error: unknown cluster subcommand: {sub}")
        print(f"Valid subcommands: {', '.join(sorted(CLUSTER_SUBCOMMANDS))}")
        return 1

    # `--theme` selects the palette for the human view only; strip it before
    # the engine sees argv (it is not a cluster argument).
    cluster_args, theme_name = _strip_theme_arg(sys.argv[2:])
    sub = cluster_args[0] if cluster_args else sub

    eng = _load_cluster_engine()
    if eng is None:
        return 1

    console = theme.make_console(theme_name)

    if sub == "stop":
        if len(cluster_args) < 2:
            print("Usage: hscc cluster stop <id>")
            return 1
        result = eng.cmd_stop(cluster_args[1])
        return cluster_render.render_stop(console, result)

    if sub == "down":
        dry_run = "--dry-run" in cluster_args
        result = eng.cmd_cluster_down(dry_run=dry_run)
        return cluster_render.render_down(console, result)

    if sub == "up":
        dry_run = "--dry-run" in cluster_args
        result = eng.cmd_cluster_up(dry_run=dry_run)
        return cluster_render.render_up(console, result)

    fn = {
        "status": eng.cmd_cluster_status,
        "hosts": eng.cmd_hosts,
        "monitor": eng.cmd_monitor,
        "jobs": eng.cmd_jobs,
        "info": eng.cmd_info,
    }[sub]
    result = fn()
    renderer = {
        "status": cluster_render.render_cluster_status,
        "hosts": cluster_render.render_hosts,
        "monitor": cluster_render.render_monitor,
        "jobs": cluster_render.render_jobs,
        "info": cluster_render.render_info,
    }[sub]
    return renderer(console, result)


def _strip_theme_and_json(args):
    """Strip ``--theme``/``--json`` tokens from a template argv slice.

    Returns ``(engine_args, theme_name, json_mode)``. ``--json`` is the machine
    contract for ``template validate`` and must never be forwarded to the engine
    as if it were a template name; ``--theme`` similarly only selects the human
    palette.
    """
    cleaned = []
    json_mode = "--json" in args
    theme_name = None
    i = 0
    n = len(args)
    while i < n:
        a = args[i]
        if a == "--theme":
            if i + 1 < n:
                theme_name = args[i + 1]
                i += 2
                continue
            i += 1
            continue
        if a.startswith("--theme="):
            theme_name = a.split("=", 1)[1]
            i += 1
            continue
        if a != "--json":
            cleaned.append(a)
        i += 1
    return cleaned, theme_name, json_mode


def _handle_template():
    """Route 'template' subcommands to hscc-cluster plugin, render human view."""
    if len(sys.argv) < 3 or sys.argv[2] == "--help":
        print("Cluster template commands:")
        print()
        print("  hscc template list                 List available cluster templates")
        print("  hscc template status               Which template is currently applied")
        print("  hscc template preview <name>       Dry-run: what applying <name> would change")
        print("  hscc template validate <name> [--structural-only] [--json]  Validate: structural (offline) layer always; placement (live) layer unless --structural-only")
        print("  hscc template apply <name> [--confirm] [--force-recreate]  Apply a template (--force-recreate re-applies changed serve flags)")
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

    engine_args, theme_name, json_mode = _strip_theme_and_json(sys.argv[3:])
    result = cmd_cluster_template([sub, *engine_args])

    # The machine ``--json`` path (template validate) stays byte-exact.
    if json_mode:
        return _emit(result)

    # Human path: overall failure semantics preserved from _emit (+ the apply
    # and validate non-zero extensions) so scripts chaining on exit codes still
    # behave. Exit codes are computed from the result here (not in the renderer)
    # only for the flags the renderer does not carry.
    console = theme.make_console(theme_name)
    rc = cluster_render.render_template(console, sub, result)
    # validate exits non-zero when EITHER layer fails (spec: "Exit non-zero if
    # either layer fails, so it scripts cleanly").
    if sub == "validate" and isinstance(result, dict) and result.get("ok") is False:
        return 1
    # An apply that was BLOCKED by pre-flight validation, or only PARTIALLY
    # succeeded, must exit non-zero (never treat a non-deployment as success).
    if sub == "apply" and isinstance(result, dict) and result.get("success") is False:
        return 1
    return rc


def _strip_theme_arg(args):
    """Remove ``--theme <name>`` / ``--theme=<name>`` tokens from an argv slice.

    ``--theme`` only selects the palette for the human view; it is not part of
    the machine ``--json`` contract, and it must never be mistaken for the
    positional ``days`` in ``stats``. Returns ``(cleaned, theme_name_or_None)``.
    """
    theme_name = None
    cleaned = []
    i = 0
    n = len(args)
    while i < n:
        a = args[i]
        if a == "--theme":
            if i + 1 < n:
                theme_name = args[i + 1]
                i += 2
                continue
            i += 1
            continue
        if a.startswith("--theme="):
            theme_name = a.split("=", 1)[1]
            i += 1
            continue
        cleaned.append(a)
        i += 1
    return cleaned, theme_name


def _handle_verify():
    """Run verify.run_all() and print a Rich per-check table (or JSON with --json).

    With `--chat` an additional deep check is run: a full chat round trip
    against the live API (POST /v1/orchestrator/chat -> poll -> assert a real
    reply -> assert the orchestrator's vllm:generation_tokens_total moved).
    That proves the model actually saw a user message, not just that the API
    accepts one. It is OPT-IN because it fires a real prompt at the live
    orchestrator and can take up to the chat timeout (~600 s) to complete —
    too expensive and too invasive for the default fast smoke test.

    The human view is a themed Rich Table with ok/warn/error glyphs; the
    ``--json`` machine view stays an exact raw JSON dump.
    """
    from hscc_daemon import verify as verify_mod

    args, theme_name = _strip_theme_arg(sys.argv[1:])
    json_mode = "--json" in args
    chat_mode = "--chat" in args
    result = verify_mod.run_all()

    chat_check = None
    if chat_mode:
        chat_check = verify_mod.run_chat_roundtrip()
        result["checks"] = result.get("checks", []) + [chat_check]
        result["ok"] = result.get("ok", True) and bool(chat_check["ok"])

    if json_mode:
        print(json.dumps(result))
    else:
        console = theme.make_console(theme_name)
        checks = result.get("checks", [])
        # A per-check Rich Table with ok/warn/error glyphs: three states, not
        # two — pass (✓), fail (✗), and "could not check" (○).
        table = theme.make_table("verify")
        table.add_column("status", width=2, no_wrap=True)
        table.add_column("check", no_wrap=True)
        table.add_column("detail")
        next_steps = []
        for c in checks:
            ok = c.get("ok")
            glyph = "\N{CHECK MARK}" if ok else (
                "\N{BALLOT X}" if ok is False else "\N{WHITE CIRCLE}")
            colour = "ok" if ok else ("error" if ok is False else "warn")
            name = c.get("name", "")
            detail = c.get("detail", "")
            next_step = c.get("next_step")
            # A Rich Table wraps long cell text, which would fold the
            # plain-language hint and break the greppable "next step: <hint>"
            # contract. Render hints as their own lines BELOW the table so the
            # substring stays contiguous and at-a-glance guidance is kept.
            if not ok and next_step:
                next_steps.append(next_step)
            table.add_row(f"[{colour}]{glyph}[/]",
                          f"[label]{name}[/]", detail)
        console.print(table)
        for hint in next_steps:
            # Keep the exact "next step: <hint>" substring the pre-Rich
            # rendering produced (automation greps it), just dimmed.
            console.print(f"[dim]next step: {hint}[/dim]")
        unver = result.get("unverified") or []
        if not result.get("ok"):
            overall = "\N{BALLOT X} Some checks failed"
        elif unver:
            overall = "\N{CHECK MARK} All checks passed (%d unverified: %s)" % (
                len(unver), ", ".join(unver))
        else:
            overall = "\N{CHECK MARK} All checks passed"
        console.print(f"[{'error' if not result.get('ok') else 'ok'}]{overall}[/]")
    sys.exit(0 if result.get("ok") else 1)


def _handle_stats():
    """Run stats.compute_stats and print a themed Rich block (or JSON with --json)."""
    from hscc_daemon import stats as stats_mod

    args, theme_name = _strip_theme_arg(sys.argv[1:])
    json_mode = "--json" in args
    days = 7
    rest = [a for a in args if a != "--json"]
    if len(rest) > 1:
        try:
            days = int(rest[1])
        except (ValueError, TypeError):
            days = 7

    if days < 0:
        print("error: days must be non-negative, got %d" % days, file=sys.stderr)
        sys.exit(1)

    result = stats_mod.compute_stats(since_days=days)

    if json_mode:
        print(json.dumps(result))
    else:
        console = theme.make_console(theme_name)
        body = stats_mod.format_stats(result)
        console.print(theme.make_panel("fleet stats", body))
    sys.exit(0)


def _handle_throughput():
    """Run throughput.compute_throughput() and print a themed Rich block (or JSON with --json)."""
    from hscc_daemon import throughput

    args, theme_name = _strip_theme_arg(sys.argv[1:])
    json_mode = "--json" in args
    tp = throughput.compute_throughput()

    if json_mode:
        print(json.dumps(tp))
    else:
        console = theme.make_console(theme_name)
        body = throughput.format_throughput(tp)
        console.print(theme.make_panel("fleet throughput", body))
    sys.exit(0)


def _handle_autoscale():
    """Run autoscale decision from throughput data and print a status Panel (or JSON with --json).

    Read-only — this never scales anything, it only reports the decision.
    """
    from hscc_daemon import throughput
    from hscc_daemon import autoscale

    args, theme_name = _strip_theme_arg(sys.argv[1:])
    json_mode = "--json" in args
    tp = throughput.compute_throughput()
    current = tp.get("fleet", {}).get("nodes_ok", 0) or len(tp.get("by_node", {}))
    d = autoscale.decide_scale(tp, current_workers=current)

    if json_mode:
        print(json.dumps(d))
    else:
        # A "none" decision carries no target — only scale_up/down do.
        tgt = f" (target {d['target']})" if "target" in d else ""
        body = f"autoscale: {d['action']}{tgt} — {d['reason']}"
        # Colour by how the operator should read it: scale_up is a live
        # capacity signal (warn), scale_down/none are calm (ok).
        colour = "warn" if d.get("action") == "scale_up" else "ok"
        console = theme.make_console(theme_name)
        console.print(theme.make_status_panel(body, status=d.get("action", "none"), color=colour))
    sys.exit(0)


def _handle_escalate():
    """Run escalate_watcher scan in dry-run mode (read-only, no mutations).

    Renders a themed Rich Table of the pending escalations (or JSON with
    --json); an empty list prints a short "none pending" line.
    """
    from hscc_daemon import escalate_watcher

    args, theme_name = _strip_theme_arg(sys.argv[1:])
    json_mode = "--json" in args
    actions = escalate_watcher.scan_and_escalate(
        _reassign=lambda *a: None,
        _notify=lambda *a: None,
    )

    if json_mode:
        print(json.dumps(actions))
    else:
        console = theme.make_console(theme_name)
        if actions:
            table = theme.make_table("pending escalations")
            table.add_column("card", no_wrap=True)
            table.add_column("action", no_wrap=True)
            table.add_column("target", no_wrap=True)
            for a in actions:
                task_id = a.get("task", "?")
                action = a.get("action", "?")
                to = a.get("to", a.get("category", "?"))
                colour = "error" if action == "escalate_failed" else (
                    "warn" if action == "escalate" else "accent")
                table.add_row(f"[{colour}]{task_id}[/]", action, to)
            console.print(table)
        else:
            console.print("[dim]no escalations pending[/dim]")
    sys.exit(0)


def _handle_help():
    """Handle 'hscc help [command]' and 'hscc --help' / '-h'."""
    if len(sys.argv) < 3:
        theme.make_console().print(_get_help_text())
        return 0

    topic = sys.argv[2]
    if topic == "advanced":
        theme.make_console().print(_get_advanced_help())
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
    """Route 'profiles' to the cluster engine's profile-status, render human view."""
    eng = _load_cluster_engine()
    if eng is None:
        return 1
    _, theme_name = _strip_theme_arg(sys.argv[1:])
    console = theme.make_console(theme_name)
    return cluster_render.render_profiles(console, eng.cmd_profile_status())


# Verbs of flightdeck's `project` subgroup, aliased to the top of
# `hscc project` so the group name does not have to be typed twice.
_PROJECT_SUBGROUP_VERBS = frozenset(
    {"new", "list", "remove", "repair", "pull", "push", "chat", "sync"})


def _handle_project():
    """Route 'project ...' to the relocated flightdeck CLI as a library.

    Everything after ``project`` on the command line (``sys.argv[2:]``) is
    handed to flightdeck's argv-driven entry point
    (:func:`flightdeck.cli.main`). That builds its OWN argparse parser
    (including flightdeck's own ``--registry``/``--apply``/etc flags),
    auto-discovers every command module under ``flightdeck/commands/*.py``,
    and dispatches — so we do not reimplement any of flightdeck's argument
    parsing here. Its exit code becomes ours.

    The relocated package lives in ``hscc-project/`` (a sibling of
    ``hscc_daemon/``). We put that dir on ``sys.path`` if not already present,
    mirroring how flightdeck itself vendors Hermes in
    ``flightdeck/core/kanban.py`` (``_load_kanban_db``): insert once, import,
    done. If the import fails we surface a clear, actionable error rather than
    a raw traceback and never swallow the failure.
    """
    project_dir = _resolve_project_dir()
    if not project_dir.is_dir():
        print(
            f"Error: relocated flightdeck package not found at {project_dir}",
            file=sys.stderr,
        )
        return 1
    if str(project_dir) not in sys.path:
        sys.path.insert(0, str(project_dir))
    try:
        from flightdeck.cli import main as flightdeck_main
    except ImportError as exc:
        print(
            "Error: could not import flightdeck from "
            f"{project_dir}: {exc}\n"
            "  flightdeck's dependencies may be missing. Install them and "
            "retry:\n"
            "    pip install -e hscc-project/   (or: pip install hscc-project/)",
            file=sys.stderr,
        )
        return 1
    # Everything after `hscc project`.
    remaining = sys.argv[2:]
    # Lifecycle/registry verbs live in flightdeck's `project` SUBGROUP, which
    # made the real invocation `hscc project project chat` — the group name
    # doubled because flightdeck's own top-level already sits under
    # `hscc project`. Promote them so `hscc project chat` works, and keep the
    # explicit `hscc project project chat` form working too.
    #
    # Safe because the two namespaces are disjoint: none of these names is a
    # flightdeck top-level command (ask, daemon, decompose, doctor, hygiene,
    # incident, ingest, init, legacy-cards, migrate-card, lint-cards, message,
    # metrics, monitor, project, qa, reconcile, release, report, review,
    # roadmap, standup, start, topics, update, verify, why). The guard below
    # re-checks that at runtime, so adding a colliding top-level command later
    # makes the alias step aside rather than shadow it.
    if remaining and remaining[0] in _PROJECT_SUBGROUP_VERBS:
        try:
            # Ask the PARSER what is actually invokable, not
            # _discover_commands() — that returns MODULE names (`sync`,
            # `legacy`, `lint`) which differ from the registered command names
            # (`legacy-cards`, `lint-cards`) and would veto valid aliases.
            from flightdeck.cli import build_parser
            _sub = build_parser()._subparsers
            top_level = set()
            for _act in (_sub._group_actions if _sub else []):
                top_level |= set(getattr(_act, "choices", {}) or {})
        except Exception:
            top_level = set()
        if remaining[0] not in top_level:
            remaining = ["project"] + remaining
    try:
        return flightdeck_main(remaining)
    except SystemExit as exc:
        # argparse raises SystemExit(code) for --help / bad usage; surface it.
        return exc.code if isinstance(exc.code, int) else 0


# Per-command --help support
DAEMON_COMMANDS = {
    "start", "stop", "status", "install", "uninstall", "plist",
    "log", "check", "watch", "triggers", "notify", "verify", "stats",
    "throughput", "autoscale", "escalate",
}


def main():
    # Every `hscc ...` invocation is its OWN process — it does not pass through
    # run_daemon_loop, so it does not inherit the daemon's private umask. Several
    # subcommands write ~/.hscc state directly (autodown enable/disable, serving
    # updates), which would recreate those files 0644. Third and last writer
    # path: daemon loop, escalate-watcher cron, and here.
    os.umask(0o077)
    args = sys.argv[1:]

    # No args or explicit help flags -> full help
    if not args or args[0] in ("--help", "-h"):
        theme.make_console().print(_get_help_text())
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

    # 'project' group (relocated flightdeck CLI)
    if cmd == "project":
        rc = _handle_project()
        sys.exit(rc)

    # 'api' group (HSCC HTTP API server lifecycle)
    if cmd == "api":
        from hscc_daemon.api_cli import cmd_api
        rc = cmd_api(args[1:])
        sys.exit(rc)

    # 'autodown' group (idle autodown/autoup for the serving layer)
    if cmd == "autodown":
        from hscc_daemon.autodown_cli import cmd_autodown
        rc = cmd_autodown(args[1:])
        sys.exit(rc)

    # 'kanban' group — board hygiene. kanban_blocked owns the single dispatcher
    # and delegates `stale` to kanban_cli, so both subcommands work regardless
    # of which card's module landed first.
    if cmd == "kanban":
        from hscc_daemon.kanban_blocked import cmd_kanban
        rc = cmd_kanban(args[1:])
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
        print(f"Available: start, stop, status, check, watch, triggers, notify, plist, install, uninstall, log, cluster, template, profiles, project, api, kanban, verify, stats, throughput, autoscale, escalate, start-daemon")
        sys.exit(1)


if __name__ == "__main__":
    main()
