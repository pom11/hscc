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
(per-node CPU/RAM/GPU + daemon-host machine data, section-aware) — and since
t_1685f884, the cluster metrics the surfaces draw (host table, serving totals,
glance metrics) through the real `MonitorSurfaceReading` rules.

It compiles the REAL source — the driver's decode structs + MonitorContent
derivation from MonitorActivityDriver.swift, the attributes from
Shared/MonitorActivity.swift, and the three Foundation-only Shared files
(ClusterNodeTally, ClusterMonitorViewModel, MonitorActivityMetrics) injected
WHOLE rather than block by block, so the rules under test are the shipped ones
and not a hand-picked subset that goes stale the next time the rules reach for
one more helper. It fetches LIVE JSON, decodes it as the SHARED
`MonitorResponse` (the type the driver decodes this route as), and prints what
each surface would draw.

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


def strip_import(src):
    """Drop the file's own `import Foundation` (the harness has exactly one
    import block) so a WHOLE source file can be injected verbatim."""
    return src.replace("import Foundation\n", "", 1)


WHOLE_FILES = [
    # (path, why the whole file rather than an extracted block)
    (("ios-app", "Sources", "Shared", "ClusterNodeTally.swift"),
     "ServingSummary + FleetFormat + the tally: `MonitorContent.make` and the "
     "surface rules below both call into it"),
    (("ios-app", "Sources", "Shared", "ClusterMonitorViewModel.swift"),
     "the CLI-identical formatters the pushed cells come from, and "
     "ClusterMetricLevel — the verdicts the surfaces colour themselves from"),
    (("ios-app", "Sources", "Shared", "MonitorActivityMetrics.swift"),
     "the t_1685f884 surface rules themselves: what goes on the Lock Screen, "
     "the compact, the expanded and the minimal, and nothing else"),
]


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
    shared_models_src = open(os.path.join(REPO, "ios-app", "Sources", "Shared", "SharedModels.swift")).read()
    serving_row = extract_block(shared_models_src, r"struct ServingRow:")
    # The SHARED monitor decode + its nested payload: the driver decodes the
    # route as `MonitorResponse` (t_1685f884) — the same type the widget fetches
    # and the type `ClusterMonitorViewModel` projects the CLI's tables from — so
    # this harness has to decode the same shape the app does, not the app's
    # parallel `ClusterMonitorResponse`.
    monitor_response = extract_block(shared_models_src, r"struct MonitorResponse:")
    monitor_payload = extract_block(shared_models_src, r"struct MonitorPayload:")
    monitor_node = extract_block(shared_models_src, r"struct MonitorNode:")
    monitor_sample = extract_block(shared_models_src, r"struct MonitorSample:")
    monitor_workload = extract_block(shared_models_src, r"struct MonitorWorkload:")
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

    # WHOLE files, not extracted blocks, for the three Foundation-only ones the
    # surface rules need: the alternative is this harness guessing which five of
    # their declarations the rules happen to call THIS week, and going red (or
    # worse, silently measuring less) every time that set changes.
    whole = "\n\n".join(
        "// ---- %s ----\n%s" % (os.path.join(*parts),
                                 strip_import(open(os.path.join(REPO, *parts)).read()))
        for parts, _reason in WHOLE_FILES)

    models = "\n".join([
        attrs, node_metric, host_metric,
        serving_row, monitor_response, monitor_payload,
        monitor_node, monitor_sample, monitor_workload,
        cluster_mon, cluster_payload, cluster_host, node_sample,
        daemon_host, daemon_cpu, daemon_mem, daemon_disk,
        monitor_content,
        whole,
    ])
    # The app's `Speakable` protocol isn't part of the harness; strip the
    # conformance (mirrors widget_datapath_live/run.py).
    models = models.replace("Decodable, Speakable", "Decodable")
    models = models.replace("Codable, Speakable", "Codable")
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
    # `sys.exit`, not a bare call: `main()` returns the harness's exit code and
    # `monitor_activity_check.sh` ends with this command, so calling main and
    # dropping its result made every FAIL print exit 0 — a gate that reports a
    # failure and cannot fail the build. Found by tampering with the very rule
    # this file checks (§7) and watching the FAIL line come back with rc 0.
    sys.exit(main())
