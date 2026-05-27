#!/usr/bin/env python3
"""
HSCC SSH Utilities — Key management, copy, and testing.
"""

import os
import subprocess
from pathlib import Path


SSH_KEY_DIR = Path.home() / ".ssh"
SSH_KEY_PATH = SSH_KEY_DIR / "id_ed25519"


def check_ssh_keys():
    """Check if SSH keys exist, generate if not."""
    if SSH_KEY_PATH.exists():
        return True
    
    print("  → No SSH keys found. Generating ed25519 key...")
    rc, stdout, stderr = run_cmd(
        f"ssh-keygen -t ed25519 -f {SSH_KEY_PATH} -N '' -C 'hscc-cluster'"
    )
    if rc == 0:
        print("  ✓ Generated SSH key")
        return True
    
    print(f"  ✗ Failed to generate key: {stderr}")
    return False


def copy_ssh_key(node):
    """Copy public SSH key to remote node."""
    if not SSH_KEY_PATH.exists():
        return False
    
    pub_key = SSH_KEY_PATH.with_suffix(".pub")
    if not pub_key.exists():
        return False
    
    key_content = pub_key.read_text().strip()
    cmd = (
        f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"spark@{node} "
        f"'mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        f"echo \"{key_content}\" >> ~/.ssh/authorized_keys && "
        f"chmod 600 ~/.ssh/authorized_keys'"
    )
    
    rc, stdout, stderr = run_cmd(cmd, timeout=30)
    if rc == 0:
        print(f"  ✓ SSH key copied to {node}")
        return True
    
    print(f"  ✗ Failed to copy key to {node}: {stderr}")
    return False


def test_ssh(node):
    """Test SSH connection to node."""
    cmd = (
        f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-o ConnectTimeout=5 spark@{node} 'echo connected'"
    )
    rc, stdout, stderr = run_cmd(cmd, timeout=10)
    return rc == 0 and "connected" in stdout


def run_cmd(cmd, timeout=30):
    """Run a shell command."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    except Exception as e:
        return 1, "", str(e)
