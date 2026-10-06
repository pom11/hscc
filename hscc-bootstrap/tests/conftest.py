import os
import sys
import tempfile

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)


# ── hermeticity guard (t_9462260b) ───────────────────────────────────────────
# Every suite here runs in the *worker env*, which exports HERMES_HOME at the
# live profile. enable_plugins binds HOOKS_DIR / CLUSTER_GUARD_DST from that
# variable at import, so any test that calls enable() without patching those
# names writes a real .bak-<stamp> into the operator's hooks/ — measured
# fallout: the live cluster-guard rollback point was replaced 9x in one test
# run, each overwrite destroying the previous backup, and the truncated-write
# bug (fixed in this card) zeroed 9 of them.
#
# So: before any test module imports the bootstrap writers, point their
# live-state destinations at a throwaway dir. Individual tests that want a
# specific destination keep monkeypatching it themselves — monkeypatch wins,
# because it runs after this. This only closes the "forgot to patch" path.
def _hooks_redirect_target():
    sandbox = tempfile.mkdtemp(prefix="hscc-bootstrap-tests-")
    hooks = os.path.join(sandbox, "hooks")
    os.makedirs(hooks, exist_ok=True)
    return sandbox, hooks


_SANDBOX, _SANDBOX_HOOKS = _hooks_redirect_target()


def _apply_hooks_redirect(module):
    module.HOOKS_DIR = _SANDBOX_HOOKS
    module.CLUSTER_GUARD_DST = os.path.join(_SANDBOX_HOOKS, "cluster-guard.py")
    module.CLUSTER_GUARD_COMMAND = "python3 " + module.CLUSTER_GUARD_DST
    module._CLUSTER_GUARD_SANDBOX = _SANDBOX


def _redirect_bootstrap_writes():
    try:
        import enable_plugins
    except ImportError:  # pragma: no cover - import ordering safety net
        return
    _apply_hooks_redirect(enable_plugins)


_redirect_bootstrap_writes()


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _hooks_redirect_survives_reload():
    """Re-apply the redirect after every test.

    test_enable_plugins reloads the module (importlib.reload) to re-read model
    aliases from a clean env — which re-executes the module body and RE-BINDS
    HOOKS_DIR/CLUSTER_GUARD_DST straight from the exported HERMES_HOME, wiping
    the redirect above. Measured with only the import-time redirect: backups
    kept appearing in the live profile hooks/ mid-suite (stamps 5 s apart,
    count pinned at keep-3 by the new pruning) for every enable() case after
    the first reload. Restoring in teardown puts the sandbox back before the
    next test runs, without touching the reload tests' own assertions (they
    finish first and only read model constants).
    """
    yield
    import sys
    module = sys.modules.get("enable_plugins")
    if module is not None and getattr(module, "_CLUSTER_GUARD_SANDBOX", None) != _SANDBOX:
        _apply_hooks_redirect(module)
