"""Unit tests for repl_delivery — `message send --apply` into the live REPL.

The delivery module is a thin CLI seam over hscc-api/repl_inject; everything
tmux-side is tested there (test_repl_inject.py, 26 cases). What is tested
HERE is the seam contract: sibling-module loading, the <name>-orch profile
convention, and honest error kinds — with a FAKED repl_inject module, so no
tmux and no REPL is ever involved.
"""
import os
import sys
import types

import pytest

from flightdeck.core import repl_delivery


class FakeRI:
    """Stand-in for the repl_inject module, wired at _repl_inject()."""

    class InjectError(Exception):
        def __init__(self, kind, message):
            super().__init__(message)
            self.kind, self.message = kind, message

    def __init__(self, pane=None, raises=None, receipt=None):
        self._pane = pane
        self._raises = raises
        self._receipt = receipt or {"target": "%9", "busy": False,
                                    "mode": "plain", "queued": False}
        self.find_calls = []
        self.deliver_calls = []

    def resolve_pane(self, profile):
        self.find_calls.append(profile)
        if self._raises and self._raises[0] == "find":
            raise self.InjectError(self._raises[1], self._raises[2])
        if self._pane is None:
            # the real resolve_pane raises for zero matches — never returns None
            raise self.InjectError("no_panes", f"0 tmux pane for {profile!r}")
        return self._pane

    def deliver(self, pane, message):
        self.deliver_calls.append((pane, message))
        if self._raises and self._raises[0] == "deliver":
            raise self.InjectError(self._raises[1], self._raises[2])
        return self._receipt


@pytest.fixture()
def wire(monkeypatch):
    def _wire(fake_or_none):
        monkeypatch.setattr(repl_delivery, "_repl_inject",
                            lambda: fake_or_none)
    return _wire


def test_deliver_uses_orch_profile_convention(wire):
    fake = FakeRI(pane={"pid": "1", "target": "%9", "tty": "/dev/ttys003"})
    wire(fake)
    r = repl_delivery.deliver("pom", "hello")
    assert fake.find_calls == ["pom-orch"]        # <name>-orch (lifecycle SSoT)
    assert fake.deliver_calls and fake.deliver_calls[0][1] == "hello"
    assert r["mode"] == "plain"


def test_module_missing_is_honest(wire):
    wire(None)
    with pytest.raises(repl_delivery.ReplDeliveryError) as ei:
        repl_delivery.deliver("pom", "x")
    assert ei.value.kind == "module_missing"
    assert "hscc-api" in ei.value.message         # names the remedy


def test_no_panes_names_the_profile_and_retry(wire):
    wire(FakeRI(pane=None))
    with pytest.raises(repl_delivery.ReplDeliveryError) as ei:
        repl_delivery.deliver("pom", "x")
    assert ei.value.kind == "no_panes"
    assert "pom-orch" in ei.value.message


def test_ambiguous_propagates_as_refusal(wire):
    wire(FakeRI(raises=("find", "ambiguous", "2 panes run the pom-orch REPL")))
    with pytest.raises(repl_delivery.ReplDeliveryError) as ei:
        repl_delivery.deliver("pom", "x")
    assert ei.value.kind == "ambiguous"


def test_delivery_busy_propagates(wire):
    wire(FakeRI(pane={"pid": "1", "target": "%9", "tty": "/dev/t"},
                raises=("deliver", "busy", "pane still streaming")))
    with pytest.raises(repl_delivery.ReplDeliveryError) as ei:
        repl_delivery.deliver("pom", "x")
    assert ei.value.kind == "busy"


# --------------------------------------------------------------------------- #
# sibling-module loader (repo checkout AND installed layout)
# --------------------------------------------------------------------------- #

def test_repl_inject_loader_resolves_repo_sibling(tmp_path, monkeypatch):
    """A fake <base>/hscc-project/flightdeck/commands/repl_delivery.py must
    find <base>/hscc-api/repl_inject.py — repo root and plugins dir share
    this shape, so one layout test covers both installs."""
    root = tmp_path / "base"
    cmd = root / "hscc-project" / "flightdeck" / "core"
    cmd.mkdir(parents=True)
    (root / "hscc-api").mkdir()
    (root / "hscc-api" / "repl_inject.py").write_text("MARKER = 'loaded'\n")
    # point the real module's __file__ at the fake tree without re-importing
    fake_file = cmd / "repl_delivery.py"  # real module lives in core/
    fake_file.write_text("")           # only parents[] matters
    monkeypatch.setattr(repl_delivery, "__file__", str(fake_file))
    ri = repl_delivery._repl_inject()
    assert ri is not None and ri.MARKER == "loaded"


def test_repl_inject_loader_missing_sibling_returns_none(tmp_path, monkeypatch):
    root = tmp_path / "solo"
    cmd = root / "hscc-project" / "flightdeck" / "core"
    cmd.mkdir(parents=True)
    fake_file = cmd / "repl_delivery.py"  # real module lives in core/
    fake_file.write_text("")
    monkeypatch.setattr(repl_delivery, "__file__", str(fake_file))
    assert repl_delivery._repl_inject() is None
