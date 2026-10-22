import os
import sys

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PARENT = os.path.dirname(_PLUGIN_DIR)
# Import the plugin as a package: `import hscc_commands` (dir is hscc-commands,
# so expose it under an underscore alias on a temp path).
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

import pytest

# One shared fence module, one home: hscc-cluster/tests/sparkrun_fence.py (where
# the leak lived). Loaded here by absolute path because each plugin suite runs in
# its OWN pytest process (scripts/run_tests.sh) with its own sys.path, and a
# second copy of a safety fixture is exactly how the two copies drift apart.
# An absent module is a loud collection error, never a silently unfenced suite.
def _load_sparkrun_fence():
    import importlib.util

    candidates = [os.path.join(_PARENT, "hscc-cluster", "tests", "sparkrun_fence.py"),
                  os.path.join(os.path.expanduser("~"), ".hermes", "plugins",
                               "hscc-cluster", "tests", "sparkrun_fence.py")]
    for path in candidates:
        if os.path.exists(path):
            spec = importlib.util.spec_from_file_location("sparkrun_fence", path)
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)
            sys.modules["sparkrun_fence"] = mod
            spec.loader.exec_module(mod)
            return mod
    raise RuntimeError(
        "sparkrun fence module not found (looked in: "
        + ", ".join(candidates)
        + ") — refusing to run this suite unfenced (t_be4b94ca: literal-argv "
          "`sparkrun proxy stop` SIGTERM'd the operator's live :4000 proxy)."
    )


sparkrun_fence = _load_sparkrun_fence()


@pytest.fixture(autouse=True)
def _fence_sparkrun_argv(monkeypatch):
    """Fence literal-argv sparkrun calls for EVERY test in this suite.

    ``cmdlib`` is self-contained by design and shells out to ``sparkrun`` with
    literal argv (``SPARKRUN = "sparkrun"``, ``_run([SPARKRUN, "stop", ...])``,
    ``restart_one``'s ``sparkrun run --no-follow --ensure``) — invisible to the
    daemon's ``HSCC_SPARKRUN_BIN`` fence. On this host the real CLI is on PATH,
    so an unpatched call here would stop/restart live fleet containers. Same
    doctrine and same shared helper as hscc-cluster/tests/conftest.py
    (t_be4b94ca — the live proxy :4000 SIGTERM incident).
    """
    sparkrun_fence.install(monkeypatch)
