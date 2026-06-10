"""Cluster incident-response logic for the operator slash commands.

Self-contained on purpose: these handlers run in the gateway process and must
work even when the orchestrator LLM is wedged, so they shell out to ``sparkrun``
directly and read ``~/.hscc/serving.json`` for topology — no cross-plugin import,
no LLM round-trip.

serving.json is the authoritative source of what should be running where: each
unit carries its own ``role`` (orchestrator|worker), ``recipe``, and ``nodes``.
"""
import json
import os
import subprocess

SERVING_JSON = os.path.expanduser("~/.hscc/serving.json")
SPARKRUN = "sparkrun"
CLUSTER = "hscc"
PORT = 8000
HEALTH_TIMEOUT = 6
RUN_TIMEOUT = 240


def read_units():
    """Units from serving.json, or [] when missing/corrupt."""
    try:
        with open(SERVING_JSON) as fh:
            data = json.load(fh)
        return data.get("units", []) if isinstance(data, dict) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def unit_node(unit):
    nodes = unit.get("nodes") or []
    return nodes[0] if nodes else None


def orchestrator_unit(units):
    for u in units:
        if u.get("role") == "orchestrator" and unit_node(u):
            return u
    return None


def worker_units(units):
    return [u for u in units if u.get("role") == "worker" and unit_node(u)]


def _run(args, timeout=RUN_TIMEOUT):
    """Run a subprocess, never raise — return (ok, stdout, stderr)."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return False, "", f"command not found: {args[0]}"
    except subprocess.TimeoutExpired:
        return False, "", f"timed out after {timeout}s"
    except OSError as e:
        return False, "", str(e)


def _curl_model(node):
    """Return the served model id on a node, or None if unreachable/empty."""
    ok, out, _ = _run(
        ["ssh", "-o", "ConnectTimeout=6", f"spark@{node}",
         f"curl -s --max-time {HEALTH_TIMEOUT} http://localhost:{PORT}/v1/models"],
        timeout=20,
    )
    if not ok or not out:
        return None
    try:
        return json.loads(out)["data"][0]["id"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None


def restart_one(unit):
    """Hard stop+run a unit's model on its node. Returns a result dict."""
    node = unit_node(unit)
    recipe = unit.get("recipe")
    label = unit.get("id") or unit.get("role") or node
    if not node or not recipe:
        return {"unit": label, "ok": False, "error": "missing node or recipe"}
    recipe = os.path.expanduser(recipe)
    _run([SPARKRUN, "stop", "--all", "--hosts", node], timeout=60)
    ok, _out, err = _run(
        [SPARKRUN, "run", recipe, "--cluster", CLUSTER, "--hosts", node,
         "--port", str(PORT), "--no-follow", "--ensure"],
        timeout=RUN_TIMEOUT,
    )
    return {"unit": label, "node": node, "ok": ok,
            "error": None if ok else (err or "sparkrun run failed")[:200]}
