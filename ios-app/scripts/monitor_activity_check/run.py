#!/usr/bin/env python3
"""monitor_activity_check/run.py — prove the HSCC Monitor Live Activity's data
path against the LIVE HSCC API, using the REAL committed models + REAL
derivation functions extracted verbatim (so they cannot drift).

Why this exists (task t_03d318cd): Live Activities are PUSH-ONLY — the app
polls the API and pushes updates; nothing runs on the physical device from
this host (no device attached; sim render is blocked by a revoked cert, per
t_b6a8c450). What this check proves is the DATA half honestly: given the real
/cluster/monitor + /daemon/host responses, the real decoders + the real
`MonitorContent.make` produce the exact ContentState the extension would render
(per-node CPU/RAM/GPU + daemon-host machine data, section-aware).

It compiles the REAL source (SharedModels-free — the driver's decode structs +
MonitorContent derivation from MonitorActivityDriver.swift, and the
MonitorActivityAttributes/ContentState from Shared/MonitorActivity.swift),
fetches LIVE JSON from the API, decodes it, and prints the derived ContentState.

Usage: ios-app/scripts/monitor_activity_check/run.py
Requires the daemon's token file (~/.hscc/api-token) + a reachable API
(reads the same live host/port it does — host resolved via WIDGET_LIVE_HOST,
default the first all-N N tailnet IP of this host, never printed).
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
DRIVER = os.path.join(REPO, "ios-app", "Sources", "HSCC", "MonitorActivityDriver.swift")
SHARED = os.path.join(REPO, "ios-app", "Sources", "Shared", "MonitorActivity.swift")
TEMPLATE = os.path.join(HERE, "template.swift")


def extract_block(src, pattern):
    """Return the first top-level declaration matching a brace-balanced regex
    (struct/enum) or `func` header, verbatim."""
    lines = src.splitlines()
    out = []
    depth = 0
    started = False
    for ln in lines:
        if not started:
            if re.search(pattern, ln):
                started = True
                depth += ln.count("{") - ln.count("}")
                out.append(ln)
                if depth <= 0:
                    break
            continue
        depth += ln.count("{") - ln.count("}")
        out.append(ln)
        if depth <= 0:
            break
    if not started:
        raise SystemExit("block not found: %s" % pattern)
    return "\n".join(out).rstrip() + "\n"


def lit(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=os.environ.get("WIDGET_LIVE_HOST", ""))
    p.add_argument("--port", default=os.environ.get("WIDGET_LIVE_PORT", "8788"))
    p.add_argument("--json-dir", default=os.environ.get("MONITOR_JSON_DIR", ""))
    p.add_argument("--keep-tmp", action="store_true")
    args = p.parse_args()

    driver = open(DRIVER).read()
    shared = open(SHARED).read()

    # The shared attributes + content state (compiled into app + extension).
    attrs = extract_block(shared, r"struct MonitorActivityAttributes:")
    node_metric = extract_block(shared, r"struct NodeMetric:")
    host_metric = extract_block(shared, r"struct HostMetric:")

    # From the driver: the NEW daemon-host decode structs + the MonitorContent
    # derivation (verbatim). MonitorContent.make references ClusterMonitorResponse
    # which lives in Models.swift — we extract the minimal monitor decode lines
    # from that too.
    models_src = open(os.path.join(REPO, "ios-app", "Sources", "HSCC", "Models.swift")).read()
    cluster_mon = extract_block(models_src, r"struct ClusterMonitorResponse:")
    cluster_payload = extract_block(models_src, r"struct ClusterMonitorPayload:")
    cluster_host = extract_block(models_src, r"struct ClusterMonitorHost:")
    node_sample = extract_block(models_src, r"struct NodeSample:")

    daemon_host = extract_block(driver, r"struct DaemonHostResponse:")
    daemon_cpu = extract_block(driver, r"struct DaemonCpu:")
    daemon_mem = extract_block(driver, r"struct DaemonMem:")
    daemon_disk = extract_block(driver, r"struct DaemonDisk:")
    monitor_content = extract_block(driver, r"enum MonitorContent")

    # MonitorContent internally references `shortLabel` which is defined INSIDE
    # the enum — so extracting the enum gets it too. But the enum also references
    # the activity's ContentState/NodeMetric/HostMetric (from shared) which we
    # have. The driver's decode structs use `Decodable`; the enum uses
    # `MonitorActivityAttributes.ContentState` — all present. Good.

    models = "\n".join([
        attrs, node_metric, host_metric,
        cluster_mon, cluster_payload, cluster_host, node_sample,
        daemon_host, daemon_cpu, daemon_mem, daemon_disk,
        monitor_content,
    ])
    # The app's `Speakable` protocol isn't part of the harness; strip the
    # conformance (mirrors widget_datapath_live/run.py).
    models = models.replace("Decodable, Speakable", "Decodable")
    # `ActivityAttributes` is unavailable on macOS (the harness compiles for the
    # macOS SDK). The harness only needs the ContentState/NodeMetric/HostMetric
    # types that MonitorContent.make returns — the protocol conformance is
    # irrelevant to a headless decode+derive check. Drop the conformance so the
    # struct type still exists but no longer requires the iOS-only protocol.
    models = models.replace("struct MonitorActivityAttributes: ActivityAttributes {",
                            "struct MonitorActivityAttributes {")

    # The harness stub: fetch live JSON, decode, run MonitorContent.make.
    tpl = open(TEMPLATE).read()
    tpl = tpl.replace("MODELS_MARKER", models)

    tmp = tempfile.mkdtemp(prefix="monitor_activity_")
    sw = os.path.join(tmp, "check.swift")
    with open(sw, "w") as f:
        f.write(tpl)
    if args.keep_tmp:
        print("kept swift at " + sw, file=sys.stderr)

    # Fetch LIVE JSON (or read captured files from --json-dir).
    if args.json_dir and os.path.isdir(args.json_dir):
        def load(name):
            with open(os.path.join(args.json_dir, name)) as f:
                return f.read()
        monitor_json = load("cluster_monitor.json")
        daemon_json = load("daemon_host.json")
    else:
        host = args.host or os.environ.get("WIDGET_LIVE_HOST", "")
        port = args.port or os.environ.get("WIDGET_LIVE_PORT", "8788")
        token = open(os.path.expanduser("~/.hscc/api-token")).read().strip()
        def fetch(path):
            req = urllib.request.Request(
                "http://%s:%s%s" % (host, port, path),
                headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8")
        monitor_json = fetch("/v1/cluster/monitor")
        daemon_json = fetch("/v1/daemon/host")
        print("fetched live (redacted)", file=sys.stderr)

    # Write JSON to the tmp dir so the harness reads them from disk.
    for name, body in [("cluster_monitor.json", monitor_json),
                       ("daemon_host.json", daemon_json)]:
        with open(os.path.join(tmp, name), "w") as f:
            f.write(body)

    exe = os.path.join(tmp, "check")
    sdk = subprocess.run(["xcrun", "--sdk", "macosx", "--show-sdk-path"],
                         capture_output=True, text=True).stdout.strip()
    if not sdk:
        print("no macOS SDK", file=sys.stderr)
        return 1
    c = subprocess.run(["xcrun", "swiftc", "-sdk", sdk, "-o", exe, sw],
                       capture_output=True, text=True)
    if c.returncode != 0:
        print("COMPILE FAILED:\n" + c.stdout + c.stderr[:4000])
        return 1
    r = subprocess.run([exe, tmp], capture_output=True, text=True)
    print(r.stdout)
    if r.stderr:
        print("STDERR:\n" + r.stderr[:2000])
    if not args.keep_tmp:
        # keep — inner harness deletes its own scratch; keep this for review
        pass
    return r.returncode


if __name__ == "__main__":
    main()
