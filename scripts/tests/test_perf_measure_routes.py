"""``scripts/perf_measure.py`` must time read endpoints, not run the fleet.

What one default ``python3 scripts/perf_measure.py --passes 5`` used to do,
verbatim in intent::

    for route in client_routes():          # EVERY literal /v1/... in the Swift client
        status, times = measure(url, tok, 5)   # curl -m 30, five times
        if status == "405":
            continue                        # POST-only: fired, then dropped

The client file names ``/v1/verify``, whose handler IS
``hscc_daemon.verify.run_all()``. Five passes therefore meant five full fleet
smoke tests with real chat round-trips against the operator's GPU units, and the
30 s curl cap only cut the *measurement* off while the verify behind it ran on to
completion — which the table then ranked as if it were an endpoint latency.
``/v1/health`` was hit the same way on a cold start, and the mutation routes were
fired at and quietly dropped. (t_b0c302b8)

PR #485 fixed exactly this on the sweep side. This file pins that the script now
converged onto the SAME derivation instead of a second copy of the rules, and
that the convergence is observable from the outside:

1. NEVER FIRE THE SMOKE TEST. ``/v1/verify`` is absent from the argv the fake
   curl is handed, and present in the report as a `skip` row naming the gap.
2. A BUDGET THE COLD START CAN FINISH INSIDE. ``/v1/health`` gets ``-m 120``;
   every other route keeps ``-m 30``.
3. ONE DERIVATION, NOT TWO COPIES. The tables the script uses are the *same
   objects* as the sweep's, and the source carries no re-declaration of them.
4. THE PUBLISHED SHAPE SURVIVES. Every row keeps the keys a consumer reads
   (route/status/median/p95), and a route that was never called carries no
   numbers at all rather than a fabricated 0.00s.

Everything runs against a fake project tree in ``tmp_path``, a closed loopback
port and a fake ``curl`` on PATH — the harness
``hscc_daemon/tests/test_api_route_sweep_standalone.py`` uses. No socket is ever
opened, the live API is never contacted, ``hscc verify`` is never run, and the
operator's ``~/.hscc`` is not read.
"""

import json
import os
import re
import shutil
import statistics
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PERF_SRC = os.path.join(REPO, "scripts", "perf_measure.py")
SWEEP_SRC = os.path.join(REPO, "hscc_daemon", "api_route_sweep.py")

# A closed loopback port: connecting to it fails instantly, so the script runs
# its real per-route path (curl + parse + classify) and reaches nothing.
DEAD_HOST, DEAD_PORT = "127.0.0.1", 1

# The published ``--json`` row shape: every key a consumer reads, in order,
# with `note` appended (docs/audits/phone-performance.md quotes a run).
ROW_KEYS = ["route", "status", "n", "median", "p95", "max", "mean", "note"]

# Routes chosen so each rule is observable on its own: one never-fired smoke
# test route, one heavy route, one mutation route, one that needs the sweep's
# params, a plain one that must inherit none of it, and an interpolated route
# that was never a literal to begin with.
_SWIFT = '''\
import Foundation
func health()  { get("/v1/health") }
func ping()    { get("/v1/ping") }
func verify()  { get("/v1/verify") }
func logs()    { get("/v1/logs") }
func sessions(){ get("/v1/sessions") }
func stop()    { post("/v1/cluster/stop") }
func one(p: String) { get("/v1/sessions/\\(p)") }
'''


def _fake_project(tmp_path, swift, *, api_config=True):
    """Lay out a fake repo checkout and return (root, perf_script).

    Copies the REAL ``perf_measure.py`` and the REAL sweep module into a tree
    under ``tmp_path``, beside a fake Swift client. That does two jobs at once:
    the script runs the way an operator runs it — a bare script path, from a
    directory that is not this repo, bootstrapping its own ``sys.path`` — and
    every route it derives comes from the planted client, never from the real
    ``ios-app/`` checkout.
    """
    root = tmp_path / "proj"
    pkg = root / "hscc_daemon"
    pkg.mkdir(parents=True)
    shutil.copyfile(SWEEP_SRC, pkg / "api_route_sweep.py")
    (pkg / "__init__.py").write_text("")
    scripts = root / "scripts"
    scripts.mkdir()
    shutil.copyfile(PERF_SRC, scripts / "perf_measure.py")
    src = root / "ios-app" / "Sources" / "HSCC"
    src.mkdir(parents=True)
    (src / "HSCCClient.swift").write_text(swift)

    home = tmp_path / "fakehome"
    (home / ".hscc").mkdir(parents=True)
    if api_config:
        (home / ".hscc" / "api.json").write_text(
            json.dumps({"host": DEAD_HOST, "port": DEAD_PORT}))
        (home / ".hscc" / "api-token").write_text("probe-token\n")
    return root, str(scripts / "perf_measure.py")


# A stand-in for curl that records the WHOLE argv it is handed and answers with
# a parseable body. Args are joined with \x1f and calls with \x1e: the script's
# own `-w` argument contains a literal newline, which no line-based format
# survives, and neither control character can appear in a curl argument.
#
# The reported time is a function of the URL plus the call number for it, so it
# is deterministic in either direction: median, p95, max and mean come out four
# different numbers for one route (a row that silently swapped them cannot pass
# the shape test), and different routes get different medians (so the
# slowest-first ordering is a real ordering, not a tie).
_CURL_STUB = '''#!@@PY@@
import json, os, sys

argv = sys.argv[1:]
url = argv[-1]
capture = @@CAPTURE@@
seen = capture + ".counts"
counts = {}
if os.path.exists(seen):
    with open(seen) as fh:
        counts = json.load(fh)
counts[url] = i = counts.get(url, 0) + 1
with open(seen, "w") as fh:
    json.dump(counts, fh)
code = "405" if any(m in url for m in @@POSTONLY@@) else "200"
with open(capture, "a") as fh:
    fh.write("\\x1f".join(argv) + "\\x1e")
sys.stdout.write('{"ok": true}\\n%s %.2f' % (code, 0.10 * (len(url) % 5) + 0.01 * i))
'''


def _fake_curl(tmp_path, post_only=()):
    """Plant an argv-logging curl on PATH; return (capture_path, PATH value).

    The script speaks HTTP through curl and nothing else, so this is the whole
    seam: nothing between it and a socket.
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    capture = tmp_path / "curl-argv.txt"
    curl = bindir / "curl"
    curl.write_text(
        _CURL_STUB.replace("@@PY@@", sys.executable)
        .replace("@@CAPTURE@@", repr(str(capture)))
        .replace("@@POSTONLY@@", repr(list(post_only))))
    curl.chmod(0o755)
    return capture, str(bindir) + os.pathsep + "/usr/bin" + os.pathsep + "/bin"


def _run(script, argv, tmp_path, path=None, cwd=None):
    """Run the script as a BARE SCRIPT PATH — the operator's invocation."""
    return subprocess.run(
        [sys.executable, script] + argv,
        cwd=str(cwd or tmp_path), capture_output=True, text=True, timeout=180,
        env=dict(os.environ, HOME=str(tmp_path / "fakehome"),
                 # Default PATH has the fake curl but NO `hscc`: api_base()'s
                 # fallback would otherwise shell out to the operator's live
                 # `hscc api status` and measure the real fleet.
                 PATH=path or (str(tmp_path / "emptybin")
                               + os.pathsep + "/usr/bin" + os.pathsep + "/bin")))


def _measured(tmp_path, swift=_SWIFT, argv=("--json",), post_only=()):
    """Run the script against the fake tree + logging curl.

    Returns (completed, argv_calls, json_rows_or_None). ``argv_calls`` is what
    curl was actually handed — the only evidence that counts, because "the
    report says skip" and "no request went out" are different claims.
    """
    root, script = _fake_project(tmp_path, swift)
    capture, path = _fake_curl(tmp_path, post_only)
    cp = _run(script, list(argv), tmp_path, path=path)
    calls = [c.split("\x1f") for c in capture.read_text().split("\x1e") if c] \
        if capture.exists() else []
    rows = None
    if cp.returncode == 0 and "--json" in argv:
        rows = json.loads(cp.stdout)
    return cp, calls, rows


@pytest.fixture(autouse=True)
def _no_live_api(monkeypatch, tmp_path):
    """Scrub HOME + PATH so no path in the script can reach real state."""
    (tmp_path / "emptybin").mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("PATH", str(tmp_path / "emptybin"))


# ── 1. the script must not fire the route that runs the fleet ────────────────

def test_verify_route_is_never_probed_and_reports_the_gap(tmp_path):
    """No curl argv names /v1/verify, yet the route is still in the report.

    Its handler IS ``hscc_daemon.verify.run_all()`` — one probe per pass was a
    full fleet smoke test with real chat round-trips. The route keeps a row the
    way a POST-only route does: the gap named in the note, and no number
    anywhere in the row that could be read as a latency.
    """
    cp, calls, rows = _measured(tmp_path)
    assert cp.returncode == 0, cp.stdout + cp.stderr

    urls = [args[-1] for args in calls]
    assert [u for u in urls if u.endswith("/v1/verify")] == [], (
        "the script fired the route that runs verify: %s" % urls)
    assert any(u.endswith("/v1/health") for u in urls), urls  # not all skips

    row = next(r for r in rows if r["route"] == "/v1/verify")
    assert row["status"] == "skip", row
    assert row["note"], row
    assert "verify-triggered" in row["note"], row
    assert "not swept" in row["note"], row
    # Nothing measured, so nothing numeric — `0.00s` here would be the same
    # fabrication in the opposite direction.
    assert row["n"] == 0 and row["median"] is None and row["p95"] is None, row
    assert row["max"] is None and row["mean"] is None, row
    # Same keys as every other row: a consumer must never special-case it.
    assert list(row) == ROW_KEYS, row


def test_mutation_routes_are_excluded_before_any_request(tmp_path):
    """POST-only routes are excluded up front, not fired and dropped after.

    The old loop hit ``/v1/cluster/stop`` with a GET and then `continue`d on the
    405 — a request out to a mutating endpoint, and a route that silently
    vanished from the table. Now the suffix rule (imported, not copied) keeps
    the request from going out at all, and the route says so in the report.
    """
    cp, calls, rows = _measured(tmp_path)
    assert cp.returncode == 0, cp.stdout + cp.stderr

    urls = [args[-1] for args in calls]
    assert [u for u in urls if "/v1/cluster/stop" in u] == [], urls

    row = next(r for r in rows if r["route"] == "/v1/cluster/stop")
    assert row["status"] == "skip" and row["median"] is None, row
    assert "POST-only" in row["note"], row
    # Interpolated routes are not literals: never derived, so never fired.
    assert not [u for u in urls if "\\(" in u], urls


def test_an_unpredicted_405_is_reported_rather_than_dropped(tmp_path):
    """A 405 the suffix rule did not predict still gets a row, with no timings.

    The route is real and it did answer; only the code path differs. Firing the
    POST would mutate live state, and a 405's latency says nothing about the
    read screen the phone waits on — so the row carries the HTTP code it
    actually earned and no numbers it did not.
    """
    cp, calls, rows = _measured(tmp_path, post_only=["/v1/ping"])
    assert cp.returncode == 0, cp.stdout + cp.stderr

    row = next(r for r in rows if r["route"] == "/v1/ping")
    assert row["status"] == "405", row          # the real code, honestly kept
    assert "POST-only" in row["note"] and "not measured" in row["note"], row
    assert row["n"] == 0 and row["median"] is None, row
    assert list(row) == ROW_KEYS, row


# ── 2. a budget the cold start can finish inside ─────────────────────────────

def test_health_probe_gets_the_raised_timeout(tmp_path):
    """``-m 120`` for /v1/health; ``-m 30`` for every other route.

    ``/v1/health`` runs one inline verify when it has no cached snapshot —
    measured 64.4 s — so the blanket 30 s cap cut the request off and the table
    ranked a route that was answering as a fast one. Everything else keeps 30 s
    on purpose: a route that turns slow for a reason nobody predicted is a
    finding, not something to hand a longer leash.
    """
    cp, calls, rows = _measured(tmp_path, argv=("--json", "--passes", "2"))
    assert cp.returncode == 0, cp.stdout + cp.stderr

    by_url = {}
    for args in calls:
        by_url.setdefault(args[-1], args)
    health = by_url["http://127.0.0.1:1/v1/health"]
    assert health[:4] == ["-s", "-m", "120", "-w"], health
    for route in ("/v1/verify", "/v1/cluster/stop"):
        assert not [a for a in calls if a[-1].endswith(route)], route
    args = by_url["http://127.0.0.1:1/v1/ping"]
    assert args[:4] == ["-s", "-m", "30", "-w"], args


def test_every_probed_route_is_measured_once_per_pass(tmp_path):
    """The pass count reaches curl: 2 passes = 2 calls per timed route.

    Guards the row's `n` from drifting away from the work actually done — a row
    claiming five samples that came from one is as much a fiction as a skipped
    route claiming a time.
    """
    cp, calls, rows = _measured(tmp_path, argv=("--json", "--passes", "2"))
    assert cp.returncode == 0, cp.stdout + cp.stderr

    urls = [args[-1] for args in calls]
    assert urls.count("http://127.0.0.1:1/v1/ping") == 2, urls
    by_route = {r["route"]: r for r in rows}
    assert by_route["/v1/ping"]["n"] == 2, by_route["/v1/ping"]
    # Four routes are timed (health, ping, logs, sessions) of the seven named.
    assert len({u.split("?")[0] for u in urls}) == 4, urls


# ── 3. ONE derivation, not two copies ───────────────────────────────────────

def test_the_script_imports_the_sweep_tables_rather_than_copying_them():
    """The tables are the sweep's objects, not equal-looking duplicates.

    Identity is the point: `==` would still hold after someone re-pinned a copy
    that matched today and drifted next month, which is precisely how this file
    ended up with a NEEDS_PARAM that named only two routes and sent a param
    NAME instead of the client's values.
    """
    import perf_measure
    from hscc_daemon import api_route_sweep as sweep

    assert perf_measure.IMPORT_ERROR is None
    for name in ("PROBE_TIMEOUT", "HEAVY_ROUTES", "NOT_SWEPT", "NEEDS_PARAM",
                 "MUTATION_SUFFIXES", "NOT_SWEPT_STATUS"):
        assert getattr(perf_measure, name) is getattr(sweep, name), name
    assert perf_measure.client_routes is sweep.client_routes
    # The rules this card cares about, still true on the single source of them.
    assert "/v1/verify" in perf_measure.NOT_SWEPT
    assert perf_measure.HEAVY_ROUTES.get("/v1/health") == 120
    assert perf_measure.PROBE_TIMEOUT == 30


def test_the_script_redeclares_none_of_the_probe_rules():
    """A source scan: no table, no param map, no literal -m budget re-appears.

    The import test above can only check what is bound today. This one bites
    the moment a second copy is re-introduced next to the import, which is how
    the drift started last time.
    """
    src = open(PERF_SRC).read()
    for name in ("PROBE_TIMEOUT", "HEAVY_ROUTES", "NOT_SWEPT", "NEEDS_PARAM",
                 "MUTATION_SUFFIXES"):
        assert not re.search(r"(?m)^%s\s*=" % name, src), (
            "%s is re-declared in scripts/perf_measure.py — import it from "
            "hscc_daemon/api_route_sweep.py instead" % name)
    assert '"/stop"' not in src, "the mutation-suffix tuple was copied again"
    assert re.search(r'"-m",\s*str\(timeout\)', src), \
        "the curl budget must be the per-route one, not a literal"
    assert '"-m", "30"' not in src, "a hardcoded 30 s budget came back"


def test_logs_is_timed_on_the_url_the_client_uses(tmp_path):
    """The param table is the sweep's, so /v1/logs carries source AND limit.

    The script's own copy had only /v1/sessions and /v1/memory and mapped a
    route to a param NAME — so /v1/logs was timed against the 400 ``handle_logs``
    answers for an omitted source, and the number in the table was the latency
    of a rejection.
    """
    cp, calls, rows = _measured(tmp_path)
    assert cp.returncode == 0, cp.stdout + cp.stderr

    urls = [args[-1] for args in calls]
    assert "http://127.0.0.1:1/v1/logs?source=daemon&limit=1" in urls, urls
    assert "http://127.0.0.1:1/v1/logs" not in urls, urls
    assert "http://127.0.0.1:1/v1/sessions?profile=hscc-orch" in urls, urls

    row = next(r for r in rows if r["route"] == "/v1/logs")
    assert row["median"] is not None, row          # timed, not skipped
    assert "source=daemon" in row["note"] and "limit=1" in row["note"], row


# ── 4. the published shape, and no fabricated numbers ────────────────────────

def test_measured_rows_keep_the_published_shape(tmp_path):
    """route/status/n/median/p95/max/mean(+note) for a timed route, numeric.

    ``docs/audits/phone-performance.md`` quotes a run of this script and any
    consumer reads route/status/median/p95; the keys, their order and their
    types are the contract. The fake curl's per-call times increase, so median,
    p95, max and mean are four distinct values and a row that quietly swapped
    them cannot pass.
    """
    cp, calls, rows = _measured(tmp_path, argv=("--json", "--passes", "3"))
    assert cp.returncode == 0, cp.stdout + cp.stderr

    row = next(r for r in rows if r["route"] == "/v1/ping")
    assert list(row) == ROW_KEYS, row
    assert row["status"] == "200" and row["n"] == 3, row

    # The fake curl's times for this URL, re-derived independently of the
    # script's own arithmetic, so the row is pinned to the FORMULA and not to a
    # number remembered from a past run.
    url = "http://127.0.0.1:1/v1/ping"
    base = 0.10 * (len(url) % 5)
    times = [float("%.2f" % (base + 0.01 * i)) for i in (1, 2, 3)]
    assert row["median"] == pytest.approx(statistics.median(times)), row
    assert row["p95"] == pytest.approx(sorted(times)[int(0.95 * 2)]), row
    assert row["max"] == pytest.approx(max(times)), row
    assert row["mean"] == pytest.approx(statistics.mean(times)), row
    assert len({row["median"], row["max"]}) == 2, \
        "median and max identical means the fake times collapsed: %s" % row
    assert row["note"] == "", row

    # Slowest first, and a `skip` can never out-rank a measurement — the row
    # that names a gap must never look like the slowest endpoint in the system.
    timed = [r for r in rows if r["median"] is not None]
    skipped = [r for r in rows if r["median"] is None]
    assert len(timed) == 4 and len(skipped) == 2, [r["route"] for r in rows]
    assert timed == sorted(timed, key=lambda r: -r["median"]), \
        [r["route"] for r in rows]
    assert rows[len(timed):] == skipped, [r["route"] for r in rows]


def test_human_table_names_the_route_it_refused_to_time(tmp_path):
    """The gap is said out loud, in the table and in a closing line.

    Dropping /v1/verify in silence would leave a reader of the human view
    believing the run measured every route the app calls.
    """
    cp, _calls, _rows = _measured(tmp_path, argv=())
    out = cp.stdout
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "/v1/verify" in out, out
    assert "verify-triggered" in out, out
    assert "NOT measured" in out, out
    # The refused route must not print a timing anywhere on its own line. (The
    # closing paragraph names the route too — match the table row only.)
    lines = [l for l in out.splitlines()
             if l.startswith("skip") and "/v1/verify" in l]
    assert len(lines) == 1, out
    # The refused route's own columns are the status plus four blanks — no
    # number that a reader could take for a latency.
    cols = lines[0].split("/v1/verify")[0].split()
    assert cols == ["skip", "-", "-", "-", "-"], lines[0]


# ── 5. a run that measured nothing says so, in words ─────────────────────────

def test_a_client_of_only_never_fired_routes_refuses_a_false_green(tmp_path):
    """A client naming only /v1/verify yielded nothing to time: exit 3.

    Without this the run prints an empty green table forever, which reads as
    "every endpoint is fast" — the false-green mirror of the false red the
    script used to print.
    """
    root, script = _fake_project(tmp_path, 'func verify() { get("/v1/verify") }\n')
    _capture, path = _fake_curl(tmp_path)
    cp = _run(script, ["--json"], tmp_path, path=path)
    combined = cp.stdout + cp.stderr
    assert cp.returncode == 3, combined
    assert "Traceback" not in combined, combined
    payload = json.loads(cp.stdout)
    assert payload["could_not_run"] is True, payload
    assert "no literal GET routes" in payload["error"], payload
    assert not isinstance(payload, list), "a failure must not parse as a table"


def test_no_client_source_is_could_not_run(tmp_path):
    """No Swift file to parse routes from = nothing was measured."""
    root, script = _fake_project(tmp_path, _SWIFT)
    os.remove(root / "ios-app" / "Sources" / "HSCC" / "HSCCClient.swift")
    cp = _run(script, ["--json"], tmp_path)
    combined = cp.stdout + cp.stderr
    assert cp.returncode == 3, combined
    assert "Traceback" not in combined, combined
    payload = json.loads(cp.stdout)
    assert payload["could_not_run"] is True and "no iOS client source" in \
        payload["error"], payload


def test_no_curl_is_could_not_run_not_an_empty_table(tmp_path):
    """Without curl nothing was observed, so no verdict — and no green table.

    PATH is only `emptybin` here: the /usr/bin that the other runs carry has a
    real curl in it, and this case is about the tool being absent.
    """
    root, script = _fake_project(tmp_path, _SWIFT)
    cp = _run(script, ["--json"], tmp_path, path=str(tmp_path / "emptybin"))
    combined = cp.stdout + cp.stderr
    assert cp.returncode == 3, combined
    assert "Traceback" not in combined, combined
    assert "curl" in json.loads(cp.stdout)["error"], combined


def test_no_api_config_still_exits_2(tmp_path):
    """No host and no token is the pre-condition failure, unchanged (rc 2)."""
    root, script = _fake_project(tmp_path, _SWIFT, api_config=False)
    cp = _run(script, ["--json"], tmp_path)
    assert cp.returncode == 2, cp.stdout + cp.stderr
    assert "Traceback" not in cp.stdout + cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["could_not_run"] is False, payload


def test_the_script_bootstraps_its_own_sys_path_from_a_bare_script_path(tmp_path):
    """Run by path from ``/``: the import of the sweep's rules must still work.

    A bare script gets only `scripts/` on sys.path, so without the bootstrap the
    module dies with ModuleNotFoundError — the same fault shape that once made a
    working API look dead to `hscc verify` (t_ef36fe71). Here it would leave the
    operator with no latency table and a traceback instead of a reason.
    """
    root, script = _fake_project(tmp_path, _SWIFT)
    capture, path = _fake_curl(tmp_path)
    cp = _run(script, ["--json"], tmp_path, path=path, cwd="/")
    combined = cp.stdout + cp.stderr
    assert "No module named" not in combined, combined
    assert "Traceback" not in combined, combined
    assert cp.returncode == 0, combined
    assert capture.exists(), "no probe ran at all"
