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
def _redirect_bootstrap_writes():
    sandbox = tempfile.mkdtemp(prefix="hscc-bootstrap-tests-")
    try:
        import enable_plugins
    except ImportError:  # pragma: no cover - import ordering safety net
        return
    if not getattr(enable_plugins, "CLUSTER_GUARD_DST", "").startswith(sandbox):
        hooks = os.path.join(sandbox, "hooks")
        os.makedirs(hooks, exist_ok=True)
        enable_plugins.HOOKS_DIR = hooks
        enable_plugins.CLUSTER_GUARD_DST = os.path.join(hooks, "cluster-guard.py")
        enable_plugins.CLUSTER_GUARD_COMMAND = (
            "python3 " + enable_plugins.CLUSTER_GUARD_DST)
        enable_plugins._CLUSTER_GUARD_SANDBOX = sandbox


_redirect_bootstrap_writes()
