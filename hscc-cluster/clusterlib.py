"""Shared helpers for hscc-cluster tools. Pure, live-truth, no persisted agent state.
Importable by the daemon for shared heal logic.

Topology (HEAD/NODES/NAS_HOST) is resolved lazily via the single discovery
module (WS2). There is NO fake-IP fallback: if the cluster can't be resolved,
accessing these names raises DiscoveryError rather than silently SSHing
documentation addresses."""
import json, os, subprocess

try:
    from . import discovery as _discovery  # package context (runtime)
    from . import sparkrun_bridge  # package context (runtime)
except ImportError:
    import discovery as _discovery  # direct import context (tests)
    import sparkrun_bridge  # direct import context (tests)

DiscoveryError = _discovery.DiscoveryError

CLUSTER_JSON = os.path.expanduser("~/.hscc/cluster.json")
SERVING_JSON = os.path.expanduser("~/.hscc/serving.json")
SPARKRUN = "sparkrun"


def _topology():
    """Resolve the live topology via the discovery module (raises on failure)."""
    return _discovery.discover()


def __getattr__(name):
    """Lazy module attributes: HEAD / NODES / NAS_HOST resolve from discovery on
    first access. Keeps import side-effect-free and never invents IPs."""
    if name == "HEAD":
        return _topology().orchestrator.ip
    if name == "NODES":
        return _topology().worker_ips
    if name == "NAS_HOST":
        t = _topology()
        return t.nas.ip if t.nas else None
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    """Run a remote command on a fleet node THROUGH sparkrun.

    sparkrun owns host resolution and the ssh kwargs (user, key, options,
    timeouts). We do not hand-roll ``ssh -o BatchMode=yes ...`` here — that
    drifts from sparkrun's configured transport and fails invisibly until
    production (t_17cdc637). The underlying bridge resolves sparkrun's own
    venv python and drives ``run_remote_script`` with sparkrun's ssh kwargs.
    Returns the same ``{ok, stdout, stderr, code}`` dict run_cmd produces.

    ``ssh_user`` on the Node/cluster is no longer threaded into the command:
    sparkrun owns the user. If the bridge is unreachable the call fails open
    to an unreachable result (never fabricates output).
    """
    return sparkrun_bridge.remote_cmd(host, command, timeout=timeout)


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
