"""Smoke-test checks for a future `hscc verify` command.

Each check returns {\"name\": str, \"ok\": bool, \"detail\": str} and never raises.
"""

import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


def check_plugins(plugins_dir=None):
    """Check that the hscc-commands plugin dir exists and contains core commands.

    Looks for \"workers-up\", \"cluster-restart\", \"template\" strings in __init__.py.
    """
    if plugins_dir is None:
        plugins_dir = os.path.expanduser("~/.hermes/plugins")
    else:
        plugins_dir = os.path.expanduser(plugins_dir)

    plugin_path = os.path.join(plugins_dir, "hscc-commands")
    init_file = os.path.join(plugin_path, "__init__.py")

    required = ["workers-up", "cluster-restart", "template"]

    try:
        if not os.path.isdir(plugin_path):
            return {"name": "plugins", "ok": True, "detail": f"skipped: {plugin_path} not found"}
        if not os.path.isfile(init_file):
            return {"name": "plugins", "ok": True, "detail": f"skipped: {init_file} not found"}

        with open(init_file, "r") as f:
            source = f.read()

    except (OSError, IOError) as exc:
        return {"name": "plugins", "ok": True, "detail": f"skipped: {exc}"}

    missing = [s for s in required if s not in source]
    if not missing:
        return {"name": "plugins", "ok": True, "detail": "all core commands found"}
    return {"name": "plugins", "ok": False, "detail": f"missing: {', '.join(missing)}"}


def check_multiplex(gateway_state=None, config=None, profiles_dir=None):
    """Check multiplex configuration and profile coverage.

    Reads multiplex_profiles from config and served_profiles from gateway state.
    OK if multiplex is truthy, served_profiles non-empty, and covers all profile dirs.
    """
    if config is None:
        config = os.path.expanduser("~/.hermes/config.yaml")
    else:
        config = os.path.expanduser(config)

    if gateway_state is None:
        gateway_state = os.path.expanduser("~/.hermes/gateway_state.json")
    else:
        gateway_state = os.path.expanduser(gateway_state)

    if profiles_dir is None:
        profiles_dir = os.path.expanduser("~/.hermes/profiles")
    else:
        profiles_dir = os.path.expanduser(profiles_dir)

    # Load config
    try:
        if yaml is None:
            return {"name": "multiplex", "ok": True, "detail": "skipped: pyyaml not installed"}
        if not os.path.isfile(config):
            return {"name": "multiplex", "ok": True, "detail": "skipped: config not found"}
        with open(config, "r") as f:
            cfg = yaml.safe_load(f)
    except (OSError, IOError):
        return {"name": "multiplex", "ok": True, "detail": "skipped: cannot read config"}

    if cfg is None:
        return {"name": "multiplex", "ok": True, "detail": "skipped: empty config"}

    if not cfg.get("multiplex_profiles"):
        return {"name": "multiplex", "ok": True, "detail": "multiplex disabled"}

    # Load gateway state
    try:
        if not os.path.isfile(gateway_state):
            return {"name": "multiplex", "ok": True, "detail": "skipped: gateway state not found"}
        with open(gateway_state, "r") as f:
            gw = json.load(f)
    except (OSError, IOError, json.JSONDecodeError):
        return {"name": "multiplex", "ok": True, "detail": "skipped: cannot read gateway state"}

    served = gw.get("served_profiles", [])
    if not served:
        return {"name": "multiplex", "ok": False, "detail": "served_profiles is empty"}

    served_set = set(served)

    # Check profile dirs are covered
    try:
        if os.path.isdir(profiles_dir):
            subdirs = [
                entry.name
                for entry in os.scandir(profiles_dir)
                if entry.is_dir()
            ]
            missing = [d for d in subdirs if d not in served_set]
        else:
            missing = []
    except OSError:
        missing = []

    if missing:
        return {"name": "multiplex", "ok": False, "detail": f"profiles not served: {', '.join(missing)}"}

    return {"name": "multiplex", "ok": True, "detail": f"all {len(served)} profiles served"}


def check_daemon_streams(state_dir=None, max_age_s=None):
    """Check all daemon state files are healthy and recent.

    OK if every *.json has ok==True and last_check within max_age_s of now.
    """
    if state_dir is None:
        state_dir = os.path.expanduser("~/.hscc/state")
    else:
        state_dir = os.path.expanduser(state_dir)

    if max_age_s is None:
        max_age_s = 600

    try:
        if not os.path.isdir(state_dir):
            return {"name": "daemon_streams", "ok": True, "detail": "skipped: state dir not found"}
        entries = os.listdir(state_dir)
    except OSError as exc:
        return {"name": "daemon_streams", "ok": True, "detail": f"skipped: {exc}"}

    json_files = [e for e in entries if e.endswith(".json")]
    if not json_files:
        return {"name": "daemon_streams", "ok": True, "detail": "no state files found"}

    now = datetime.now(timezone.utc)
    issues = []

    for fn in sorted(json_files):
        filepath = os.path.join(state_dir, fn)
        try:
            with open(filepath, "r") as f:
                data = json.load(f)
        except (OSError, IOError, json.JSONDecodeError):
            issues.append(f"{fn}: unreadable")
            continue

        if not data.get("ok"):
            issues.append(f"{fn}: ok=False")

        # Check recency via last_check or timestamp
        ts_str = data.get("last_check") or data.get("timestamp")
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if now - ts > timedelta(seconds=max_age_s):
                    issues.append(f"{fn}: stale ({ts_str})")
            except (ValueError, TypeError):
                pass  # ignore bad timestamps

    if issues:
        return {"name": "daemon_streams", "ok": False, "detail": "; ".join(issues)}

    return {"name": "daemon_streams", "ok": True, "detail": f"all {len(json_files)} streams healthy"}


def check_proxy(url=None, timeout=None):
    """Check that the local proxy responds with a non-empty model list.

    GETs the URL; OK if HTTP 200 and body has a non-empty 'data' list.
    """
    if url is None:
        url = "http://localhost:4000/v1/models"
    if timeout is None:
        timeout = 4

    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return {"name": "proxy", "ok": False, "detail": f"HTTP {resp.status}"}
            body = json.loads(resp.read().decode())
            data = body.get("data", [])
            if not data:
                return {"name": "proxy", "ok": False, "detail": "no models in data list"}
            return {"name": "proxy", "ok": True, "detail": f"{len(data)} models available"}
    except urllib.error.HTTPError as exc:
        return {"name": "proxy", "ok": False, "detail": f"HTTP {exc.code}: {exc.reason}"}
    except urllib.error.URLError as exc:
        return {"name": "proxy", "ok": False, "detail": f"connection error: {exc.reason}"}
    except json.JSONDecodeError:
        return {"name": "proxy", "ok": False, "detail": "response is not valid JSON"}
    except Exception as exc:
        return {"name": "proxy", "ok": False, "detail": str(exc)}


def check_config_wiring(config=None):
    """Check config.yaml has required HSCC wiring.

    Requires: multiplex_profiles truthy, kanban.max_in_progress is int,
    toolsets contains 'hscc-cluster'.
    """
    if config is None:
        config = os.path.expanduser("~/.hermes/config.yaml")
    else:
        config = os.path.expanduser(config)

    try:
        if yaml is None:
            return {"name": "config_wiring", "ok": True, "detail": "skipped: pyyaml not installed"}
        if not os.path.isfile(config):
            return {"name": "config_wiring", "ok": True, "detail": "skipped: config not found"}
        with open(config, "r") as f:
            cfg = yaml.safe_load(f)
    except (OSError, IOError):
        return {"name": "config_wiring", "ok": True, "detail": "skipped: cannot read config"}

    if cfg is None:
        return {"name": "config_wiring", "ok": True, "detail": "skipped: empty config"}

    missing = []

    if not cfg.get("multiplex_profiles"):
        missing.append("multiplex_profiles")

    kanban = cfg.get("kanban", {})
    if not isinstance(kanban, dict) or not isinstance(kanban.get("max_in_progress"), int):
        missing.append("kanban.max_in_progress")

    # toolsets can be a list or a JSON-encoded string
    toolsets = cfg.get("toolsets", [])
    if isinstance(toolsets, str):
        try:
            toolsets = json.loads(toolsets)
        except (json.JSONDecodeError, TypeError):
            toolsets = []
    if isinstance(toolsets, list) and "hscc-cluster" not in toolsets:
        missing.append("toolsets: hscc-cluster")

    if missing:
        return {"name": "config_wiring", "ok": False, "detail": f"missing: {', '.join(missing)}"}

    return {"name": "config_wiring", "ok": True, "detail": "all wiring checks passed"}


def run_all(**overrides):
    """Run all checks, returning aggregated results.

    Accepts keyword overrides that match the parameter names of each check
    function. Returns {\"checks\": [...], \"ok\": bool}.
    """
    checks = [
        check_plugins,
        check_multiplex,
        check_daemon_streams,
        check_proxy,
        check_config_wiring,
    ]

    results = []
    for check_fn in checks:
        import inspect
        sig = inspect.signature(check_fn)
        params = set(sig.parameters.keys())
        kwargs = {k: v for k, v in overrides.items() if k in params}
        results.append(check_fn(**kwargs))

    return {
        "checks": results,
        "ok": all(r["ok"] for r in results),
    }
