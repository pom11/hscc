#!/usr/bin/env python3
"""
HSCC Installer — Core wiring logic.

Handles:
- Detecting existing setup
- Wiring plugins (symlink, register)
- Installing launchd plist
- Creating config from template
- Deploying model to cluster
- Verifying setup
"""

import json
import os
import shutil
import subprocess
from pathlib import Path


# ── Constants ────────────────────────────────────────────────────────────────

HOME = Path.home()
HSCC_DIR = HOME / ".hscc"
HERMES_DIR = HOME / ".hermes"
OPENCLAW_DIR = HOME / ".openclaw"
SPARKRUN_DIR = HOME / ".sparkrun-local"
R2D2CC_DIR = HOME / ".r2d2cc"

# Qwen3.6 model details
QWEN36_REPOS = {
    "35B": {
        "id": "Qwen/Qwen3.6-35B-A3B-FP8",
        "repo": "models--Qwen--Qwen3.6-35B-A3B-FP8",
        "path": "/mnt/nas/hub/models--Qwen--Qwen3.6-35B-A3B-FP8",
    },
    "27B": {
        "id": "Qwen/Qwen3.6-27B-FP8",
        "repo": "models--Qwen--Qwen3.6-27B-FP8",
        "path": "/mnt/nas/hub/models--Qwen--Qwen3.6-27B-FP8",
    },
}

CLUSTER_NODES = ["192.0.2.10", "192.0.2.10", "192.0.2.11", "192.0.2.12"]
NAS_NODE = "192.0.2.10"
PRIMARY_NODE = "192.0.2.11"


# ── Detection ────────────────────────────────────────────────────────────────

def detect_existing_setup():
    """Detect what's already installed."""
    return {
        "hermes_config": HERMES_DIR / "config.yaml",
        "hscc_plugins": list(HSCC_DIR.glob("hscc-*/hscc.py")),
        "launchd_plist": HOME / "Library" / "LaunchAgents" / "com.hermes.hscc-daemon.plist",
        "gateway_reachable": check_gateway_reachable(),
        "daemon_running": check_daemon_running(),
        "qwen36_cached": detect_model_cache("35B"),
        "sparkrun_config": SPARKRUN_DIR / "config.yaml",
        "cluster_nodes": detect_reachable_nodes(),
    }


def check_gateway_reachable():
    """Check if OpenClaw gateway is reachable."""
    try:
        import urllib.request
        req = urllib.request.Request("http://localhost:18789/health")
        req.add_header("User-Agent", "hscc/1.0")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except:
        return False


def check_daemon_running():
    """Check if HSCC daemon is running via launchd."""
    try:
        result = subprocess.run(
            ["launchctl", "list", "com.hermes.hscc-daemon"],
            capture_output=True, text=True
        )
        return result.returncode == 0
    except:
        return False


def detect_model_cache(model_key="35B"):
    """Detect if a model is cached on any cluster node."""
    model_info = QWEN36_REPOS[model_key]
    repo = model_info["repo"]
    
    # Check NAS first (fast)
    nas_path = model_info["path"]
    if check_ssh(NAS_NODE, f"test -d {nas_path}/blobs"):
        return True
    
    # Check each node
    for node in CLUSTER_NODES:
        if node == NAS_NODE:
            continue
        # Count blobs on node
        rc, stdout, _ = run_ssh(node, 
            f"find /home/spark/.cache/huggingface/hub/{repo}/blobs -type f 2>/dev/null | wc -l"
        )
        if rc == 0 and int(stdout.strip()) > 0:
            return True
    
    return False


def detect_reachable_nodes():
    """Detect which cluster nodes are reachable."""
    reachable = {}
    for node in CLUSTER_NODES:
        rc, _, _ = run_ssh(node, "true", timeout=3)
        reachable[node] = (rc == 0)
    return reachable


# ── Wiring ───────────────────────────────────────────────────────────────────

def wire_plugins():
    """Register HSCC plugins with Hermes."""
    plugins_dir = HERMES_DIR / "plugins"
    if not plugins_dir.exists():
        print("  → Creating plugins directory")
        plugins_dir.mkdir(parents=True, exist_ok=True)
    
    # Check which plugins exist
    hscc_plugins = sorted([d for d in HSCC_DIR.glob("hscc-*/hscc.py")])
    
    for plugin_path in hscc_plugins:
        plugin_name = plugin_path.parent.name
        target = plugins_dir / plugin_name
        
        if not target.exists():
            # Symlink plugin to Hermes plugins dir
            try:
                os.symlink(str(plugin_path.parent), target)
                print(f"  ✓ Linked {plugin_name}")
            except FileExistsError:
                print(f"  → {plugin_name} already exists")
        else:
            print(f"  → {plugin_name} already linked")


def install_launchd_plist():
    """Install launchd plist for HSCC daemon."""
    plist_path = HOME / "Library" / "LaunchAgents" / "com.hermes.hscc-daemon.plist"
    
    if plist_path.exists():
        print("  → Launchd plist already exists")
        return
    
    # Create plist content
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(PLIST_TEMPLATE.format(
        hscc_daemon=str(HSCC_DIR / "hscc-daemon" / "hscc.py"),
        log_path=str(HOME / "Library" / "Logs" / "hscc-daemon.log"),
        home=str(HOME),
    ))
    print("  ✓ Installed launchd plist")


PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.hermes.hscc-daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/python3</string>
        <string>{hscc_daemon}</string>
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
"""


def create_config(config_path=None):
    """Create config.yaml from template or defaults."""
    if config_path is None:
        config_path = HSCC_DIR / "config.yaml"
    
    if config_path.exists():
        print("  → Config already exists")
        return config_path
    
    # Try template first
    template_path = R2D2CC_DIR / "configs" / "hscc-config.yaml.template"
    if template_path.exists():
        config_path.write_text(template_path.read_text())
        print("  ✓ Created config.yaml from template")
        return config_path
    
    # Fallback: write default config
    config_path.write_text("""# HSCC Configuration — generated
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
    print("  ✓ Created default config.yaml")
    return config_path


# ── Model Deployment ─────────────────────────────────────────────────────────

def deploy_model(model_key="35B", force=False):
    """Deploy Qwen3.6 model to cluster."""
    model_info = QWEN36_REPOS[model_key]
    repo = model_info["repo"]
    path = model_info["path"]
    
    print(f"  → Deploying {model_key}B model...")
    
    # Check if already cached anywhere
    if detect_model_cache(model_key) and not force:
        print("  ✓ Model already cached on cluster")
        return True
    
    # Check NAS
    if not check_ssh(NAS_NODE, f"test -d {path}/blobs"):
        print("  ✗ Model not found on NAS")
        return False
    
    # Sync to each node
    synced = 0
    for node in CLUSTER_NODES:
        if node == NAS_NODE:
            continue
        
        # Check if already cached on this node
        blobs = run_ssh(node, 
            f"find /home/spark/.cache/huggingface/hub/{repo}/blobs -type f 2>/dev/null | wc -l"
        )
        if int(blobs[1].strip()) > 0:
            print(f"  → {node}: already cached")
            synced += 1
            continue
        
        # Copy from NAS to node
        print(f"  → {node}: syncing from NAS...")
        rc = run_ssh(NAS_NODE, 
            f"rsync -avz -e 'ssh -o StrictHostKeyChecking=no' "
            f"{path}/blobs/ spark@{node}:/home/spark/.cache/huggingface/hub/{repo}/blobs/"
        )[0]
        
        if rc == 0:
            print(f"  ✓ {node}: synced")
            synced += 1
        else:
            print(f"  ✗ {node}: sync failed")
    
    # Start vLLM on primary node
    print(f"  → Starting vLLM on {PRIMARY_NODE}...")
    start_vllm(PRIMARY_NODE, model_info["id"])
    
    print(f"  ✓ Model deployed to {synced} nodes")
    return synced > 0


def start_vllm(node, model_id):
    """Start vLLM container on a node."""
    # This would use sparkrun to start the model
    # For now, show placeholder
    print(f"  → {node}: would start sparkrun run with model {model_id}")


# ── Verification ─────────────────────────────────────────────────────────────

def verify_setup():
    """Verify HSCC setup is complete."""
    verifications = []
    
    # 1. Config exists
    config = HSCC_DIR / "config.yaml"
    verifications.append(("Config", config.exists()))
    
    # 2. Gateway reachable
    verifications.append(("Gateway", check_gateway_reachable()))
    
    # 3. Daemon running
    verifications.append(("Daemon", check_daemon_running()))
    
    # 4. Plugins registered
    plugins = list(HERMES_DIR.glob("plugins/hscc-*"))
    verifications.append(("Plugins", len(plugins) >= 5))
    
    # 5. Model cached
    verifications.append(("Model", detect_model_cache("35B")))
    
    return verifications


# ── Helpers ──────────────────────────────────────────────────────────────────

def run_ssh(node, command, timeout=30):
    """Run SSH command on remote node."""
    cmd = (
        f"ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null -o ConnectTimeout={timeout} "
        f"spark@{node} '{command}'"
    )
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    except Exception as e:
        return 1, "", str(e)


def check_ssh(node, command):
    """Quick check if SSH command succeeds."""
    rc, _, _ = run_ssh(node, command)
    return rc == 0
