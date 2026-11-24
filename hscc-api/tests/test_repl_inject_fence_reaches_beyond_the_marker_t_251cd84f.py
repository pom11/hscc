"""t_251cd84f — repl_inject's hermeticity seam must refuse for BOTH pytest signals, not
only the marker.

``repl_inject._under_pytest`` was marker-only (``bool(os.environ.get("PYTEST_CURRENT_TEST"))``).
The marker is the half that crosses a spawn, so the child case was covered — what was
not was this process OUTSIDE an item. Measured on pytest 9.1.1: the marker is unset while
a module is being imported and set only from session-fixture setup through item teardown.
A ``run_ps``/``run_tmux`` call reached during collection, from a plugin hook or from an
atexit path therefore answered "no suite", passed the identity check, and exec'd against
the operator's panes and process table — the very reads the module's own guard text
forbids ("no test may touch a live session").

RULING (this site, per site and not swept): widen this copy to both halves; do NOT import
the daemon's ``suite_fence``. ``hscc-api`` and ``hscc_daemon`` are deployed as standalone
plugin dirs and share no namespace, so a cross-plugin import would make a safety line
failable — and the copy stays stdlib-only and unable to fail. The agreement between the
copies is pinned from the daemon leg
(``hscc_daemon/tests/test_suite_fence_agrees_across_packages_t_251cd84f.py``), which also
discovers any new copy in the tree; this file pins the behaviour the copy is for.

Red-before: the "this process, outside an item" case — with the marker removed and pytest
loaded, the ORIGINAL seam exec'd instead of refusing. The child pair passes either way by
construction and is here so the refusals above cannot be a blanket ban.

Hermetic by construction: the in-process cases replace ``subprocess.run`` before the seam
is called, and the child's only ``ps`` on ``PATH`` is the fake this file plants.
"""

import json
import os
import subprocess
import sys

import pytest

import repl_inject

#: The plugin dir (``<repo>/hscc-api``) — what this leg's conftest puts on
#: ``sys.path``, and therefore what the child needs to ``import repl_inject`` the way
#: the deployed plugin imports it.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GUARD = "disabled under pytest"

_FAKE_PS = (
    "#!/bin/sh\n"
    'if [ -n "${FAKE_PS_LOG:-}" ]; then printf \'%s\\n\' "$@" >> "$FAKE_PS_LOG"; fi\n'
    "printf 'hermes -p general chat\\n'\n"
)

#: The child: call the ORIGINAL seam the way a caller that stubbed nothing calls it.
_CHILD = r'''
import json, os, sys

sys.path.insert(0, sys.argv[1])
out = {"home": os.environ.get("HOME"),
       "pytest_imported": "pytest" in sys.modules,
       "marker": os.environ.get("PYTEST_CURRENT_TEST"),
       "raised": None, "stdout": None}

import repl_inject

assert repl_inject.run_ps is repl_inject._ORIG_RUN_PS, "the child needs the ORIGINAL seam"
try:
    out["stdout"] = repl_inject.run_ps("/dev/ttys001").strip()
except Exception as exc:
    out["raised"] = "%s: %s" % (type(exc).__name__, exc)
print(json.dumps(out))
'''


def _clear_signals(monkeypatch, *, in_modules, marker):
    """Set the two signals explicitly — the marker MUST be cleared in the body, because
    pytest re-exports ``PYTEST_CURRENT_TEST`` for the call phase after fixtures run."""
    if in_modules:
        monkeypatch.setitem(sys.modules, "pytest",
                            sys.modules.get("pytest", object()))
    else:
        monkeypatch.delitem(sys.modules, "pytest", raising=False)
    if marker:
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "some.module::case (call)")
    else:
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)


def _spy_run(monkeypatch):
    called = []

    def fake(cmd, **kw):
        called.append([str(c) for c in cmd])
        return type("CP", (), {"returncode": 0, "stdout": "hermes chat\n",
                               "stderr": ""})()

    monkeypatch.setattr(repl_inject.subprocess, "run", fake)
    return called


@pytest.mark.parametrize("in_modules,marker", [(True, False), (False, True),
                                               (True, True)])
def test_the_original_seam_refuses_on_either_signal(monkeypatch, in_modules, marker):
    """Every pytest-shaped signal refuses, through the ORIGINAL seam, before anything
    execs. The first row (pytest loaded, marker gone) is the hole this card closed.
    """
    called = _spy_run(monkeypatch)
    _clear_signals(monkeypatch, in_modules=in_modules, marker=marker)

    with pytest.raises(repl_inject.InjectError) as exc:
        repl_inject.run_ps("/dev/ttys001")
    assert GUARD in str(exc.value), exc.value
    assert "run_ps" in str(exc.value), (
        f"the refusal does not name the seam a caller must stub: {exc.value}")
    assert called == [], f"a refused seam still execed: {called}"


def test_a_plain_run_reaches_the_seam_so_the_refusal_is_not_a_blanket_ban(monkeypatch):
    """No pytest signal at all: the original seam is allowed, because a fence that
    denies the operator's own ``hscc orch inject`` is a broken fence.
    """
    called = _spy_run(monkeypatch)
    _clear_signals(monkeypatch, in_modules=False, marker=False)

    assert "hermes" in repl_inject.run_ps("/dev/ttys001")
    assert len(called) == 1 and called[0][0] == "ps", called


@pytest.mark.parametrize("under_test", [True, False])
def test_a_child_of_a_pytest_item_answers_from_the_marker(tmp_path, under_test):
    """The spawn-crossing half, in the leg that owns the seam: a marked child refuses
    and never execs the planted ``ps``; an unmarked one execs it and returns its answer.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "ps"
    fake.write_text(_FAKE_PS, encoding="utf-8")
    fake.chmod(0o755)
    exec_log = bindir / "ps.calls"
    home = tmp_path / "home"
    home.mkdir()
    probe = tmp_path / "probe_repl_inject_run_ps.py"
    probe.write_text(_CHILD, encoding="utf-8")

    env = dict(os.environ)
    env.update(HOME=str(home), XDG_CACHE_HOME=str(tmp_path / "cache"),
               PYTHONDONTWRITEBYTECODE="1",
               PATH=os.pathsep.join([str(bindir), "/usr/bin", "/bin"]),
               FAKE_PS_LOG=str(exec_log))
    for var in ("PYTEST_CURRENT_TEST", "PYTEST_VERSION", "PYTEST_XDIST_WORKER", "TMUX",
                "TMUX_TMPDIR", "HSCC_DIR", "HSCC_HOME", "HERMES_HOME",
                "HERMES_AGENT_PATH", "HERMES_REAL_HOME", "HERMES_PROFILE",
                "HSCC_TESTS_ENFORCE"):
        env.pop(var, None)
    if under_test:
        env["PYTEST_CURRENT_TEST"] = __name__ + "::probe (call)"

    proc = subprocess.run([sys.executable, str(probe), _REPO], cwd=str(tmp_path),
                          capture_output=True, text=True, timeout=600, env=env)
    assert proc.returncode == 0, f"probe died rc={proc.returncode}: {proc.stderr[-600:]}"
    report = json.loads(proc.stdout)
    assert report["pytest_imported"] is False, (
        "the probe imported pytest itself — not the spawned-child shape")

    if under_test:
        assert report["raised"] and GUARD in report["raised"], (
            f"a suite-spawned caller reached the operator's process table: "
            f"{report['raised']}")
        assert not exec_log.exists(), "the planted ps was exec'd by a suite-spawned child"
    else:
        assert report["raised"] is None, f"the guard fired on a plain run: {report}"
        assert "hermes" in (report["stdout"] or ""), report
        assert exec_log.exists() and "-t" in exec_log.read_text(), (
            "the planted ps never exec'd, so the refusal above guards nothing")
