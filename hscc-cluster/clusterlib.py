"""Shared helpers for hscc-cluster tools. Pure, live-truth, no persisted agent state.
Importable by the daemon for shared heal logic."""
import json, os, subprocess

SSH_USER = "spark"
CLUSTER_JSON = os.path.expanduser("~/.hscc/cluster.json")
SERVING_JSON = os.path.expanduser("~/.hscc/serving.json")
SPARKRUN = "sparkrun"

# Generic fallbacks (RFC-5737 documentation range). Real topology is resolved
# from ~/.hscc/cluster.json at import; these only apply when that file is
# absent/unreadable on a fresh machine.
_DEFAULT_HEAD = "192.0.2.10"
_DEFAULT_NODES = ["192.0.2.11", "192.0.2.12", "192.0.2.13"]
_DEFAULT_NAS = "192.0.2.20"


def _resolve_topology():
    """Resolve (head, nodes, nas) from ~/.hscc/cluster.json, else generic
    fallbacks. cluster.json shape: {"gateway": {"ip": ...}, "workers": [{"ip": ...}], "nasDevices": [{"ip": ...}]}."""
    try:
        with open(CLUSTER_JSON) as fh:
            d = json.load(fh)
        head = (d.get("gateway") or {}).get("ip") or _DEFAULT_HEAD
        nodes = [w.get("ip") for w in (d.get("workers") or []) if w.get("ip")] or list(_DEFAULT_NODES)
        nas_list = d.get("nasDevices") or d.get("nas_devices") or []
        nas = (nas_list[0].get("ip") if nas_list else None) or _DEFAULT_NAS
        return head, nodes, nas
    except (FileNotFoundError, json.JSONDecodeError, OSError, KeyError, AttributeError, IndexError):
        return _DEFAULT_HEAD, list(_DEFAULT_NODES), _DEFAULT_NAS


HEAD, NODES, NAS_HOST = _resolve_topology()


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
