"""Tests for hscc_daemon/gateway_probe.py — the daemon's handle on the shared
gateway-liveness implementation (t_c14d18cb).

The loader is the only thing standing between `health.check_gateway()` and a
second, drifting copy of the probe, so it is pinned here the way
`guard_posture.py`'s loader would be: it finds the file in both deployment
layouts, it loads and validates it, and when it cannot, it raises with the
paths it tried — it never quietly returns something that looks like a verdict.
"""

import os
import sys
import types

import pytest

from hscc_daemon import gateway_probe


REAL_SRC = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))),
    "hscc-bootstrap", "gateway_liveness.py")


@pytest.fixture(autouse=True)
def _fresh_module_cache(monkeypatch):
    """The loader memoises by resolved path; start each case with an empty cache."""
    monkeypatch.setattr(gateway_probe, "_MODULES", {}, raising=False)


def test_the_shipped_file_is_where_the_loader_looks_by_default():
    """Default resolution must find the real sibling in THIS checkout.

    If someone moves the file or renames the directory, this fails here rather
    than turning the daemon's gateway check into a permanently degraded stream.
    """
    assert str(gateway_probe.liveness_source()) == REAL_SRC


def test_repo_layout_and_deployed_layout_are_both_searched(tmp_path):
    """Sibling-of-the-package-dir first, then ~/.hermes/plugins/<...>."""
    boot = tmp_path / "plugins" / "hscc-bootstrap"
    boot.mkdir(parents=True)
    shipped = boot / "gateway_liveness.py"
    shipped.write_text("def job_is_alive():\n    return True, 'stub'\n")

    found = gateway_probe.liveness_source(root=tmp_path / "plugins",
                                          home=tmp_path / "nope")
    assert found == shipped

    # Only the last-resort layout exists -> that one wins.
    home = tmp_path / "fakehome"
    alt = home / ".hermes" / "plugins" / "hscc-bootstrap"
    alt.mkdir(parents=True)
    (alt / "gateway_liveness.py").write_text("def job_is_alive():\n    return True, ''\n")
    assert gateway_probe.liveness_source(root=tmp_path / "nowhere",
                                        home=home) == (alt / "gateway_liveness.py")


def test_missing_sibling_reports_none_not_a_guess(tmp_path):
    assert gateway_probe.liveness_source(root=tmp_path / "a",
                                        home=tmp_path / "b") is None


def test_load_returns_the_single_implementation():
    mod = gateway_probe.load_gateway_liveness()
    assert mod.__file__ == REAL_SRC
    for name in ("job_is_alive", "gateway_verdict", "gateway_states"):
        assert callable(getattr(mod, name))


def test_load_is_cached_by_path(monkeypatch):
    """The daemon calls this every 10s — it must not re-exec the file each time."""
    calls = []
    real = gateway_probe.liveness_source

    def counting(root=None, home=None):
        calls.append(1)
        return real(root=root, home=home)
    monkeypatch.setattr(gateway_probe, "liveness_source", counting)

    first = gateway_probe.load_gateway_liveness()
    second = gateway_probe.load_gateway_liveness()
    assert first is second
    assert len(calls) >= 2          # source() consulted each call; the module is not


def test_unfindable_sibling_raises_naming_the_paths(tmp_path, monkeypatch):
    """Fail LOUD. A silent None here would read as 'gateway down' downstream."""
    monkeypatch.setattr(gateway_probe, "liveness_source",
                        lambda root=None, home=None: None)
    with pytest.raises(gateway_probe.GatewayLivenessSourceError) as exc:
        gateway_probe.load_gateway_liveness()
    assert "gateway_liveness.py" in str(exc.value)


def test_module_missing_a_required_name_is_rejected(tmp_path, monkeypatch):
    """A partially-present file must not half-load into the health check.

    Real code path: point the resolver at a stub file and let
    `load_gateway_liveness()` do the validating, so the guard itself is what
    fails rather than a hand-rolled re-implementation of it.
    """
    src = tmp_path / "gateway_liveness.py"
    src.write_text("def job_is_alive():\n    return True, ''\n")   # no verdict/states
    monkeypatch.setattr(gateway_probe, "liveness_source",
                        lambda root=None, home=None: str(src))
    with pytest.raises(gateway_probe.GatewayLivenessSourceError) as exc:
        gateway_probe.load_gateway_liveness()
    assert "gateway_verdict" in str(exc.value)


def test_broken_file_raises_instead_of_importing_partially(tmp_path, monkeypatch):
    """A file that cannot execute must surface as an error naming the file."""
    src = tmp_path / "gateway_liveness.py"
    src.write_text("def job_is_alive(:\n")     # syntax error
    monkeypatch.setattr(gateway_probe, "liveness_source",
                        lambda root=None, home=None: str(src))
    with pytest.raises(gateway_probe.GatewayLivenessSourceError) as exc:
        gateway_probe.load_gateway_liveness()
    assert str(src) in str(exc.value)


def test_health_check_treats_a_raising_loader_as_unknown(tmp_path, monkeypatch):
    """The contract that makes the loader safe to put on a check path.

    A raise must never escape `check_gateway()`, and must never be published as
    a healthy read: the stream goes not-ok with `gateway_job: null` (nothing was
    verified) plus the named reason, so a broken probe is never mistaken for a
    verified green — nor blamed on the gateway itself.
    """
    from hscc_daemon import health, state as state_mod

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
    monkeypatch.setattr(health, "log", lambda *a, **kw: None)
    monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": True})
    monkeypatch.setattr(health, "_GATEWAY_PROBE_UNAVAILABLE", None)

    def boom():
        raise gateway_probe.GatewayLivenessSourceError("sibling not deployed")
    monkeypatch.setattr(gateway_probe, "load_gateway_liveness", boom)

    # Fail CLOSED on an unverifiable job (never a false green), and the entry
    # says the job was not verified rather than that it is down.
    assert health.check_gateway() is False
    state = state_mod.read_state("gateway")
    assert state is not None
    assert state["gateway_job"] is None
    assert "sibling not deployed" in state["gateway_job_degraded"]
