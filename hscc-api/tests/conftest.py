"""Shared test fixtures for hscc-api.

Puts the plugin dir on sys.path so ``import api_server`` works when this dir's
tests are run in isolation by scripts/run_tests.sh (the plugin dir name is
hyphenated and not an importable package name).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import api_server  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_chat_jobs():
    """Clear the process-global chat-job registry around every test.

    ``routes_orchestrator._jobs`` is a module-level dict that outlives any one
    test, and ``_in_flight_job(project)`` scans it to decide whether a WS
    ``stop`` frame had anything to stop. A test that leaves a queued/running
    job behind therefore makes a LATER test's "nothing in flight" assertion
    fail — which is exactly why
    ``test_ws_relay_not_noop.py::test_stop_kind_with_nothing_in_flight_is_noop``
    passed when its file ran alone and failed when the whole directory ran.

    ``_isolate_hscc`` below cannot catch this: that leak is in memory, not on
    disk. Cleared both before and after, so the registry is empty no matter
    whether the previous test cleaned up or blew up mid-way.
    """
    import routes_orchestrator as _ro

    def _clear():
        try:
            with _ro._jobs_lock:
                _ro._jobs.clear()
        except Exception:
            # Never let isolation bookkeeping fail a test run.
            pass

    _clear()
    yield
    _clear()


@pytest.fixture
def hscc_dir(tmp_path):
    """A fresh, isolated ~/.hscc stand-in for each test."""
    return str(tmp_path)


@pytest.fixture(autouse=True)
def _isolate_hscc(tmp_path, monkeypatch):
    """Redirect hscc-api's ~/.hscc write path to a per-test tmp dir.

    Belt-and-braces, mirroring hscc_daemon/tests/conftest.py::_isolate_hscc.
    An authenticated request stamps autodown activity via
    ``api_server._do_stamp_http_activity``, which writes
    ``~/.hscc/activity.json`` (computed at runtime through
    ``os.path.expanduser``). If an api test ever exercises an authenticated
    request without pinning hscc_dir, it would write the activity file into the
    operator's real ~/.hscc. This autouse fixture makes that impossible by
    redirecting every ``~/.hscc`` path off the live home dir for every test.

    Directory-wide belt-and-braces (covers ~/.hscc AS A WHOLE):
      (a) Patch ``os.path.expanduser`` so any ``~/.hscc/...`` path (module
          constant ``api_server.DEFAULT_HSCC_DIR`` or a runtime call like
          routes_project.py:75 or the activity stamp) resolves under the
          per-test tmp dir. Nothing outside ``~/.hscc`` is touched.
      (b) Overwrite the module-level ``api_server.DEFAULT_HSCC_DIR`` constant
          (baked in at import) too.
      (c) Redirect ``state.STATE_DIR`` to the tmp state dir (the periodic
          stream funnel — the activity marker is NOT written there).
    """
    base = str(tmp_path / "hscc")

    # (a) Runtime expanduser redirect for the whole ~/.hscc directory tree.
    real_hscc = os.path.expanduser("~/.hscc")
    _real_expanduser = os.path.expanduser

    def _redirect_expanduser(path):
        expanded = _real_expanduser(path)
        if expanded == real_hscc:
            return base
        if expanded.startswith(real_hscc + os.sep):
            return os.path.join(base, expanded[len(real_hscc) + 1:])
        return expanded

    monkeypatch.setattr(os.path, "expanduser", _redirect_expanduser)

    # (b) Module-level constant baked in at import — overwrite. The api tests
    # always pass an explicit hscc_dir, so DEFAULT_HSCC_DIR is only a fallback;
    # still pin it so a forgotten path can never reach the live home dir.
    monkeypatch.setattr(api_server, "DEFAULT_HSCC_DIR", base, raising=False)

    # (c) The write_state funnel.
    from hscc_daemon import state
    monkeypatch.setattr(state, "STATE_DIR",
                        os.path.join(base, "state"), raising=False)
