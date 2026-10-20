"""SEMANTIC test of the inline proxy.yaml write script (spec §2.3 / §4.4).

The previous file tests the bridge's CALL contract with a fake runner — which
means the script's own behavior (deep merge, lock ordering, atomic write,
changed-detection) was only ever read as TEXT. That is how a shallow merge and a
dropped lock both survived mutation probes: a string cannot fail a test.

So this file EXECUTES the real `_PROXY_WRITE_SCRIPT` against a fake
`sparkrun.proxy.config` module. Nothing here touches the operator's
~/.config/sparkrun: the fake ProxyConfig reads and writes a tmp file, and the
script is exec'd in-process (no sparkrun import, no venv spawn, no network).
"""

import json
import os
import sys
import types
from pathlib import Path

import pytest
import yaml

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

import sparkrun_bridge as sb


class FakeProxyConfig:
    """The slice of sparkrun's ProxyConfig the script actually uses.

    Mirrors the real contract it depends on: `_read(strict=True)` returns the
    whole document, `_write_atomic(data)` replaces it, `_write_lock()`
    serialises writers, `set_proxy(**kwargs)` + `save()` are the public
    pending-merge path for the `proxy:` section.
    """

    path = None                  # set per-test by the fixture
    lock_held = 0                # nesting depth observed during a write
    wrote_outside_lock = False
    _nested = False

    @classmethod
    def _read(cls, strict=False):
        if not cls.path.exists():
            return {}
        return yaml.safe_load(cls.path.read_text()) or {}

    @classmethod
    def _write_atomic(cls, data):
        if cls.lock_held == 0:
            cls.wrote_outside_lock = True
        cls.path.write_text(yaml.safe_dump(data, sort_keys=False))

    @classmethod
    def _write_lock(cls):
        outer = cls._nested
        cls._nested = True

        class _L:
            def __enter__(self_inner):
                FakeProxyConfig.lock_held += 1
                return self_inner

            def __exit__(self_inner, *exc):
                FakeProxyConfig.lock_held -= 1
                FakeProxyConfig._nested = outer
                return False

        return _L()

    @classmethod
    def set_proxy(cls, **kwargs):
        cls.pending = kwargs

    @classmethod
    def save(cls):
        data = cls._read()
        data.setdefault("proxy", {}).update(cls.pending)
        cls._write_atomic(data)


@pytest.fixture()
def fake_sparkrun(tmp_path, monkeypatch):
    """Install a fake `sparkrun.proxy.config` module and point it at a tmp file."""
    cfg = tmp_path / "proxy.yaml"
    FakeProxyConfig.path = cfg
    FakeProxyConfig.lock_held = 0
    FakeProxyConfig.wrote_outside_lock = False
    FakeProxyConfig._nested = False
    FakeProxyConfig.pending = {}

    cfg.write_text(yaml.safe_dump({
        "proxy": {"port": 4000, "host": "127.0.0.1"},
        "gateways": {"litellm": {"router_settings":
                                 {"routing_strategy": "simple-shuffle"}}},
        "aliases": {"litellm-model": "litellm-model"},
    }, sort_keys=False))

    mod = types.ModuleType("sparkrun.proxy.config")
    mod.ProxyConfig = FakeProxyConfig
    pkg_proxy = types.ModuleType("sparkrun.proxy")
    pkg = types.ModuleType("sparkrun")
    pkg_proxy.config = mod
    pkg.proxy = pkg_proxy
    for name, obj in (("sparkrun", pkg), ("sparkrun.proxy", pkg_proxy),
                      ("sparkrun.proxy.config", mod)):
        monkeypatch.setitem(sys.modules, name, obj)
    return cfg


def run_script(regions, mode="direct", capsys=None, capfd=None):
    """Execute the REAL script string the way sparkrun's venv python would."""
    import io
    import contextlib
    argv = sys.argv
    sys.argv = ["_proxy_write_script", json.dumps(regions), mode]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(sb._PROXY_WRITE_SCRIPT, "<bridge-write-script>",
                         "exec"), {"__name__": "__main__"})
    finally:
        sys.argv = argv
    return json.loads(buf.getvalue())


# ── the merge really is deep ────────────────────────────────────────────────


def test_writing_balancing_preserves_every_sibling_key(fake_sparkrun):
    """THE reason the merge is deep: apply owns `gateways.litellm.balancing` and
    NOTHING else. A shallow replace of `gateways` would erase
    `router_settings.routing_strategy` — which §2.2 says must stay
    `simple-shuffle` or the proxy dies at startup — and a replace of the whole
    document would erase the bind host and the aliases."""
    out = run_script({"gateways": {"litellm": {"balancing":
                                               "least-in-flight"}}})
    assert out["ok"] is True
    doc = yaml.safe_load(fake_sparkrun.read_text())
    assert doc["gateways"]["litellm"]["balancing"] == "least-in-flight"
    assert doc["gateways"]["litellm"]["router_settings"] == {
        "routing_strategy": "simple-shuffle"}, "router_settings clobbered"
    assert doc["proxy"] == {"port": 4000, "host": "127.0.0.1"}, "bind clobbered"
    assert doc["aliases"] == {"litellm-model": "litellm-model"}, "aliases lost"


def test_a_second_gateway_namespace_survives(fake_sparkrun):
    """Another gateway's settings are not apply's to write, and must not be
    collateral damage when it writes its own."""
    fake_sparkrun.write_text(yaml.safe_dump({
        "gateways": {"litellm": {"balancing": "x"},
                     "sparkroute": {"balancing": "y"}},
    }, sort_keys=False))
    run_script({"gateways": {"litellm": {"balancing": "least-in-flight"}}})
    doc = yaml.safe_load(fake_sparkrun.read_text())
    assert doc["gateways"]["sparkroute"]["balancing"] == "y"
    assert doc["gateways"]["litellm"]["balancing"] == "least-in-flight"


# ── changed-detection is real ───────────────────────────────────────────────


def test_rewriting_the_same_value_reports_no_change(fake_sparkrun):
    """§2.3's restart rule keys off this: "write, and restart the proxy only
    when the region ACTUALLY CHANGED". Always reporting a change would make
    every apply nag the operator to run the restart unit."""
    run_script({"gateways": {"litellm": {"balancing": "least-in-flight"}}})
    again = run_script({"gateways": {"litellm": {"balancing":
                                                 "least-in-flight"}}})
    assert again["ok"] is True
    assert again["before"] == again["after"]


def test_a_real_change_reports_before_and_after(fake_sparkrun):
    """before/after are what the bridge diffs into `changed`, and what the apply
    report shows — they must be the two documents, not one of them."""
    out = run_script({"gateways": {"litellm": {"balancing": "least-in-flight"}}})
    assert out["before"]["gateways"]["litellm"].get("balancing") is None
    assert out["after"]["gateways"]["litellm"]["balancing"] == "least-in-flight"


# ── the write happens inside sparkrun's own lock ────────────────────────────


def test_the_atomic_write_happens_under_the_writer_lock(fake_sparkrun):
    """A discovery sweep writes proxy.yaml concurrently. Writing outside
    `ProxyConfig._write_lock()` can clobber it. The previous test only read the
    ordering out of the source text; this one OBSERVES the lock depth at the
    moment of the write."""
    run_script({"gateways": {"litellm": {"balancing": "least-in-flight"}}})
    assert FakeProxyConfig.wrote_outside_lock is False, (
        "_write_atomic was called with no lock held")


def test_the_merge_rereads_inside_the_lock_not_from_a_prior_snapshot(fake_sparkrun,
                                                                    monkeypatch):
    """The snapshot read BEFORE the lock must not be what gets merged: a
    concurrent writer that landed in between has to be in the result."""
    seen = {"n": 0}
    original_read = FakeProxyConfig._read.__func__

    def racing_read(cls, strict=False):
        doc = original_read(cls, strict)
        seen["n"] += 1
        if seen["n"] == 2:      # the read taken INSIDE the lock
            doc.setdefault("aliases", {})["late-sweep"] = "added-by-sweep"
        return doc

    monkeypatch.setattr(FakeProxyConfig, "_read",
                        classmethod(racing_read))
    run_script({"gateways": {"litellm": {"balancing": "least-in-flight"}}})
    doc = yaml.safe_load(fake_sparkrun.read_text())
    assert doc.get("aliases", {}).get("late-sweep") == "added-by-sweep", (
        "the write dropped a concurrent sweep's change")


# ── the public setter path ──────────────────────────────────────────────────


def test_proxy_section_goes_through_the_public_setter(fake_sparkrun):
    """Where sparkrun HAS a setter, use it — `set_proxy` + `save()` keeps
    sparkrun's own pending-merge validation. The private route is only for the
    sections sparkrun has no setter for."""
    run_script({"proxy": {"port": 4001}}, mode="pending")
    doc = yaml.safe_load(fake_sparkrun.read_text())
    assert doc["proxy"] == {"port": 4001, "host": "127.0.0.1"}
    assert FakeProxyConfig.pending == {"port": 4001}


def test_routing_strategy_is_never_written(fake_sparkrun):
    """§2.2 in force: LiteLLM 1.82.6's Literal rejects `least-in-flight` as a
    `routing_strategy`, so writing it there would fail proxy startup. apply's
    region is `balancing`, and the base strategy must come out untouched."""
    run_script({"gateways": {"litellm": {"balancing": "least-in-flight"}}})
    doc = yaml.safe_load(fake_sparkrun.read_text())
    assert doc["gateways"]["litellm"]["router_settings"][
        "routing_strategy"] == "simple-shuffle"
    assert "least-in-flight" not in json.dumps(
        doc["gateways"]["litellm"]["router_settings"])
