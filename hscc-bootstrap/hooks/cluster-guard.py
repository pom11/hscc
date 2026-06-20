#!/usr/bin/env python3
"""HSCC cluster-guard — Hermes shell hooks for cluster ops safety and auditability.

Registers 3 hooks via ~/.hermes/config.yaml:

    hooks:
      pre_tool_call:
        - matcher: hscc-cluster
          command: python3 ~/.hermes/hooks/cluster-guard.py
          timeout: 10
      post_tool_call:
        - matcher: hscc-cluster
          command: python3 ~/.hermes/hooks/cluster-guard.py
          timeout: 5
      on_session_start:
        - command: python3 ~/.hermes/hooks/cluster-guard.py
          timeout: 5

HOOK 1 — pre_tool_call:  Capacity gate for provision_model / restart_model.
    Queries live sparkrun status + serving.json. Denies + suggests /heal if all
    worker GPUs are busy.  stop_model always passes (it frees capacity).

HOOK 2 — post_tool_call:  Audit logger for every hscc-cluster tool invocation.
    Appends timestamp + tool + node + outcome to ~/.hermes/logs/cluster-ops-audit.log.

HOOK 3 — on_session_start:  Snapshot of serving.json + applied_template.json
    to ~/.hermes/state/session_snapshots/<sid>/ for reproducibility.

Fails OPEN on any parse / subprocess error — a bug here must never brick the
cluster toolset.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

SERVING_JSON = os.path.expanduser("~/.hscc/serving.json")
APPLIED_TEMPLATE_JSON = os.path.expanduser("~/.hscc/applied_template.json")
AUDIT_LOG = os.path.expanduser("~/.hermes/logs/cluster-ops-audit.log")
SNAPSHOT_ROOT = os.path.expanduser("~/.hermes/state/session_snapshots")

GATED_TOOLS = {"provision_model", "restart_model"}
SNAPSHOT_KEEP = 50


# ---------------------------------------------------------------------------
# Hook 1 — pre_tool_call: capacity gate
# ---------------------------------------------------------------------------

def _running_worker_ips():
    """Parse `sparkrun status` for IPs that have running containers.

    Skips the 'Idle hosts' section. Returns empty set on any failure."""
    try:
        r = subprocess.run(
            ["sparkrun", "status"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return set()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()

    ip_re = re.compile(
        r"\b(?:192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)\b"
    )
    running = set()
    in_idle = False
    for line in r.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Idle hosts"):
            in_idle = True
            continue
        if stripped.startswith("Job:"):
            in_idle = False
            continue
        if in_idle:
            continue
        for ip in ip_re.findall(stripped):
            running.add(ip)
    return running


def _worker_node_ips():
    """Extract the set of worker node IPs from serving.json."""
    try:
        with open(SERVING_JSON) as fh:
            data = json.load(fh)
        nodes = set()
        for unit in data.get("units", []):
            if unit.get("role") == "worker":
                for n in unit.get("nodes", []):
                    nodes.add(n)
        return nodes
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()


def _check_capacity():
    """(idle, total) — number of idle worker nodes and total configured.

    Returns (0, 0) on any failure so the gate stays open."""
    try:
        worker_ips = _worker_node_ips()
        total = len(worker_ips)
        if total == 0:
            return 0, 0
        running_all = _running_worker_ips()
        running_workers = running_all & worker_ips
        idle = total - len(running_workers)
        return idle, total
    except Exception:
        return 0, 0


def handle_pre_tool_call(payload):
    tool_name = payload.get("tool_name", "")
    toolset = payload.get("toolset", "")

    if toolset != "hscc-cluster":
        return

    # stop_model always allowed — it frees capacity.
    if tool_name == "stop_model":
        return

    if tool_name not in GATED_TOOLS:
        return

    idle, total = _check_capacity()
    if idle > 0:
        return

    # All GPUs busy — deny and suggest /heal.
    print(json.dumps({
        "decision": "block",
        "reason": (
            f"BLOCKED by cluster-guard: all {total} worker GPUs are busy "
            f"(0 idle). Cannot {tool_name} — no capacity available.\n"
            "Suggested action: run /heal (reap_orphans or restart a stale model) "
            "to free a node, then retry."
        ),
    }))


# ---------------------------------------------------------------------------
# Hook 2 — post_tool_call: audit logger
# ---------------------------------------------------------------------------

def handle_post_tool_call(payload):
    toolset = payload.get("toolset", "")
    if toolset != "hscc-cluster":
        return

    tool_name = payload.get("tool_name", "")
    result = payload.get("result", {})

    # Normalize result to dict
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            pass
    if not isinstance(result, dict):
        result = {}

    # Extract node from tool_input or result
    tool_input = payload.get("tool_input") or {}
    node = result.get("node") or tool_input.get("node") or "N/A"

    # Determine outcome
    if result.get("ok"):
        outcome = "success"
    elif result.get("refused"):
        outcome = "refused"
    elif result.get("preview"):
        outcome = "preview"
    elif result.get("error"):
        outcome = f"error: {result['error']}"
    else:
        outcome = "unknown"

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool_name,
        "node": str(node),
        "outcome": outcome,
        "executed": result.get("executed", False),
    }

    try:
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        with open(AUDIT_LOG, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # fail open


# ---------------------------------------------------------------------------
# Hook 3 — on_session_start: state snapshot
# ---------------------------------------------------------------------------

def _rotate_snapshots():
    """Auto-rotate session_snapshots: keep only the most recent SNAPSHOT_KEEP."""
    try:
        entries = sorted(
            os.listdir(SNAPSHOT_ROOT),
            key=lambda e: os.path.getmtime(os.path.join(SNAPSHOT_ROOT, e)),
            reverse=True,
        )
        for old in entries[SNAPSHOT_KEEP:]:
            old_path = os.path.join(SNAPSHOT_ROOT, old)
            if os.path.isdir(old_path):
                shutil.rmtree(old_path)
            else:
                os.remove(old_path)
    except OSError:
        pass


def handle_on_session_start(payload):
    sid = payload.get("session_id") or os.environ.get("SESSION_ID", "unknown")

    dest = os.path.join(SNAPSHOT_ROOT, sid)
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError:
        return

    for src_path in (SERVING_JSON, APPLIED_TEMPLATE_JSON):
        try:
            shutil.copy2(src_path, os.path.join(dest, os.path.basename(src_path)))
        except (FileNotFoundError, OSError):
            pass

    # Auto-rotate: keep only the most recent SNAPSHOT_KEEP snapshots
    _rotate_snapshots()


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

def main():
    raw = sys.stdin.read()
    if not raw.strip():
        return

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return

    if not isinstance(payload, dict):
        return

    hook_type = payload.get("hook_type", "pre_tool_call")

    try:
        if hook_type == "pre_tool_call":
            handle_pre_tool_call(payload)
        elif hook_type == "post_tool_call":
            handle_post_tool_call(payload)
        elif hook_type == "on_session_start":
            handle_on_session_start(payload)
    except Exception:
        # Fail open — never block the cluster toolset due to a hook bug
        pass


if __name__ == "__main__":
    main()