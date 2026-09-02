#!/usr/bin/env python3
"""Prove every endpoint the iOS app calls actually answers.

This is the check that found the dead chat pipeline on 2026-09-01: every
orchestrator profile pointed at a placeholder host, so `hermes chat` hung in
SYN_SENT and the model never saw the message. Nothing in the test suite noticed,
because the suite verifies code — not that the running system answers.

Usage:
    python3 scripts/api_route_sweep.py            # sweep the live API
    python3 scripts/api_route_sweep.py --json     # machine-readable

Exit code is non-zero if any route is unreachable, 5xx, or returns a body that
does not parse. A 400 from a route that REQUIRES a parameter is expected and is
NOT a failure — the client supplies it; a bare sweep call does not.

Only GETs are swept. Mutating POSTs are listed as not covered rather than fired,
so the gap is explicit instead of silently missing.
"""

import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(REPO, "ios-app/Sources/HSCC/HSCCClient.swift")

# Routes whose handler requires a parameter the client always supplies. A bare
# call returns 400 by design, so treat that as PASS and say why.
NEEDS_PARAM = {
    "/v1/sessions": "profile",
    "/v1/memory": "profile",
}


def api_base():
    cfg = os.path.expanduser("~/.hscc/api.json")
    host, port = None, 8788
    try:
        with open(cfg) as fh:
            d = json.load(fh)
        host, port = d.get("host"), d.get("port", 8788)
    except Exception:
        pass
    if not host:
        # Fall back to whatever the running API reports rather than guessing a
        # literal — a hardcoded host here is how the chat outage hid for days.
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
    """Every /v1 path the Swift client references, literal and interpolated."""
    src = open(CLIENT).read()
    literal, dynamic = set(), set()
    for m in re.finditer(r'"(/v1/[^"]*)"', src):
        p = m.group(1)
        (dynamic if "\\(" in p else literal).add(p)
    return sorted(literal), sorted(dynamic)


def sweep():
    base, tok = api_base(), token()
    if not base or not tok:
        print("cannot sweep: no API host or token (is `hscc api status` running?)")
        return 2

    literal, dynamic = client_routes()
    gets = [r for r in literal if not r.endswith(("/stop", "/up", "/down"))]

    rows, failures = [], []
    for route in gets:
        url = base + route
        param = NEEDS_PARAM.get(route)
        if param:
            url += "?%s=hscc-orch" % param
        proc = subprocess.run(
            ["curl", "-s", "-m", "30", "-w", "\n%{http_code} %{time_total}",
             "-H", "Authorization: Bearer " + tok, url],
            capture_output=True, text=True)
        parts = proc.stdout.rsplit("\n", 1)
        body = parts[0] if len(parts) == 2 else ""
        code, elapsed = (parts[-1].split() + ["000", "0"])[:2]

        parses = True
        if code == "200":
            try:
                json.loads(body)
            except Exception:
                parses = False

        # 405 means the route IS registered but is POST-only. That is a PASS
        # for reachability — which is what this sweep proves — and firing the
        # POST would mutate live state. Treat it as covered-but-not-exercised
        # rather than a failure, and say so, so the gap stays visible.
        post_only = code == "405"
        ok = (code == "200" and parses) or post_only
        note = ""
        if post_only:
            note = "route exists; POST-only, not exercised"
        elif param:
            note = "(needs ?%s= — supplied)" % param
        if code == "200" and not parses:
            note = "BODY DID NOT PARSE"
        rows.append({"route": route, "status": code, "seconds": float(elapsed),
                     "parses": parses, "ok": ok, "note": note})
        if not ok:
            failures.append(route)

    return rows, failures, dynamic


def main():
    result = sweep()
    if result == 2:
        return 2
    rows, failures, dynamic = result

    if "--json" in sys.argv:
        print(json.dumps({"routes": rows, "failures": failures,
                          "not_swept_dynamic": dynamic}, indent=2))
    else:
        print("Sweeping %d literal GET routes the app calls\n" % len(rows))
        for r in rows:
            mark = ("ok  " if r["status"] == "200" else
                    "post" if r["status"] == "405" else "FAIL")
            print("  %s %s %5.1fs  %-34s %s"
                  % (mark, r["status"], r["seconds"], r["route"], r["note"]))
        print("\n%d interpolated route(s) not swept (need a live id):" % len(dynamic))
        for d in dynamic:
            print("   ", d)
        print("\nMutating POSTs are deliberately NOT fired by this sweep.")
        if failures:
            print("\n%d ROUTE(S) FAILED: %s" % (len(failures), ", ".join(failures)))
            print("A route the app calls that does not answer means that screen is dead.")
        else:
            print("\nAll swept routes answered with parseable JSON.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
