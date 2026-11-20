#!/usr/bin/env python3
"""Prove every endpoint the iOS app calls actually answers.

This is the check that found the dead chat pipeline on 2026-09-01: every
orchestrator profile pointed at a placeholder host, so `hermes chat` hung in
SYN_SENT and the model never saw the message. Nothing in the test suite noticed,
because the suite verifies code — not that the running system answers.

Usage:
    python3 scripts/api_route_sweep.py            # sweep the live API
    python3 scripts/api_route_sweep.py --json     # machine-readable

Exit codes:
    0  every swept route answered with parseable JSON
    1  a route the app calls did not answer (unreachable / 5xx / bad body)
    2  preconditions missing — no API host or token, i.e. the API is not up
    3  the sweep COULD NOT RUN TO THE END (no client source to derive routes
       from, no renderer available, or its own total budget ran out before
       every route was called). Distinct from 1 on purpose: "I could not look"
       is not "I looked and it was dead", and ``hscc verify`` maps the two to
       different rows — 1 is red, 3 is unverified (t_ef36fe71, budget
       extension t_f439c923). A budget-limited run still prints the full
       report: which routes answered, and which were never called.

A 400 from a route that REQUIRES a parameter is expected and is NOT a failure —
the client supplies it; a bare sweep call does not.

Only GETs are swept. Mutating POSTs are listed as not covered rather than fired,
so the gap is explicit instead of silently missing. The same rule covers a GET
whose handler IS the fleet smoke test: ``NOT_SWEPT`` below names it, and the
sweep reports it covered-but-not-exercised instead of firing it. A route that
legitimately needs longer than the blanket probe budget gets one of its own
(``HEAVY_ROUTES``) rather than a permanent false red.

Per-route caps are not a bound on the SWEEP: 34 routes at ``-m 30`` plus
``/v1/health`` at ``-m 120`` is ~19 minutes of worst case, and the parent that
runs this file — ``check_api_routes`` through ``verify._capture`` — allows 120 s
of wall clock in total. So the sweep also caps its OWN total (``SWEEP_BUDGET``),
stops probing when it is spent, and still prints the report, naming every route
it never reached instead of letting the parent kill it mid-run and throw the
whole thing away (t_f439c923).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlencode

# ── Standalone-import bootstrap (t_ef36fe71) ────────────────────────────────
# This file is run TWO ways: as a package module (``python -m``) and as a bare
# script path. The latter is what ``hscc verify`` does, and what the installed
# keg does: ``python3 <keg>/libexec/hscc/hscc_daemon/api_route_sweep.py``. A
# script gets ONLY its own directory on sys.path — ``hscc_daemon/`` — so the
# parent dir that actually CONTAINS the ``hscc_daemon`` package is missing and
# the import below dies with ``ModuleNotFoundError: No module named
# 'hscc_daemon'``. The traceback then exits 1, which verify used to read as
# "a route did not answer" — a working API reported dead (2026-11-04 keg 2.15.2).
# Sanctioned pattern: hscc_daemon/verify_chat_roundtrip.py does the same
# insert; here it must come BEFORE the package import to be worth anything.
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

try:
    from hscc_daemon import cli_theme as theme
except ImportError:  # pragma: no cover - standalone/deps-missing fallback
    # Two distinct causes land here: the bootstrap above could not find the
    # package (a layout we did not anticipate), or `rich` is absent from the
    # interpreter running us (Homebrew's bare python3 has no rich; the keg
    # venv does). Neither is a route failure, so do NOT crash — leave the
    # human renderer unavailable and let main() report exit 3. The machine
    # ``--json`` path never touches the theme, so it stays fully working.
    theme = None

REPO = _PKG_ROOT
CLIENT = os.path.join(REPO, "ios-app/Sources/HSCC/HSCCClient.swift")

# Routes whose handler requires query parameters the client always supplies. A
# bare call returns 400 by design, so the probe sends the client's own values
# and treats that as PASS, saying why.
#
# /v1/logs belongs here because `handle_logs` rejects an omitted `source`
# before it reads anything (400 bad_request, valid: daemon|api|worker), which
# made an unparameterised probe a guaranteed 400 on a healthy API. `limit=1`
# mirrors the app's bounded tail and keeps the probe's read to one line
# (t_0494b09e).
NEEDS_PARAM = {
    "/v1/sessions": {"profile": "hscc-orch"},
    "/v1/memory": {"profile": "hscc-orch"},
    "/v1/logs": {"source": "daemon", "limit": "1"},
}

# ── Probe budgets and never-fired routes (DATA, not a special-case `if`) ────
# The sweep proves routes ANSWER. It must not run the fleet smoke test.
#
# Blanket curl `-m` for every route HEAVY_ROUTES does not name.
PROBE_TIMEOUT = 30

# Routes whose handler legitimately needs longer than the blanket budget:
# route -> its own curl `-m`, in seconds.
#
# /v1/health runs one inline `hscc verify` when it has no cached snapshot to
# serve — measured 64.4 s on a healthy fleet (t_81713661) — so the blanket 30 s
# cap gave up mid-request and the row reported `000` for a route that was
# answering: a gate that false-flags a healthy API. With the health snapshot
# cache in place the cold path is the exception, so 120 s is a fallback for that
# first call rather than the normal one. Every other route keeps PROBE_TIMEOUT
# on purpose: a route that turns slow for a reason nobody predicted is a finding
# to look at, not something to quietly hand a longer leash.
HEAVY_ROUTES = {"/v1/health": 120}

# Routes the sweep must never fire, with the note that reports the gap.
#
# /v1/verify's handler IS `hscc_daemon.verify.run_all()` — the very check that
# shells out to this sweep. Probing it from inside `hscc verify` therefore starts
# a nested full verify per hit, each doing real chat round-trips against the
# operator's GPU units, and each of those sweeps probes /v1/verify again: a
# branching-factor-2 chain of fleet smoke tests behind one reachability check.
# Reachability is proven by the routes that answer cheaply; it is never proven
# by firing the most expensive operation in the system.
#
# A skipped route still gets a row — the same shape a POST-only route gets — so
# the gap stays visible in the report instead of the route silently vanishing.
NOT_SWEPT = {
    "/v1/verify": ("route exists; verify-triggered, "
                   "not swept from inside verify"),
}

# What a skipped row reports for `status`. No request was made, so no HTTP code
# may be claimed; deliberately not a number, so it can never be read as one.
NOT_SWEPT_STATUS = "skip"

# ── The budget that WRAPS the per-route caps (t_f439c923) ───────────────────
# PROBE_TIMEOUT / HEAVY_ROUTES bound ONE request. Nothing bounded the SWEEP.
# The client names 34 literal GETs this file calls: 33 × 30 s + 120 s is 1110 s
# of worst case, while the parent that runs it — ``check_api_routes`` through
# ``verify._capture`` — kills the subprocess after 120 s (its
# ``timeout = timeout or 120`` default). Python then kills the sweep mid-loop:
# no ``--json`` arrives, and the row can only read
# ``unverified: api route sweep did not produce a route report (exit -9)`` —
# every route that HAD answered is thrown away with it, and the operator cannot
# see which one was slow.
#
# So the sweep now bounds its own total wall clock and stops politely: it prints
# the report it has, names every route it never reached, and exits 3
# (unverified — nothing was observed about those routes, so neither a green nor
# a red is available).
#
# The arithmetic is one place, and it is DATA:
PARENT_BUDGET = 120        # verify._capture's outer subprocess timeout. Pinned
                           # against verify.py's own default by
                           # test_sweep_budget_fits_inside_the_parents_budget.
STARTUP_RESERVE = 40       # what must fit BEFORE the first probe: interpreter
                           # start + imports (`rich` rides in through
                           # cli_theme), api_base()'s `hscc api status` fallback
                           # (itself capped at 30 s), the Swift parse, printing
                           # the report.
SWEEP_BUDGET = int(os.environ.get(  # probes only; env knob per the
    "HSCC_SWEEP_BUDGET_SECONDS",   # HSCC_NAS_PROBE_TIMEOUT precedent
    PARENT_BUDGET - STARTUP_RESERVE))

# A probe with less than this left cannot produce anything but a timeout, so it
# is not started: the gap is named instead. Keeps `-m 0` (curl: no limit at all)
# unreachable.
MIN_PROBE_SECONDS = 1

# curl's exit code for "operation timed out" (`-m` fired). The sweep needs it
# apart from a connection failure: a route cut off at a budget the SWEEP chose
# has proved nothing, while a route cut off at its own cap has.
CURL_TIMED_OUT = 28

# What a route the budget never reached reports for `status`. Non-numeric for
# the same reason as NOT_SWEPT_STATUS: no request went out, so no HTTP code may
# be claimed.
NOT_PROBED_STATUS = "unprobed"


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


def _bail(code, reason):
    """Report a NON-ROUTE failure, print a classifiable reason, return the code.

    2 = the API is not up (no host / no token). 3 = the sweep itself could not
    run. Both print an explicit one-line reason instead of dying with a
    traceback: ``hscc verify`` reads that text, and a raw
    ``Traceback (most recent call last):`` in a verify row tells the operator
    nothing (t_ef36fe71).
    """
    if "--json" in sys.argv:
        print(json.dumps({"error": reason, "could_not_run": code == 3},
                         indent=2))
    else:
        print(reason)
    return code


def _gap_row(route, note):
    """A row for a route the sweep did NOT call, with the reason named.

    Same six keys in the same order as a probed row — the consumer (verify's
    ``api_routes`` row) must never special-case a route. ``ok`` is True because
    nothing here says the route is dead: an uneaten budget proves nothing about
    the route, and a FALSE RED is exactly what this file was rewritten to stop
    producing. The completeness guard is the exit code (3, not 0) and the
    ``not_probed`` list in the payload, not this flag.
    """
    return {"route": route, "status": NOT_PROBED_STATUS, "seconds": 0.0,
            "parses": True, "ok": True, "note": note}


def sweep():
    base, tok = api_base(), token()
    if not base or not tok:
        return _bail(2, "cannot sweep: no API host or token "
                        "(is `hscc api status` running?)")

    try:
        literal, dynamic = client_routes()
    except OSError as exc:
        # No client source to derive the route list from — an installed layout
        # without ios-app/. That is "nothing to check", NOT "a route is dead".
        return _bail(3, "cannot sweep: no iOS client source to derive routes "
                        "from (%s: %s)" % (CLIENT, exc.strerror or exc))

    gets = [r for r in literal if not r.endswith(("/stop", "/up", "/down"))]
    # A route in NOT_SWEPT is reported, never called, so it must not count as
    # coverage either: a client source whose only literal GET were /v1/verify
    # would otherwise "sweep" nothing and still print every route answering.
    probed = [r for r in gets if r not in NOT_SWEPT]
    if not probed:
        # The client source parsed but yielded no GET route to call. Reporting
        # "every route the app calls answers" over an empty list would be a
        # FALSE GREEN — the mirror image of the false red this card fixes (a
        # renamed or emptied client file would make the row pass forever).
        detail = ("" if not gets else
                  " (its only literal GETs are routes this sweep never fires: %s)"
                  % ", ".join(gets))
        return _bail(3, "cannot sweep: %s yielded no literal GET routes to call%s"
                          % (CLIENT, detail))

    if shutil.which("curl") is None:
        # The sweep speaks HTTP through curl. Without it NOTHING was observed,
        # so this is "could not run" (exit 3) — not a verdict on the API. A
        # bare subprocess.run would instead die with FileNotFoundError and hand
        # verify a traceback to interpret (t_ef36fe71).
        return _bail(3, "cannot sweep: `curl` not found on PATH (%s)"
                          % os.environ.get("PATH"))

    rows, failures = [], []
    # The wall clock starts HERE, not at import: whatever the sweep spent
    # getting this far (interpreter start, api_base()'s `hscc api status`
    # fallback, the Swift parse) is charged against STARTUP_RESERVE; only the
    # probes are charged against SWEEP_BUDGET.
    started = time.monotonic()
    for route in gets:
        if route in NOT_SWEPT:
            # Covered, not exercised — the report shape the sweep already uses
            # for a POST-only route, so the gap is a named row rather than a
            # route that quietly disappeared from the list. `ok` is True because
            # nothing here says the route is dead; the note says what was NOT
            # proven. No request went out, so `status` claims no HTTP code.
            rows.append({"route": route, "status": NOT_SWEPT_STATUS,
                         "seconds": 0.0, "parses": True, "ok": True,
                         "note": NOT_SWEPT[route]})
            continue
        # Budget check FIRST: a probe started with nothing left can only burn
        # wall clock and produce a timeout that says nothing about the route.
        left = SWEEP_BUDGET - (time.monotonic() - started)
        if left < MIN_PROBE_SECONDS:
            rows.append(_gap_row(
                route, "not probed: the sweep's %d s total budget ran out "
                       "before it" % SWEEP_BUDGET))
            continue
        # Never ask for more than the route's own cap, and never more than the
        # sweep has left. The second clause is what makes the total bounded.
        own_cap = HEAVY_ROUTES.get(route, PROBE_TIMEOUT)
        cap = max(MIN_PROBE_SECONDS, min(own_cap, int(left)))
        shrunk = cap < own_cap

        url = base + route
        params = NEEDS_PARAM.get(route)
        if params:
            url += "?" + urlencode(params)
        proc = subprocess.run(
            ["curl", "-s", "-m", str(cap),
             "-w", "\n%{http_code} %{time_total}",
             "-H", "Authorization: Bearer " + tok, url],
            capture_output=True, text=True)
        # A route cut off at a budget the SWEEP chose proved nothing: it may
        # have answered one second later. That is a named gap, never a dead
        # route. Cut off at its OWN cap (shrunk False) it is the finding it
        # always was, and the failure path below still fires.
        if shrunk and proc.returncode == CURL_TIMED_OUT:
            rows.append(_gap_row(
                route, "not probed: cut off at the sweep's %d s budget after "
                       "%d s of this route's %d s cap"
                       % (SWEEP_BUDGET, cap, own_cap)))
            continue
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
        elif params:
            note = "(needs ?%s — supplied)" % "&".join(
                "%s=%s" % (k, v) for k, v in params.items())
        if code == "200" and not parses:
            note = "BODY DID NOT PARSE"
        rows.append({"route": route, "status": code, "seconds": float(elapsed),
                     "parses": parses, "ok": ok, "note": note})
        if not ok:
            failures.append(route)

    return rows, failures, dynamic


def _strip_theme(args):
    """Remove ``--theme <name>`` / ``--theme=<name>`` tokens from an argv slice.

    ``--theme`` only selects the palette for the human view; it is not part of
    the machine ``--json`` contract. Mirrors ``hscc._strip_theme_arg`` so the
    standalone sweep honors the same flag as the rest of the CLI. Returns
    ``(cleaned, theme_name_or_None)``.
    """
    theme_name = None
    cleaned = []
    i = 0
    n = len(args)
    while i < n:
        a = args[i]
        if a == "--theme":
            if i + 1 < n:
                theme_name = args[i + 1]
                i += 2
                continue
            i += 1
            continue
        if a.startswith("--theme="):
            theme_name = a.split("=", 1)[1]
            i += 1
            continue
        cleaned.append(a)
        i += 1
    return cleaned, theme_name


def render_human(rows, failures, dynamic, theme_name=None):
    """Render the sweep's human view through a themed Rich console.

    ``theme_name`` selects the palette (dark | light) or None for auto-detect.
    On a NON-tty stdout (pipe/redirect) Rich degrades to PLAIN text with NO
    ANSI escape codes, so scripts capturing the human view still get clean
    text. Only fixed labels are colourised; route/status values stay plain so
    sweep-derived data can never inject Rich markup — every sweep-derived
    value below goes through ``theme.esc`` first (Rich would otherwise raise
    MarkupError on a route or note containing ``[/bold]``).
    """
    console = theme.make_console(theme_name) if theme is not None else None
    if console is None:  # pragma: no cover - guarded by main()'s exit-3 path
        print("cannot render: hscc_daemon.cli_theme unavailable")
        return

    # ``rows`` also carries the routes this sweep refuses to fire AND the ones
    # its budget never reached; the panel says what was actually called, so
    # neither can inflate the coverage count the operator reads at the top of
    # the table.
    gaps = [r["route"] for r in rows
            if r.get("status") == NOT_PROBED_STATUS]
    swept = sum(1 for r in rows
                if r.get("status") not in (NOT_SWEPT_STATUS, NOT_PROBED_STATUS))
    console.print(theme.make_panel(
        "api route sweep",
        "Sweeping %d literal GET routes the app calls" % swept))

    table = theme.make_table("api route sweep")
    table.add_column("result", no_wrap=True)
    table.add_column("http", justify="right", no_wrap=True)
    table.add_column("secs", justify="right", no_wrap=True)
    table.add_column("route")
    table.add_column("note")
    for r in rows:
        if r["status"] == "200":
            mark = "ok  "
            glyph = "ok"
        elif r["status"] == "405":
            mark = "post"
            glyph = "label"
        elif r["status"] == NOT_SWEPT_STATUS:
            # Not called, not failed: same neutral label a POST-only route gets.
            # Without this branch a skipped route would print as FAIL, which is
            # a false alarm of exactly the kind this file exists to prevent.
            mark = "skip"
            glyph = "label"
        elif r["status"] == NOT_PROBED_STATUS:
            # Same rule for a route the budget never reached: it was not called,
            # so it cannot be reported dead. The note column says why.
            mark = "gap"
            glyph = "label"
        else:
            mark = "FAIL"
            glyph = "error"
        table.add_row(f"[{glyph}]{mark}[/{glyph}]", theme.esc(r["status"]),
                      "%.1f" % r["seconds"], theme.esc(r["route"]),
                      theme.esc(r["note"]))
    console.print(table)

    if dynamic:
        console.print("")
        console.print(theme.make_panel(
            "interpolated routes (not swept)",
            "\n".join("  %s" % theme.esc(d) for d in dynamic) +
            "\n\nSwept GETs only cover literal paths; interpolated routes need "
            "a live id to exercise."))

    console.print("")
    console.print("Mutating POSTs are deliberately NOT fired by this sweep.")
    if swept != len(rows):
        console.print("Some rows above were not called either: `skip` rows are "
                      "routes that would run the fleet smoke test from inside "
                      "it, `gap` rows are routes the sweep's %d s budget never "
                      "reached — the note column says which." % SWEEP_BUDGET)
    if failures:
        console.print(theme.make_status_panel(
            "%d ROUTE(S) FAILED: %s\n"
            "A route the app calls that does not answer means that screen is "
            "dead." % (len(failures), ", ".join(theme.esc(f) for f in failures)),
            status="error", title="failures"))
        if gaps:
            # Named here as well as in the table: the operator skims the panel,
            # and a dead route must not hide the fact that the sweep also
            # stopped early.
            console.print(theme.make_status_panel(
                "%d route(s) were never called — the %d s budget ran out: %s"
                % (len(gaps), SWEEP_BUDGET,
                   ", ".join(theme.esc(g) for g in gaps)),
                status="warn", title="not probed"))
    elif gaps:
        # Deliberately NOT the "all routes answered" panel: nothing was proven
        # about these routes, so the sweep says so instead of going green.
        console.print(theme.make_status_panel(
            "NOT VERIFIED: the sweep stopped at its %d s total budget and %d "
            "route(s) were never called: %s\n"
            "Nothing is known about those routes — this is not a pass."
            % (SWEEP_BUDGET, len(gaps), ", ".join(theme.esc(g) for g in gaps)),
            status="warn", title="result"))
    else:
        console.print(theme.make_status_panel(
            "All swept routes answered with parseable JSON.",
            status="ok", title="result"))


def _renderer_available():
    """True when the themed human view can actually be built.

    Checks for the callable, not just that the import returned something: a
    stub/empty ``cli_theme`` would pass an ``is not None`` test and then die on
    the first ``make_console`` call with an AttributeError traceback — the very
    shape of failure this file must never produce again (t_ef36fe71).
    """
    return all(callable(getattr(theme, name, None)) for name in
               ("make_console", "make_panel", "make_table", "make_status_panel"))


def main():
    result = sweep()
    if isinstance(result, int):
        # sweep() bailed before sweeping anything: 2 (API not up) or 3 (could
        # not run). Already printed its reason; do not touch the renderers.
        return result
    rows, failures, dynamic = result
    # A NOT_PROBED_STATUS row is a route the sweep never called — its own wall
    # clock ran out. That is NOT a green sweep: "every route the app calls
    # answers" over a report with holes is precisely the false green this file
    # already refuses for an emptied client source. Nor is it a red: nothing was
    # observed about those routes. Exit 3 is the exit verify already maps to
    # UNVERIFIED, so the row says what is true and keeps the report.
    gaps = [r["route"] for r in rows if r.get("status") == NOT_PROBED_STATUS]
    # A dead route outranks an unprobed one: if something did fail, that is the
    # finding, and the report still names the routes never reached.
    verdict = 1 if failures else (3 if gaps else 0)

    # ``--theme`` is only for the human palette; strip it before the --json
    # check so it can never leak into the machine path or argv handling.
    _, theme_name = _strip_theme(sys.argv[1:])

    if "--json" in sys.argv:
        payload = {"routes": rows, "failures": failures,
                   "not_swept_dynamic": dynamic,
                   "not_probed": gaps,
                   "budget": {"sweep_seconds": SWEEP_BUDGET,
                              "parent_seconds": PARENT_BUDGET,
                              "exhausted": bool(gaps)}}
        if gaps:
            # verify's `_sweep_reason` reads exactly this pair on a 2/3 exit, so
            # the operator's row names the routes instead of printing `{`.
            payload["could_not_run"] = True
            payload["error"] = ("sweep stopped at its %d s total budget; %d "
                                "route(s) were never called: %s"
                                % (SWEEP_BUDGET, len(gaps), ", ".join(gaps)))
        print(json.dumps(payload, indent=2))
        return verdict

    # Machine path above is theme-free and always works. The human view is the
    # only thing that needs cli_theme/rich, so a missing renderer degrades the
    # HUMAN view to exit 3 ("could not run") instead of crashing it with a
    # traceback — verify must never see a crash it could mistake for a dead
    # route, and an operator piping the human view still gets a real answer.
    if not _renderer_available():
        print("cannot render: hscc_daemon.cli_theme unavailable for this "
              "interpreter (%s lacks the package or `rich`). Re-run with "
              "--json for the machine view." % sys.executable)
        return 3
    render_human(rows, failures, dynamic, theme_name)
    return verdict


if __name__ == "__main__":
    sys.exit(main())
