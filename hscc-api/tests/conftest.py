"""Shared test fixtures for hscc-api.

Puts the plugin dir on sys.path so ``import api_server`` works when this dir's
tests are run in isolation by scripts/run_tests.sh (the plugin dir name is
hyphenated and not an importable package name).
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import api_server  # noqa: E402


# Seconds one boundary drain may spend signalling + joining turn workers.
_DRAIN_TIMEOUT_S = 2.0


def isolation_boundary(stamp=False):
    """Run one test-boundary over the process-global chat-job state.

    THE canonical boundary: the autouse fixture below calls it at every setup
    and teardown, and the regression tests import it so they exercise the
    boundary the suite actually runs (a copy could drift from the real thing and
    then prove nothing).

    Order is the fix and must not be reordered (t_163fa09f):
      1. DRAIN — signal every live turn worker (retire event + cancel its job
         through the real stop path) and JOIN it, so any write it was going to
         make has already happened before the store is emptied;
      2. CLEAR the job store — a mop for terminal leftovers, not the defence;
      3. RESET the event stores (process-global relay state too);
      4. ADVANCE the job generation; when ``stamp`` is set, also stamp the
         CALLING thread with the new epoch, so workers the test spawns inherit
         it and may register, while any straggler born before the boundary is
         rejected at ``_new_job`` even if the join above timed out (defence in
         depth).

    Returns the names of workers the drain could not join (empty in the normal
    case) so callers can assert on them.
    """
    import threading

    import routes_orchestrator as _ro
    import session_event

    try:
        stragglers = _ro.drain_workers(timeout=_DRAIN_TIMEOUT_S)
    except Exception:
        # Never let isolation bookkeeping fail a test run.
        stragglers = []
    try:
        with _ro._jobs_lock:
            _ro._jobs.clear()
    except Exception:
        pass
    try:
        session_event.reset_stores()
    except Exception:
        pass
    try:
        gen = _ro.advance_job_generation()
        if stamp:
            setattr(threading.current_thread(), "_hscc_job_generation", gen)
    except Exception:
        pass
    return stragglers


@pytest.fixture(autouse=True)
def _isolate_chat_jobs():
    """Quiesce ALL process-global chat-job state around every test.

    ``routes_orchestrator._jobs`` is a module-level dict that outlives any one
    test, and ``_in_flight_job(project)`` scans it to decide whether a WS
    ``stop`` frame had anything to stop. Historically
    ``test_ws_relay_not_noop.py::test_stop_kind_with_nothing_in_flight_is_noop``
    passed alone and failed in full-directory runs because an EARLIER test's
    relay worker thread registered a live ``hscc`` job AFTER this fixture had
    cleared the store (instrumented proof:
    docs/audits/ws_relay_stop_noop_flake_t_163fa09f.md). The t_7cc2e7d5 fix —
    clearing before AND after — could not work: clearing a dict cannot stop an
    unjoined thread from writing it again afterwards.

    The boundary (see :func:`isolation_boundary`) drains the writers BEFORE it
    clears, and retires the epoch so late writes are rejected outright.
    """
    isolation_boundary(stamp=True)   # test body runs stamped with the live epoch
    yield
    isolation_boundary()             # drain + clear + retire this test's epoch
    # Un-stamp the (reused, process-wide) main thread so nothing outside a test
    # body can ever carry a stale epoch into a later _new_job call.
    import threading
    setattr(threading.current_thread(), "_hscc_job_generation", None)


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
