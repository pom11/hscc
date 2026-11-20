"""The api_routes row must never report a working API as dead (t_ef36fe71).

What shipped in the 2.15.2 keg, verbatim::

    api_routes ✗ route(s) the app calls did not answer: Traceback (most recent call last):

The API was up the whole time. The row was red because the *checker* died:
``hscc_daemon/api_route_sweep.py`` imports ``from hscc_daemon import
cli_theme``, and when it is run the way ``verify`` runs it — a bare script path
(``python3 <keg>/libexec/hscc/hscc_daemon/api_route_sweep.py``) — a script gets
only its OWN directory on ``sys.path``, so the package root is missing and the
import raises. The traceback exits 1, which ``check_api_routes`` mapped to "some
route did not answer", and the row text fell back to the traceback's first line.

Two invariants are pinned here:

1. STANDALONE RUNNABILITY. Run the sweep the way verify and the keg do — a bare
   script path, from a directory that is not the repo, with a scrubbed HOME —
   and it must produce a route report, not a ``ModuleNotFoundError``.
2. NEVER CRY WOLF. Any sweep failure that is not a route report (a crash, a
   missing client source, a missing renderer) is UNVERIFIED (``ok is None``) or
   the explicit "API not reachable" fail — never "route(s) did not answer", and
   never a raw traceback as the row text.
3. NEVER FIRE THE SMOKE TEST. The sweep proves routes ANSWER. ``/v1/verify``'s
   handler IS ``verify.run_all()`` — the check that shells out to this sweep —
   so probing it seeded nested full verifies with real GPU chat round-trips; it
   is reported, not called. ``/v1/health``'s cold start is legitimately slower
   than the blanket 30 s budget, so it gets one of its own instead of a
   permanent false red (t_109bcc97).

Everything runs against a FAKE keg in tmp_path and a closed loopback port; the
live API, the operator's ``~/.hscc`` and the fleet are never touched.
"""

import json
import os
import shutil
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SWEEP_SRC = os.path.join(REPO, "hscc_daemon", "api_route_sweep.py")
CLI_THEME_SRC = os.path.join(REPO, "hscc_daemon", "cli_theme.py")

# A closed loopback port: connecting to it fails instantly, so the sweep runs
# its real per-route path (curl + parse + classify) without reaching anything.
DEAD_HOST, DEAD_PORT = "127.0.0.1", 1

_SWIFT = '''\
import Foundation
func health() { get("/v1/health") }
func feed()   { get("/v1/activity/feed") }
func stop()   { post("/v1/cluster/stop") }
func one(p: String) { get("/v1/sessions/\\(p)") }
'''

# An interpreter-level failure inside the module verify's sweep depends on — the
# shape of "the keg venv / PATH python3 does not have rich" (the second half of
# the 2.15.2 fault) without depending on which interpreter runs the suite.
_BROKEN_THEME = 'raise ImportError("simulated: no rich in this interpreter")\n'


def _fake_keg(tmp_path, *, theme=_BROKEN_THEME, with_client=True,
              api_config=True, keg_name="keg"):
    """Lay out a minimal *installed* tree and return (root, sweep_script).

    Mirrors the keg: ``<root>/hscc_daemon/`` holds the sweep as a plain file and
    the package beside it, and ``<root>/ios-app/...`` is where a repo checkout —
    and ONLY a repo checkout — keeps the Swift client the route list is parsed
    from. The scrubbed HOME is always ``<tmp_path>/fakehome`` so the sweep finds
    its api.json no matter which keg dir it was asked to build.

    ``theme`` is the text written into the fake keg's ``cli_theme.py``; pass
    None to leave the file out entirely (an installed tree without a theme).
    """
    root = tmp_path / keg_name
    pkg = root / "hscc_daemon"
    pkg.mkdir(parents=True)
    shutil.copyfile(SWEEP_SRC, pkg / "api_route_sweep.py")
    (pkg / "__init__.py").write_text("")
    if theme is not None:
        (pkg / "cli_theme.py").write_text(theme)
    if with_client:
        src = root / "ios-app" / "Sources" / "HSCC"
        src.mkdir(parents=True)
        (src / "HSCCClient.swift").write_text(_SWIFT)
    home = tmp_path / "fakehome"
    (home / ".hscc").mkdir(parents=True, exist_ok=True)
    if api_config:
        (home / ".hscc" / "api.json").write_text(
            json.dumps({"host": DEAD_HOST, "port": DEAD_PORT}))
        (home / ".hscc" / "api-token").write_text("probe-token\n")
    return root, str(pkg / "api_route_sweep.py")


def _run(script, argv, tmp_path, cwd=None, path=None):
    """Run the sweep as a BARE SCRIPT PATH — the operator/verify invocation."""
    return subprocess.run(
        [sys.executable, script] + argv,
        cwd=str(cwd or tmp_path), capture_output=True, text=True, timeout=180,
        env=dict(os.environ, HOME=str(tmp_path / "fakehome"),
                 # Default PATH has curl (the sweep needs it) but NO `hscc`:
                 # the sweep's api_base() fallback would otherwise shell out to
                 # the operator's live `hscc api status`.
                 PATH=path or (str(tmp_path / "emptybin")
                               + os.pathsep + "/usr/bin" + os.pathsep + "/bin")))


@pytest.fixture(autouse=True)
def _no_live_api(monkeypatch, tmp_path):
    """Scrub HOME + PATH so no path in the sweep can reach real state."""
    (tmp_path / "emptybin").mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("PATH", str(tmp_path / "emptybin"))


# ── 1. standalone runnability (the actual 2.15.2 repro) ─────────────────────

def test_sweep_is_importable_as_bare_script(tmp_path):
    """The operator's manual repro must not raise ModuleNotFoundError.

    Run from a directory that is NOT the repo, by script path, with only the
    fake keg on disk: the package root must still resolve, because the sweep
    bootstraps ``sys.path`` itself (the sanctioned verify_chat_roundtrip
    pattern) rather than relying on whoever invoked it.
    """
    _, script = _fake_keg(tmp_path, theme="")  # real cli_theme import not needed
    cp = _run(script, ["--json"], tmp_path, cwd="/")
    combined = cp.stdout + cp.stderr
    assert "No module named" not in combined, combined
    assert "Traceback" not in combined, combined
    assert cp.returncode in (0, 1), combined  # it swept; the routes just failed


def test_bare_script_run_can_import_the_real_theme(tmp_path):
    """THE root-cause pin: the human view must render from a bare script path.

    With the REAL ``cli_theme.py`` planted in the fake keg, a run of
    ``python <keg>/hscc_daemon/api_route_sweep.py`` from ``/`` has to import it.
    Delete the ``sys.path`` bootstrap and this goes red: the import dies, the
    theme stays unavailable, and the run degrades to exit 3 with 'cannot render'
    — which is the 2.15.2 behaviour, just politely labelled.
    """
    root = tmp_path / "keg"
    _, script = _fake_keg(tmp_path, theme=None)
    shutil.copyfile(CLI_THEME_SRC, root / "hscc_daemon" / "cli_theme.py")
    cp = _run(script, [], tmp_path, cwd="/")
    combined = cp.stdout + cp.stderr
    assert cp.returncode in (0, 1), combined
    assert "cannot render" not in combined, combined
    assert "Traceback" not in combined, combined
    assert "/v1/health" in combined           # the themed table really rendered


def test_sweep_reports_routes_when_run_like_verify_does(tmp_path):
    """The fixed sweep still names its routes: the machine contract is intact."""
    _, script = _fake_keg(tmp_path, theme="")
    cp = _run(script, ["--json"], tmp_path, cwd="/")
    payload = json.loads(cp.stdout)  # raises (and fails) if this is a traceback
    routes = [r["route"] for r in payload["routes"]]
    assert "/v1/health" in routes and "/v1/activity/feed" in routes
    # POST-only mutation routes are listed, never fired at by a bare sweep.
    assert "/v1/cluster/stop" not in routes
    assert payload["not_swept_dynamic"] == ["/v1/sessions/\\(p)"]


# ── 2. never cry wolf ────────────────────────────────────────────────────────

def test_sweep_without_renderer_exits_3_not_a_traceback(tmp_path):
    """No cli_theme/rich ⇒ exit 3 ('could not run'), stated in words."""
    _, script = _fake_keg(tmp_path)           # broken cli_theme
    cp = _run(script, [], tmp_path, cwd="/")
    combined = cp.stdout + cp.stderr
    assert cp.returncode == 3, combined
    assert "Traceback" not in combined, combined
    assert "cannot render" in combined


def test_machine_json_path_survives_a_missing_renderer(tmp_path):
    """``--json`` never needs the theme, so verify's path is renderer-proof.

    This is why verify can keep trusting the sweep on a box whose interpreter
    lacks rich: the human view degrades, the machine view does not.
    """
    _, script = _fake_keg(tmp_path)           # broken cli_theme
    cp = _run(script, ["--json"], tmp_path, cwd="/")
    combined = cp.stdout + cp.stderr
    assert "Traceback" not in combined, combined
    assert cp.returncode in (0, 1), combined
    assert "routes" in json.loads(cp.stdout)


def test_sweep_without_curl_says_could_not_run(tmp_path):
    """A missing curl is 'could not run' too, never a dead-route verdict.

    The sweep shells out to curl for every route; an unguarded run died with
    FileNotFoundError and handed verify a traceback to interpret.
    """
    _, script = _fake_keg(tmp_path, theme="")
    empty = str(tmp_path / "emptybin")
    cp = _run(script, ["--json"], tmp_path, cwd="/", path=empty)
    combined = cp.stdout + cp.stderr
    assert cp.returncode == 3, combined
    assert "Traceback" not in combined, combined
    assert "curl" in json.loads(cp.stdout)["error"]


def test_sweep_with_no_get_routes_refuses_a_false_green(tmp_path):
    """A client file that yields no GET routes must not read as 'all routes ok'.

    The mirror image of the false red this card fixes: an emptied or renamed
    client source would otherwise make the row pass forever.
    """
    root = tmp_path / "keg"
    _, script = _fake_keg(tmp_path, theme="")
    (root / "ios-app" / "Sources" / "HSCC" / "HSCCClient.swift").write_text(
        "// routes moved elsewhere\n")
    cp = _run(script, ["--json"], tmp_path, cwd="/")
    combined = cp.stdout + cp.stderr
    assert cp.returncode == 3, combined
    assert "Traceback" not in combined, combined
    payload = json.loads(cp.stdout)
    assert payload["could_not_run"] is True
    assert "no literal GET routes" in payload["error"]


def test_sweep_without_client_source_says_could_not_run(tmp_path):
    """No ios-app/ to parse routes from = nothing was checked, not a dead API."""
    _, script = _fake_keg(tmp_path, theme="", with_client=False)
    cp = _run(script, ["--json"], tmp_path, cwd="/")
    combined = cp.stdout + cp.stderr
    assert cp.returncode == 3, combined
    assert "Traceback" not in combined, combined
    payload = json.loads(cp.stdout)
    assert payload["could_not_run"] is True
    assert "no iOS client source" in payload["error"]


def test_sweep_without_api_config_exits_2(tmp_path):
    """No host and no token is the ONE non-route case that stays red (rc 2)."""
    _, script = _fake_keg(tmp_path, theme="", api_config=False)
    cp = _run(script, ["--json"], tmp_path, cwd="/")
    assert cp.returncode == 2, cp.stdout + cp.stderr
    assert "Traceback" not in cp.stdout + cp.stderr
    assert json.loads(cp.stdout)["could_not_run"] is False


# ── 3. verify's interpretation ───────────────────────────────────────────────

def test_check_api_routes_against_the_fake_keg_is_never_a_traceback(tmp_path):
    """End-to-end through verify, on a keg-shaped tree with no repo on sys.path.

    The row must either name routes or say it could not run — a traceback in
    the row text is the regression this card exists to kill.
    """
    from hscc_daemon.verify import check_api_routes

    for i, theme in enumerate((None, _BROKEN_THEME)):  # renderer / no renderer
        _, script = _fake_keg(tmp_path / ("t%d" % i),
                              theme=theme or "")
        r = check_api_routes(script=script, python=sys.executable)
        assert r["name"] == "api_routes"
        assert "Traceback" not in r["detail"], r
        assert r["ok"] is not True              # dead port: nothing passed here


def test_check_api_routes_crash_is_unverified_not_dead(tmp_path):
    """Exit 1 with NO route report = the sweep died = unverified, never red.

    This is the 2.15.2 row verbatim: the sweep exited 1 from an import error and
    verify printed the traceback's first line as if it were a route name.
    """
    from hscc_daemon.verify import check_api_routes
    script = tmp_path / "api_route_sweep.py"
    script.write_text(
        "import sys\n"
        "print('Traceback (most recent call last):')\n"
        "print('ModuleNotFoundError: No module named %r' % 'hscc_daemon')\n"
        "sys.exit(1)\n")
    r = check_api_routes(script=str(script), python=sys.executable)
    assert r["ok"] is None, r
    assert r["detail"].startswith("unverified"), r
    assert "did not answer" not in r["detail"], r
    # The USEFUL line is surfaced, not the useless 'Traceback (most recent...'
    assert "ModuleNotFoundError" in r["detail"], r
    assert r["next_step"]


def test_check_api_routes_exit_three_is_unverified(tmp_path):
    """The sweep's own 'could not run' exit must land as unverified."""
    from hscc_daemon.verify import check_api_routes
    script = tmp_path / "api_route_sweep.py"
    script.write_text(
        "import json, sys\n"
        "print(json.dumps({'error': 'no client source', 'could_not_run': True}))\n"
        "sys.exit(3)\n")
    r = check_api_routes(script=str(script), python=sys.executable)
    assert r["ok"] is None, r
    assert "could not run" in r["detail"] and "no client source" in r["detail"], r


def test_check_api_routes_no_api_still_fails_loudly(tmp_path):
    """rc 2 keeps its red verdict AND stops printing '{' as the reason."""
    from hscc_daemon.verify import check_api_routes
    script = tmp_path / "api_route_sweep.py"
    script.write_text(
        "import json, sys\n"
        "print(json.dumps({'error': 'cannot sweep: no API host or token', "
        "'could_not_run': False}))\n"
        "sys.exit(2)\n")
    r = check_api_routes(script=str(script), python=sys.executable)
    assert r["ok"] is False, r
    assert "API not reachable" in r["detail"] and "no API host or token" in r["detail"], r
    assert r["detail"] != "API not reachable: {"


def test_check_api_routes_uses_the_current_interpreter(tmp_path, monkeypatch):
    """Default interpreter is verify's own, not a PATH `python3` gamble.

    The keg bug needed two faults: the missing bootstrap AND verify shelling out
    to a `python3` that had no rich. The row must therefore be run by the
    interpreter that already proved it can import this package.
    """
    from hscc_daemon.verify import check_api_routes
    monkeypatch.delenv("HSCC_TEST_PY", raising=False)
    seen = tmp_path / "argv.txt"
    script = tmp_path / "api_route_sweep.py"
    script.write_text("import sys; open(%r,'w').write(sys.executable)\n" % str(seen))
    check_api_routes(script=str(script), timeout=60)
    assert seen.read_text() == sys.executable


def test_run_all_keeps_a_crashed_sweep_out_of_the_red(tmp_path):
    """A sweep that could not run must not turn the whole verdict red.

    ``run_all`` treats ok=None as unverified, not failed — that is the contract
    that lets an operator keep believing a red row.
    """
    from hscc_daemon.verify import check_api_routes
    script = tmp_path / "api_route_sweep.py"
    script.write_text("raise SystemExit(1)\n")
    assert check_api_routes(script=str(script),
                            python=sys.executable)["ok"] is None


# ── 4. required query parameters (t_0494b09e) ───────────────────────────────

def test_logs_probe_sends_source_and_limit(tmp_path):
    """The /v1/logs probe must send the query the app sends, not a bare path.

    ``handle_logs`` answers 400 when ``source`` is omitted (it validates the
    parameter before reading anything), so an unparameterised probe was a
    permanent false red on a healthy API. Capture the URL curl is actually
    handed and pin both the required ``source`` and the bounded ``limit``.
    """
    root, script = _fake_keg(tmp_path, theme="")
    (root / "ios-app" / "Sources" / "HSCC" / "HSCCClient.swift").write_text(
        'func health() { get("/v1/health") }\n'
        'func logs()   { get("/v1/logs") }\n')

    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    capture = tmp_path / "urls.txt"
    curl = bindir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        "for last; do :; done\n"
        'echo "$last" >> "%s"\n'
        "printf '{\"ok\": true}\\n200 0.01'\n" % capture)
    curl.chmod(0o755)

    cp = _run(script, ["--json"], tmp_path, cwd="/",
              path=str(bindir) + os.pathsep + "/usr/bin" + os.pathsep + "/bin")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    captured = capture.read_text().splitlines()
    assert "http://127.0.0.1:1/v1/logs?source=daemon&limit=1" in captured
    assert "http://127.0.0.1:1/v1/logs" not in captured

    row = next(r for r in json.loads(cp.stdout)["routes"]
               if r["route"] == "/v1/logs")
    assert row["ok"] is True
    assert "source=daemon" in row["note"] and "limit=1" in row["note"]


# ── 5. the sweep must not re-trigger verify from inside verify (t_109bcc97) ──

# A client whose routes make the two new rules observable at once: one heavy
# route, one verify-triggered route, and a plain one that must NOT inherit
# either treatment.
_SWIFT_HEAVY = '''\
import Foundation
func health() { get("/v1/health") }
func ping()   { get("/v1/ping") }
func feed()   { get("/v1/activity/feed") }
func verify() { get("/v1/verify") }
'''

# A stand-in for curl that logs the WHOLE argv it is handed and answers 200 with
# a parseable body. ``test_logs_probe_sends_source_and_limit`` records only the
# URL, which is all a query-parameter pin needs; a probe BUDGET lives in the
# flags, so this keeps every argument.
#
# Arguments are joined with \\x1f and calls separated by \\x1e rather than by
# lines: the sweep's ``-w`` argument contains a literal newline, which no
# line-based format survives, and neither control character can appear in an
# argument curl is given.
_CURL_ARGV_STUB = (
    "#!%s\n"
    "import sys\n"
    "with open(%r, 'a') as fh:\n"
    "    fh.write('\\x1f'.join(sys.argv[1:]) + '\\x1e')\n"
    "sys.stdout.write('{\"ok\": true}\\n200 0.01')\n"
)


def _fake_argv_curl(tmp_path, root, swift):
    """Plant ``swift`` as the client and an argv-logging curl on PATH.

    Returns (capture_path, path_for_run). The sweep talks HTTP through curl and
    nothing else, so this is the whole seam: no socket is ever opened.
    """
    (root / "ios-app" / "Sources" / "HSCC" / "HSCCClient.swift").write_text(swift)
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    capture = tmp_path / "curl-argv.txt"
    curl = bindir / "curl"
    curl.write_text(_CURL_ARGV_STUB % (sys.executable, str(capture)))
    curl.chmod(0o755)
    return capture, (str(bindir) + os.pathsep + "/usr/bin" + os.pathsep + "/bin")


def _sweep_json_capturing_argv(tmp_path, swift):
    """``--json`` sweep against the logging curl; return (completed, argv_calls)."""
    root, script = _fake_keg(tmp_path, theme="")
    capture, path = _fake_argv_curl(tmp_path, root, swift)
    cp = _run(script, ["--json"], tmp_path, cwd="/", path=path)
    argv = [call.split("\x1f") for call in capture.read_text().split("\x1e") if call]
    return cp, argv


def test_verify_route_is_never_probed_and_reports_the_gap(tmp_path):
    """``hscc verify`` must not seed a nested verify through its own sweep.

    ``/v1/verify``'s handler IS ``hscc_daemon.verify.run_all()`` — the check that
    shells out to this very sweep — so one probe started a fresh full verify with
    real chat round-trips against the GPU units, and that child swept
    ``/v1/verify`` again. The route stays in the report the way a POST-only route
    does: covered, with the gap named in the note.
    """
    cp, argv = _sweep_json_capturing_argv(tmp_path, _SWIFT_HEAVY)
    assert cp.returncode == 0, cp.stdout + cp.stderr

    urls = [args[-1] for args in argv]
    assert [u for u in urls if u.endswith("/v1/verify")] == [], (
        "the sweep fired the route that runs verify: %s" % urls)
    assert any(u.endswith("/v1/health") for u in urls), urls  # not all skips

    row = next(r for r in json.loads(cp.stdout)["routes"]
               if r["route"] == "/v1/verify")
    assert row["ok"] is True, row
    assert "verify-triggered" in row["note"], row
    assert "not swept" in row["note"], row
    # Same machine shape as every other row, key order included: the consumer
    # (verify's api_routes row) must never have to special-case this route.
    assert list(row) == ["route", "status", "seconds", "parses", "ok", "note"], row


def test_health_probe_gets_the_raised_timeout(tmp_path):
    """The curl argv carries a per-route budget: 120 s for /v1/health, 30 s on
    every other route.

    ``/v1/health`` runs one inline verify on a cold start (measured 64.4 s), so
    the blanket ``-m 30`` cut the probe off and the row printed ``000`` for a
    route that was answering. Everything else keeps 30 s on purpose: a route
    that turns slow for an unforeseen reason is a finding, not a thing to hand a
    longer leash.
    """
    cp, argv = _sweep_json_capturing_argv(tmp_path, _SWIFT_HEAVY)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    by_url = {args[-1]: args for args in argv}
    assert len(argv) == 3, by_url  # health, ping, feed — verify is never called

    health = by_url["http://127.0.0.1:1/v1/health"]
    assert health[:4] == ["-s", "-m", "120", "-w"], health
    for route in ("/v1/ping", "/v1/activity/feed"):
        args = by_url["http://127.0.0.1:1" + route]
        assert args[:4] == ["-s", "-m", "30", "-w"], (route, args)


def test_client_naming_only_never_swept_routes_refuses_a_false_green(tmp_path):
    """A client whose only literal GET is /v1/verify swept NOTHING.

    Excluding a route from the probes must not also exclude it from the
    "did we look at all?" guard: the same false-green mirror this file already
    pins for an emptied client source, one step further out.
    """
    root, script = _fake_keg(tmp_path, theme="")
    (root / "ios-app" / "Sources" / "HSCC" / "HSCCClient.swift").write_text(
        'func verify() { get("/v1/verify") }\n')
    cp = _run(script, ["--json"], tmp_path, cwd="/")
    combined = cp.stdout + cp.stderr
    assert cp.returncode == 3, combined
    payload = json.loads(cp.stdout)
    assert payload["could_not_run"] is True
    assert "no literal GET routes" in payload["error"], payload


def test_skipped_route_renders_as_skip_not_fail(tmp_path):
    """The human view must not print the never-fired route as FAIL.

    A skipped row is neither a pass nor a dead route; rendering it as FAIL would
    reintroduce the false alarm this file exists to kill, this time with the
    operator reading a red row for a route the sweep deliberately left alone.
    """
    root, script = _fake_keg(tmp_path, theme=None)
    shutil.copyfile(CLI_THEME_SRC, root / "hscc_daemon" / "cli_theme.py")
    _, path = _fake_argv_curl(tmp_path, root, _SWIFT_HEAVY)
    cp = _run(script, [], tmp_path, cwd="/", path=path)
    combined = cp.stdout + cp.stderr
    assert cp.returncode == 0, combined
    assert "FAIL" not in combined, combined
    assert "/v1/verify" in combined, combined
    assert "skip" in combined, combined

