#!/usr/bin/env python3
"""widget_datapath_live/run.py — prove the Cluster widget's data path end-to-end
against the LIVE HSCC API on macOS, using the REAL committed models + REAL
widget derivation functions extracted verbatim (so they cannot drift).

Why this exists (task t_b6a8c450): the widget/Live Activity "show but no data".
The App Group container was EMPTY -> APIConfig.load() nil -> widget renders
.unconfigured ("Set up the app to see the cluster"). App-Group sharing itself
is proven by the prior card t_d64ea494. What THIS check proves is the other
half: given a complete config, the widget's fetch+derive path turns the real
API responses into real rendered numbers (state, topology, model count, idle
to autodown, running/queue/blocked). It decodes the REAL API JSON and runs the
REAL derivation functions, so every number is reproduced from the live API,
not fabricated.

Usage: ios-app/scripts/widget_datapath_live/run.py
Requires HOST/port/token reachable (reads the same sources the app does) and
captured response files in --json-dir (default: the TMP capture dir).
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
WIDGET = os.path.join(REPO, "ios-app", "Sources", "HSCCWidgets", "ClusterWidget.swift")
TEMPLATE = os.path.join(HERE, "template.swift")


def extract_struct(src, name):
    """Return the `struct <name>:` block via proper brace counting (handles
    computed properties like `var id: String { name }` that contain `}`)."""
    lines = src.splitlines()
    out = []
    on = False
    depth = 0
    for ln in lines:
        if not on:
            if re.search(rf"struct {re.escape(name)}:", ln):
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


def extract_fn(src, name):
    """Return `private static func <name>` body verbatim, braces balanced,
    with the `static` keyword dropped and tightened to a top-level `func` (the
    harness calls them as free functions)."""
    lines = src.splitlines()
    out = []
    depth = 0
    started = False
    for ln in lines:
        if not started:
            if re.search(rf"private static func {re.escape(name)}\b", ln):
                ln = re.sub(r"private static func ", "func ", ln, count=1)
                started = True
                depth += ln.count("{") - ln.count("}")
                out.append(ln)
                continue
        else:
            depth += ln.count("{") - ln.count("}")
            out.append(ln)
            if depth == 0:
                break
    if not started:
        raise SystemExit(f"function {name} not found in {WIDGET}")
    return "\n".join(out)


def lit(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--json-dir", default=os.environ.get("WIDGET_JSON_DIR", ""))
    p.add_argument("--host", default=os.environ.get("WIDGET_LIVE_HOST", ""))
    p.add_argument("--port", default=os.environ.get("WIDGET_LIVE_PORT", ""))
    args = p.parse_args()

    src = open(SHARED).read()
    widget = open(WIDGET).read()

    structs = [
        "ClusterWorkload", "ClusterStatusResponse", "AutodownStatusResponse",
        "KanbanRunningLite", "RunningCardLite", "KanbanBlockedLite",
        "BlockedCardLite", "KanbanStaleLite", "StaleCardLite",
    ]
    models = "\n".join(extract_struct(src, s) for s in structs)
    models = models.replace("Decodable, Speakable", "Decodable")

    fns = "\n".join(
        extract_fn(widget, n).rstrip()
        for n in ["resolveState", "canonicalPairs", "idleRemaining", "queueDepth"]
    )

    # JSON injection args.
    json_dir = args.json_dir or os.environ.get("WIDGET_JSON_DIR", "")
    # If no captured response dir given (the default), fetch LIVE from the API
    # using the daemon's real token file (the same token the operator scans
    # into the app). Never print the token.
    if not json_dir or not os.path.isdir(json_dir):
        host = args.host or os.environ.get("WIDGET_LIVE_HOST", "")
        port = args.port or os.environ.get("WIDGET_LIVE_PORT", "8788")
        token_path = os.path.expanduser("~/.hscc/api-token")
        if not os.path.exists(token_path):
            print("no captured responses and no ~/.hscc/api-token to fetch live", file=sys.stderr)
            return 1
        token = open(token_path).read().strip()
        import urllib.request
        routes = {
            "autodown_status.json": "/v1/autodown/status",
            "cluster_status.json": "/v1/cluster/status",
            "kanban_running.json": "/v1/kanban/running",
            "kanban_blocked.json": "/v1/kanban/blocked",
            "kanban_stale.json": "/v1/kanban/stale?older_than=0",
        }
        tmpfetch = tempfile.mkdtemp(prefix="widget_live_fetch_")
        json_dir = tmpfetch
        for fname, path in routes.items():
            req = urllib.request.Request(
                f"http://{host}:{port}{path}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                with open(os.path.join(tmpfetch, fname), "wb") as f:
                    f.write(resp.read())
        print(f"fetched live from {host}:{port} (redacted)", file=sys.stderr)

    def load(name):
        with open(os.path.join(json_dir, name)) as f:
            return f.read()
    auto = load("autodown_status.json")
    cluster = load("cluster_status.json")
    running = load("kanban_running.json")
    blocked = load("kanban_blocked.json")
    stale = load("kanban_stale.json")

    tpl = open(TEMPLATE).read()
    tpl = tpl.replace("MODELS_MARKER", models)
    tpl = tpl.replace("FNS_MARKER", fns)

    tmp = tempfile.mkdtemp(prefix="widget_live_")
    sw = os.path.join(tmp, "check.swift")
    with open(sw, "w") as f:
        f.write(tpl)
    if os.environ.get("WIDGET_KEEP_TMP"):
        print("generated swift kept at: " + sw, file=sys.stderr)
    # Write the JSON responses to the tmp dir so the harness reads them from
    # disk (no string-escaping fragility).
    json_map = {
        "autodown_status.json": auto,
        "cluster_status.json": cluster,
        "kanban_running.json": running,
        "kanban_blocked.json": blocked,
        "kanban_stale.json": stale,
    }
    for name, body in json_map.items():
        with open(os.path.join(tmp, name), "w") as f:
            f.write(body)
    exe = os.path.join(tmp, "check")
    sdk = subprocess.run(
        ["xcrun", "--sdk", "macosx", "--show-sdk-path"],
        capture_output=True, text=True
    ).stdout.strip()
    if not sdk:
        print("no macOS SDK", file=sys.stderr)
        return 1
    c = subprocess.run(
        ["xcrun", "swiftc", "-sdk", sdk, "-o", exe, sw],
        capture_output=True, text=True
    )
    if c.returncode != 0:
        print("COMPILE FAILED:\n" + c.stdout + c.stderr[:4000])
        return 1
    r = subprocess.run([exe, tmp], capture_output=True, text=True)
    print(r.stdout)
    if r.stderr:
        print("STDERR:\n" + r.stderr[:2000])
    shutil.rmtree(tmp, ignore_errors=True)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
