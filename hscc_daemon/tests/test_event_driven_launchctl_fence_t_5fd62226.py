"""t_5fd62226 — event_driven's two ``launchctl`` exec sites must be fenced.

THE HOLE. ``LaunchdJobGenerator.uninstall_job`` ran a real
``launchctl bootout gui/<uid> com.nousresearch.hscc-periodic.<stream>`` and
``status()`` ran a real ``launchctl list <label>``, and the module never asked
``hscc_daemon.suite_fence`` at all — ``grep -n "under_pytest\\|suite_fence"
hscc_daemon/event_driven.py`` returned nothing before this card. The cases that
exist today survive only by convention: ``test_event_driven_cli_no_ansi.py``
requests the OPT-IN ``fake_subprocess`` fixture and ``_patch_paths`` redirects
``PLIST_DIR``/``HSCC_DIR``. A new case that forgets the fixture execs a real
bootout of the operator's periodic jobs. The autouse ``_isolate_hscc`` seal does
not cover it either: it redirects ``~/.hscc`` only, and ``PLIST_DIR`` is
``~/Library/LaunchAgents``.

And the hazard is not just the CLI verb. The same exec is reachable from
``EventDrivenDaemon._cleanup_stale_launchd()`, which runs on EVERY daemon start
(``start()`` calls it), and from ``EventDrivenDaemon.stop()``. One fence at the
exec site covers all of them; three per-file conventions cover none of them.

WHY IT IS NOT ``com.hscc.api``. The labels here are
``f"{PERIODIC_LABEL_PREFIX}.{stream}"`` — the operator's periodic stream jobs —
so a forgotten stub tears down the operator's scheduled checks rather than the
live API the merge queue talks through. Same class of hole as t_e1c7c925,
different unit, hence this file.

WHY THE FENCE IS IN PRODUCTION, NOT A CONFTEST AUTOUSE (t_e1c7c925's decision,
carried over). ``_launchctl`` itself asks ``suite_fence.under_pytest()``: that is
what makes it reach a FRESH interpreter a suite item spawned (pytest was never
imported there; only ``PYTEST_CURRENT_TEST`` crossed the fork — t_251cd84f) and
every other test dir in the repo. A conftest fixture reaches neither. And per
``hscc-daemon-ambient-seam-stub``, a fence that REFUSES must not be
default-stubbed away — every case that forgot the seam would then pass for the
wrong reason. So there is no conftest default here; the escape hatch is the case
installing its own runner (``monkeypatch.setattr(ed, "_launchctl", ...)``), which
REPLACES the function and takes the fence off the path.

WHAT THIS FILE PINS, AND HOW IT STAYS HERMETIC. Nothing here can reach the real
service manager. The in-suite cases spy on ``ed.subprocess.run`` itself, so "no
fork" is asserted AT THE FORK rather than inferred from a message; the child
cases plant a FAKE ``launchctl`` first on the child's ``PATH`` (a /bin/sh script,
so it needs no interpreter) whose exec log is the evidence, and every child
redirects ``PLIST_DIR``/``HSCC_DIR`` under its own planted HOME, so no plist of
the operator's is ever removed either.

THE CONTROL IS THE POINT. The marked child must exec the fake ZERO times; the
unmarked child — the operator's shape, the same code with no marker — must exec
it once per stream for BOTH sites (``list`` then ``bootout``) and still report
success. Without the second half, the first half would only prove that a branch
nothing can enter is not taken.

THE SILENT-NOT-LOADED TRAP, pinned separately. ``status()`` used to fold "the
probe could not be asked" into ``loaded: False``, which is the same false claim
t_8bf34130 rules out for the API's supervisor row: an unasked probe is not
evidence the job is unloaded. The row now carries ``loaded_error`` and the human
view prints ``unknown`` instead of the ``no`` an unloaded job gets.
"""

import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

from hscc_daemon import api_cli
from hscc_daemon import event_driven as ed

#: The repo's refusal convention (``doctor_rows``, ``_refused_supervision_check``,
#: ``api_cli.REFUSAL``) asserted as the WHOLE phrase, sourced from this module's
#: own constant so a rewording cannot drift out from under a fragment.
REFUSAL = ed.REFUSAL

#: ``<repo>/hscc_daemon/tests/<this file>`` -> repo root, what the child puts on
#: ``sys.path`` so ``import hscc_daemon`` resolves the way the CLI does.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: The canned answers the planted fake gives. Synthesised — no operator path
#: appears in either, and no real job is ever queried or unloaded.
FAKE_BOOTOUT_OK = "Boot-out successful: 0.\n"


def _bootout_argv(stream):
    """The exact argv ``uninstall_job`` must build for one stream."""
    return ["launchctl", "bootout", f"gui/{os.getuid()}",
            f"{ed.PERIODIC_LABEL_PREFIX}.{stream}"]


def _list_argv(stream):
    """The exact argv ``status()`` must build for one stream."""
    return ["launchctl", "list", f"{ed.PERIODIC_LABEL_PREFIX}.{stream}"]


def _redirect_paths(monkeypatch, tmp_path):
    """Keep every write of these verbs inside the case's own tmp dir.

    The autouse ``_isolate_hscc`` seal does NOT reach ``PLIST_DIR`` (it is
    ``~/Library/LaunchAgents``, outside ``~/.hscc``) and does not overwrite
    ``event_driven``'s module constants at all, so a case that drives these verbs
    has to pin them itself or it removes the operator's real plists — a hazard
    beside the exec one, and this is the card that knows about both.
    """
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    hscc_dir = tmp_path / "hscc"
    hscc_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ed, "PLIST_DIR", str(plist_dir))
    monkeypatch.setattr(ed, "HSCC_DIR", str(hscc_dir))
    monkeypatch.setattr(ed, "STATE_DIR", str(hscc_dir / "state"))
    return plist_dir, hscc_dir


def _recording_log(monkeypatch):
    """Capture ``_event_log`` calls — the channel this module's refusals use."""
    lines = []
    monkeypatch.setattr(ed, "_event_log",
                        lambda msg, level="INFO": lines.append((level, str(msg))))
    return lines


# --------------------------------------------------------------------------- #
# the in-suite fence: asserted AT THE FORK, not inferred from a message
# --------------------------------------------------------------------------- #

@pytest.fixture()
def _no_fork(monkeypatch):
    """Make a real fork impossible AND observable, for the in-suite cases only.

    Requested EXPLICITLY, never autouse: ``ed.subprocess`` IS the shared
    ``subprocess`` module object, so an autouse patch here would also blind the
    child-driving cases below, which must really spawn.

    Every case using it asserts on this record, so "the fence refused" means
    ``subprocess.run`` was never entered — not "the output looked like a refusal".
    """
    calls = []

    def _never(*args, **kwargs):
        calls.append([str(a) for a in (args[0] if args else [])])
        raise AssertionError(f"a real fork reached subprocess.run: {args!r}")

    monkeypatch.setattr(ed.subprocess, "run", _never)
    return calls


def test_the_live_seam_refuses_under_pytest_with_no_runner_installed(_no_fork):
    """The fence itself: no fork, a visible refusal, and the right channel.

    ``(None, msg)`` is ``_launchctl``'s own "the supervisor could not be asked"
    channel — the same one a missing ``launchctl`` binary uses — so callers that
    already handle "not asked" handle a refusal without new branches, and a
    refusal can never be misread as a returncode of 0.
    """
    rc, out = ed._launchctl(_bootout_argv("dgx"))
    assert rc is None, f"a refused bootout must not carry a returncode: {rc!r}"
    assert REFUSAL in out, f"the refusal must stay visible: {out!r}"
    assert _no_fork == [], f"the fence forked anyway: {_no_fork}"
    # The message must name what it refused and how to stop needing it.
    assert "launchctl bootout" in out, f"the refusal must name the verb: {out!r}"
    assert "_launchctl" in out, f"the refusal must name the seam: {out!r}"


def test_a_case_that_forgot_the_stub_cannot_bootout_the_operator_jobs(
        _no_fork, monkeypatch, tmp_path):
    """THE CARD'S EXACT HAZARD: the verb, with no runner and no ``fake_subprocess``.

    Before this card this exec'd one real ``launchctl bootout gui/<uid>
    com.nousresearch.hscc-periodic.<stream>`` per stream against the operator's
    scheduled checks. Now the fork is never entered, and the refusal is visible
    in the daemon log the verb writes.
    """
    _redirect_paths(monkeypatch, tmp_path)
    lines = _recording_log(monkeypatch)

    out = io.StringIO()
    with redirect_stdout(out):
        ed.cmd_uninstall_event_driven()

    assert _no_fork == [], f"the bootout reached the fork anyway: {_no_fork}"
    refused = [msg for level, msg in lines
               if "Could not ask launchd" in msg and REFUSAL in msg]
    assert len(refused) == len(ed.PERIODIC_STREAMS), (
        f"one refusal per stream must be logged, got {refused}")
    assert all(f"{ed.PERIODIC_LABEL_PREFIX}." in msg for msg in refused), (
        f"a refusal must name the job it would have touched: {refused}")
    # Refusing the supervisor must NOT turn into a failure to do the verb's own
    # work: the plist under the redirected PLIST_DIR is still removed.
    assert "Uninstalled" in out.getvalue(), (
        f"a refused bootout must still uninstall the plist: {out.getvalue()!r}")


def test_a_case_that_forgot_the_stub_cannot_list_the_operator_jobs(
        _no_fork, monkeypatch, tmp_path):
    """The READ site, same shape — and the unasked probe must not read "not loaded".

    ``cmd_event_status`` with no ``fake_subprocess`` used to fork one
    ``launchctl list`` per stream. Now it forks none, and every row says the
    supervisor was not consulted instead of printing the same ``no`` an actually
    unloaded job gets.
    """
    _redirect_paths(monkeypatch, tmp_path)
    _recording_log(monkeypatch)

    out = io.StringIO()
    with redirect_stdout(out):
        ed.cmd_event_status()
    rendered = out.getvalue()

    assert _no_fork == [], f"the list probe reached the fork: {_no_fork}"
    rows = ed.LaunchdJobGenerator(hscc_script="/bin/true").status()
    assert len(rows) == len(ed.PERIODIC_STREAMS)
    for stream, row in rows.items():
        assert row["loaded"] is False, f"an unasked probe is not 'unloaded': {row}"
        assert row["loaded_error"] and REFUSAL in row["loaded_error"], (
            f"the row must say the probe was refused, not stay silent: {row}")
    assert "unknown" in rendered, (
        f"the human view must not render an unasked probe as a plain `no`: "
        f"{rendered!r}")


def test_the_daemon_start_cleanup_path_is_fenced_too(_no_fork, monkeypatch,
                                                     tmp_path):
    """The path the card's title does not name, and the one that worries most.

    ``EventDrivenDaemon.start()`` calls ``_cleanup_stale_launchd()``, which boots
    out every periodic stream on EVERY daemon start — so an in-suite daemon
    fixture that does not stub it reaches the operator's jobs through a path no
    CLI-level convention ever covered.
    """
    _redirect_paths(monkeypatch, tmp_path)
    lines = _recording_log(monkeypatch)
    daemon = ed.EventDrivenDaemon(check_map={}, watchdog_fn=lambda: True,
                                  trigger_fn=lambda: None,
                                  install_launchd=False)
    daemon._cleanup_stale_launchd()

    assert _no_fork == [], f"the stale cleanup reached the fork: {_no_fork}"
    assert any(REFUSAL in msg for _level, msg in lines), (
        f"the stale cleanup must log its refusal, not pass silently: {lines}")


def test_installing_a_runner_is_the_escape_hatch(_no_fork, monkeypatch, tmp_path):
    """A case that installs its own runner passes unchanged and pins its argv.

    This is why the fence needs no new parameter and why the pre-existing file
    needed no edits: ``monkeypatch.setattr`` REPLACES ``_launchctl``, so the fence
    is not on the path at all. The counts are pinned too — an unbounded scan of
    the operator's jobs would otherwise pass as "it worked".
    """
    _redirect_paths(monkeypatch, tmp_path)
    _recording_log(monkeypatch)
    seen = []

    def _runner(argv, timeout=10):
        seen.append(([str(a) for a in argv], timeout))
        return 0, FAKE_BOOTOUT_OK

    monkeypatch.setattr(ed, "_launchctl", _runner)
    out = io.StringIO()
    with redirect_stdout(out):
        ed.cmd_uninstall_event_driven()

    assert [argv for argv, _t in seen] == [
        _bootout_argv(s) for s in ed.PERIODIC_STREAMS
    ], f"one bootout per periodic stream, with the periodic label: {seen}"
    assert [t for _a, t in seen] == [10] * len(ed.PERIODIC_STREAMS), (
        f"the bootout's own bound is part of the contract: {seen}")
    assert _no_fork == [], f"an installed runner must not fork either: {_no_fork}"
    assert "Uninstalled" in out.getvalue(), (
        f"an installed runner must drive the real verb: {out.getvalue()!r}")


def test_status_reports_what_an_installed_runner_saw(_no_fork, monkeypatch,
                                                     tmp_path):
    """The read side of the escape hatch: rc 0 really does mean loaded."""
    plist_dir, _hscc = _redirect_paths(monkeypatch, tmp_path)
    _recording_log(monkeypatch)
    seen = []

    def _runner(argv, timeout=10):
        seen.append(([str(a) for a in argv], timeout))
        return 0, FAKE_BOOTOUT_OK

    monkeypatch.setattr(ed, "_launchctl", _runner)
    rows = ed.LaunchdJobGenerator(hscc_script="/bin/true").status()

    assert [argv for argv, _t in seen] == [
        _list_argv(s) for s in ed.PERIODIC_STREAMS
    ], f"one list per periodic stream: {seen}"
    assert [t for _a, t in seen] == [5] * len(ed.PERIODIC_STREAMS)
    for stream, row in rows.items():
        assert row["loaded"] is True, f"rc 0 means loaded: {row}"
        assert row["loaded_error"] is None, f"an answered probe has no error: {row}"
    assert _no_fork == []


# --------------------------------------------------------------------------- #
# the two children: the fence, and the control that proves it is a fence
# --------------------------------------------------------------------------- #

_CHILD = r'''
import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, sys.argv[1])
out = {"home": os.path.expanduser("~"), "pytest_imported": "pytest" in sys.modules,
       "uid": os.getuid(), "error": None,
       "stdout": "", "stderr": "", "rows": {}, "log": None, "plists_left": None}

try:
    from hscc_daemon import event_driven as ed
except Exception as exc:
    out["error"] = "import: %s: %s" % (type(exc).__name__, exc)
    print(json.dumps(out))
    raise SystemExit(0)

tmp = os.environ["CHILD_TMP"]
# Both writable roots go under the child's own planted HOME: no plist of the
# operator's is ever removed, and no operator daemon.log is ever appended to.
ed.HSCC_DIR = os.path.join(tmp, "hscc")
ed.STATE_DIR = os.path.join(ed.HSCC_DIR, "state")
ed.PLIST_DIR = os.path.join(tmp, "LaunchAgents")
os.makedirs(ed.STATE_DIR, exist_ok=True)
os.makedirs(ed.PLIST_DIR, exist_ok=True)
# A plist per stream, so the uninstall really removes something and the operator
# shape of the verb is what runs.
gen = ed.LaunchdJobGenerator(hscc_script=os.path.join(tmp, "hscc.py"))
for stream in ed.PERIODIC_STREAMS:
    ok, msg = gen.install_job(stream, ed.PERIODIC_STREAMS[stream])
    if not ok:
        out["error"] = "plant install_job(%s): %s" % (stream, msg)
        print(json.dumps(out))
        raise SystemExit(0)

_o, _e = io.StringIO(), io.StringIO()
with redirect_stdout(_o), redirect_stderr(_e):
    ed.cmd_event_status()               # the read site: one `launchctl list`
    ed.cmd_uninstall_event_driven()     # the write site: one `launchctl bootout`
out["stdout"], out["stderr"] = _o.getvalue(), _e.getvalue()

# A second read of the rows, so the caller can see what each site ANSWERED
# (the unmarked child must report loaded=True through the fake; the marked one
# must report refused). It is one more `launchctl list` per stream on the exec
# log, which the CONTROL case accounts for.
try:
    out["rows"] = {s: {k: v for k, v in r.items() if k in ("loaded", "loaded_error")}
                   for s, r in gen.status().items()}
except Exception as exc:
    out["error"] = "status: %s: %s" % (type(exc).__name__, exc)
out["plists_left"] = sorted(os.listdir(ed.PLIST_DIR))
try:
    out["log"] = open(os.path.join(ed.HSCC_DIR, "daemon.log")).read()
except OSError:
    out["log"] = ""
print(json.dumps(out))
'''

#: A fake ``launchctl`` that logs its own exec. Plain /bin/sh so it needs no
#: interpreter on the child's deliberately narrow PATH.
_FAKE_LAUNCHCTL = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_LAUNCHCTL_LOG"
echo "Boot-out successful: 0."
exit 0
"""


def _plant(tmp_path):
    """HOME + a PATH whose only ``launchctl`` is this file's fake.

    The fake is FIRST on PATH, ahead of ``/usr/bin``, so no argv this file builds
    can resolve to the real binary — the card forbids verifying this by driving a
    real bootout, because what is loaded there are the operator's live periodic
    jobs.
    """
    home = tmp_path / "home"
    (home / ".hscc").mkdir(parents=True, exist_ok=True)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "launchctl"
    fake.write_text(_FAKE_LAUNCHCTL, encoding="utf-8")
    fake.chmod(0o755)
    return home, bindir, bindir / "launchctl.calls"


def _spawn(tmp_path, *, under_test: bool) -> dict:
    """Drive both verbs from a child and report which branch they took.

    ``PYTEST_CURRENT_TEST`` is the only variable separating the two halves, which
    is the same channel the operator's own ``hscc event-driven uninstall`` never
    carries.
    """
    home, bindir, exec_log = _plant(tmp_path)
    child_dir = tmp_path / "child"
    child_dir.mkdir(exist_ok=True)
    probe = child_dir / "drive_verbs.py"
    probe.write_text(_CHILD, encoding="utf-8")

    env = dict(os.environ)
    env.update(HOME=str(home), XDG_CACHE_HOME=str(tmp_path / "cache"),
               PYTHONDONTWRITEBYTECODE="1", CHILD_TMP=str(child_dir),
               PATH=os.pathsep.join([str(bindir), "/usr/bin", "/bin"]),
               FAKE_LAUNCHCTL_LOG=str(exec_log))
    for var in ("PYTEST_CURRENT_TEST", "PYTEST_VERSION", "PYTEST_XDIST_WORKER",
                "HSCC_DIR", "HSCC_HOME", "HSCC_LAUNCHD_PROBE", "HERMES_HOME",
                "HERMES_AGENT_PATH", "HERMES_REAL_HOME", "HERMES_PROFILE",
                "HSCC_TESTS_ENFORCE"):
        env.pop(var, None)
    if under_test:
        env["PYTEST_CURRENT_TEST"] = __name__ + "::drive_verbs (call)"

    proc = subprocess.run([sys.executable, str(probe), _REPO],
                          cwd=str(child_dir), capture_output=True, text=True,
                          timeout=600, env=env)
    assert proc.returncode == 0, f"driver died rc={proc.returncode}: {proc.stderr[-900:]}"
    report = json.loads(proc.stdout)
    assert report.get("error") is None, f"the driver failed: {report['error']}"
    assert report["home"] == str(home), (
        f"driver ran under {report['home']!r}, not the planted home — its bootout "
        "target could have been the operator's real gui domain")
    assert report["pytest_imported"] is False, (
        "the driver imported pytest itself: it is not the spawned-child shape and "
        "every assertion below would pass for the wrong reason")
    report["exec_lines"] = (exec_log.read_text().splitlines()
                            if exec_log.exists() else [])
    return report


def test_both_sites_are_refused_in_a_child_the_suite_spawned(tmp_path):
    """The arm a conftest fixture could never cover.

    ``PYTEST_CURRENT_TEST`` crossed the fork and pytest was never imported here,
    so the only thing protecting the operator's periodic jobs in a spawned verb is
    the production fence asking ``suite_fence``. The fake's exec log is the
    evidence: it must be EMPTY for both sites.
    """
    report = _spawn(tmp_path, under_test=True)
    assert report["exec_lines"] == [], (
        "a suite-spawned verb exec'd launchctl — the operator's periodic jobs "
        f"were reachable: {report['exec_lines']}")
    assert REFUSAL in (report["log"] or ""), (
        "the refusal must be visible in the child's own daemon log: "
        f"{(report['log'] or '')[-600:]!r}")
    assert "Could not ask launchd" in (report["log"] or "")
    for stream, row in report["rows"].items():
        assert row["loaded"] is False and REFUSAL in (row["loaded_error"] or ""), (
            f"the child's status row must say the probe was refused: {stream} {row}")
    # The work the verb owns still happens: refusing the supervisor must not turn
    # into a failure to remove the plist the operator asked to remove.
    assert report["plists_left"] == [], (
        f"a refused bootout must still uninstall the plist: {report['plists_left']}")


def test_the_operator_path_still_lists_and_boots_through_the_fake(tmp_path):
    """THE CONTROL: without the marker the same code really execs both commands.

    This is what makes the zero above a fence rather than a dead branch, and it is
    the card's "keeping the operator path unchanged". It execs only this file's
    fake (PATH is planted), and it is the ONLY case here that may exec.
    """
    report = _spawn(tmp_path, under_test=False)
    lists = [f"list {ed.PERIODIC_LABEL_PREFIX}.{s}" for s in ed.PERIODIC_STREAMS]
    boots = [f"bootout gui/{os.getuid()} {ed.PERIODIC_LABEL_PREFIX}.{s}"
             for s in ed.PERIODIC_STREAMS]
    # `cmd_event_status` reads, the uninstall writes, and the driver reads once
    # more to report what the rows answered — 12 lists and 6 bootouts, in order.
    expected = lists + boots + lists
    assert report["exec_lines"] == expected, (
        "the operator path did not drive both sites through the fake — the refusal "
        f"above proves nothing: {report['exec_lines']}")
    assert REFUSAL not in (report["log"] or ""), (
        f"the operator path was refused: {(report['log'] or '')[-600:]!r}")
    assert "Could not ask launchd" not in (report["log"] or "")
    for stream, row in report["rows"].items():
        assert row["loaded"] is True and row["loaded_error"] is None, (
            f"the operator's probe was really answered: {stream} {row}")
    assert report["plists_left"] == [], (
        f"the operator path must remove the plists: {report['plists_left']}")


# --------------------------------------------------------------------------- #
# the predicate: refusing because a suite is driving is a property of the
# process AND of any process it spawned, so the site must ASK the shared copy
# --------------------------------------------------------------------------- #

def test_the_exec_site_asks_the_shared_predicate_and_not_a_private_copy(
        monkeypatch):
    """Both signals, and a call count on the shared predicate (t_fdbc24f9's ruling).

    A private ``"pytest" in sys.modules`` copy answers correctly in the suite
    process and misses every child; a copy that answers correctly while never
    ASKING ``suite_fence`` can drift from it silently. This is the same delegation
    spy t_e1c7c925 pinned on the API's write site, applied to this module's two.
    """
    from hscc_daemon import suite_fence

    calls = []
    _asked = suite_fence.under_pytest
    monkeypatch.setattr(suite_fence, "under_pytest",
                        lambda: (calls.append(1), _asked())[1])
    # Patch the FORK, never `_launchctl` itself: replacing that function replaces
    # the fence this row is pinning and every answer below would read "not refused".
    class _Cp:
        returncode, stdout, stderr = 0, "Boot-out successful: 0.\n", ""

    forks = []

    def _fake_run(argv, **kw):
        forks.append([str(a) for a in argv])
        return _Cp()

    monkeypatch.setattr(ed.subprocess, "run", _fake_run)

    for in_modules, marker, expected_refusal in (
            (True, False, True),     # the suite process, outside an item
            (False, True, True),     # a child a suite item spawned
            (True, True, True),      # a child of a suite that also imported pytest
            (False, False, False),   # a plain operator run: must never be refused
    ):
        calls.clear()
        forks.clear()
        if in_modules:
            monkeypatch.setitem(sys.modules, "pytest",
                                sys.modules.get("pytest", object()))
        else:
            monkeypatch.delitem(sys.modules, "pytest", raising=False)
        if marker:
            monkeypatch.setenv("PYTEST_CURRENT_TEST", "t_5fd62226::pin (call)")
        else:
            monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

        rc, out = ed._launchctl(_bootout_argv("dgx"))
        refused = rc is None and REFUSAL in (out or "")
        assert refused is expected_refusal, (
            f"pytest={in_modules} marker={marker}: expected refusal="
            f"{expected_refusal}, got {refused}: {out!r}")
        assert (forks == []) is expected_refusal, (
            f"pytest={in_modules} marker={marker}: the fork itself must follow the "
            f"refusal, got {forks}")
        assert calls, (
            "the exec site must ASK hscc_daemon.suite_fence, not keep a copy")


def test_the_refusal_phrase_is_the_repo_convention():
    """This module's refusal and the API's must say the same sentence.

    ``doctor_rows``, ``doctor_supervision._refused_supervision_check`` and
    ``api_cli``'s read refusal all carry this exact phrase, and hscc-bootstrap
    asserts on it verbatim. A second unit rewording it would silently split the
    convention — which is the drift t_eb768c2a warns about.
    """
    assert REFUSAL == "refused inside a pytest process"
    read_note = api_cli._refused_launchd_status()["note"]
    assert REFUSAL in read_note, (
        f"the API's read refusal no longer carries the shared phrase: {read_note!r}")
    assert api_cli._refused_launchd_status()["refused"] is True
