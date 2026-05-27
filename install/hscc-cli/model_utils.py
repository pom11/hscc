#!/usr/bin/env python3
"""
HSCC Model Utilities — Cache detection, download, and verification.
"""

import os
import subprocess
from pathlib import Path


# Model definitions
MODELS = {
    "35B": {
        "id": "Qwen/Qwen3.6-35B-A3B-FP8",
        "repo": "models--Qwen--Qwen3.6-35B-A3B-FP8",
        "size_gb": 35,
        "blobs": 56,
        "path": "/mnt/nas/hub/models--Qwen--Qwen3.6-35B-A3B-FP8",
    },
    "27B": {
        "id": "Qwen/Qwen3.6-27B-FP8",
        "repo": "models--Qwen--Qwen3.6-27B-FP8",
        "size_gb": 27,
        "blobs": 48,
        "path": "/mnt/nas/hub/models--Qwen--Qwen3.6-27B-FP8",
    },
}


def detect_model_cache(model_key="35B"):
    """Detect if model is cached on any cluster node."""
    model = MODELS[model_key]
    repo = model["repo"]
    
    # Check NAS first
    if check_nas(model["path"]):
        return True
    
    # Check each node
    for node in ["192.0.2.10", "192.0.2.11", "192.0.2.12"]:
        if check_node_cache(node, repo):
            return True
    
    return False


def check_nas(path):
    """Check if model exists on NAS."""
    cmd = f"ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null spark@192.0.2.10 'test -d {path}/blobs && echo yes'"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return "yes" in result.stdout


def check_node_cache(node, repo):
    """Check if model is cached on a specific node."""
    cmd = (
        f"ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null spark@{node} "
        f"'find /home/spark/.cache/huggingface/hub/{repo}/blobs -type f 2>/dev/null | wc -l'"
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return int(result.stdout.strip()) > 0


def sync_model_to_node(model_key="35B", node=None):
    """Sync model from NAS to a specific node."""
    model = MODELS[model_key]
    repo = model["repo"]
    
    if node is None:
        # Sync to all reachable nodes except NAS
        nodes = ["192.0.2.11", "192.0.2.12"]
        for n in nodes:
            if not check_node_cache(n, repo):
                sync_model_to_node(model_key, n)
        return
    
    # Check if already cached
    if check_node_cache(node, repo):
        print(f"  → {node}: already cached")
        return
    
    # Sync from NAS
    print(f"  → {node}: syncing from NAS...")
    cmd = (
        f"ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"spark@192.0.2.10 "
        f"'rsync -avz --progress -e "
        f"'ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no' "
        f"{model['path']}/blobs/ "
        f"spark@{node}:/home/spark/.cache/huggingface/hub/{repo}/blobs/'"
    )
    
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"  ✓ {node}: synced")
    else:
        print(f"  ✗ {node}: sync failed")


def verify_model_loaded(node, model_id):
    """Verify model is loaded in vLLM on a node."""
    cmd = (
        f"ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null spark@{node} "
        f"'docker exec spark-192-168-1-202 curl -s http://localhost:8000/v1/models | "
        f"python3 -c \"import sys,json; print(json.load(sys.stdin)[\'data\'][0][\'id\'])\"'"
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return model_id in result.stdout


def download_model_from_hub(node, model_id):
    """Download model from HF Hub to a node (bypasses NAS)."""
    cmd = (
        f"ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null spark@{node} "
        f"'HF_HUB_OFFLINE=0 huggingface-cli download {model_id}'"
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.returncode == 0
