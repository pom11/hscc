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
