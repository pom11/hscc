"""t_251cd84f — tmux_chat's identity guard must refuse for BOTH pytest signals.

``flightdeck/core/tmux_chat.py::_under_pytest`` was a marker-only copy. Same class as the
sibling hscc-api seam and the same ruling, measured rather than assumed: on pytest 9.1.1
``PYTEST_CURRENT_TEST`` is unset while a module is being imported and set only from
session-fixture setup through item teardown, so a ``run_ps``/``run_tmux``/``exec_chat``
call reached from collection, a plugin hook or an atexit path passed ``_guard`` and exec'd
against the operator's tmux server and process table — which this module's own header
calls forbidden ("no suite run can exec a chat, touch a live tmux server, or block on
input()").

Widened in place; the predicate is NOT imported from another plugin. ``hscc-project``,
``hscc-api`` and ``hscc_daemon`` ship as standalone plugin dirs, and this module already
shows what a runtime sibling lookup costs (the pane matcher is loaded from the sibling dir
and a missing one is a loud refusal — right for a matcher, wrong for a fence that must
never be the thing that fails). The copy stays stdlib-only and unable to fail; its
agreement with the other copies is pinned — and any new copy discovered — from
``hscc_daemon/tests/test_suite_fence_agrees_across_packages_t_251cd84f.py``.

Red-before: the "this process, outside an item" case — marker removed, pytest loaded, the
ORIGINAL seam exec'd instead of raising. The child pair passes either way by construction
and is here so the refusals cannot be a blanket ban.

Hermetic by construction: the in-process cases replace ``subprocess.run`` before the seam
is called, and the child's only ``ps`` on ``PATH`` is the fake this file plants.
"""

import json
import os
import subprocess
import sys

import pytest

from flightdeck.core import tmux_chat

#: The plugin dir (``<repo>/hscc-project``) — what this leg's conftest puts on
#: ``sys.path``, and therefore what the child needs to import ``flightdeck.core.tmux_chat``
#: the way the deployed plugin imports it.
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

from flightdeck.core import tmux_chat

assert tmux_chat.run_ps is tmux_chat._ORIG_RUN_PS, "the child needs the ORIGINAL seam"
try:
    out["stdout"] = tmux_chat.run_ps("/dev/ttys001").strip()
except Exception as exc:
    out["raised"] = "%s: %s" % (type(exc).__name__, exc)
print(json.dumps(out))
'''


def _clear_signals(monkeypatch, *, in_modules, marker):
    """The marker must be cleared in the test BODY: pytest re-exports
    ``PYTEST_CURRENT_TEST`` for the call phase after fixtures have run."""
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

    monkeypatch.setattr(tmux_chat.subprocess, "run", fake)
    return called


@pytest.mark.parametrize("in_modules,marker", [(True, False), (False, True),
                                               (True, True)])
def test_the_original_seam_refuses_on_either_signal(monkeypatch, in_modules, marker):
    """Every pytest-shaped signal refuses through ``_guard``, before anything execs.

    The first row — pytest loaded, marker gone — is the window the marker-only copy left
    open, and it is the row this card closed here.
    """
    called = _spy_run(monkeypatch)
    _clear_signals(monkeypatch, in_modules=in_modules, marker=marker)

    with pytest.raises(RuntimeError) as exc:
        tmux_chat.run_ps("/dev/ttys001")
    assert GUARD in str(exc.value), exc.value
    assert "run_ps" in str(exc.value), (
        f"the refusal does not name the seam a caller must stub: {exc.value}")
    assert called == [], f"a refused seam still execed: {called}"


def test_a_plain_run_reaches_the_seam_so_the_refusal_is_not_a_blanket_ban(monkeypatch):
    """No pytest signal at all: the original seam is allowed. A guard that denies the
    operator's own launch door is a broken guard, not a safer one.
    """
    called = _spy_run(monkeypatch)
    _clear_signals(monkeypatch, in_modules=False, marker=False)

    assert "hermes" in tmux_chat.run_ps("/dev/ttys001")
    assert len(called) == 1 and called[0][0] == "ps", called


@pytest.mark.parametrize("under_test", [True, False])
def test_a_child_of_a_pytest_item_answers_from_the_marker(tmp_path, under_test):
    """The spawn-crossing half in the leg that owns the seam: a marked child refuses and
    never execs the planted ``ps``; an unmarked one execs it and returns its answer.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "ps"
    fake.write_text(_FAKE_PS, encoding="utf-8")
    fake.chmod(0o755)
    exec_log = bindir / "ps.calls"
    home = tmp_path / "home"
    home.mkdir()
    probe = tmp_path / "probe_tmux_chat_run_ps.py"
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
