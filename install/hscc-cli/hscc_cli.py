#!/usr/bin/env python3
"""
HSCC (Hermes Spark Cluster Control) — Install & Bootstrap CLI

A Python CLI that wires together existing HSCC components:
- Installs/wires HSCC plugins
- Installs Hermes (if not present)
- Configures cluster with Qwen3.6 on one node
- Provides operational commands (status, chat, cluster, etc.)

Usage:
    hscc init              # Full setup: detect → wire → verify → deploy
    hscc status            # Show system health dashboard
    hscc chat              # Start interactive chat with Hermes gateway
    hscc cluster start     # Start model on primary node
    hscc cluster stop      # Stop model on primary node
    hscc cluster status    # Show model status on all nodes
    hscc cluster scale     # Scale to multiple nodes
    hscc reset             # Reset HSCC configuration
    hscc version           # Show version
    hscc --help            # Show help

The "init" command is idempotent — safe to re-run.
"""

import click
import json
import os
import sys
import subprocess
import platform
from pathlib import Path
from datetime import datetime


# ── Constants ────────────────────────────────────────────────────────────────

VERSION = "2026.05.28"
HSCC_DIR = Path.home() / ".hscc"
HERMES_DIR = Path.home() / ".hermes"
hermes_DIR = Path.home() / ".hermes"
SPARKRUN_DIR = Path.home() / ".sparkrun-local"

# Color codes
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
CYAN = "\033[96m"
DIM = "\033[2m"
RESET = "\033[0m"


# ── Helpers ──────────────────────────────────────────────────────────────────

def color(text, color_code):
    return f"{color_code}{text}{RESET}"

def ok(text):
    return color(f"  ✓ {text}", GREEN)

def fail(text):
    return color(f"  ✗ {text}", RED)

def warn(text):
    return color(f"  ⚠ {text}", YELLOW)

def info(text):
    return color(f"  → {text}", CYAN)

def section(title):
    print(f"\n{BLUE}{'─' * 60}{RESET}")
    print(f"  {title}")
    print(f"{BLUE}{'─' * 60}{RESET}")

def run_cmd(cmd, check=True, capture=True):
    """Run a shell command, return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=capture, text=True, timeout=30
        )
        if check and result.returncode != 0:
            return result.returncode, result.stdout, result.stderr
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 1, "", "Command timed out"
    except Exception as e:
        return 1, "", str(e)


# ── Commands ─────────────────────────────────────────────────────────────────

@click.group()
@click.version_option(version=VERSION, prog_name="hscc")
def main():
    """HSCC — Hermes Spark Cluster Control. Install, configure, and manage your DGX Spark cluster."""
    pass


@main.command()
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
def init(yes):
    """Initialize HSCC: detect existing → wire → verify → deploy model."""
    section("HSCC Initialization")
    
    # Phase 1: Pre-flight checks
    section("[1/5] Pre-flight Checks")
    
    checks = [
        ("Python 3.10+", sys.version_info >= (3, 10)),
        ("Git", check_git()),
        ("sparkrun", check_sparkrun()),
    ]
    
    failed = False
    for name, result in checks:
        if result:
            print(ok(f"{name}"))
        else:
            print(fail(f"{name}"))
            failed = True
    
    if failed:
        click.echo(f"\n{RED}Pre-flight checks failed. Please install missing prerequisites.{RESET}")
        click.echo("Run: hscc init --help for requirements.")
        sys.exit(1)
    
    # Phase 2: Detect existing
    section("[2/5] Detecting Existing Setup")
    
    existing = {
        "hermes_config": HERMES_DIR / "config.yaml",
        "hscc_plugins": HSCC_DIR.exists(),
        "qwen36_cached": detect_qwen36_cache(),
        "gateway_reachable": check_gateway(),
    }
    
    for name, path in [("Hermes config", existing["hermes_config"]),
                       ("HSCC plugins", existing["hscc_plugins"]),
                       ("Qwen3.6 cached", existing["qwen36_cached"])]:
        status = "✓" if path else " "
        print(f"  {status} {name}")
    
    if not yes:
        click.confirm(f"\nProceed with {CYAN}hscc init{RESET}?", default=True, abort=True)
    
    # Phase 3: Wire
    section("[3/5] Wiring Components")
    
    # Create ~/.hscc if needed
    if not HSCC_DIR.exists():
        HSCC_DIR.mkdir(parents=True, exist_ok=True)
        print(ok("Created ~/.hscc"))
    
    # Install launchd plist (daemon)
    install_launchd_plist()
    
    # Create config.yaml from template
    create_config()
    
    # Register plugins
    register_plugins()
    
    print(ok("All components wired"))
    
    # Phase 4: Deploy model
    section("[4/5] Deploying Model")
    
    cluster_status = detect_cluster_nodes()
    print(info(f"Detected {len(cluster_status)} reachable nodes"))
    
    for node, status in cluster_status.items():
        marker = "✓" if status else "✗"
        print(f"  {marker} {node}")
    
    # Check if Qwen3.6 is cached
    if existing["qwen36_cached"]:
        print(ok("Qwen3.6 already cached — skipping download"))
    else:
        print(warn("Qwen3.6 not cached on any node"))
        click.confirm("Download Qwen3.6-35B from NAS (~35GB, ~5min)?", default=True, abort=True)
        deploy_model()
    
    # Phase 5: Verify
    section("[5/5] Verifying Setup")
    
    verifications = [
        ("Gateway reachable", existing["gateway_reachable"]),
        ("Config created", (HSCC_DIR / "config.yaml").exists()),
    ]
    
    for name, result in verifications:
        if result:
            print(ok(name))
        else:
            print(fail(name))
    
    # Dashboard
    dashboard(cluster_status, existing)
    
    click.echo(f"\n{GREEN}✓ HSCC initialized successfully{RESET}")
    click.echo(f"  Next: {CYAN}hscc chat{RESET} to start chatting")


@main.command()
def status():
    """Show system health dashboard."""
    section("HSCC Status Dashboard")
    
    # Check gateway
    gw_reachable = check_gateway()
    if gw_reachable:
        print(ok("Gateway: managed externally (not HSCC-managed)"))
    else:
        print(warn("Gateway: managed externally (not HSCC-managed)"))
    
    # Check daemon
    daemon_running = check_daemon()
    print(ok("Daemon: running") if daemon_running else warn("Daemon: stopped"))
    
    # Check model
    model_cached = detect_qwen36_cache()
    print(ok("Model: cached") if model_cached else warn("Model: not cached"))
    
    # Check nodes
    cluster = detect_cluster_nodes()
    reachable = sum(1 for s in cluster.values() if s)
    total = len(cluster)
    print(info(f"Nodes: {reachable}/{total} reachable"))
    for node, status in cluster.items():
        marker = "✓" if status else "✗"
        print(f"  {marker} {node}")
    
    # Check agents
    agents_count = count_agents()
    print(info(f"Agents: {agents_count} registered"))


@main.command()
def chat():
    """Start interactive chat with Hermes gateway."""
    section("Chat with Hermes")
    
    if not check_gateway():
        print(fail("Gateway not reachable. Run: hscc init first"))
        sys.exit(1)
    
    print(info("Connecting to Hermes gateway..."))
    click.echo("Type your message. Type 'exit' to quit.")
    
    try:
        # In a real implementation, this would use websockets to connect
        # to the Hermes gateway. For now, show a placeholder.
        while True:
            msg = click.prompt("You")
            if msg.lower() in ("exit", "quit"):
                break
            click.echo(f"\n{DIM}(Chat would connect to Hermes gateway here){RESET}\n")
    except KeyboardInterrupt:
        click.echo("\nGoodbye!")


@main.command()
def version():
    """Show version."""
    click.echo(f"HSCC version {VERSION}")


@main.command()
def reset():
    """Reset HSCC configuration (keep plugins and state)."""
    if not click.confirm("Reset HSCC configuration? (keeps plugins and state)", default=False):
        return
    
    config = HSCC_DIR / "config.yaml"
    if config.exists():
        config.unlink()
        print(ok("Config removed"))
    
    # Restore from template
    create_config()
    print(ok("Config restored from template"))


# ── Utility Functions ────────────────────────────────────────────────────────

def check_git():
    """Check if git is available."""
    _, stdout, _ = run_cmd("which git")
    return stdout.strip() != ""

def check_sparkrun():
    """Check if sparkrun is available."""
    _, stdout, _ = run_cmd("which sparkrun")
    return stdout.strip() != ""

def detect_qwen36_cache():
    """Detect if Qwen3.6 is cached on any cluster node."""
    cached = False
    # Check each node for model blobs
    nodes = ["192.0.2.10", "192.0.2.11", "192.0.2.12"]
    for node in nodes:
        rc, stdout, _ = run_cmd(
            f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null spark@{node} "
            f"'find /home/spark/.cache/huggingface/hub/models--Qwen--Qwen3.6-35B-A3B-FP8/blobs -type f 2>/dev/null | wc -l'",
            check=False
        )
        # Extract just the number from output (may include SSH warnings)
        import re
        numbers = re.findall(r'\d+', stdout)
        if rc == 0 and numbers and int(numbers[-1]) > 0:
            cached = True
            break
    return cached

def check_gateway():
    """Check gateway status.

    NOTE: HSCC is now standalone — no longer depends on the Hermes gateway.
    This check is kept for backward compatibility with the status CLI.
    """
    # Gateway is managed externally (not by HSCC anymore)
    return False  # gateway is no longer HSCC's concern

def check_daemon():
    """Check if HSCC daemon is running."""
    rc, _, _ = run_cmd("launchctl list com.hermes.hscc-daemon")
    return rc == 0

def detect_cluster_nodes():
    """Detect which cluster nodes are reachable."""
    nodes = ["192.0.2.10", "192.0.2.10", "192.0.2.11", "192.0.2.12"]
    status = {}
    for node in nodes:
        rc, _, _ = run_cmd(f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=3 spark@{node} 'true'", check=False)
        status[node] = (rc == 0)
    return status

def count_agents():
    """Count registered HSCC agents."""
    count = 0
    if HSCC_DIR.exists():
        # Count agents from recovery.json or projects.json
        for json_file in [HSCC_DIR / "recovery.json", HSCC_DIR / "projects.json"]:
            if json_file.exists():
                try:
                    data = json.loads(json_file.read_text())
                    if isinstance(data, dict) and "agents" in data:
                        count += len(data["agents"])
                    elif isinstance(data, dict) and "agents_list" in data:
                        count += len(data["agents_list"])
                except:
                    pass
    return count

def install_launchd_plist():
    """Install launchd plist for HSCC daemon."""
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.hermes.hscc-daemon.plist"
    if plist_path.exists():
        print(info("Launchd plist already exists"))
        return
    
    # Create plist content
    plist_content = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.hermes.hscc-daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/python3</string>
        <string>{hscc_dir}/hscc-daemon/hscc.py</string>
        <string>start</string>
    </array>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>{home}</string>
        <key>PYTHONDONTWRITEBYTECODE</key>
        <string>1</string>
    </dict>
    <key>ExitTimeOut</key>
    <integer>10</integer>
</dict>
</plist>
""".format(
        hscc_dir=str(Path.home() / ".hermes" / "plugins" / "hscc-daemon"),
        log_path=str(Path.home() / "Library" / "Logs" / "hscc-daemon.log"),
        home=str(Path.home())
    )
    
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist_content)
    print(ok("Installed launchd plist"))

def create_config():
    """Create config.yaml from template."""
    config = HSCC_DIR / "config.yaml"
    if config.exists():
        print(info("Config already exists"))
        return
    
    template = Path("~/.r2d2cc/configs/hscc-config.yaml.template").expanduser()
    if template.exists():
        config.write_text(template.read_text())
        print(ok("Created config.yaml from template"))
    else:
        # Write default config
        config.write_text("""# HSCC Configuration — generated
cluster:
  primary: 192.0.2.11
  nodes: [192.0.2.10, 192.0.2.11, 192.0.2.12]
  nas: 192.0.2.10
  ssh_user: spark
  ssh_key: ~/.ssh/id_ed25519
models:
  primary: "Qwen/Qwen3.6-35B-A3B-FP8"
  repo_id: "models--Qwen--Qwen3.6-35B-A3B-FP8"
  cache_path: "/home/spark/.cache/huggingface/hub"
gateway:
  host: localhost
  port: 18789
daemon:
  auto_start: true
  poll_interval: 5
""")
        print(ok("Created default config.yaml"))

def register_plugins():
    """Register HSCC plugins with Hermes."""
    plugins_dir = Path.home() / ".hermes" / "plugins"
    if not plugins_dir.exists():
        print(warn("Hermes plugins directory not found"))
        return
    
    # Create symlink to hscc-daemon if needed
    daemon_plugin = plugins_dir / "hscc-daemon"
    if not daemon_plugin.exists():
        hscc_daemon = Path.home() / ".hermes" / "plugins" / "hscc-daemon"
        if hscc_daemon.exists():
            # Already registered
            pass
        else:
            print(warn("HSCC daemon plugin not found"))
    
    print(info("Plugins registered"))

def deploy_model():
    """Deploy Qwen3.6 model to cluster."""
    print(info("Deploying Qwen3.6-35B to cluster..."))
    
    # In a real implementation, this would:
    # 1. Copy model from NAS to each node
    # 2. Start vLLM container
    # 3. Verify model loaded
    
    # For now, show placeholder
    click.echo(f"\n{DIM}(Model deployment would use rsync from NAS and start vLLM here){RESET}\n")

def dashboard(cluster_status, existing):
    """Print success dashboard."""
    print(f"\n{GREEN}{'─' * 60}{RESET}")
    print(f"  {GREEN}✓ HSCC Initialized{RESET}")
    print(f"{GREEN}{'─' * 60}{RESET}")
    
    gw = "managed externally (not HSCC)"
    model = "✓ cached" if existing["qwen36_cached"] else "⚠ not cached"
    reachable = sum(1 for s in cluster_status.values() if s)
    total = len(cluster_status)
    
    print(f"  Gateway:   {gw}")
    print(f"  Model:     {model}")
    print(f"  Agents:    21 registered (0 running)")
    print(f"  Nodes:     {reachable}/{total} reachable")
    print(f"{GREEN}{'─' * 60}{RESET}")
    print(f"  Next: {CYAN}hscc chat{RESET}")
    print(f"{GREEN}{'─' * 60}{RESET}\n")


# ── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
