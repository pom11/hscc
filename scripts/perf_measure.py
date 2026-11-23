#!/usr/bin/env python3
"""Measure per-endpoint latency of the HSCC read API against the live server.

Derives the base address dynamically (never hardcodes the IP — repo is public).
Runs each read endpoint N times and reports median / p95 / max / mean so the
ranking is stable against one-off stragglers (a cold/cached datapoint).

Read-only: only GETs, no mutations. The token header is masked (the repo's
AddressGuard rejects real secrets). Output goes to stdout as a ranked table.

WHAT THIS SCRIPT DOES NOT TIME
------------------------------
Route discovery and every probe rule are imported from
``hscc_daemon/api_route_sweep.py``, not copied. This file used to keep its own
parse-and-probe loop and none of the sweep's fixes reached it (t_b0c302b8); the
tables below are the single derivation both tools now share.

  * ``NOT_SWEPT`` — routes that are not read paths. ``/v1/verify``'s handler IS
    ``hscc_daemon.verify.run_all()``: the fleet smoke test, doing real chat
    round-trips against the operator's GPU units. It gets a ``skip`` row naming
    the gap and never a timing. One default ``--passes 5`` run used to fire it
    five times, cut each off at the curl cap, and rank the result as if it were
    an endpoint latency — while the verify behind it ran on to completion.
  * ``MUTATION_SUFFIXES`` — the client's POST-only /stop /up /down family,
    excluded up front instead of fired at and dropped after a 405.
  * ``HEAVY_ROUTES`` / ``PROBE_TIMEOUT`` — a route whose handler legitimately
    needs longer than the blanket budget gets one of its own. ``/v1/health``
    runs an inline verify on a cold start (measured 64.4 s), which ``-m 30`` cut
    off; the table then ranked a route that was answering as a fast one.
  * ``NEEDS_PARAM`` — the query the client always supplies, so a route that
    requires a parameter is timed on the URL the app actually uses.

Usage:
    python3 scripts/perf_measure.py [--passes 5] [--json]

Exit codes:
    0  a latency table was produced
    2  preconditions missing — no API host or token, i.e. the API is not up
    3  the script COULD NOT RUN: the rules could not be imported, there was no
       client source to derive routes from, no curl, or nothing left to time
       after the never-fired rules were applied. Distinct from 0 on purpose —
       an empty table printed green would read as "every endpoint is fast".
       Any non-zero exit reports ``{"error": ..., "could_not_run": true|false}``
       on ``--json`` rather than a list, so a failed run can never parse as zero
       routes — the convention ``hscc_daemon/api_route_sweep.py`` already uses.
"""
import json
import os
import shutil
import statistics
import subprocess
import sys
from urllib.parse import urlencode

# ── Standalone-import bootstrap ──────────────────────────────────────────────
# This script is invoked by path (`python3 scripts/perf_measure.py`), and a bare
# script gets ONLY its own directory — `scripts/` — on sys.path. The directory
# that CONTAINS the `hscc_daemon` package is the parent, so it has to be added
# before the import below or the script dies with ModuleNotFoundError. Same
# sanctioned pattern as the sweep's own bootstrap (hscc_daemon/api_route_sweep.py,
# t_ef36fe71): it must come BEFORE the import to be worth anything.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

IMPORT_ERROR = None
try:
    # The module, for the callables — late-bound, so a test can substitute one.
    from hscc_daemon import api_route_sweep as sweep
    # The tables and the route parser, imported BY NAME and never re-declared
    # here: a name bound this way IS the sweep's object, so the drift guard in
    # scripts/tests/test_perf_measure_routes.py can assert identity and go red
    # the moment someone re-pins a copy in this file.
    from hscc_daemon.api_route_sweep import (
        HEAVY_ROUTES, MUTATION_SUFFIXES, NEEDS_PARAM, NOT_SWEPT,
        NOT_SWEPT_STATUS, PROBE_TIMEOUT, client_routes)
except ImportError as exc:  # pragma: no cover - depends on the interpreter
    # Two causes: an interpreter that cannot import the package (a bare python3
    # without the deps), or a layout without hscc_daemon/ at all. Neither is a
    # measurement, so main() reports exit 3 in words — never a traceback, and
    # never an empty table that reads as "everything is fast".
    sweep = None
    HEAVY_ROUTES = MUTATION_SUFFIXES = NEEDS_PARAM = NOT_SWEPT = None
    NOT_SWEPT_STATUS = PROBE_TIMEOUT = client_routes = None
    IMPORT_ERROR = "%s: %s" % (type(exc).__name__, exc)

# What this script says about a route it refuses to fire because its handler
# MUTATES. Which routes those are comes from MUTATION_SUFFIXES (shared); only
# the wording is local, because it describes a *latency* gap.
MUTATION_NOTE = "route exists; POST-only mutation, not fired by a latency probe"

# The same reason for a 405 the suffix rule did not predict: the route is real,
# it is just not a read path. Its 405 latency says nothing about the screen the
# phone waits on, so no timings are claimed for it.
POST_ONLY_NOTE = "route exists; POST-only, not measured"


def _bail(code, reason):
    """Report a NON-MEASUREMENT failure in words and return the exit code.

    Mirrors the sweep's `_bail` (t_ef36fe71): "I could not look" is not "I
    looked and it was slow", and a raw traceback on stderr tells the operator
    nothing. On ``--json`` the failure is an object with `could_not_run`, so a
    consumer can never read a failed run as an empty — i.e. perfectly fast —
    route table.
    """
    if "--json" in sys.argv:
        print(json.dumps({"error": reason, "could_not_run": code == 3}, indent=2))
    else:
        print(reason)
    return code


def route_plan(literal_routes):
    """Split the client's literal routes into (to_time, skipped).

    `skipped` is ``[(route, why)]``. Every route this script refuses to time
    stays in the output as a row carrying its reason, so the gap is visible
    instead of the route quietly disappearing from the table — the rule the
    sweep adopted in t_109bcc97, applied at last to the script that had no
    filter of any kind.
    """
    to_time, skipped = [], []
    for route in literal_routes:
        if route in NOT_SWEPT:
            skipped.append((route, NOT_SWEPT[route]))
        elif route.endswith(MUTATION_SUFFIXES):
            skipped.append((route, MUTATION_NOTE))
        else:
            to_time.append(route)
    return to_time, skipped


def probe_url(route):
    """The URL to time for one route, plus the params that made it.

    The client always supplies what NEEDS_PARAM lists; a bare path would be
    timed against a 400 (a different code path) instead of the screen the phone
    actually loads.
    """
    params = NEEDS_PARAM.get(route)
    url = route + "?" + urlencode(params) if params else route
    return url, params


def _row(route, status, times, note=""):
    """One table row: the same keys whether the route was timed or skipped.

    Keys and their order are the published shape — `docs/audits/phone-performance.md`
    quotes a run, and any consumer reads route/status/median/p95 — with `note`
    appended, so existing readers keep working untouched. `note` is already the
    sweep's vehicle for a gap, and one row shape for every route means a
    consumer never has to special-case the routes that were not timed.

    With no samples every statistic is None, never 0.0. A route that was never
    called has NO latency, and `0.00s` in a latency table is a fabricated
    datapoint — the old /v1/verify row reported the fleet smoke test's wall time
    as if it were an endpoint's; this must not fabricate the other direction.
    """
    if times:
        srt = sorted(times)
        stats = {"n": len(times), "median": statistics.median(times),
                 "p95": srt[int(0.95 * (len(times) - 1))],
                 "max": max(times), "mean": statistics.mean(times)}
    else:
        stats = {"n": 0, "median": None, "p95": None, "max": None, "mean": None}
    return {"route": route, "status": status, **stats, "note": note}


def measure(url, tok, n, timeout):
    """Time `url` `n` times. `timeout` is this route's own curl `-m` budget.

    The cap only ever cuts the MEASUREMENT short: whatever the server started
    behind a request that exceeds it runs on to completion. That is precisely
    why a route that runs the fleet smoke test must never be probed at all
    (NOT_SWEPT) rather than probed with a short cap.
    """
    times = []
    status = None
    for _ in range(n):
        proc = subprocess.run(
            ["curl", "-s", "-m", str(timeout), "-w", "\n%{http_code} %{time_total}",
             "-H", "Authorization: Bearer " + tok, url],
            capture_output=True, text=True)
        parts = proc.stdout.rsplit("\n", 1)
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
    if IMPORT_ERROR:
        return _bail(3, "cannot measure: the probe rules live in "
                        "hscc_daemon/api_route_sweep.py and importing them failed "
                        "(%s) — re-run with an interpreter that can import the "
                        "package (this one: %s)" % (IMPORT_ERROR, sys.executable))

    base, tok = sweep.api_base(), sweep.token()
    if not base or not tok:
        # Same shape as every other failure: a reason in words, and on --json an
        # object saying so. A consumer must never read "the API is not up" as an
        # empty list of endpoints that all answered instantly.
        return _bail(2, "cannot measure: no API host or token")

    try:
        literal, _dynamic = client_routes()
    except OSError as exc:
        # No client source to derive the route list from means nothing was
        # measured — not "every endpoint answered fast".
        return _bail(3, "cannot measure: no iOS client source to derive routes "
                        "from (%s: %s)" % (sweep.CLIENT, exc.strerror or exc))

    to_time, skipped = route_plan(literal)
    if not to_time:
        # The false-green mirror: a client file that is empty, renamed, or made
        # only of routes these rules refuse would otherwise print a green, empty
        # "everything is fast" table forever.
        detail = ("" if not literal else
                  " (its only literal routes are ones this script never fires: %s)"
                  % ", ".join(sorted(literal)))
        return _bail(3, "cannot measure: %s yielded no literal GET routes to "
                        "time%s" % (sweep.CLIENT, detail))
    if shutil.which("curl") is None:
        # The script speaks HTTP through curl and nothing else. Without it
        # NOTHING was observed, so this is "could not run", not a verdict.
        return _bail(3, "cannot measure: `curl` not found on PATH (%s)"
                          % os.environ.get("PATH"))

    # The skipped routes are rows from the start, not an afterthought tacked on
    # after the loop: they are part of what this run did NOT measure, and the
    # table is the place a reader looks.
    rows = [_row(route, NOT_SWEPT_STATUS, [], why) for route, why in skipped]

    for route in to_time:
        url, params = probe_url(route)
        status, times = measure(base + url, tok, passes,
                                HEAVY_ROUTES.get(route, PROBE_TIMEOUT))
        if status == "405":
            # A 405 the suffix rule did not predict: covered, not exercised.
            # Reported rather than `continue`d in silence, with no timings.
            rows.append(_row(route, status, [], POST_ONLY_NOTE))
            continue
        note = ""
        if params:
            note = "(needs ?%s — supplied)" % "&".join(
                "%s=%s" % (k, v) for k, v in params.items())
        rows.append(_row(route, status, times, note))

    # Slowest first, as before; rows with no samples sort last so a `skip` can
    # never sit at the top of a latency ranking looking like a measurement, and
    # ties stay alphabetical so the table is reproducible.
    rows.sort(key=lambda r: (r["median"] is None, -(r["median"] or 0.0),
                             r["route"]))

    if "--json" in sys.argv:
        print(json.dumps(rows, indent=2))
        return 0

    timed = sum(1 for r in rows if r["median"] is not None)
    print("Per-endpoint GET latency (median of %d passes) ranked slowest-first: "
          "%d of %d routes timed\n" % (passes, timed, len(rows)))
    print("%-8s %6s %6s %6s %6s  %s" % ("status", "median", "p95", "max", "mean",
                                        "route"))
    for r in rows:
        if r["median"] is None:
            # Blank, not 0.00s: there is no latency for a route that was never
            # called, and a number here would be the same lie in miniature that
            # this script no longer tells.
            cols, fmt = ("-",) * 4, "%-8s %6s %6s %6s %6s  %s"
        else:
            cols = (r["median"], r["p95"], r["max"], r["mean"])
            fmt = "%-8s %6.2fs %6.2fs %6.2fs %6.2fs  %s"
        line = fmt % ((r["status"],) + cols + (r["route"],))
        if r["note"]:
            line += "   <- " + r["note"]
        print(line)

    if timed != len(rows):
        # Say it in words as well as in the table: a reader who skims past the
        # rows still learns that some routes the app calls were deliberately not
        # measured, and where the rule lives.
        print("\n%d route(s) above were NOT measured (blank timings): /v1/verify "
              "runs the fleet smoke test (hscc_daemon.verify.run_all) instead of "
              "answering a read, and POST-only routes would mutate live state. "
              "The rules are data in hscc_daemon/api_route_sweep.py — NOT_SWEPT "
              "and MUTATION_SUFFIXES — not a copy kept here."
              % (len(rows) - timed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
