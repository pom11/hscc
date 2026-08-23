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


@pytest.fixture
def hscc_dir(tmp_path):
    """A fresh, isolated ~/.hscc stand-in for each test."""
    return str(tmp_path)


@pytest.fixture(autouse=True)
def _isolate_hscc(tmp_path, monkeypatch):
    """Redirect hscc-api's ~/.hscc write path to a per-test tmp dir.

    Belt-and-braces, mirroring hscc_daemon/tests/conftest.py::_isolate_hscc.
    An authenticated request stamps autodown activity via
    ``state.write_state(\"activity\", ...)`` (api_server._do_stamp_http_activity),
    which writes to ``~/.hscc/state/activity.json`` through ``state.STATE_DIR``.
    If an api test ever exercises an authenticated request without pinning
    hscc_dir, it would write the activity file into the operator's real
    ~/.hscc. This autouse fixture makes that impossible by redirecting
    ``state.STATE_DIR`` off the live home dir for every test.

    Directory-wide belt-and-braces (covers ~/.hscc AS A WHOLE):
      (a) Patch ``os.path.expanduser`` so any ``~/.hscc/...`` path (module
          constant ``api_server.DEFAULT_HSCC_DIR`` or a runtime call like
          routes_project.py:75) resolves under the per-test tmp dir. Nothing
          outside ``~/.hscc`` is touched.
      (b) Overwrite the module-level ``api_server.DEFAULT_HSCC_DIR`` constant
          (baked in at import) too.
      (c) Redirect ``state.STATE_DIR`` to the tmp state dir.
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
