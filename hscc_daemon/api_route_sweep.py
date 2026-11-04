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
    3  the sweep COULD NOT RUN (no client source to derive routes from, or no
       renderer available). Distinct from 1 on purpose: "I could not look" is
       not "I looked and it was dead", and ``hscc verify`` maps the two to
       different rows — 1 is red, 3 is unverified (t_ef36fe71).

A 400 from a route that REQUIRES a parameter is expected and is NOT a failure —
the client supplies it; a bare sweep call does not.

Only GETs are swept. Mutating POSTs are listed as not covered rather than fired,
so the gap is explicit instead of silently missing.
"""

import json
import os
import re
import shutil
import subprocess
import sys

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

    if shutil.which("curl") is None:
        # The sweep speaks HTTP through curl. Without it NOTHING was observed,
        # so this is "could not run" (exit 3) — not a verdict on the API. A
        # bare subprocess.run would instead die with FileNotFoundError and hand
        # verify a traceback to interpret (t_ef36fe71).
        return _bail(3, "cannot sweep: `curl` not found on PATH (%s)"
                          % os.environ.get("PATH"))

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

    console.print(theme.make_panel(
        "api route sweep",
        "Sweeping %d literal GET routes the app calls" % len(rows)))

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
    if failures:
        console.print(theme.make_status_panel(
            "%d ROUTE(S) FAILED: %s\n"
            "A route the app calls that does not answer means that screen is "
            "dead." % (len(failures), ", ".join(theme.esc(f) for f in failures)),
            status="error", title="failures"))
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

    # ``--theme`` is only for the human palette; strip it before the --json
    # check so it can never leak into the machine path or argv handling.
    _, theme_name = _strip_theme(sys.argv[1:])

    if "--json" in sys.argv:
        print(json.dumps({"routes": rows, "failures": failures,
                          "not_swept_dynamic": dynamic}, indent=2))
        return 1 if failures else 0

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
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
