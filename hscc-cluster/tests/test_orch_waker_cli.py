"""Engine-side `hscc orch waker …` verbs (orch-waker W3, t_2b34b5c7).

Hermetic by construction: the daemon module is reached ONLY through the
engine's ``_load_daemon_orch_waker`` seam, which every test here replaces with
a recording fake — no ~/.hscc/config.json is read or written, no state file is
touched, no daemon is contacted. What these tests own is the ENGINE's half of
W3: routing, argv handling (``--json``), the receipt shape the two CLI
surfaces render, and the refusal vocabulary. The waker module's own behaviour
(config resolution, staleness math, audit tail) is pinned in
hscc_daemon/tests/test_orch_waker_w3.py against the real module.
"""

import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

import hscc as engine  # noqa: E402


class FakeWaker:
    """Stand-in for hscc_daemon.orch_waker — records calls, returns canned data."""

    def __init__(self, status=None, block=None):
        self.status = status if status is not None else {
            "enabled": True,
            "config": {"enabled": True, "debounce_s": 90, "min_gap_s": 300,
                       "poll_s": 30},
            "stream": {"name": "orch_waker", "interval_s": 30,
                       "last_tick": 1_800_000_000, "tick_age_s": 12.0},
            "boards": {"hscc": {"last_id": 41, "pending": 2}},
            "audit": [{"ts": 1_800_000_000, "board": "hscc",
                       "profile": "hscc-orch", "ok": True, "outcome": "sent",
                       "msg": "wake"}],
        }
        self.block = block if block is not None else {"enabled": False}
        self.status_calls = []
        self.enabled_calls = []

    def waker_status(self, now=None):
        self.status_calls.append(now)
        return dict(self.status)

    def set_enabled(self, enabled, path=None):
        self.enabled_calls.append(enabled)
        return dict(self.block)

    def config_path(self):
        return "/home/u/.hscc/config.json"

    def load_config(self, path=None):
        return {"enabled": True, "debounce_s": 90, "min_gap_s": 300,
                "poll_s": 30}

    def stream_interval(self, cfg=None, path=None):
        return (cfg or self.load_config())["poll_s"]


@pytest.fixture
def waker(monkeypatch):
    fake = FakeWaker()
    monkeypatch.setattr(engine, "_load_daemon_orch_waker", lambda: fake)
    return fake


# ── routing ────────────────────────────────────────────────────────────────

def test_orch_group_routes_waker(waker, monkeypatch):
    seen = {}
    monkeypatch.setattr(engine, "cmd_orch_waker",
                        lambda argv=None: (seen.update(argv=argv) or {"ok": 1}))
    engine.cmd_orch(["waker", "status", "--json"])
    assert seen["argv"] == ["status", "--json"]


def test_unknown_orch_subcommand_lists_both_verbs():
    with pytest.raises(ValueError, match="available: inject, waker"):
        engine.cmd_orch(["frobnicate"])


# ── argv handling ──────────────────────────────────────────────────────────

def test_bare_waker_verb_raises_usage(waker):
    with pytest.raises(ValueError, match="usage: hscc orch waker"):
        engine.cmd_orch_waker([])


@pytest.mark.parametrize("verb", ["", "-h", "--help"])
def test_help_tokens_raise_usage_not_a_status(waker, verb):
    """A help token must never masquerade as `status` — the F5 surface rule
    says help is answered, not executed (the unified entry prints the group
    listing; this guard is the standalone engine entry's half)."""
    with pytest.raises(ValueError, match="usage: hscc orch waker"):
        engine.cmd_orch_waker([verb])
    assert waker.status_calls == []


def test_json_flag_is_accepted_and_never_reaches_the_waker(waker):
    """--json is the RENDERING contract of the two entries, not a waker
    argument — same posture cmd_orch_inject takes."""
    r = engine.cmd_orch_waker(["status", "--json"])
    assert r["verb"] == "waker status"
    assert waker.status_calls == [None]


def test_unknown_waker_subcommand_refuses(waker):
    with pytest.raises(ValueError, match="unknown waker subcommand: frob"):
        engine.cmd_orch_waker(["frob"])
    assert waker.enabled_calls == []


def test_missing_daemon_module_is_a_named_runtime_error(monkeypatch):
    """The standalone engine entry can run where hscc_daemon is NOT deployed
    next to it. Silence would print an empty status; the error names the fix
    (the same honesty rule as the doctor's UNVERIFIABLE row)."""
    monkeypatch.setattr(engine, "_load_daemon_orch_waker", lambda: None)
    with pytest.raises(RuntimeError, match="not importable"):
        engine.cmd_orch_waker(["status"])


# ── receipts ───────────────────────────────────────────────────────────────

def test_status_receipt_verbatim_plus_verb(waker):
    r = engine.cmd_orch_waker(["status"], now=1_800_000_042)
    assert r["verb"] == "waker status"
    assert r["stream"]["tick_age_s"] == 12.0
    assert r["boards"]["hscc"] == {"last_id": 41, "pending": 2}
    assert waker.status_calls == [1_800_000_042], "now= reaches the clock seam"


@pytest.mark.parametrize("verb,expect", [("enable", True), ("disable", False)])
def test_enable_disable_receipts(waker, verb, expect):
    waker.block = {"enabled": expect}
    r = engine.cmd_orch_waker([verb])
    assert waker.enabled_calls == [expect], "the flip goes through the module"
    assert r["verb"] == f"waker {verb}"
    assert r["enabled"] is expect
    assert r["config"] == "/home/u/.hscc/config.json"
    assert r["note"], "the receipt must tell the operator what happens next"


def test_disable_note_sets_staleness_expectation(waker):
    """The note must tell the operator the stream KEEPS stamping — the one
    fact that stops them reading 'stream fresh but nothing sent' as a bug."""
    waker.block = {"enabled": False}
    r = engine.cmd_orch_waker(["disable"])
    assert "no-op" in r["note"] and "stamping" in r["note"]


def test_enable_note_points_at_the_next_tick(waker):
    waker.block = {"enabled": True}
    r = engine.cmd_orch_waker(["enable"])
    assert "next daemon tick" in r["note"]
    assert "30" in r["note"], "the note quotes the real cadence, not a guess"
