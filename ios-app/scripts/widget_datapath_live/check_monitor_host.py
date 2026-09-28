#!/usr/bin/env python3
"""Probe the LIVE /v1/cluster/monitor + /v1/daemon/host shapes for the monitor
widget card (t_6d545375). Tries localhost + the configured host; never prints
the token. SCRUBS real host to the 100.64.0.1 placeholder in output."""
import json
import os
import sys
import urllib.request

token_path = os.path.expanduser("~/.hscc/api-token")
token = open(token_path).read().strip() if os.path.exists(token_path) else ""
if not token:
    print("no api-token file", file=sys.stderr); sys.exit(1)

candidates = ["127.0.0.1", "localhost", os.environ.get("WIDGET_LIVE_HOST", "")]
port = os.environ.get("WIDGET_LIVE_PORT", "8788")

def scrub(text, used_host):
    return text.replace(used_host, "100.64.0.1")

def try_reach(host):
    if not host:
        return None
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/v1/daemon/host",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.read().decode()
    except Exception as e:
        print(f"  {host}:{port} -> {e}", file=sys.stderr)
        return None

used_host = None
for host in candidates:
    body = try_reach(host)
    if body is not None:
        used_host = host
        print(f"REACHED via {host}:{port}", file=sys.stderr)
        break
if used_host is None:
    print("could not reach API on any candidate host", file=sys.stderr); sys.exit(1)

for name, path in [
    ("daemon_host", "/v1/daemon/host"),
    ("monitor", "/v1/cluster/monitor"),
]:
    req = urllib.request.Request(
        f"http://{used_host}:{port}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            body = resp.read().decode()
    except Exception as e:
        print(f"{name}: ERROR {e}", file=sys.stderr)
        continue
    try:
        pretty = json.dumps(json.loads(body), indent=2)
    except Exception:
        pretty = body
    print(f"===== {name} {path} =====")
    print(scrub(pretty, used_host)[:5000])
    print()
