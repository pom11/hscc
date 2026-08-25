"""Utility functions for the HSCC daemon."""

import json
import os
import subprocess
import socket
import urllib.request
import urllib.error


def run_cmd(args, timeout=30, as_json=False, shell=False):
    """Run a command and return structured output.

    ``output`` is stdout on success; on a non-zero exit it folds stdout AND
    stderr together so the error text is never lost (tools like sparkrun write
    their errors to stderr — autodown's failure reason was empty for exactly
    this reason). ``as_json`` parses stdout as JSON instead.
    """
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, shell=shell
        )
        if as_json and result.stdout.strip():
            try:
                return {"ok": True, "output": json.loads(result.stdout.strip())}
            except json.JSONDecodeError:
                return {"ok": False, "output": result.stdout.strip()}
        return {"ok": result.returncode == 0, "output": result.stdout.strip() if result.returncode == 0 else (result.stdout + "\n" + result.stderr).strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": f"Command timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "output": f"Command failed: {e}"}


def ssh_cmd(host, command, timeout=20):
    """Run a command on a remote host via SSH."""
    cmd = f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout={timeout} {host} \"{command}\""
    return run_cmd(cmd, timeout=timeout + 5, shell=True)


def http_check(url, timeout=5):
    """Check if an HTTP endpoint is reachable."""
    try:
        req = urllib.request.urlopen(url, timeout=timeout)
        return {"ok": True, "status": req.status}
    except urllib.error.URLError as e:
        return {"ok": False, "output": str(e.reason)}
    except Exception as e:
        return {"ok": False, "output": str(e)}


def ensure_dir(path):
    """Ensure a directory exists, creating it if needed."""
    os.makedirs(path, exist_ok=True)


def read_json_file(path, default=None):
    """Read a JSON file with a default fallback."""
    if not os.path.exists(path):
        return default if default is not None else {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return default if default is not None else {}


def write_json_file(path, data):
    """Write a JSON file atomically."""
    ensure_dir(os.path.dirname(path) or ".")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=4)
    os.replace(tmp, path)
