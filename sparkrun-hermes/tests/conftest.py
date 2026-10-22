import os
import sys

import pytest

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)


# Sparkrun argv fence (t_be4b94ca — live proxy SIGTERM incident): execlib is a
# `sparkrun ...` passthrough, so an un-faked call here would exec the operator's
# real CLI (including `proxy stop`, which SIGTERMs the live :4000 balancer).
# Tests fake execlib.subprocess.run themselves; a wrapper is invisible to them.
def _load_sparkrun_fence():
    import importlib.util

    _parent = os.path.dirname(_PLUGIN_DIR)
    candidates = [os.path.join(_parent, "hscc-cluster", "tests", "sparkrun_fence.py"),
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
        + ") — refusing to run this suite unfenced (t_be4b94ca).")


_sparkrun_fence = _load_sparkrun_fence()


@pytest.fixture(autouse=True)
def _fence_sparkrun_argv(monkeypatch):
    """Block any test that would exec the operator's real sparkrun CLI."""
    _sparkrun_fence.install(monkeypatch)


_fence_reported = set()


@pytest.fixture(autouse=True)
def _fence_guard():
    """Fail the test/fixture that tried to reach the live sparkrun argv."""
    yield
    fresh = []
    for e in _sparkrun_fence.unexpected_blocked():
        key = (tuple(e["argv"]), e["why"])
        if key not in _fence_reported:
            _fence_reported.add(key)
            fresh.append(e)
    if fresh:
        raise AssertionError(
            "SPARKRUN FENCE GUARD: a test/fixture tried to exec the operator's "
            f"live sparkrun CLI: {[e['argv'] for e in fresh]}. Fake "
            "execlib.subprocess.run instead — t_be4b94ca.")


def pytest_terminal_summary(terminalreporter):
    """The fence's session report is part of the visible output, every run."""
    lines = _sparkrun_fence.report().splitlines()
    terminalreporter.write_sep("=", lines[0])
    for line in lines[1:]:
        terminalreporter.write_line(line)
