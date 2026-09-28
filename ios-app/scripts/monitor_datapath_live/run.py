#!/usr/bin/env python3
"""monitor_datapath_live/run.py — prove the HSCC Monitor widget's data path
end-to-end against the LIVE HSCC API on macOS, using the REAL committed models
extracted verbatim (so they cannot drift).

Why this exists (task t_6d545375): the new Monitor widget fetches
GET /v1/cluster/monitor (per-node CPU/RAM/GPU) + GET /v1/daemon/host (the
machine HSCC runs on). This harness decodes the REAL live JSON with the REAL
committed decode structs (MonitorResponse / MonitorSample / DaemonHostResponse,
extracted verbatim from SharedModels.swift) and reproduces the numbers, so a
route/decode drift — or a missing optional field — is caught before the widget
ever renders.

The fetch is READ-ONLY (GET only, token read from ~/.hscc/api-token, never
printed). The API base is resolved at runtime from `hscc api status` (this repo
is public: a hardcoded host would leak the operator's tailnet address; nothing
here stores or prints a real address).

Usage: ios-app/scripts/monitor_datapath_live/run.py [--json-dir DIR]
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
SHARED = os.path.join(REPO, "ios-app", "Sources", "Shared", "SharedModels.swift")
TEMPLATE = os.path.join(HERE, "template.swift")

# The decode structs the Monitor widget needs (widget-local names per the
# `Lite` precedent — app's own ClusterMonitorResponse/NodeSample live in
# Sources/HSCC/Models.swift so they can't be shared).
STRUCTS = [
    "MonitorResponse", "MonitorPayload", "MonitorNode", "MonitorSample",
    "DaemonHostResponse", "DaemonHostCPU", "DaemonHostMemory", "DaemonHostDisk",
]


def extract_struct(src, name):
    """Return the `struct <name>:` block via proper brace counting (handles
    computed properties like `var id: String { host }` that contain `}`)."""
    lines = src.splitlines()
    out = []
    depth = 0
    on = False
    for ln in lines:
        if not on:
            if re.search(r"struct %s:" % re.escape(name), ln):
                on = True
                depth += ln.count("{") - ln.count("}")
                out.append(ln)
                if depth <= 0:
                    break
            continue
        depth += ln.count("{") - ln.count("}")
        out.append(ln)
        if depth <= 0:
            break
    return "\n".join(out).rstrip() + "\n"


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--json-dir", default=os.environ.get("MONITOR_JSON_DIR", ""))
    p.add_argument("--host", default="")
    p.add_argument("--port", default="8788")
    args = p.parse_args()

    src = open(SHARED).read()
    models = "\n".join(extract_struct(src, s) for s in STRUCTS)
    # The `Speakable` protocol is declared once in SharedModels (not part of the
    # extracted struct block) — conforming structs reference it, so strip the
    # conformance in the harness (the template doesn't define the protocol).
    models = models.replace("Codable, Speakable", "Codable")
    sdk = subprocess.run(
        ["xcrun", "--sdk", "macosx", "--show-sdk-path"],
        capture_output=True, text=True,
    ).stdout.strip()
    if not sdk:
        print("no macOS SDK", file=sys.stderr)
        return 1

    json_dir = args.json_dir or os.environ.get("MONITOR_JSON_DIR", "")
    tmp = tempfile.mkdtemp(prefix="monitor_datapath_")
    try:
        if not json_dir or not os.path.isdir(json_dir):
            # Fetch LIVE from the running API.
            host = args.host
            port = args.port
            if not host:
                status = subprocess.run(["hscc", "api", "status"],
                                        capture_output=True, text=True).stdout
                m = re.search(r"(\d+\.\d+\.\d+\.\d+):(\d+)", status)
                if not m:
                    print("cannot resolve API base from `hscc api status` — pass --host", file=sys.stderr)
                    return 1
                host, port = m.group(1), m.group(2)
            token_path = os.path.expanduser("~/.hscc/api-token")
            if not os.path.exists(token_path):
                print("no ~/.hscc/api-token to fetch live", file=sys.stderr)
                return 1
            token = open(token_path).read().strip()
            import urllib.request
            for name, path in [("monitor.json", "/v1/cluster/monitor"),
                               ("daemon_host.json", "/v1/daemon/host")]:
                req = urllib.request.Request(
                    "http://%s:%s%s" % (host, port, path),
                    headers={"Authorization": "Bearer " + token,
                             "Accept": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    with open(os.path.join(tmp, name), "wb") as f:
                        f.write(resp.read())
            # The resolved host is the operator's live tailnet address — never
            # echo it. Redact everything to the documented placeholder.
            print("fetched live (host redacted to 100.64.0.1)", file=sys.stderr)
            host = "100.64.0.1"
        else:
            # Use captured responses; no host involvement.
            for name in ["monitor.json", "daemon_host.json"]:
                shutil.copy(os.path.join(json_dir, name), os.path.join(tmp, name))

        tpl = open(TEMPLATE).read().replace("MODELS_MARKER", models.rstrip())
        sw = os.path.join(tmp, "check.swift")
        with open(sw, "w") as f:
            f.write(tpl)
        exe = os.path.join(tmp, "check")
        c = subprocess.run(["xcrun", "swiftc", "-sdk", sdk, "-o", exe, sw],
                           capture_output=True, text=True)
        if c.returncode != 0:
            print("COMPILE FAILED:\n" + c.stdout + c.stderr[:4000])
            return 1
        r = subprocess.run([exe, tmp], capture_output=True, text=True)
        print(r.stdout)
        if r.stderr:
            print("STDERR:\n" + r.stderr[:2000])
        return r.returncode
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
