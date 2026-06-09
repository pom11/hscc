"""Shared helpers for hscc-cluster tools. Pure, live-truth, no persisted agent state.
Importable by the daemon for shared heal logic."""
import json, os, subprocess

NODES = ["192.0.2.11", "192.0.2.12", "192.0.2.13"]
HEAD = "192.0.2.10"
NAS_HOST = "192.0.2.20"
SSH_USER = "spark"
SERVING_JSON = os.path.expanduser("~/.hscc/serving.json")
SPARKRUN = "sparkrun"


def run_cmd(args, timeout=30):
    """Run a local command; return dict(ok, stdout, stderr, code)."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {"ok": r.returncode == 0, "stdout": r.stdout,
                "stderr": r.stderr, "code": r.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": "timeout", "code": 124}
    except FileNotFoundError as e:
        return {"ok": False, "stdout": "", "stderr": str(e), "code": 127}


def ssh_cmd(host, command, timeout=30):
    return run_cmd(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                    f"{SSH_USER}@{host}", command], timeout=timeout)


def read_serving_units():
    """Read serving.json units. Tolerate missing/corrupt -> []."""
    try:
        with open(SERVING_JSON) as fh:
            data = json.load(fh)
        return data.get("units", []) if isinstance(data, dict) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def confirm_gate(confirm, action):
    """Guard for mutating tools. If not confirm -> return preview dict (caller
    returns it, does NOT mutate). If confirm -> return None (proceed)."""
    if confirm:
        return None
    return {"preview": True, "executed": False,
            "would_do": action,
            "note": "Re-call with confirm=true to execute."}
