#!/usr/bin/env python3
"""
HSCC Verify — Bootstrap verification and health checks.
"""

import os
import subprocess
from pathlib import Path


def verify_bootstrap():
    """Run full bootstrap verification."""
    results = {
        "config": verify_config(),
        "gateway": verify_gateway(),
        "daemon": verify_daemon(),
        "plugins": verify_plugins(),
        "cluster": verify_cluster(),
        "model": verify_model(),
    }
    
    all_passed = all(results.values())
    return all_passed, results


def verify_config():
    """Verify HSCC config exists and is valid."""
    config = Path.home() / ".hscc" / "config.yaml"
    if not config.exists():
        return False
    
    # Try to parse YAML (simple check)
    try:
        import yaml
        with open(config) as f:
            data = yaml.safe_load(f)
        return "cluster" in data or "gateway" in data
    except:
        # Fallback: check for key strings
        content = config.read_text()
        return "cluster" in content or "gateway" in content


def verify_gateway():
    """Verify OpenClaw gateway is reachable."""
    try:
        import urllib.request
        req = urllib.request.Request("http://localhost:18789/health")
        req.add_header("User-Agent", "hscc/1.0")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except:
        return False


def verify_daemon():
    """Verify HSCC daemon is running."""
    try:
        result = subprocess.run(
            ["launchctl", "list", "com.hermes.hscc-daemon"],
            capture_output=True, text=True
        )
        return result.returncode == 0
    except:
        return False


def verify_plugins():
    """Verify HSCC plugins are registered."""
    plugins_dir = Path.home() / ".hermes" / "plugins"
    if not plugins_dir.exists():
        return False
    
    hscc_plugins = list(plugins_dir.glob("hscc-*/hscc.py"))
    return len(hscc_plugins) >= 5


def verify_cluster():
    """Verify cluster nodes are reachable."""
    nodes = ["192.0.2.10", "192.0.2.11", "192.0.2.12"]
    reachable = 0
    for node in nodes:
        cmd = (
            f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"-o ConnectTimeout=3 spark@{node} 'true'"
        )
        result = subprocess.run(cmd, shell=True, capture_output=True, timeout=5)
        if result.returncode == 0:
            reachable += 1
    return reachable >= 2  # At least 2/3 nodes reachable


def verify_model():
    """Verify Qwen3.6 model is cached."""
    for node in ["192.0.2.10", "192.0.2.11", "192.0.2.12"]:
        cmd = (
            f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"spark@{node} "
            f"'find /home/spark/.cache/huggingface/hub/models--Qwen--Qwen3.6-35B-A3B-FP8/blobs -type f 2>/dev/null | wc -l'"
        )
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
        if int(result.stdout.strip()) > 0:
            return True
    return False


def health_check():
    """Run quick health check."""
    checks = {
        "gateway": verify_gateway(),
        "daemon": verify_daemon(),
        "plugins": verify_plugins(),
        "model": verify_model(),
    }
    
    passed = sum(1 for v in checks.values() if v)
    total = len(checks)
    
    print(f"Health: {passed}/{total} checks passed")
    for check, result in checks.items():
        marker = "✓" if result else "✗"
        print(f"  {marker} {check}")
    
    return passed == total
