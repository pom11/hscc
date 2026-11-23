"""Shared helpers for hscc-cluster tools. Pure, live-truth, no persisted agent state.
Importable by the daemon for shared heal logic.

Topology (HEAD/NODES/NAS_HOST) is resolved lazily via the single discovery
module (WS2). There is NO fake-IP fallback: if the cluster can't be resolved,
accessing these names raises DiscoveryError rather than silently SSHing
documentation addresses."""
import json, os, subprocess, time

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


#: ``state`` values for :func:`serving_snapshot`.
SERVING_OK = "ok"
SERVING_ABSENT = "absent"
SERVING_UNPARSEABLE = "unparseable"
SERVING_UNREADABLE = "unreadable"


def serving_snapshot(path=None):
    """Read serving.json ONCE and report its content AND its file state.

    :func:`read_serving_units` is the tolerant read: missing or corrupt answers
    ``[]``. That is the right answer for a read-only tool and a dangerous one for
    a reaper — "the file said nothing" and "the fleet serves nothing" collapse to
    the same value, and the second one stops containers (F13,
    docs/audits/2026-11-23-agent-surface.md §4/§9). Anything that mutates on the
    strength of serving.json needs to tell those apart, so it gets this.

    Returns ``{"path", "state", "units", "age_s"}``:

      * ``state`` — ``"ok"`` (parsed a JSON object), ``"absent"`` (no file),
        ``"unreadable"`` (OSError: permissions, unreadable home, dangling
        symlink) or ``"unparseable"`` (not JSON, or JSON that is not an object).
      * ``units`` — the declared unit list; ``[]`` whenever ``state`` is not
        ``"ok"``, so a caller can never pair a non-``"ok"`` state with units it
        would then trust.
      * ``age_s`` — seconds since the file's mtime, or ``None`` when it could not
        be stat'ed. Age is REPORTED and never enforced here: serving.json is
        written by bootstrap's ``serving_gen``, ``template apply`` and
        ``onboard.py`` — operator-triggered events, not a heartbeat — so there is
        no age at which it is legitimately stale. Age is context for the verdict,
        not a threshold.

    ONE ``open()`` for the whole read, so a verdict and the node set it judges
    cannot come from two different versions of a file that has >=3 writers.
    ``path`` defaults to the module-level :data:`SERVING_JSON` read at CALL time
    (not a default-arg bound at definition), so tests that monkeypatch the
    constant take effect. Never raises.
    """
    if path is None:
        path = SERVING_JSON
    snap = {"path": str(path), "state": SERVING_OK, "units": [], "age_s": None}
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        snap["state"] = SERVING_ABSENT
        return snap
    except json.JSONDecodeError:
        snap["state"] = SERVING_UNPARSEABLE
        return snap
    except OSError:
        snap["state"] = SERVING_UNREADABLE
        return snap
    if not isinstance(data, dict):
        snap["state"] = SERVING_UNPARSEABLE
        return snap
    units = data.get("units", [])
    snap["units"] = units if isinstance(units, list) else []
    try:
        snap["age_s"] = max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        snap["age_s"] = None
    return snap


def confirm_gate(confirm, action):
    """Guard for mutating tools. If not confirm -> return preview dict (caller
    returns it, does NOT mutate). If confirm -> return None (proceed)."""
    if confirm:
        return None
    return {"preview": True, "executed": False,
            "would_do": action,
            "note": "Re-call with confirm=true to execute."}
