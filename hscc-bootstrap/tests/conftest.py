import os
import sys
import tempfile

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

# A pytest process must not execv itself mid-run. Importing hermes code deep
# inside a test (here: the video-gen e2e file pulling in agent.* ) can reach
# hermes_bootstrap's module-level launch preparation, which os.execv's the
# whole interpreter when this python is not hermes' selected dependency
# generation — the session vanishes with rc=1, no summary, no FAILED lines,
# nothing on stderr. Full evidence + the upstream knob: scripts/run_tests.sh
# ("no mid-run interpreter swap", t_82fa20f5). Direct `pytest
# hscc-bootstrap/tests` runs must be as safe as the runner's.
os.environ.setdefault("HERMES_DISABLE_LAZY_INSTALLS", "1")


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

# ── serving.json redirect (t_417610e6) ──────────────────────────────────────
# enable_plugins now DERIVES COMPACT_URL / the aux-repair policy from
# ~/.hscc/serving.json at import (uniform_pool -> proxy route + repair armed).
# Left live, the suite's answers would depend on whatever template the operator
# last applied: the legacy tests below pin the no-serving.json fallback form
# (node-direct default, repair disarmed) and would flip on a pool host. Pin the
# path at a missing file — deterministic fallback mode for every test — and let
# tests that want a plan shape monkeypatch the derived constants explicitly
# (test_aux_routing_serving.py). importlib.reload() re-reads this SAME env
# value, so reload tests stay in fallback mode too.
os.environ.setdefault("HSCC_SERVING_JSON",
                      os.path.join(_SANDBOX, "no-serving.json"))


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
