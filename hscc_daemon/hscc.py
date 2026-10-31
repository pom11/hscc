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
import shutil
import subprocess
import textwrap
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


# Budgets for the main help (see _get_help_text): the (B) gate measures the
# render at COLUMNS 80 and 120, so every authored line must be inside 78
# (the stricter budget — identical render at both widths, matching the
# cluster/template group convention: _CLUSTER_HELP_WIDTH / _TEMPLATE_HELP_WIDTH).
# ``usage`` sits in a 2-space-indented column of width _TOP_HELP_COL; a pair
# that no longer fits splits into usage-alone + hanging-indent description.
_TOP_HELP_COL = 21
_TOP_HELP_INDENT = 6
_TOP_HELP_WIDTH = 78


def _get_help_text():
    """Build the full grouped help as a themed Rich renderable (Group).
    The output is a single scrolling Group of section blocks in the
    Hermes-default gold palette: gold section headings, plain cornsilk command
    lines, dim footer. ``main()`` prints it through a themed Console.

    Layout contract (F5, card t_fc307a51; audit Class B): the console renders
    at the caller's COLUMNS (the (B) gate measures 80 and 120), and a hard
    line wider than the console is what made Rich spill unindented
    continuations at column 0 — the base tree measured 10 such wraps at width
    80 and 2 at width 120 here. Every authored line therefore stays inside the
    78-column budget: usage/description pairs render on ONE line while they
    fit, otherwise the usage stands alone and the description follows as
    hanging-indented continuation — the two-line layout the cluster/template
    group help already uses (t_875c5019 / t_90082309). Wording is the base's
    verbatim (re-flowed, never reworded); the ONLY wording change is dropping
    the internal ``(flightdeck)`` blurb from the project line (audit Class A,
    row A5).
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
            block.append(Text(line, style="text"))
        block.append(Text(""))
        return block

    def _entry(usage, desc, notes=()):
        """One command line: hard-wrapped to the budget, never spilling.

        One line while ``usage`` + ``desc`` fit side by side (the base's
        two-column look), otherwise the usage stands alone and the
        description wraps to hanging-indented continuation lines. ``notes``
        are the sub-lines the base printed under some commands (e.g. the
        check streams list); they align to the description column when they
        fit there, else to the continuation indent. The gutter is at least
        one space (``str.ljust`` alone would butt the description against a
        usage longer than the column)."""
        gutter = " " * max(1, _TOP_HELP_COL - len(usage))
        one_line = f"  {usage}{gutter}{desc}"
        if len(one_line) <= _TOP_HELP_WIDTH:
            out = [one_line]
        else:
            out = [f"  {usage}"]
            out.extend(textwrap.wrap(
                desc, width=_TOP_HELP_WIDTH,
                initial_indent=" " * _TOP_HELP_INDENT,
                subsequent_indent=" " * _TOP_HELP_INDENT))
        for note in notes:
            aligned = f"{' ':<{_TOP_HELP_COL + 2}}{note}"
            if len(aligned) <= _TOP_HELP_WIDTH:
                out.append(aligned)
            else:
                out.extend(textwrap.wrap(
                    note, width=_TOP_HELP_WIDTH,
                    initial_indent=" " * _TOP_HELP_INDENT,
                    subsequent_indent=" " * _TOP_HELP_INDENT))
        return out

    daemon = _section("Daemon control", [
        *_entry("start", "Start the monitoring daemon in the background"),
        *_entry("stop", "Gracefully stop the running daemon"),
        *_entry("status", "Daemon status + last result of every health stream"),
        *_entry("install", "Install the launchd service (auto-start at login)"),
        *_entry("uninstall", "Remove the service and stop the daemon"),
        *_entry("plist", "Print the launchd plist (no install)"),
        *_entry("log", "Show the daemon log output"),
    ])

    health = _section("Health & monitoring", [
        *_entry("check [stream]",
                "Run one check cycle now (default: all)",
                ["streams: dgx gateway local heartbeat nas watchdog triggers"]),
        *_entry("check --repo <path>",
                "Commit-time address-guard posture for that checkout",
                ["(default cwd; --json; exit 0 only when 'armed')"]),
        *_entry("watch [stream]", "Live-tail check results"),
        *_entry("triggers", "Show trigger-engine rules and recent firings"),
        *_entry("verify", "Run a full compatibility/health smoke-test of the cluster"),
        *_entry("stats [days]",
                "Fleet activity — completions & tool usage over N days (default 7)"),
        *_entry("throughput",
                "Aggregate vLLM throughput + queue depth across the fleet"),
        *_entry("autoscale",
                "Show autoscale decision from current queue depth (read-only)"),
        *_entry("escalate",
                "Show pending failure escalations (dry-run, no mutations)"),
    ])

    cluster = _section("Cluster & templates", [
        *_entry("cluster status", "Running workloads + idle hosts"),
        *_entry("cluster hosts", "All cluster hosts and saved sparkrun clusters"),
        *_entry("cluster monitor [--live]",
                "Fleet CPU/RAM/GPU; --live adds per-node, per-model load"),
        *_entry("cluster jobs", "All sparkrun jobs currently running"),
        *_entry("cluster info", "Detailed resolved cluster configuration"),
        *_entry("cluster stop <id>", "Stop a running workload by container id"),
        *_entry("cluster down [--dry-run]",
                "Stop ALL sparkrun workloads fleet-wide"),
        *_entry("cluster up   [--dry-run]",
                "Start every unit in serving.json (orch + keepalive)"),
        *_entry("template list",
                "List available cluster templates (declared fleet layouts)"),
        *_entry("template status", "Which template is currently applied"),
        *_entry("template preview <name>",
                "Dry-run: what applying <name> would change"),
        *_entry("template validate <name> [--structural-only] [--json]",
                "Validate a template: structural (offline) layer always; "
                "placement (live) layer unless --structural-only. Exit "
                "non-zero if either layer fails."),
        *_entry("template apply <name> [--confirm] [--force-recreate]",
                "Apply a template (--confirm executes; --force-recreate "
                "stops+reruns units so changed serve flags reach vLLM)"),
        *_entry("profiles", "Running kanban task counts per profile"),
        *_entry("project <cmd>",
                "Project-portfolio commands: standup verify doctor ..."),
        *_entry("api <cmd>",
                "HSCC HTTP API server: start stop status (external apps)"),
        *_entry("autodown <cmd>",
                "Idle autodown/autoup: status enable disable wake cancel"),
        *_entry("kanban <cmd>",
                "Board hygiene: blocked (show/recover), stale (list/archive)"),
        *_entry("cards monitor [--live]",
                "One live panel per running card, streaming its worker"),
        # orch group (t_5da7fd56): dispatched at main() and listed in
        # COMMAND_HELP, so the main banner must show it too (F5 surface-honesty
        # rule: a live command must not hide from the primary help surface).
        *_entry('orch inject <p> "<msg>"',
                "Inject into a running Hermes REPL pane (tmux)"),
    ])

    util = _section("Utility", [
        *_entry("notify <message>", "Send a desktop notification"),
        *_entry("bootstrap [flags]",
                "Run the HSCC installer from any layout (repo, plugins, brew)",
                ["flags pass through: --yes --dry-run --force --no-backup",
                 "--skip-{skills,roles,daemon,patches,cli} (see `hscc help bootstrap`)"]),
        *_entry("update --check",
                "Check GitHub releases for a newer version (read-only)"),
    ])

    examples = _section("Examples", [
        "  hscc status                          # is the daemon healthy?",
        "  hscc check gateway                   # re-run just the gateway check",
        "  hscc template list                   # see the declared fleet layouts",
        "  hscc template preview 3node-coding",
        "  hscc template apply 3node-coding --confirm",
        "  hscc cluster status",
    ])

    # Every footer segment is authored inside the budget for the same reason
    # as the section lines: two base segments (88 and 86 cols) wrapped at 80
    # and left "auto)." at column 0 — (B) findings on the base tree.
    footer = Text("\nDocs: ~/dev/hscc/README.md   Slash commands (in Hermes chat):", style="dim")
    footer.append("\n  /cluster /workers-up /cluster-restart /template", style="dim")
    footer.append("\nRun `hscc help advanced` for internal/service-manager commands.", style="dim")
    footer.append("\nTheme: add `--theme dark|light` after any command to pick a palette", style="dim")
    footer.append("\n  (default: auto).", style="dim")

    return Group(title, *daemon, *health, *cluster, *util, *examples, footer)


def _get_advanced_help():
    """Return advanced/internal help as a themed Rich Panel/Group.

    The ed-status / ed-install / ed-uninstall placeholder trio was DELETED
    from this surface (and from COMMAND_HELP and main()'s dispatch) by card
    t_fc307a51: the audit's Class DEAD measured them advertising commands
    that do nothing ("placeholder, not yet available"), and the operator's
    fan-out ruling was DELETE, not hide. The library functions stay in cli.py
    for their own tests; only the CLI surface is gone.
    """
    from rich.console import Group
    from rich.text import Text

    heading = Text("Advanced / internal commands (omitted from main help):", style="label")
    lines = Text("", style="text")
    lines.append("", style="text")
    for cmd, desc in [
        ("start-daemon", "Run the daemon loop in the foreground (used by launchd)"),
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
    "check": "Run one check cycle now. Usage: hscc check [stream]\n  streams: dgx gateway local heartbeat nas watchdog triggers\n  hscc check --repo <path> [--json]   address-guard posture for that checkout\n          (default: cwd's top-level). Calls posture() from hscc-bootstrap/\n          install_hooks.py — single implementation, nothing recomputed here.\n          States: armed | unarmed | armed-but-absent (config advertises\n          protection, this checkout resolves no runnable hook = FAIL-OPEN) |\n          not-a-repo. Exit 0 only for 'armed', non-zero otherwise.",
    "watch": "Live-tail check results. Usage: hscc watch [stream]",
    "triggers": "Show trigger-engine rules and recent firings",
    "notify": "Send a desktop notification. Usage: hscc notify <message>",
    # bootstrap (card t_bc5a3fee): installer verb. Every line authored <=76 so
    # the (B) gate's 78 budget holds at both mandated widths (only the FIRST
    # line receives _handle_help's 2-space prefix).
    "bootstrap": "Run the HSCC installer (bootstrap.sh), whatever layout this\n"
                 "  hscc lives in: repo checkout, deployed plugins, or a brew\n"
                 "  libexec tree. Flags pass through to the installer:\n"
                 "  [--yes] [--dry-run] [--force] [--no-backup]\n"
                 "  [--skip-skills|--skip-roles|--skip-daemon|--skip-patches|\n"
                 "  --skip-cli]. --dry-run prints every planned write, does\n"
                 "  none. HSCC_BOOTSTRAP_SH overrides the script lookup.\n"
                 "  Usage: hscc bootstrap [flags]",
    # update (card t_a4055572): the version-check verb. Every line authored
    # <=76 for the same (B)-gate reason as the multi-line entries above
    # (_handle_help adds only a 2-space prefix to the FIRST line).
    "update": "Check for a newer HSCC release. Read-only: prints the\n"
              "  comparison and the upgrade command for YOUR install mode;\n"
              "  never downloads, installs or upgrades anything.\n"
              "  Usage: hscc update --check\n"
              "  Reads GitHub's releases API (5s timeout; authenticated gh\n"
              "  is used when available, plain HTTPS otherwise). A newer\n"
              "  release is cached for 24h under ~/.hscc/update-check.json,\n"
              "  which is what `hscc status` notices once a day. Offline or\n"
              "  rate-limited: one line on stderr, exit 1.",
    # Multi-line value: _handle_help prints `  {value}`, so only the FIRST
    # line gets the 2-space prefix — every line is authored to stay <=76 so
    # the rendered block fits the (B) gate's 78 budget (t_90082309: the old
    # single "Subcommands: status hosts … up [--dry-run]" line overflowed at
    # width 80). Verb list unchanged, split into two aligned lines.
    "cluster": "Cluster management commands.\n"
               "  Subcommands: status  hosts  monitor  jobs  info\n"
               "               stop <id>  down [--dry-run]  up [--dry-run]\n"
               "  Usage: hscc cluster <subcommand> [args]",
    "template": "Cluster template commands.\n  Subcommands: list status preview validate apply\n  preview/validate take <name>; validate also takes\n  [--structural-only] [--json]; apply also takes\n  [--confirm] [--force-recreate]. Try 'hscc template --help'.\n  Usage: hscc template <subcommand> [args]",
    "profiles": "Running kanban task counts per profile",
    # Multi-line value: _handle_help prints `  {value}`, so only the FIRST
    # line gets the 2-space prefix — every line is authored to stay <=76 so
    # the rendered block fits the (B) gate's 78 budget (F5 card t_fc307a51:
    # the base "Delegates to the relocated flightdeck CLI under
    # hscc-project/. Try 'hscc project --help'…" sentence measured 123 cols
    # and overflowed at BOTH mandated widths). Class A (row A6): the rendered
    # blurb no longer names the internal package — "relocated flightdeck CLI"
    # became "relocated portfolio CLI" (the hscc-project/ path stays: it is
    # where the code actually lives, and a path string is not the A
    # invariant's target).
    "project": "Project-portfolio commands.\n"
               "  Delegates to the relocated portfolio CLI under hscc-project/.\n"
               "  Try 'hscc project --help' for the full subcommand surface.\n"
               "  Usage: hscc project <subcommand> [args]",
    "api": "HSCC HTTP API server lifecycle.\n  Subcommands: start [--tailscale] [--bind <ip>] [--port <n>] stop status\n  Usage: hscc api <subcommand> [args]",
    "autodown": "Idle autodown/autoup for the GPU serving layer.\n  Subcommands: status enable [--idle-minutes <n>] [--force] disable wake cancel\n  Usage: hscc autodown <subcommand> [args]",
    # Multi-line value, same 76-col authored budget as project/cluster above
    # (F5 card t_fc307a51: the base blocked/stale sentences measured 121 and
    # 111 cols — (B) overflow at width 80 and, for blocked, at width 120.
    # Wording verbatim; re-flowed onto hanging-indent continuations).
    "kanban": "Board hygiene for autodown.\n"
              "  blocked [--json]: list BLOCKED cards with why; blocked --recover\n"
              "    <id> [--reason <t>]: recover one blocked card to ready\n"
              "  stale [--older-than <days>] [--json]: list non-terminal cards;\n"
              "    stale --archive <id>: archive one (t_e751e652)\n"
              "  monitor [--live] [--interval <s>] [--board <slug>]: workers\n"
              "    on every board, running/ready/counts/outcomes (read-only)\n"
              "  Usage: hscc kanban <subcommand> [args]",
    "cards": "Live per-card worker view (read-only).\n"
             "  monitor [--live] [--interval <s>] [--board <slug>] [--tools]:\n"
             "    one panel per running card streaming its transcript; waiting\n"
             "    cards at the bottom; keys tab/enter/t/p/q in --live\n"
             "  Usage: hscc cards monitor [args]",
    # orch group (card t_5da7fd56): the unified-CLI surface for the cluster
    # engine's cmd_orch router. Every line authored <=76 for the (B) gate's
    # 78 budget (only the FIRST line receives _handle_help's 2-space prefix),
    # same convention as cluster/project above. The verb list mirrors what the
    # engine router actually resolves TODAY (inject); a new orch verb must be
    # advertised here in the same change that adds it to the router (F5 rule:
    # help never advertises a command that does not exist).
    "orch": "Orchestrator commands.\n"
            "  Subcommands: inject <profile> \"<message>\"\n"
            "  inject flags: [--mode auto|plain|queue|steer]\n"
            "  [--dry-run] [--json] [--pane <target>]\n"
            "  --dry-run resolves the pane and sends nothing.\n"
            "  Usage: hscc orch <subcommand> [args]",
    "help": "Show help. Usage: hscc help [command]\n  Use 'hscc help advanced' for internal commands.",
    "verify": "Run a full compatibility/health smoke-test of the cluster.\n  Usage: hscc verify [--json] [--chat]\n  --chat  also run a deep end-to-end chat round trip against the live\n          orchestrator (real POST, poll, assert reply, assert model tokens\n          moved). Opt-in: it fires a real prompt and can take ~600s.",
    "stats": "Fleet activity — completions & tool usage over N days (default 7).\n  Usage: hscc stats [days] [--json]",
    "throughput": "Aggregate vLLM throughput + queue depth across the fleet.\n  Usage: hscc throughput [--json]",
    "autoscale": "Show autoscale decision from current queue depth (read-only, never scales).\n  Usage: hscc autoscale [--json]",
    "escalate": "Show pending failure escalations (dry-run, no mutations).\n  Usage: hscc escalate [--json]",
    "advanced": "Internal/service-manager commands.\n  See 'hscc help advanced' for details.",
    "start-daemon": "Run the daemon loop in the foreground (used by launchd)",
    # heal verbs (audit B5, card t_aeb982bc): the breaker's alert text names
    # `hscc heal-clear <node>`, so `hscc help heal-clear` must answer — until
    # these entries existed it exited 1. Multi-line values print as authored
    # (only the FIRST line gets _handle_help's 2-space prefix), so every line
    # stays <=76 to fit the (B) gate's 78 budget at both widths. Wording note:
    # the audit's (D) EXEC_ECHO sensor matches the executing verb's output
    # ("Cleared heal strikes" / "healing resumes"), so the help text must
    # never quote that wording — "clears nothing" is the contract B4 proves.
    "heal-clear": "Clear one node's heal-breaker strikes (MUTATES the ledger).\n"
                  "Usage: hscc heal-clear <node> [reason...]\n"
                  "  --help/-h prints this help and clears nothing. See: hscc heal-status",
    "heal-status": "List heal-breaker strikes per node (read-only).\n"
                   "Usage: hscc heal-status\n"
                   "  --help/-h prints this help. See also: hscc heal-clear <node>",
}


CLUSTER_SUBCOMMANDS = {"status", "hosts", "monitor", "jobs", "info", "stop",
                       "down", "up"}
TEMPLATE_SUBCOMMANDS = {"list", "status", "preview", "validate", "apply"}

# ── template group help surface (t_9032f96b) ───────────────────────────────
# Shared help entries for the template verbs: `hscc template --help` prints
# the validate/apply entries in the two-line layout (the commands that
# overflowed as single two-column lines), `hscc template <verb> --help`
# prints the same text as per-verb usage, so the two cannot drift.
# Layout rules (help-surface gate, widths 80/120): the command sits on its
# own line, the description is wrapped with a continuation indent (never
# flush-left), and every emitted line stays inside 78 columns. The examples
# name REAL templates from `hscc template list` — the gate parse-checks
# every example line against the real subcommand table.
_TEMPLATE_HELP_INDENT = 6
_TEMPLATE_HELP_WIDTH = 78

_TEMPLATE_HELP = {
    "list": {
        "usage": "hscc template list",
        "desc": "List available cluster templates.",
        "example": None,
    },
    "status": {
        "usage": "hscc template status",
        "desc": "Which template is currently applied.",
        "example": None,
    },
    "preview": {
        "usage": "hscc template preview <name>",
        "desc": "Dry-run: what applying <name> would change.",
        "example": None,
    },
    "validate": {
        "usage": "hscc template validate <name> [--structural-only] [--json]",
        "desc": ("Validate a template: the structural (offline) layer "
                 "always, the placement (live) layer unless "
                 "--structural-only. Exit non-zero if either layer fails."),
        "example": "hscc template validate 4node-coding --structural-only",
    },
    "apply": {
        "usage": "hscc template apply <name> [--confirm] [--force-recreate]",
        "desc": ("Apply a template (--confirm executes; --force-recreate "
                 "stops and reruns units so changed serve flags reach vLLM)."),
        "example": "hscc template apply 4node-coding --confirm",
    },
}


def _wrap_template_desc(desc, width=None):
    """Description lines for a template help entry: wrapped with a fixed
    continuation indent, so no continuation ever lands at col 0."""
    if width is None:
        width = _template_help_width()
    return textwrap.wrap(desc, width=width,
                         initial_indent=" " * _TEMPLATE_HELP_INDENT,
                         subsequent_indent=" " * _TEMPLATE_HELP_INDENT)


def _template_entry_lines(sub, width=None):
    """Render one template help entry (command line, wrapped description, and
    — for the name-taking verbs — one real, parse-checked example line)."""
    if width is None:
        width = _template_help_width()
    entry = _TEMPLATE_HELP[sub]
    lines = [f"  {entry['usage']}"]
    lines.extend(_wrap_template_desc(entry["desc"], width))
    if entry["example"]:
        lines.append(f"{' ' * _TEMPLATE_HELP_INDENT}example: "
                     f"{entry['example']}")
    return lines


def _print_template_verb_usage(sub: str):
    """`hscc template <verb> --help`: usage + description + example.
    Prints text only — no engine import, no dispatch (t_9032f96b: the help
    token used to fall through to the engine as the template name, so
    `--help` printed “Template not found: --help” and exited 1)."""
    width = _template_help_width()
    entry = _TEMPLATE_HELP[sub]
    print(f"Usage: {entry['usage']}")
    print()
    for line in _wrap_template_desc(entry["desc"], width):
        print(line)
    if entry["example"]:
        print()
        print(f"{' ' * _TEMPLATE_HELP_INDENT}example: {entry['example']}")


# ── cluster group help surface (t_875c5019, audit B6 + B12) ────────────────
# `_handle_cluster` used to handle `--help` ONLY at group level
# (`len(sys.argv) < 3 or sys.argv[2] == "--help"`), so every per-subcommand
# help request fell through to verb dispatch: `cluster stop --help` forwarded
# the help token to `sparkrun stop` as the container id (B6 — a MUTATING
# shell-out), and `status`/`hosts`/`info`/`jobs --help` shelled out to their
# read-only sparkrun calls (B12 — "help executes nothing" violated, and the
# help costs seconds). Each subcommand now carries its own help entry, printed
# by an intercept that runs before `--theme` stripping, before the engine
# import and before dispatch, so a help request loads nothing and shells out
# nothing.
#
# The layout helpers are separate from the template ones above on purpose:
# `hscc_daemon/hscc.py` is the four-card collision file (F2/F3/F4/F5 all touch
# it), so this card adds code rather than reshaping F2's just-landed helpers.
_CLUSTER_HELP_INDENT = 6
_CLUSTER_HELP_WIDTH = 78

# The group help (`hscc cluster --help`): one (usage, description) pair per
# verb, rendered in the same two-line layout as the per-verb entries below —
# usage alone on its line, description wrapped with a fixed continuation
# indent (t_90082309 / F3: the base printed each entry as one hard line, and
# the monitor + `up` entries overflowed the gate's width-80 budget).
# Descriptions are the base-tree wording verbatim — this surface is re-flowed,
# never reworded (the monitor entry especially: its verb's own help text
# _MONITOR_HELP is operator-owned, t_1cc33407, and untouched).
_CLUSTER_GROUP_ENTRIES = [
    ("hscc cluster status", "Running workloads + idle hosts"),
    ("hscc cluster hosts", "All cluster hosts and saved sparkrun clusters"),
    ("hscc cluster monitor",
     "One CPU/RAM/GPU snapshot (--live: refreshing view + per-model load)"),
    ("hscc cluster jobs", "All sparkrun jobs currently running"),
    ("hscc cluster info", "Detailed resolved cluster configuration"),
    ("hscc cluster stop <id>", "Stop a running workload by container id"),
    ("hscc cluster down [--dry-run]",
     "Stop ALL sparkrun workloads fleet-wide"),
    ("hscc cluster up [--dry-run]",
     "Start every unit in serving.json (orch + keepalive)"),
]

_CLUSTER_SUBCOMMAND_HELP = {
    # No `example:` lines on purpose: every verb here takes no arguments
    # (except stop's container id, which only exists at run time), so there is
    # no real example to print — and a fabricated one would be parse-checked
    # by the (C) gate as a lie.
    "status": {
        "usage": "hscc cluster status",
        "desc": ("Running workloads across the fleet — tensor/pipeline "
                 "parallel layout and container id per workload. Read-only."),
    },
    "hosts": {
        "usage": "hscc cluster hosts",
        "desc": ("Every host recorded in cluster.json plus the sparkrun "
                 "clusters it resolves to, with ip and role. Read-only."),
    },
    "jobs": {
        "usage": "hscc cluster jobs",
        "desc": ("Every sparkrun job currently running on the cluster. "
                 "Read-only."),
    },
    "info": {
        "usage": "hscc cluster info",
        "desc": ("The resolved cluster configuration: cluster.json, the "
                 "sparkrun cluster it maps to and the serving topology. "
                 "Read-only."),
    },
    "stop": {
        "usage": "hscc cluster stop <container-id>",
        "desc": ("Stop ONE running workload by container id — this stops a "
                 "live container. List the current ids with "
                 "'hscc cluster status'."),
    },
    # up/down (card t_5132deb3, operator ruling on PR #33's final pass):
    # `hscc cluster up --help` used to print the F6 refusal ("unknown
    # argument", rc=1) — inert, but not helpful. Help now WINS over the
    # refusal for a help token; the refusal code path is untouched and still
    # refuses rc=1, byte-identical, for every token that is NOT a help flag
    # (--foo, a dangling --theme, a positional typo). Wording note: the
    # descriptions name what the verb does to a LIVE fleet, because the
    # B6-class failure this surface prevents is a help request that started
    # or stopped real workloads.
    "down": {
        "usage": "hscc cluster down [--dry-run]",
        "desc": ("Stop ALL sparkrun workloads fleet-wide — this stops every "
                 "running container on every node. Pass --dry-run to see "
                 "what would stop without stopping anything."),
    },
    "up": {
        "usage": "hscc cluster up [--dry-run]",
        "desc": ("Start every unit in serving.json (orch + keepalive) — "
                 "this starts live workloads across the fleet. Pass "
                 "--dry-run to see the plan without touching the cluster."),
    },
}


def _cluster_help_width():
    """Column budget for the cluster help surface: terminal width when the
    terminal (or COLUMNS) reports a usable one, else 80, capped at 78 — the
    same rule as the template surface, so no help line overflows at width
    80/120 (the gate's wrap widths)."""
    try:
        cols = shutil.get_terminal_size((80, 24)).columns
    except OSError:
        cols = 80
    return min(cols, _CLUSTER_HELP_WIDTH)


def _cluster_entry_lines(sub, width=None):
    """Render one cluster help entry (usage line + wrapped description, with
    a continuation indent so no continuation ever lands at col 0)."""
    if width is None:
        width = _cluster_help_width()
    entry = _CLUSTER_SUBCOMMAND_HELP[sub]
    lines = [f"  {entry['usage']}"]
    lines.extend(textwrap.wrap(entry["desc"], width=width,
                               initial_indent=" " * _CLUSTER_HELP_INDENT,
                               subsequent_indent=" " * _CLUSTER_HELP_INDENT))
    if entry.get("example"):
        lines.append(f"{' ' * _CLUSTER_HELP_INDENT}example: "
                     f"{entry['example']}")
    return lines


def _print_cluster_verb_usage(sub: str):
    """`hscc cluster <verb> --help`: usage + description, exit 0, executing
    nothing (no engine import, no sparkrun call)."""
    print(f"Usage: {_CLUSTER_SUBCOMMAND_HELP[sub]['usage']}")
    print()
    for line in _cluster_entry_lines(sub)[1:]:
        print(line)


# The verbs whose per-subcommand `--help`/`-h` prints help instead of
# dispatching. `monitor` is deliberately absent: it already had its own
# `--help` branch (proven inert by the audit — zero canary trips) and its
# help text is operator-owned (t_1cc33407), so that surface stays as it is.
# `up`/`down` joined with t_5132deb3 (operator ruling: help wins over the
# F6 refusal for a help token); the F6 refusal itself still guards every
# NON-help unknown token — `_parse_cluster_flags` and
# `_cluster_refusal_message` are unchanged and their wording belongs to
# F3's gate rules (canonical-whitespace pin in
# hscc_daemon/tests/test_cluster_group_help.py).
_CLUSTER_HELP_VERBS = ("status", "hosts", "jobs", "info", "stop", "up",
                       "down")


def _help_token_index(args):
    """Index of a `--help`/`-h` token in a raw argv slice, else None.

    The scan is deliberately over the RAW slice — before `--theme` stripping.
    `_strip_theme_arg` consumes the token after `--theme` as the palette name,
    so `cluster stop --theme --help` would otherwise swallow the help flag and
    dispatch the mutating verb with `--theme` as its container id: the exact
    hole the F6 raw-argv guard closes for up/down.
    """
    for i, tok in enumerate(args):
        if tok in ("-h", "--help"):
            return i
    return None


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


def _hscc_version_key(path):
    """Sort key for a ``hscc-<ver>`` payload dir: numeric version tuple first,
    name as tie-breaker. Plain lexicographic order would rank ``hscc-2.9.0``
    above ``hscc-2.10.0`` (``9`` > ``1``); digits compare as integers (the
    repo's own version scheme is numeric dotted, see VERSION)."""
    digits = re.findall(r"\d+", Path(path).name)
    return ([int(d) for d in digits], Path(path).name)


def _bootstrap_script_candidates(source_root=None):
    """Ordered candidate paths for ``bootstrap.sh``, derived from where THIS
    running CLI resolved its code (card t_bc5a3fee).

    The three roots a shipped ``hscc`` can live under, plus two escapes:

      1. repo checkout / in-tree copy — ``<source_root>/../hscc-bootstrap``
         (the sibling layout; also matches a brew tarball extracted flat);
      2. deployed plugins — ``$HERMES_HOME/plugins/hscc-bootstrap``
         (bootstrap.sh copied there by install_payload);
      3. brew libexec — walking up from the source root for the tap's payload
         tree under ``libexec/`` (the tap installs the CLI into a venv INSIDE
         libexec, so site-packages lookups never see the sibling). Two names
         are offered, in this order: the version-STABLE ``libexec/hscc`` the
         MERGED tap uses (homebrew-hscc Formula: ``tree = libexec/"hscc"``)
         then every ``libexec/hscc-*`` newest-first (a versioned tree, which
         no shipped formula produces today but the resolver still honours);
      4. ``HSCC_BOOTSTRAP_SH`` env override (checked first; tests + custom
         layouts).

    A versioned libexec tree with several installs resolves the NEWEST (see
    ``_hscc_version_key``) — the same posture brew takes for the active
    formula version.
    """
    root = Path(source_root) if source_root else Path(__file__).resolve().parent
    cands = []

    # 4. explicit override wins outright (a bogus value still falls through
    #    to the real candidates rather than silently exec-ing nothing).
    env = os.environ.get("HSCC_BOOTSTRAP_SH")
    if env:
        cands.append(Path(env))

    # 1. sibling of the resolved package dir (repo checkout, in-tree copy).
    cands.append(root.parent / "hscc-bootstrap" / "bootstrap.sh")

    # 3. ancestor walk: a libexec/venv layout puts the script under one of
    #    the parent prefixes (covers a brew tree extracted under Cellar/).
    #    The stable name first: the merged tap's payload tree is
    #    `libexec/hscc` (version-stable), so a real keg never depends on the
    #    versioned glob below matching.
    for anc in list(root.parents)[:6]:
        cands.append(anc / "hscc-bootstrap" / "bootstrap.sh")
        libexec = anc / "libexec"
        if libexec.is_dir():
            cands.append(libexec / "hscc" / "hscc-bootstrap" / "bootstrap.sh")
            try:
                for ver in sorted(libexec.glob("hscc-*"),
                                  key=_hscc_version_key, reverse=True):
                    cands.append(ver / "hscc-bootstrap" / "bootstrap.sh")
            except OSError:
                pass

    # 2. deployed plugins dir (works even when the source tree has no copy —
    #    a wheel-style install).
    hermes_home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    cands.append(Path(hermes_home) / "plugins" / "hscc-bootstrap" / "bootstrap.sh")
    return cands


def _resolve_bootstrap_script(source_root=None):
    """First existing candidate ``bootstrap.sh``, or None. A set-but-bogus
    ``HSCC_BOOTSTRAP_SH`` does not mask the real candidates (it is simply not
    'existing'), so a typo cannot make the verb report success for a script
    that is not there."""
    for cand in _bootstrap_script_candidates(source_root):
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


def _handle_bootstrap():
    """``hscc bootstrap [flags...]`` — locate bootstrap.sh relative to the
    resolved source root and exec it, passing the remaining argv through.

    Deliberately a transparent hop (card t_bc5a3fee): the installer stays the
    single source of truth for the install sequence; this verb only makes it
    reachable as ``hscc bootstrap --dry-run`` from ANY layout (repo checkout,
    deployed plugins, brew libexec). The installer's exit code is the verb's
    exit code; stdout/stderr are inherited so the operator sees the stage
    output live.
    """
    script = _resolve_bootstrap_script()
    if script is None:
        print("Error: bootstrap.sh not found relative to this install "
              f"({Path(__file__).resolve().parent}). Searched the repo "
              "sibling, libexec/hscc-*/, and $HERMES_HOME/plugins — or set "
              "HSCC_BOOTSTRAP_SH.", file=sys.stderr)
        return 1
    # Shadow WARN (card t_9b98e696), printed BEFORE the installer's stage
    # output so the operator reads it next to the CLI-shim stage. Warn-only
    # by construction: this verb execs the installer; the scan touches
    # nothing, and when the running install is not a brew keg (or the PATH
    # is clean) it is completely silent. detect_path_shadow swallows
    # everything; the try/except is belt-and-braces around the import too.
    try:
        from hscc_daemon import update_check
        shadow = update_check.detect_path_shadow()
        for line in update_check.format_shadow_warning(shadow):
            print(line)
    except Exception:
        pass
    tail = [a for a in sys.argv[2:]]
    argv = [str(script)] + tail
    if not os.access(script, os.X_OK):
        # A copied tree can lose the exec bit; bash runs it either way.
        argv = ["bash"] + argv
    # env inherited verbatim: HERMES_HOME/HSCC_DIR sandboxing must survive.
    return subprocess.run(argv, env=os.environ).returncode


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


def _cluster_group_entry_lines(cmd, desc, width):
    """Two-line group-help entry: usage alone, description wrapped with a
    continuation indent (never col 0). Wording is passed through verbatim —
    only line breaks are chosen here."""
    lines = [f"  {cmd}"]
    lines.extend(textwrap.wrap(desc, width=width,
                               initial_indent=" " * _CLUSTER_HELP_INDENT,
                               subsequent_indent=" " * _CLUSTER_HELP_INDENT,
                               break_long_words=False))
    return lines


def _print_cluster_group_help():
    """`hscc cluster --help` / bare `hscc cluster`: the group listing in the
    two-line layout (t_90082309 / F3: the base's single hard lines for
    `monitor` and `up` overflowed the (B) gate's 80-col budget). Text only —
    no engine import, no dispatch."""
    width = _cluster_help_width()
    print("Cluster management commands:")
    print()
    for cmd, desc in _CLUSTER_GROUP_ENTRIES:
        for line in _cluster_group_entry_lines(cmd, desc, width):
            print(line)


def _handle_cluster():
    """Route 'cluster' subcommands to hscc-cluster plugin, render human view."""
    # Group help: a bare `cluster`, or a help flag as the group token. The
    # -h half joined with t_5132deb3 (the CLI-wide short-flag close; every
    # other group — template/api/autodown/kanban — already accepted it).
    if len(sys.argv) < 3 or sys.argv[2] in ("--help", "-h"):
        _print_cluster_group_help()
        return 0

    sub = sys.argv[2]
    if sub not in CLUSTER_SUBCOMMANDS:
        print(f"Error: unknown cluster subcommand: {sub}")
        print(f"Valid subcommands: {', '.join(sorted(CLUSTER_SUBCOMMANDS))}")
        return 1

    # `--theme` selects the palette for the human view only; strip it before
    # the engine sees argv (it is not a cluster argument). Keep the raw slice:
    # a *dangling* --theme is dropped silently by the stripper, and the F6
    # flag parser needs the untouched argv to refuse it.
    cluster_args_raw = list(sys.argv[2:])
    cluster_args, theme_name = _strip_theme_arg(cluster_args_raw)
    sub = cluster_args[0] if cluster_args else sub

    # ── per-subcommand --help (B6 + B12) ───────────────────────────────────
    # Must sit BEFORE the engine load: `hscc cluster status --help` used to
    # import the cluster engine and shell `sparkrun status` before anybody
    # looked for a help token. It also sits on the RAW slice so `--help` can
    # never be eaten as a `--theme` value (see _help_token_index).
    help_at = _help_token_index(cluster_args_raw)
    if help_at is not None and sub in _CLUSTER_HELP_VERBS:
        _print_cluster_verb_usage(sub)
        return 0

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
        dry_run, err = _parse_cluster_flags(cluster_args, "down",
                                           raw_args=cluster_args_raw)
        if err:
            print(err)
            return 1
        result = eng.cmd_cluster_down(dry_run=dry_run)
        return cluster_render.render_down(console, result)

    if sub == "up":
        dry_run, err = _parse_cluster_flags(cluster_args, "up",
                                           raw_args=cluster_args_raw)
        if err:
            print(err)
            return 1
        result = eng.cmd_cluster_up(dry_run=dry_run)
        return cluster_render.render_up(console, result)

    if sub == "monitor":
        opts, err = _parse_monitor_flags(cluster_args)
        if err:
            print(err)
            return 1
        if opts.get("help"):
            print(_MONITOR_HELP)
            return 0
        if opts.get("live"):
            from hscc_daemon import cluster_live
            return cluster_live.run_live(console, interval=opts["interval"],
                                         cluster=opts.get("cluster"))

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


# ── orch group surface (card t_5da7fd56) ───────────────────────────────────
# `hscc orch inject <profile> "<msg>"` — the unified-CLI entry the operator
# asked for. The VERB lives in the cluster engine (hscc-cluster/hscc.py
# cmd_orch -> cmd_orch_inject -> hscc-api/repl_inject, merged by t_75f17c83);
# this branch owns only dispatch + rendering + exit codes, exactly like
# _handle_cluster delegates cmd_status/cmd_up. It holds NO pane logic: no
# mode vocabulary, no prefix, no screen read — that is repl_inject's contract
# (parent card's ruling), and duplicating it here is how the two drift.
_ORCH_GROUP_ENTRIES = [
    ('hscc orch inject <profile> "<message>"',
     "Inject a message into the profile's running Hermes REPL pane (tmux "
     "bracketed paste; exactly one pane for the profile or refuse). Flags: "
     "[--mode auto|plain|queue|steer] [--dry-run] [--pane <target>] [--json]. "
     "--dry-run resolves the pane and reports target/busy/verb, sending "
     "nothing."),
]


def _print_orch_group_help():
    """`hscc orch` / `hscc orch --help` / `hscc orch inject --help`.

    Text only — no engine import, no dispatch (the cluster/profiles rule:
    a help request loads nothing and shells out nothing, t_875c5019 /
    t_5132deb3). One verb today, so the group listing doubles as the verb
    usage; when a second verb lands it gets its own entry here and this
    function stays the single source for all three help paths.
    """
    width = _cluster_help_width()
    print("Orchestrator commands:")
    print()
    for cmd, desc in _ORCH_GROUP_ENTRIES:
        for line in _cluster_group_entry_lines(cmd, desc, width):
            print(line)


def _orch_receipt_line(receipt):
    """The one-line human view of an inject receipt.

    A dry run carries repl_inject's own `line` (format_plan) — reused verbatim
    so `hscc orch inject --dry-run` prints exactly what the reference tool
    prints for the same pane. A real send has no `line`, so the same shape is
    rebuilt from the receipt fields the engine already returns; the char count
    is appended because that is the one thing a send proves that a plan does
    not.
    """
    if not isinstance(receipt, dict):
        return str(receipt)
    line = receipt.get("line")
    if line is None and ("target" not in receipt or "mode" not in receipt):
        # Not an inject receipt — a future orch verb (e.g. `orch waker
        # status`, t_2b34b5c7) whose dict has no pane target. Fabricating
        # "target: None @ None (idle) -> None" would lie; print the result
        # as it is until that verb owns its own rendering here.
        return json.dumps(receipt, indent=2, default=str)
    if not line:
        line = (f"target: {receipt.get('profile')} @ {receipt.get('target')} "
                f"({'busy' if receipt.get('busy') else 'idle'}) "
                f"-> {receipt.get('mode')}")
    chars = receipt.get("chars")
    if chars is None and receipt.get("sent_text") is not None:
        chars = len(receipt["sent_text"])
    if chars is not None and "chars" not in line:
        line += f" ({chars} chars)"
    return line


def _handle_orch(argv):
    """Route `hscc orch <verb>` to the cluster engine's cmd_orch router.

    Shape mirrors _handle_cluster / the api branch: help first (loads nothing),
    then the engine load, then one call, then rendering. Exit codes:
    0 = delivered (or a printed plan), 1 = refusal or a bad verb.

    `--json` is passed straight through to the engine (cmd_orch_inject accepts
    and ignores it — rendering is ours) and only selects the raw receipt dump
    here; `--theme` is stripped (it is a human-view flag the engine does not
    know).
    """
    tail, _theme_name = _strip_theme_arg(list(argv))
    json_mode = "--json" in tail

    # A help token ANYWHERE in the tail is help — before the engine load, so
    # `hscc orch inject --help` can never reach a verb that injects (the
    # t_5132deb3 rule: -h must never execute).
    if not tail or _help_token_index(tail) is not None:
        _print_orch_group_help()
        return 0

    eng = _load_cluster_engine()
    if eng is None:
        return 1

    try:
        receipt = eng.cmd_orch(list(tail))
    except Exception as exc:
        # repl_inject.InjectError (no pane / ambiguous pane / empty text / bad
        # mode / tmux failure) and ValueError (bad argv, unknown verb) are
        # REFUSALS: one reported line + exit 1, never a traceback and never a
        # stack trace leaking a pane id. `--json` keeps its machine contract
        # (an {"error": ...} object on stdout, same shape as the engine's own
        # error path).
        if json_mode:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"error: {exc}")
        return 1

    if json_mode:
        # Byte-identical to the standalone engine entry's --json dump
        # (hscc-cluster/hscc.py::_emit_result): daemon scripts and the iOS
        # console parse this.
        print(json.dumps(receipt, indent=2, default=str))
    else:
        print(_orch_receipt_line(receipt))
    return 0


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


def _template_help_width():
    """Column budget for the template help surface: the terminal width when
    the terminal (or COLUMNS) reports a usable one, else 80. Capped at 78 so
    a description never reaches the widths the help-surface gate measures
    (80/120) as an unindented overflow, and continuation lines have room."""
    try:
        cols = shutil.get_terminal_size((80, 24)).columns
    except OSError:
        cols = 80
    return min(cols, _TEMPLATE_HELP_WIDTH)


def _handle_template():
    """Route 'template' subcommands to hscc-cluster plugin, render human view."""
    if len(sys.argv) < 3 or sys.argv[2] in ("--help", "-h"):
        print("Cluster template commands:")
        print()
        for sub in ("list", "status", "preview", "validate", "apply"):
            for line in _template_entry_lines(sub):
                print(line)
            print()
        return 0

    sub = sys.argv[2]
    if sub not in TEMPLATE_SUBCOMMANDS:
        print(f"Error: unknown template subcommand: {sub}")
        print(f"Valid subcommands: {', '.join(sorted(TEMPLATE_SUBCOMMANDS))}")
        return 1

    # `--help` on a verb prints that verb's usage and exits 0 — BEFORE the
    # engine import and dispatch. It used to fall through, so the help token
    # was read as the template name: "Template not found: --help", rc=1.
    if "--help" in sys.argv[3:] or "-h" in sys.argv[3:]:
        _print_template_verb_usage(sub)
        return 0

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


_MONITOR_HELP = """Usage: hscc cluster monitor [--live] [--interval N] [--cluster NAME]

  hscc cluster monitor                 one CPU/RAM/GPU snapshot (sparkrun's JSON frame)
  hscc cluster monitor --live          refreshing view: host table like
                                       `sparkrun cluster monitor` (Jobs column, no
                                       container detail) + per-node, per-model load
                                       from each vLLM backend behind the proxy
                                       (requests running/waiting, KV cache, tok/s)
  --interval N                         refresh every N seconds (default 2)
  --cluster NAME                       sparkrun cluster to watch (default: sparkrun's default)

Examples:
  hscc cluster monitor --live
  hscc cluster monitor --live --interval 5"""


def _parse_monitor_flags(cluster_args):
    """Parse ``hscc cluster monitor`` flags. Returns ``(opts, error)``.

    Read-only command, but keep the F6 rule: an unknown token is an error, not
    something to ignore.
    """
    opts = {"live": False, "interval": 2, "cluster": None, "help": False}
    args = list(cluster_args[1:])
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in ("-h", "--help"):
            opts["help"] = True
        elif tok == "--live":
            opts["live"] = True
        elif tok in ("--interval", "-i", "--cluster"):
            val = args[i + 1] if i + 1 < len(args) else None
            if val is None or val.startswith("-"):
                return opts, f"Error: '{tok}' needs a value.\n\n{_MONITOR_HELP}"
            if tok == "--cluster":
                opts["cluster"] = val
            else:
                try:
                    opts["interval"] = int(val)
                except ValueError:
                    return opts, f"Error: '{tok}' must be a whole number of seconds, got {val!r}."
                if opts["interval"] < 1:
                    return opts, f"Error: '{tok}' must be at least 1."
            i += 1
        else:
            return opts, f"Error: unknown argument for 'hscc cluster monitor': {tok}\n\n{_MONITOR_HELP}"
        i += 1
    if (opts["cluster"] or opts["interval"] != 2) and not opts["live"]:
        return opts, "Error: --interval/--cluster only apply with --live."
    return opts, None


def _cluster_refusal_message(sub, tok, allowed):
    """The F6 refusal for an unknown argument to a DESTRUCTIVE verb (up/down).

    Layout only (t_90082309 / F3): the base emitted the two body sentences as
    single ~90- and ~140-character lines, which the (B) gate measured as
    overflow at width 80 AND at 120 — a safety refusal nobody can read in a
    normal terminal. textwrap here only chooses line breaks; the wording is
    the base's verbatim, and the canonical-whitespace form of the whole
    message is pinned byte-for-byte in
    hscc_daemon/tests/test_cluster_group_help.py (this card owns the F6
    refusal's gate rules).
    """
    width = _cluster_help_width()
    ind = " " * 2
    lines = [f"Error: unknown argument for 'hscc cluster {sub}': {tok}"]
    lines.extend(textwrap.wrap(
        f"'hscc cluster {sub}' accepts only: " + ", ".join(allowed)
        + " (and --theme <name> for the human view).",
        width=width, initial_indent=ind, subsequent_indent=ind,
        break_long_words=False))
    lines.extend(textwrap.wrap(
        f"Nothing was started or stopped. Refusing to guess what {tok} meant "
        f"— a destructive command runs only on arguments it understands.",
        width=width, initial_indent=ind, subsequent_indent=ind,
        break_long_words=False))
    return "\n".join(lines)


def _parse_cluster_flags(cluster_args, sub, raw_args=None):
    """Validate the flag list of a DESTRUCTIVE cluster command (up/down).

    Returns ``(dry_run, error_message)``; exactly one of the two is meaningful
    — when ``error_message`` is not None the caller must NOT run the command.

    F6 (HEAL_CASCADE 2026-10-14): these two branches used to read
    ``dry_run = "--dry-run" in cluster_args`` and nothing else, so every other
    token was silently ignored. ``hscc cluster up --help`` therefore STARTED
    THE FLEET, and a mistyped ``--hosts`` upped all four nodes while looking
    like a normal invocation. Anything the parser does not understand is an
    error, never an implicit "go ahead": the default for a destructive command
    must be refusal, not optimism.

    ``raw_args`` is the *unstripped* argv slice. ``--theme`` is consumed earlier
    by ``_strip_theme_arg``, which drops a dangling ``--theme`` (no value)
    silently — leaving ``up`` looking like a clean bare call that starts the
    fleet. Passing the raw slice lets the same refusal cover that hole.
    """
    allowed = ("--dry-run",)
    dry_run = False
    raw = list(raw_args or [])
    for i, tok in enumerate(raw):
        if tok == "--theme":
            nxt = raw[i + 1] if i + 1 < len(raw) else None
            if nxt is None or str(nxt).startswith("-"):
                # Same re-flow rule as the unknown-argument refusal below:
                # the wording is base-verbatim (one sentence), only the
                # 91-char single line becomes wrapped indented lines. The
                # "Error: " prefix goes through initial_indent so the width
                # budget counts it (prefix-then-wrap could exceed 78).
                return False, "\n".join(textwrap.wrap(
                    "'--theme' needs a palette name (e.g. --theme "
                    "dark). Nothing was started or stopped.",
                    width=_cluster_help_width(),
                    initial_indent="Error: ", subsequent_indent="  ",
                    break_long_words=False))
    for tok in cluster_args[1:]:
        if tok in allowed:
            dry_run = True
            continue
        return False, _cluster_refusal_message(sub, tok, allowed)
    return dry_run, None


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
            # name/detail are written by the checks themselves (they quote
            # paths, versions, command output) — DATA, never markup.
            table.add_row(f"[{colour}]{glyph}[/]",
                          f"[label]{theme.esc(name)}[/]", theme.esc(detail))
        console.print(table)
        for hint in next_steps:
            # Keep the exact "next step: <hint>" substring the pre-Rich
            # rendering produced (automation greps it), just dimmed.
            console.print(f"[dim]next step: {theme.esc(hint)}[/dim]")
        unver = result.get("unverified") or []
        if not result.get("ok"):
            overall = "\N{BALLOT X} Some checks failed"
        elif unver:
            overall = "\N{CHECK MARK} All checks passed (%d unverified: %s)" % (
                len(unver), ", ".join(theme.esc(u) for u in unver))
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
        # format_stats() embeds profile/tool names read from the kanban DB —
        # DATA that must not be markup-parsed (t_716cf37c).
        body = theme.esc(stats_mod.format_stats(result))
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
        # format_throughput() embeds node names/labels from the fleet — DATA.
        body = theme.esc(throughput.format_throughput(tp))
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
        tgt = f" (target {theme.esc(d['target'])})" if "target" in d else ""
        body = (f"autoscale: {theme.esc(d['action'])}{tgt} — "
                f"{theme.esc(d['reason'])}")
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
                table.add_row(f"[{colour}]{theme.esc(task_id)}[/]",
                              theme.esc(action), theme.esc(to))
            console.print(table)
        else:
            console.print("[dim]no escalations pending[/dim]")
    sys.exit(0)


def _handle_update():
    """``hscc update --check`` — compare installed VERSION to the latest release.

    Read-only verb (card t_a4055572): the ONLY write it may ever perform is
    the 24h cache at ``~/.hscc/update-check.json``. It never downloads,
    installs, upgrades or starts anything — it prints the exact command for
    the detected install mode and the operator runs it.

    Loudness contract (documented per surface in test_update_check.py): the
    explicit verb is the LOUD surface — a failed check prints one short line
    to stderr and exits 1 (the card's ruling: "rc of the check verb itself
    on hard failure = 1 is fine"). The ambient surfaces (status notice,
    daemon log line) are the QUIET ones.

    Exit codes: 0 up to date / newer-than-latest / newer available; 1 only
    when the check itself could not run (offline, quota, malformed payload).
    A newer release is NOT an error — scripts chaining on rc must read the
    text, and cron-style callers get 0 for "worked, here is the answer".
    """
    from hscc_daemon import update_check

    argv, theme_name = _strip_theme_arg(sys.argv[2:])
    # F6 (same rule `cluster up/down` learned the hard way): an argument this
    # verb does not understand is a refusal, never an implicit "go ahead".
    if "--check" not in argv:
        print("Error: 'hscc update' needs --check.\n\n"
              "Usage: hscc update --check [--json]", file=sys.stderr)
        return 1
    json_mode = "--json" in argv
    leftover = [a for a in argv if a not in ("--check", "--json")]
    if leftover:
        print(f"Error: unknown argument for 'hscc update': {leftover[0]}\n\n"
              "Usage: hscc update --check [--json]", file=sys.stderr)
        return 1

    result = update_check.run_check()

    # Shadow scan (card t_9b98e696) — a REPORT about PATH ordering, never a
    # repair, and never a reason this check fails. Only for a completed
    # comparison: an offline check has its own single stderr line to spend,
    # and the offline-quiet contract (card t_a4055572) is not amended here.
    # detect_path_shadow swallows everything; the guard is belt-and-braces.
    shadow = None
    if result.get("ok"):
        try:
            shadow = update_check.detect_path_shadow(
                install_mode=result.get("mode"))
        except Exception:
            shadow = None

    if json_mode:
        payload = {k: v for k, v in result.items() if k != "entry"}
        if result.get("ok"):
            # Extension, not a break: existing keys and their meanings are
            # untouched; `shadow` is null-or-object (pinned back-compat).
            payload["shadow"] = shadow
        print(json.dumps(payload, sort_keys=True))
        return 0 if result.get("ok") else 1

    console = theme.make_console(theme_name)
    if not result.get("ok"):
        # One short line on stderr (spec) — the LOUD surface by design. A
        # failure on STDOUT would be swallowed by `hscc update --check 2>/dev/null`
        # pipelines and break `... || echo current`: the failure belongs on the
        # error channel the shell routes it to.
        err = theme.make_console(theme_name, file=sys.stderr)
        err.print(f"[error]update check failed:[/error] "
                  f"{theme.esc(result.get('error', 'unknown error'))}")
        cached = result.get("cached")
        if cached and cached.get("latest_tag"):
            # Salvage, not silence: what we knew last time it worked.
            err.print(f"[dim]last good check: "
                      f"{theme.esc(cached['latest_tag'])} was "
                      "latest[/dim]")
        return 1

    for line in update_check.format_check_result(result):
        # tag/version strings are DATA from the network — never markup-parsed.
        # soft_wrap=True keeps each line byte-contiguous: the `upgrade:  <cmd>`
        # line is what operators copy-paste, and a Rich fold at column 80
        # would break the command mid-path (same "keep the actionable
        # substring contiguous" contract as verify's `next step:` lines).
        console.print(theme.esc(line), soft_wrap=True)
    for line in update_check.format_shadow_warning(shadow):
        # Same rendering contract as the comparison block: paths are DATA
        # (esc) and must not be folded mid-path (soft_wrap) — the explicit
        # keg path on the last-but-one line is copy-paste material.
        console.print(theme.esc(line), soft_wrap=True)
    return 0


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
    # t_5132deb3: `hscc profiles --help` (and `-h`) used to load the cluster
    # engine and run cmd_profile_status — the only group-less verb the audit
    # had no help intercept for. Same raw-argv shape as the cluster/kanban/
    # api intercepts: a help token anywhere in the tail prints COMMAND_HELP
    # (the same text `hscc help profiles` prints) and loads nothing.
    if _help_token_index(sys.argv[1:]) is not None:
        print("hscc profiles")
        print(f"  {COMMAND_HELP['profiles']}")
        return 0
    eng = _load_cluster_engine()
    if eng is None:
        return 1
    _, theme_name = _strip_theme_arg(sys.argv[1:])
    console = theme.make_console(theme_name)
    return cluster_render.render_profiles(console, eng.cmd_profile_status())


# Verbs of flightdeck's `project` subgroup, aliased to the top of
# `hscc project` so the group name does not have to be typed twice.
# MUST mirror flightdeck's real subgroup — test_project_alias_covers_whole_subgroup
# pins this set against the live parser and fails listing what drifted
# (sessions/digest/link/unlink were added to the subgroup and silently
# missing here: those four kept reading the doubled `hscc project project X`).
# Adding a verb to flightdeck's subgroup is still safe without touching this
# table (the doubled form always works); adding it here upgrades that verb to
# the short form.
_PROJECT_SUBGROUP_VERBS = frozenset(
    {"chat", "digest", "link", "list", "new", "pull", "push", "remove",
     "repair", "sessions", "sync", "unlink"})


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
    "throughput", "autoscale", "escalate", "bootstrap", "update",
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

    # --version / -v -> print the version banner (e.g. "hscc 2.4.0") and exit
    # 0, before any other dispatch. Same source of truth as the banner.
    if args[0] in ("--version", "-v"):
        print(f"hscc {_get_version()}")
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

    # 'cards' group — live per-card worker view (read-only)
    if cmd == "cards":
        from hscc_daemon.cards_live import cmd_cards
        rc = cmd_cards(args[1:])
        sys.exit(rc)

    # 'orch' group — orchestrator verbs (t_5da7fd56). The VERB lives in the
    # cluster engine (its cmd_orch router, merged by t_75f17c83); without this
    # branch `hscc orch inject` died on "Unknown command: orch" while the
    # standalone engine entry worked — the split the operator called out.
    # Help is intercepted INSIDE _handle_orch (before the engine load), so it
    # is deliberately not routed through the DAEMON_COMMANDS intercept below:
    # that one prints COMMAND_HELP, which is a one-line-per-verb blurb, and
    # `hscc cluster`/`template`/`api` set the group precedent of a dedicated
    # group listing instead.
    if cmd == "orch":
        rc = _handle_orch(args[1:])
        sys.exit(rc)

    # Per-command help for the daemon verbs (t_5132deb3: the branch used to
    # fire only on `args[1] == "--help"`, so `escalate -h` / `autoscale -h`
    # RAN the read-only verb instead of printing help, and a --help that was
    # not the first tail token (e.g. `verify --json --help`) fell through to
    # dispatch too — the B10 class one level deeper. Now ANY "-h"/"--help"
    # token anywhere in the RAW tail intercepts before dispatch, the same
    # raw-argv shape as the cluster/kanban/api/heal intercepts. Wording comes
    # from COMMAND_HELP — the same source `hscc help <cmd>` prints — so the
    # two cannot drift. `-h` and `--help` print the SAME bytes (pinned).
    if cmd.lower() in DAEMON_COMMANDS and _help_token_index(args[1:]) is not None:
        help_text = COMMAND_HELP.get(cmd.lower(), f"Run 'hscc help {cmd}' for details.")
        print(f"hscc {cmd.lower()}")
        print(f"  {help_text}")
        sys.exit(0)

    # 'bootstrap' verb — locate + exec the installer (card t_bc5a3fee). MUST
    # sit after the DAEMON_COMMANDS help intercept above: `hscc bootstrap
    # --help` (or `-h` anywhere in the tail) prints COMMAND_HELP and never
    # execs the installer; reaching here means a real run.
    if cmd == "bootstrap":
        rc = _handle_bootstrap()
        sys.exit(rc)

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

    # Advanced commands (also support help). The ed-* placeholder trio is
    # GONE (F5 card t_fc307a51, operator fan-out ruling DELETE): it was
    # advertised as "placeholder, not yet available" and did nothing — help
    # must not advertise dead commands. `advanced` joined this set so
    # `hscc advanced --help` prints its COMMAND_HELP entry instead of the
    # unknown-command blob + exit 1 (audit D-rc finding). t_5132deb3 closes
    # the `-h` half of that surface (the gap F5 recorded in
    # docs/audits/NOTES_t_fc307a51.md): `start-daemon -h` used to fall
    # straight into the daemon-loop launcher — the worst case of the CLI-wide
    # -h blind spot — and `advanced -h` printed "Unknown command". Same
    # raw-argv any-tail-token shape as the DAEMON_COMMANDS branch above.
    advanced_cmds = {"start-daemon", "advanced"}
    if cmd.lower() in advanced_cmds and _help_token_index(args[1:]) is not None:
        help_text = COMMAND_HELP.get(cmd.lower(), "")
        print(f"hscc {cmd.lower()}")
        print(f"  {help_text}")
        sys.exit(0)

    # Daemon command dispatch
    cmd_lower = cmd.lower()

    # Per-command --help for the heal verbs (BROKEN B4, card t_aeb982bc).
    # Must sit BEFORE the dispatch below and scan the RAW tail: on the base
    # tree `heal-clear --help` reached cmd_heal with "--help" as the NODE
    # NAME and ran the clear path (audit EXEC_ECHO: "No strikes recorded for
    # --help"), and `heal-clear <node> --help` CLEARED <node>'s strikes while
    # the operator believed they were reading help. Same raw-argv shape as
    # the cluster/kanban/api intercepts. Wording comes from COMMAND_HELP —
    # the same source `hscc help heal-clear` prints — so the two cannot
    # drift. cmd_heal keeps a second-line guard for direct callers.
    if cmd_lower in ("heal-clear", "heal-status") and any(
            a in ("-h", "--help") for a in args[1:]):
        print(f"hscc {cmd_lower}")
        print(f"  {COMMAND_HELP[cmd_lower]}")
        sys.exit(0)

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
        # Forward EVERYTHING after `check`: cmd_check separates the optional
        # stream positional from the `--repo`/`--json` flags (t_5abdb13d).
        cmd_check(*args[1:])
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
    elif cmd_lower == "unit-emit":
        # INTERNAL verb (t_d8f13693), deliberately absent from DAEMON_COMMANDS /
        # COMMAND_HELP / the help surface: it is the machine-readable unit-file
        # writer systemd-setup.sh calls (`hscc.py unit-emit --json`), not an
        # operator command. It replaced `hscc.py plist > UNIT_FILE`, which
        # redirected a rendered Rich view into a unit file.
        from hscc_daemon.install import cmd_unit_emit
        sys.exit(cmd_unit_emit(args[1:]))
    elif cmd_lower == "install":
        from hscc_daemon.install import cmd_install
        cmd_install()
    elif cmd_lower == "uninstall":
        from hscc_daemon.install import cmd_uninstall
        cmd_uninstall()
    elif cmd_lower == "log":
        from hscc_daemon.cli import cmd_log
        cmd_log()
    elif cmd_lower == "update":
        # Read-only version check (t_a4055572). The `hscc update -h/--help`
        # case never reaches here: the DAEMON_COMMANDS help intercept above
        # prints COMMAND_HELP first (the same source `hscc help update`
        # prints), so a help token cannot trigger a network call.
        sys.exit(_handle_update())
    elif cmd_lower == "start-daemon":
        from hscc_daemon.cli import cmd_start_daemon
        cmd_start_daemon()
    elif cmd_lower == "heal-clear":
        # F5 operator override: the breaker's alert text names this command,
        # so it must resolve (an alert pointing at a non-existent command is
        # worse than no alert).
        from hscc_daemon.cli import cmd_heal
        cmd_heal(node=args[1] if len(args) > 1 else None,
                 reason=" ".join(args[2:]))
    elif cmd_lower == "heal-status":
        from hscc_daemon.cli import cmd_heal
        cmd_heal(status_only=True)
    else:
        print(f"Unknown command: {cmd}")
        print("Available: start, stop, status, check, watch, triggers, notify, plist, install, uninstall, log, bootstrap, update, cluster, template, profiles, project, api, kanban, orch, verify, stats, throughput, autoscale, escalate, start-daemon, heal-clear, heal-status")
        sys.exit(1)


if __name__ == "__main__":
    main()
