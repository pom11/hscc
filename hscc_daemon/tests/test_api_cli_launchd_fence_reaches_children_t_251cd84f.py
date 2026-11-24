"""t_251cd84f — ``launchd_status``'s in-suite fence must bite in a child the suite
SPAWNS, not only in the process that imported pytest.

``hscc_daemon/api_cli.py::launchd_status`` fenced the live supervisor probe with
``_run_probe is None and "pytest" in sys.modules`` — a process-local test, the same
shape t_fdbc24f9 fixed for the node-pause-expiry row. The default target of the probe
is the OPERATOR's own gui domain (``gui/<uid>/com.hscc.api``), and the caller that
reaches it without a seam is not necessarily the suite process: a probe child of a
pytest item — which is how ``hscc api status`` gets exercised from a spawn-based guard,
and how the verb itself is reached from a script a test writes — never imported pytest,
so the fence answered for a process that was never involved and the child ran
``launchctl print`` against the operator's session.

Measured before the fix, with a fake ``launchctl`` first on the child's ``PATH`` (so
the measurement never touched the real service manager): the marked child exec'd the
fake once with ``["launchctl", "print", "gui/<uid>/com.hscc.api"]`` and got back a
parsed verdict — pid and state — instead of the refusal ``_refused_launchd_status``
exists to hand out.

Both directions, one planted home, same interpreter, ``PYTEST_CURRENT_TEST`` the only
variable (the #732/#761 shape, because a control is what stops a refusal from guarding
a branch nothing can take):

* marked child — the probe is refused, the live seam (``_launchctl_run``) is reached
  ZERO times and the fake binary is never exec'd;
* unmarked child — the same call execs the fake and parses its pid and state, which is
  the proof the zero above is a fence rather than a code path nothing can enter.

The child reports WHICH BRANCH IT TOOK — the argv the live seam was called with plus a
log line the fake binary writes when it really execs — and loads the module by ordinary
``import`` (not ``runpy.run_path``, whose throwaway namespace makes a spy on a module
attribute count nothing — t_0d991e1d measured that).

The second half of the file pins the predicate itself at this site: refusing because a
suite is driving is a property of the process AND of any process it spawned, so all
four combinations of ``pytest in sys.modules`` × the marker must answer the way
``hscc_daemon.suite_fence`` answers, and the site must ASK the shared predicate rather
than keep a private copy that can drift from it (the ruling t_fdbc24f9 carried over).

Hermetic by construction: nothing here execs the real ``launchctl`` — every call goes
through a spy on the module's own seam, and the only binary any child can find is the
fake this file plants. The canned output is synthesised (no operator paths) and the pid
it reports is never signalled.
"""

import json
import os
import subprocess
import sys

#: ``<repo>/hscc_daemon/tests/<this file>`` -> the repo root, which is what the child
#: puts on ``sys.path`` so ``import hscc_daemon`` resolves the way the CLI does.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: The phrase the repo's refusal convention always carries (``doctor_rows``,
#: ``_refused_transport``, ``_refused_supervision_check``, ``_refused_launchd_status``).
REFUSAL = "refused inside a pytest process"

#: The canned verdict the fake binary prints. Tab-indented because
#: ``_LAUNCHCTL_STATE_RE`` anchors the job state on ONE tab (a nested section's state
#: must not be read as the job's), and deliberately free of any real path.
FAKE_PID = "42424"
_FAKE_LAUNCHCTL = (
    "#!/bin/sh\n"
    'if [ -n "${FAKE_LAUNCHCTL_LOG:-}" ]; then printf \'%s\\n\' "$@" >> '
    '"$FAKE_LAUNCHCTL_LOG"; fi\n'
    "printf 'GuiJob: com.hscc.api\\n'\n"
    "printf '\\ttype = LaunchAgent\\n'\n"
    "printf '\\tstate = running\\n'\n"
    "printf '\\n'\n"
    "printf '\\tpid = " + FAKE_PID + "\\n'\n"
)

#: The child: spy the ONE seam that can exec launchctl, call the probe the way a
#: spawned caller calls it (no seam injected), and report which branch it took.
_CHILD = r'''
import json, os, sys

repo = sys.argv[1]
sys.path.insert(0, repo)

out = {"home": os.environ.get("HOME"),
       "pytest_imported": "pytest" in sys.modules,
       "marker": os.environ.get("PYTEST_CURRENT_TEST"),
       "launchctl_calls": [], "status": None, "note": None, "error": None}

try:
    import hscc_daemon.api_cli as api_cli
except Exception as exc:                       # a child that cannot load the module
    out["error"] = "import: %s: %s" % (type(exc).__name__, exc)
    print(json.dumps(out))
    raise SystemExit(0)

_real_run = api_cli._launchctl_run


def _spy(argv, timeout=10):
    out["launchctl_calls"].append([str(a) for a in argv])
    return _real_run(argv, timeout=timeout)


api_cli._launchctl_run = _spy

try:
    st = api_cli.launchd_status()             # exactly what a spawned caller passes
    out["status"] = {k: st.get(k) for k in
                     ("available", "loaded", "pid", "state", "refused")}
    out["note"] = (st.get("note") or "")[:400]
except Exception as exc:
    out["error"] = "%s: %s" % (type(exc).__name__, exc)

print(json.dumps(out))
'''


def _plant(tmp_path, tag: str = "run"):
    """A ``HOME`` plus a ``PATH`` whose only ``launchctl`` is this file's fake.

    The exec log is per ``tag`` so the two halves of a test cannot inherit each
    other's proof (a log left by a control would make the marked half's "never
    exec'd" assertion vacuous).
    """
    home = tmp_path / "home"
    (home / ".hscc").mkdir(parents=True, exist_ok=True)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "launchctl"
    fake.write_text(_FAKE_LAUNCHCTL, encoding="utf-8")
    fake.chmod(0o755)
    return home, bindir, bindir / f"launchctl.calls.{tag}"


def _spawn(tmp_path, *, under_test: bool, tag: str = "run") -> dict:
    """Call the probe from a child of a pytest item and report which branch it took.

    This is the CI shape: ``scripts/run_tests.sh`` seals ``HOME`` and every child a leg
    spawns inherits that seal, so the only thing that distinguishes the two halves here
    is the marker pytest exports per item — the same channel the operator's own
    ``hscc api status`` never carries.
    """
    home, bindir, exec_log = _plant(tmp_path, tag)
    probe = tmp_path / "probe_launchd_status.py"
    probe.write_text(_CHILD, encoding="utf-8")

    env = dict(os.environ)
    env.update(HOME=str(home), XDG_CACHE_HOME=str(tmp_path / "cache"),
               PYTHONDONTWRITEBYTECODE="1",
               PATH=os.pathsep.join([str(bindir), "/usr/bin", "/bin"]),
               FAKE_LAUNCHCTL_LOG=str(exec_log))
    # The variable under test must be the ONLY pytest signal the child can see, and no
    # ambient override may point the probe somewhere else.
    for var in ("PYTEST_CURRENT_TEST", "PYTEST_VERSION", "PYTEST_XDIST_WORKER",
                "HSCC_DIR", "HSCC_HOME", "HSCC_LAUNCHD_PROBE", "HERMES_HOME",
                "HERMES_AGENT_PATH", "HERMES_REAL_HOME", "HERMES_PROFILE",
                "HSCC_TESTS_ENFORCE"):
        env.pop(var, None)
    if under_test:
        env["PYTEST_CURRENT_TEST"] = __name__ + "::probe (call)"

    proc = subprocess.run([sys.executable, str(probe), _REPO],
                          cwd=str(tmp_path), capture_output=True, text=True,
                          timeout=600, env=env)
    assert proc.returncode == 0, f"probe died rc={proc.returncode}: {proc.stderr[-900:]}"
    report = json.loads(proc.stdout)
    assert report.get("error") is None, f"the probe failed: {report['error']}"
    assert report["home"] == str(home), (
        f"the probe ran under {report['home']!r}, not the planted home — its probe "
        "target could have been the operator's real gui domain")
    assert report["pytest_imported"] is False, (
        "the probe imported pytest itself: it is not the spawned-child shape and every "
        "assertion below would pass for the wrong reason")
    report["exec_log"] = exec_log
    return report


def _status(report, label):
    got = report.get("status")
    assert got is not None, f"the child never produced a {label} verdict: {report}"
    return got


def test_a_spawned_child_refuses_the_live_probe_and_execs_no_launchctl(tmp_path):
    """The hole this card names: a marked child reached the operator's gui domain.

    Pre-fix the fence here read only ``sys.modules``, so this child — a fresh
    interpreter, pytest never imported — took the live path. Both proofs are required:
    the seam was never called in-process AND the planted binary was never exec'd, so a
    refusal that merely stopped parsing cannot masquerade as a fence.
    """
    r = _spawn(tmp_path, under_test=True)
    got = _status(r, "launchd_status()")
    assert got["refused"] is True, (
        f"a suite-spawned child took the live supervisor probe: {got}")
    assert got["available"] is False, (
        f"a refused probe must not claim the supervisor answered: {got}")
    assert REFUSAL in (r["note"] or ""), (
        f"the refusal is not the visible one the convention promises: {r['note']!r}")
    assert "inject" in (r["note"] or ""), (
        "the refusal does not name what a caller must supply — a refusal nobody can "
        f"satisfy is a permanent 'unknown': {r['note']!r}")
    assert r["launchctl_calls"] == [], (
        f"the live seam was reached {len(r['launchctl_calls'])} time(s) by a "
        "suite-spawned caller even though the probe reported a refusal")
    assert not r["exec_log"].exists(), (
        "the fake launchctl was exec'd by a suite-spawned caller: "
        f"{r['exec_log'].read_text() if r['exec_log'].exists() else ''}")


def test_the_refusal_names_the_target_it_refused_to_ask(tmp_path):
    """A refusal that hides WHICH domain it stayed out of is not auditable.

    The probe's target is the operator's own ``gui/<uid>`` session, so the guard's
    value depends on the argv being exactly the one the live path would have used —
    pinned here from the control half's own report rather than restated as a string.
    """
    from hscc_daemon import api_launchd

    marked = _spawn(tmp_path, under_test=True, tag="marked")
    control = _spawn(tmp_path, under_test=False, tag="control")
    assert marked["launchctl_calls"] == []
    expected = control["launchctl_calls"]
    assert expected == [["launchctl", "print",
                         f"gui/{os.getuid()}/{api_launchd.API_LABEL}"]], (
        f"the live path's argv changed shape: {expected}")


def test_without_the_marker_the_same_child_asks_the_supervisor(tmp_path):
    """Control: the refusal above bites, and the marker is what makes it.

    Same probe, same planted home, only ``PYTEST_CURRENT_TEST`` removed — the operator's
    ``hscc api status`` must still get a real answer, so the fake binary must really
    exec and its canned print output must really be parsed. If this ever reports a
    refusal, the fence has started denying the verb it guards.
    """
    r = _spawn(tmp_path, under_test=False)
    got = _status(r, "launchd_status()")
    assert got.get("refused") is not True, (
        f"the probe refused with no pytest in sight — the fence now denies a human "
        f"CLI run: {got}")
    assert got["available"] is True and got["loaded"] is True, (
        f"the control did not read the fake launchctl: {got}")
    assert str(got["pid"]) == FAKE_PID and got["state"] == "running", (
        f"the control exec'd nothing it parsed, so the zero in the marked child "
        f"guards a branch nothing can take: {got}")
    assert len(r["launchctl_calls"]) == 1
    assert r["exec_log"].exists() and "print" in r["exec_log"].read_text(), (
        "the fake launchctl never exec'd on the ambient path")


def test_the_site_asks_the_shared_predicate_for_every_signal_combination(
        monkeypatch, tmp_path):
    """All four combinations must answer the way ``suite_fence`` answers — and the
    refusal must arrive from the shared predicate, not from a private copy.

    The marker-only row is the spawned child and the ``sys.modules``-only row is the
    suite process itself outside an item (module import during collection leaves the
    marker unset — measured on this pytest). A copy here that drops either half fails
    this, and a copy that answers correctly while never asking the shared predicate
    fails the call count, which is the drift t_fdbc24f9 rules out.
    """
    import hscc_daemon.api_cli as api_cli
    from hscc_daemon import suite_fence

    calls = []
    # Record the ask, then DELEGATE to the real predicate: the answers below are the
    # package's, never a restatement of it in this file (a spy that answered for
    # suite_fence would let a broken predicate pass while proving only the call).
    _asked = suite_fence.under_pytest
    monkeypatch.setattr(suite_fence, "under_pytest",
                        lambda: (calls.append(1), _asked())[1])
    seen = []
    monkeypatch.setattr(api_cli, "_launchctl_run",
                        lambda argv, timeout=10: (seen.append(list(argv)),
                                                  (0, "\tpid = 1\n"))[1])

    for in_modules, marker, expected_refusal in (
            (False, False, False),   # a plain operator run: must not fire
            (False, True, True),     # a child a suite item spawned
            (True, False, True),     # the suite process itself, between items
            (True, True, True),      # a child of a suite that also imported pytest
    ):
        calls.clear()
        seen.clear()
        if in_modules:
            monkeypatch.setitem(sys.modules, "pytest",
                                sys.modules.get("pytest", object()))
        else:
            monkeypatch.delitem(sys.modules, "pytest", raising=False)
        if marker:
            monkeypatch.setenv(suite_fence.PYTEST_MARKER, "some.module::case (call)")
        else:
            monkeypatch.delenv(suite_fence.PYTEST_MARKER, raising=False)

        st = api_cli.launchd_status()

        assert calls, (
            "launchd_status() decided its fence without asking suite_fence.under_pytest"
            " — a module-local copy of the test is the drift this card rules out")
        if expected_refusal:
            assert st.get("refused") is True, (
                f"no refusal with pytest={in_modules} marker={marker}: {st}")
            assert seen == [], (
                f"the live seam was reached with pytest={in_modules} marker={marker}")
        else:
            assert st.get("refused") is not True, (
                f"the fence fired on a plain run (pytest={in_modules}, "
                f"marker={marker}): {st}")
            assert len(seen) == 1, (
                f"a plain run never reached the supervisor: {seen}")
