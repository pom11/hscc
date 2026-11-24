"""t_251cd84f — the ``main`` CLI's tmux guard must answer what the package's predicate
answers, in this process AND outside an item.

``hscc_daemon/main_cli.py::_under_pytest`` was a marker-only copy
(``bool(os.environ.get("PYTEST_CURRENT_TEST"))``). The marker is the half that crosses a
spawn, so the spawned-child case this repo cares about was already covered here — what
was not covered is every phase *outside* an item. Measured on this pytest (9.1.1): the
marker is **unset while a module is being imported** and set only from session-fixture
setup through item teardown. A live ``subprocess.run([binary, ...])`` of the operator's
tmux reached during collection, from a plugin hook or from an atexit path therefore
answered "no suite" and exec'd.

RULING (this site): route it through ``hscc_daemon.suite_fence``. The predicate is
already the package's own (t_fdbc24f9 ruled one per package and pinned the copies to
agree), a second copy inside the same package is exactly the drift that ruling exists to
stop, and widening costs nothing a test owns: the guard next to it fires only for the
ORIGINAL seam with the DEFAULT binary, so a test that injected a runner or pointed
``repl_inject.TMUX_BIN`` at a fake keeps its path.

Two halves, one file:

* the predicate at the site, for all four signal combinations, with the ask pinned to
  the shared predicate (a delegate-spy, so the answers stay the package's and a broken
  predicate cannot hide behind the spy);
* the spawned-child shape the guard exists for — a marked child that injects nothing
  must refuse and must not exec the planted ``tmux``, and the unmarked control must
  really exec it, so the refusal guards a branch the operator still takes.

Hermetic by construction: the in-process half never reaches a real ``subprocess.run``
(it is replaced before the call), and the child's only ``tmux`` on ``PATH`` is the fake
this file plants, with ``TMUX_TMPDIR`` pointing at an empty planted directory so the
socket resolution cannot look at ``/tmp`` either.
"""

import json
import os
import subprocess
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: The phrase the hermeticity guard carries (also in ``repl_inject``/``tmux_chat``).
GUARD = "disabled under pytest"

#: The planted tmux: records that it really exec'd, then answers one list-sessions call.
_FAKE_TMUX = (
    "#!/bin/sh\n"
    'if [ -n "${FAKE_TMUX_LOG:-}" ]; then printf \'%s\\n\' "$@" >> '
    '"$FAKE_TMUX_LOG"; fi\n'
    "printf 'hscc-main\\t1\\t100\\n'\n"
)

#: The child: call the seam exactly as a caller that injected nothing calls it, and
#: report which branch it took.
_CHILD = r'''
import json, os, sys

repo = sys.argv[1]
sys.path.insert(0, repo)

out = {"home": os.environ.get("HOME"),
       "pytest_imported": "pytest" in sys.modules,
       "marker": os.environ.get("PYTEST_CURRENT_TEST"),
       "raised": None, "result": None}

import hscc_daemon.main_cli as mc

assert mc.run_tmux is mc._ORIG_RUN_TMUX, "the child must exercise the ORIGINAL seam"

try:
    # `repl=object()` is the shape of "no hscc-api payload to read TMUX_BIN from", so
    # the binary stays the default and the guard's identity test is the live question.
    rc, stdout, stderr = mc.run_tmux(["list-sessions", "-F", "#n"],
                                     socket_args=(), repl=object())
    out["result"] = [rc, stdout.strip(), (stderr or "").strip()]
except Exception as exc:
    out["raised"] = "%s: %s" % (type(exc).__name__, exc)

print(json.dumps(out))
'''


def _plant(tmp_path, tag: str):
    """A ``HOME``, a ``PATH`` whose only ``tmux`` is the fake, and an empty tmpdir."""
    home = tmp_path / "home"
    (home / ".hscc").mkdir(parents=True, exist_ok=True)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "tmux"
    fake.write_text(_FAKE_TMUX, encoding="utf-8")
    fake.chmod(0o755)
    tmpdir = tmp_path / "tmux-tmp"
    tmpdir.mkdir(exist_ok=True)
    return home, bindir, bindir / f"tmux.calls.{tag}", tmpdir


def _spawn(tmp_path, *, under_test: bool, tag: str) -> dict:
    home, bindir, exec_log, tmpdir = _plant(tmp_path, tag)
    probe = tmp_path / "probe_main_cli_run_tmux.py"
    probe.write_text(_CHILD, encoding="utf-8")

    env = dict(os.environ)
    env.update(HOME=str(home), XDG_CACHE_HOME=str(tmp_path / "cache"),
               PYTHONDONTWRITEBYTECODE="1", TMUX_TMPDIR=str(tmpdir),
               PATH=os.pathsep.join([str(bindir), "/usr/bin", "/bin"]),
               FAKE_TMUX_LOG=str(exec_log))
    for var in ("PYTEST_CURRENT_TEST", "PYTEST_VERSION", "PYTEST_XDIST_WORKER",
                "TMUX", "HSCC_DIR", "HSCC_HOME", "HERMES_HOME", "HERMES_AGENT_PATH",
                "HERMES_REAL_HOME", "HERMES_PROFILE", "HSCC_TESTS_ENFORCE"):
        env.pop(var, None)
    if under_test:
        env["PYTEST_CURRENT_TEST"] = __name__ + "::probe (call)"

    proc = subprocess.run([sys.executable, str(probe), _REPO],
                          cwd=str(tmp_path), capture_output=True, text=True,
                          timeout=600, env=env)
    assert proc.returncode == 0, f"probe died rc={proc.returncode}: {proc.stderr[-900:]}"
    report = json.loads(proc.stdout)
    assert report["home"] == str(home), report["home"]
    assert report["pytest_imported"] is False, (
        "the probe imported pytest itself: not the spawned-child shape, and the "
        "assertions below would pass for the wrong reason")
    report["exec_log"] = exec_log
    return report


def test_the_guard_follows_the_shared_predicate_for_every_signal_combination(
        monkeypatch):
    """Red-before on the ``sys.modules``-only row: the suite process outside an item.

    The other three rows pass either way — a plain run must never be refused, and the
    two rows where the marker is set are what the marker-only copy already answered.
    """
    import hscc_daemon.main_cli as mc
    from hscc_daemon import suite_fence

    asked, execed = [], []
    _real_fence = suite_fence.under_pytest
    monkeypatch.setattr(
        suite_fence, "under_pytest",
        lambda: (asked.append(1), _real_fence())[1])   # delegate: answers stay theirs
    monkeypatch.setattr(
        mc.subprocess, "run",
        lambda argv, **kw: (execed.append([str(a) for a in argv]),
                            type("CP", (), {"returncode": 0, "stdout": "s\n",
                                            "stderr": ""})())[1])

    for in_modules, marker, expected_guard in (
            (True, False, True),     # this process, OUTSIDE an item (collection) — the
                                     # row the marker-only copy left unguarded
            (False, True, True),     # a child a suite item spawned
            (True, True, True),      # a child of a suite that also imported pytest
            (False, False, False),   # a plain operator run: must never be refused
    ):
        asked.clear()
        execed.clear()
        if in_modules:
            monkeypatch.setitem(sys.modules, "pytest",
                                sys.modules.get("pytest", object()))
        else:
            monkeypatch.delitem(sys.modules, "pytest", raising=False)
        if marker:
            monkeypatch.setenv(suite_fence.PYTEST_MARKER, "some.module::case (call)")
        else:
            monkeypatch.delenv(suite_fence.PYTEST_MARKER, raising=False)

        if expected_guard:
            try:
                mc.run_tmux(["list-sessions"], socket_args=(), repl=object())
            except mc.MainPaneError as exc:
                assert GUARD in str(exc), f"not the hermeticity refusal: {exc}"
            else:
                raise AssertionError(
                    f"the original seam execed with pytest={in_modules} "
                    f"marker={marker} — the operator's tmux server is live state")
            assert execed == [], (
                f"a refused tmux call still execed with pytest={in_modules} "
                f"marker={marker}: {execed}")
        else:
            rc, _out, _err = mc.run_tmux(["list-sessions"], socket_args=(),
                                         repl=object())
            assert rc == 0 and len(execed) == 1, (
                f"a plain run was refused (pytest={in_modules}, marker={marker}): "
                f"{execed}")
        assert asked, (
            "main_cli decided its guard without asking suite_fence.under_pytest — a "
            "module-local copy of the test is the drift t_fdbc24f9 rules out")


def test_a_spawned_child_that_injected_nothing_refuses_and_execs_no_tmux(tmp_path):
    """The guard bites in the child shape, and refuses before anything execs.

    Pre-fix this pair already passed (the marker crosses the spawn) — it is here because
    the widened predicate must not turn the guard into a blanket ban, and because a
    refusal that still exec'd would pass an env-only assertion.
    """
    r = _spawn(tmp_path, under_test=True, tag="marked")
    assert r["raised"] and GUARD in r["raised"], (
        f"a suite-spawned caller reached the operator's tmux server: {r['raised']}")
    assert "inject a runner" in r["raised"], (
        f"the refusal does not name what a caller must supply: {r['raised']}")
    assert r["result"] is None
    assert not r["exec_log"].exists(), (
        "the planted tmux was exec'd by a suite-spawned caller that injected nothing")


def test_without_the_marker_the_same_child_runs_the_command(tmp_path):
    """Control: the refusal above is a fence, not a dead verb.

    The planted binary really execs and its canned list-sessions line comes back, which
    is what makes the zero in the marked half evidence rather than an artefact.
    """
    r = _spawn(tmp_path, under_test=False, tag="control")
    assert r["raised"] is None, f"the guard fired on a plain run: {r['raised']}"
    assert r["result"] and r["result"][0] == 0 and "hscc-main" in r["result"][1], (
        f"the control never ran the planted tmux: {r['result']}")
    assert r["exec_log"].exists() and "list-sessions" in r["exec_log"].read_text(), (
        "the planted tmux never exec'd on the ambient path")
