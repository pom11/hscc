"""Hermeticity guard for scripts/tests (GAP-B-4, t_87663869).

``dep_pr_watcher.main()`` and ``escalate_watcher_run.main()`` now stamp every
pass through ``hscc_daemon.state.write_state`` — and ``state.STATE_DIR`` is a
module-level constant bound from HOME at IMPORT time, so a test that patches
HOME afterwards still writes the OPERATOR's live ``~/.hscc/state`` (the same
trap bootstrap's conftest documents for HERMES_HOME-derived constants).

Two redirects, per test, autouse:

  * ``hscc_daemon.state.STATE_DIR`` -> tmp, when the package is importable
    (the deployed-plugins case — this host, CI). Patched attribute is what
    write_state/read_state resolve at CALL time, so it wins over the import-
    time binding.
  * ``HOME`` -> tmp, always. Covers the other half: when the package import
    FAILS (a bare checkout, a faked sys.modules), cron_watchers falls back to
    loading state.py standalone — and a freshly loaded state.py derives
    STATE_DIR from HOME at load time. Redirecting HOME covers both orderings.

Individual tests that want their own stamp dir keep monkeypatching
``hscc_daemon.state.STATE_DIR`` themselves — monkeypatch runs after this
fixture's setup, so it wins.
"""

import os
import sys
import tempfile

import pytest

# The scripts dir itself (bare imports: `import dep_pr_watcher`, and the
# scripts/../hscc_daemon sibling the watchers resolve via __file__). Tests in
# this dir used to insert it themselves; doing it here once keeps every module
# importable regardless of collection order.
_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

# The repo root, so `hscc_daemon` resolves to the CHECKOUT (not only to a
# deployed ~/.hermes/plugins copy) for tests that assert against repo truth —
# the sibling path via __file__ already wins inside the watchers themselves.
_ROOT = os.path.dirname(_SCRIPTS)


@pytest.fixture(autouse=True)
def _watcher_stamps_cannot_reach_live_state(monkeypatch):
    """Redirect HOME + the canonical STATE_DIR into a throwaway tree."""
    sandbox = tempfile.mkdtemp(prefix="scripts-tests-state-")
    monkeypatch.setenv("HOME", sandbox)
    monkeypatch.delenv("HSCC_DIR", raising=False)
    try:
        from hscc_daemon import state as _st
        monkeypatch.setattr(_st, "STATE_DIR",
                            os.path.join(sandbox, ".hscc", "state"))
    except Exception:
        pass  # covered by the HOME redirect at standalone-load time
    yield os.path.join(sandbox, ".hscc", "state")
