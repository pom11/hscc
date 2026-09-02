#!/usr/bin/env python3
"""Measure per-endpoint latency of the HSCC read API against the live server.

Derives the base address dynamically (never hardcodes the IP — repo is public).
Runs each read endpoint N times and reports median / p95 / max / mean so the
ranking is stable against one-off stragglers (a cold/cached datapoint).

Read-only: only GETs, no mutations. The token header is masked (the repo's
AddressGuard rejects real secrets). Output goes to stdout as a ranked table.

Usage:
    python3 scripts/perf_measure.py [--passes 5] [--json]
"""
import json
import os
import re
import statistics
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(REPO, "ios-app/Sources/HSCC/HSCCClient.swift")

# Routes requiring a param the client always supplies (bare call -> 400).
NEEDS_PARAM = {"/v1/sessions": "profile", "/v1/memory": "profile"}


def api_base():
    cfg = os.path.expanduser("~/.hscc/api.json")
    host, port = None, 8788
    try:
        with open(cfg) as fh:
            d = json.load(fh)
        host, port = d.get("host"), d.get("port", 8788)
    except Exception:
        pass
    if not host:  # fall back to live-reported address, never a literal
        try:
            out = subprocess.run(["hscc", "api", "status"], capture_output=True,
                                 text=True, timeout=30).stdout
            m = re.search(r"Listening:\s+([0-9.]+):(\d+)", out)
            if m:
                host, port = m.group(1), int(m.group(2))
        except Exception:
            pass
    return ("http://%s:%s" % (host, port)) if host else None


def token():
    try:
        with open(os.path.expanduser("~/.hscc/api-token")) as fh:
            return fh.read().strip()
    except OSError:
        return None


def client_routes():
    src = open(CLIENT).read()
    literal = set()
    for m in re.finditer(r'"(/v1/[^"]*)"', src):
        p = m.group(1)
        if "\\(" not in p:
            literal.add(p)
    return sorted(literal)


def measure(url, tok, n):
    times = []
    status = None
    for _ in range(n):
        proc = subprocess.run(
            ["curl", "-s", "-m", "30", "-w", "\n%{http_code} %{time_total}",
             "-H", "Authorization: Bearer " + tok, url],
            capture_output=True, text=True)
        parts = proc.stdout.rsplit("\n", 1)
        body = parts[0] if len(parts) == 2 else ""
        try:
            code, elapsed = parts[-1].split()
        except ValueError:
            code, elapsed = "000", "0"
        status = code
        times.append(float(elapsed))
    return status, times


def main():
    passes = 5
    if "--passes" in sys.argv:
        passes = int(sys.argv[sys.argv.index("--passes") + 1])
    base, tok = api_base(), token()
    if not base or not tok:
        print("cannot measure: no API host or token")
        return 2

    routes = client_routes()
    rows = []
    for route in routes:
        url = base + route
        param = NEEDS_PARAM.get(route)
        if param:
            url += "?%s=hscc-orch" % param
        status, times = measure(url, tok, passes)
        if status == "405":  # POST-only route, skip
            continue
        med = statistics.median(times)
        rows.append({
            "route": route, "status": status, "n": len(times),
            "median": med, "p95": sorted(times)[int(0.95 * (len(times) - 1))],
            "max": max(times), "mean": statistics.mean(times),
        })

    rows.sort(key=lambda r: -r["median"])
    if "--json" in sys.argv:
        print(json.dumps(rows, indent=2))
        return 0
    print("Per-endpoint GET latency (median of %d passes) ranked slowest-first:\n"
          % passes)
    print("%-8s %6s %6s %6s %6s  %s" % ("status", "median", "p95", "max", "mean", "route"))
    for r in rows:
        print("%-8s %6.2fs %6.2fs %6.2fs %6.2fs  %s"
              % (r["status"], r["median"], r["p95"], r["max"], r["mean"], r["route"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
