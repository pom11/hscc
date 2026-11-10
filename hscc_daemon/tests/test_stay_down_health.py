"""Tests for the STAY-DOWN health gates (t_ba1424b7).

The bug (daemon.log 2026-11-08, addresses redacted): `hscc cluster down` cleared
the expected_up marker at 17:51:23 and the daemon undid it ~25s later —
`check_workers` relaunched all three keepalive units (17:51:47-54, they then sat
idle all night) and `check_proxy` declared the proxy unhealthy on :4000 at
17:52:02, "repairing proxy backend drift", reaping an orphan listener and
relaunching every few minutes. Only the orch unit respected the intent. The
reason: check_workers' only suppression gate is the AUTO DOWN watchdog window
(`_intentional_window`), which a manual `cluster down` never latches, and
check_proxy had no operator gate at all.

The fix under test: both checks read ``post_boot.deliberately_down()`` through
the ``health._operator_down()`` seam and stand down (D2 shape — one INFO line +
``intentional: "operator_down"`` stream state) while that record stands.

Proof shape (the card's acceptance list):
  * record set   -> workers: nothing probed, nothing relaunched, ok=True +
    intentional=operator_down, the ONE log line exactly once;
  * record set   -> proxy: no probe, no drift read, no orphan reap, no
    `proxy start`, same state shape;
  * negative A (fleet meant to be up: marker present, units non-empty, NO
    deliberate_down key) -> supervision unchanged;
  * negative B (fail-safe: marker absent / corrupt / non-dict / a raising
    predicate) -> supervision unchanged;
  * negative C -> the autodown-window gate still wins when both would apply.

Discipline: every side-effect is stubbed (probes, run_cmd, subprocess.Popen,
the auto-heal fn), so nothing here shells out or reaches a live node — the same
setup as test_health.TestCheckWorkers/TestCheckProxy. The marker path is
redirected suite-wide by conftest and pinned per test like test_post_boot.py.
Address doctrine: sanctioned 10.0.0.x placeholder range only.
"""

import json
import os

import pytest

from hscc_daemon import health, post_boot as pb, serving
from hscc_daemon import state as state_mod

ORCH = "192.0.2.1"
WORKER = "192.0.2.247"
RECIPE = "~/r/27b.yaml"
REASON = "hscc cluster down"

# D2 pins the wording exactly: `fleet deliberately down — not relaunching
# (<area>; reason: <reason>)`. One place, so a reword fails loudly here.
def _expected_line(area, reason=REASON):
    return f"fleet deliberately down — not relaunching ({area}; reason: {reason})"


# ── fixtures / fakes ───────────────────────────────────────────────────────

@pytest.fixture
def marker(tmp_path, monkeypatch):
    """The expected-up marker. conftest already redirects post_boot's constant
    for the whole suite; pin it explicitly (test_post_boot.py's convention) so
    these tests say which file they are about."""
    path = str(tmp_path / "expected-up.json")
    monkeypatch.setattr(pb, "EXPECTED_UP_FILE", path)
    return path


def _stand_down(marker, reason=REASON):
    """Put the fleet in the state `hscc cluster down` leaves on disk — written
    by the REAL producer, not hand-planted JSON, so the key shape cannot drift
    from post_boot without these tests noticing."""
    pb.clear_expected_up(reason, path=marker)
    assert pb.deliberately_down(marker)["reason"] == reason


def _write_raw(path, data):
    """Plant arbitrary marker bytes (negative controls: present-but-not-a-record)."""
    with open(path, "w") as f:
        if isinstance(data, str):
            f.write(data)
        else:
            json.dump(data, f)


def _capture_log(monkeypatch):
    lines = []
    monkeypatch.setattr(health, "log",
                        lambda msg, level="INFO", **kw: lines.append((level, msg)))
    return lines


def _state_dir(tmp_hfcc_dir, monkeypatch):
    d = tmp_hfcc_dir / "state"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state_mod, "STATE_DIR", str(d))
    return d


def _workers_env(tmp_hfcc_dir, monkeypatch):
    """One DOWN keep-alive unit, every side-effect recorded.

    Mirrors test_health.TestCheckWorkers._setup: a single check gives a
    first-time-down unit (streak 1 < debounce 3), i.e. the exact tick that
    relaunched the fleet at 17:51:47 on the incident day.
    """
    log_lines = _capture_log(monkeypatch)
    _state_dir(tmp_hfcc_dir, monkeypatch)
    units = [
        {"id": "orch", "role": "orchestrator", "nodes": [ORCH],
         "recipe": "~/r/orch.yaml", "model": "M"},
        {"id": f"w-{WORKER}", "role": "worker", "keepalive": True,
         "nodes": [WORKER], "recipe": RECIPE, "model": "W"},
    ]
    monkeypatch.setattr(serving, "ORCH_NODES", {ORCH})
    monkeypatch.setattr(serving, "load_serving",
                        lambda: {"version": 1, "units": units})
    health._worker_relaunch_at.clear()
    health._worker_down_streak.clear()
    health._worker_last_autoheal.clear()
    monkeypatch.setattr(health, "_WORKER_RELATCH_FILE",
                        str(tmp_hfcc_dir / "worker_relaunch.json"))
    monkeypatch.setattr(health, "_WORKER_CONTAINER_STATE_FILE",
                        str(tmp_hfcc_dir / "worker_container_state.json"))
    monkeypatch.setattr(health, "_tp_peer_nodes", lambda: set())

    probed, stopped, spawned = [], [], []
    monkeypatch.setattr(health, "http_check",
                        lambda url, timeout=5: probed.append(url) or {"ok": False})
    monkeypatch.setattr(health, "run_cmd",
                        lambda args, **k: stopped.append(list(args)) or {"ok": True})
    monkeypatch.setattr(health.subprocess, "Popen",
                        lambda *a, **k: spawned.append((a, k)))
    monkeypatch.setattr(health, "_autoheal_worker_fn",
                        lambda *a, **k: spawned.append(("autoheal", a))
                        or {"ok": True})
    return log_lines, probed, stopped, spawned


def _proxy_forbidden(monkeypatch):
    """Every action/probe check_proxy can take, wired to fail the test if hit."""
    calls = []

    def forbidden(name, ret=None):
        def _f(*a, **k):
            calls.append(name)
            raise AssertionError(
                f"proxy check must not {name} while the fleet is deliberately "
                f"down (D2: do not probe, do not relaunch, do not reap)")
        return _f

    monkeypatch.setattr(health, "_router_drift_probe",
                        forbidden("_router_drift_probe"))
    monkeypatch.setattr(health, "_proxy_models_probe", forbidden("_proxy_models_probe"))
    monkeypatch.setattr(health, "_proxy_backend_probe", forbidden("_proxy_backend_probe"))
    monkeypatch.setattr(health, "_proxy_relaunch_hosts", forbidden("_proxy_relaunch_hosts"))
    monkeypatch.setattr(health, "_reap_orphan_proxy_listener",
                        forbidden("_reap_orphan_proxy_listener"))
    monkeypatch.setattr(health, "_proxy_repair_orphan", forbidden("_proxy_repair_orphan"))
    monkeypatch.setattr(health, "run_cmd", forbidden("run_cmd"))
    monkeypatch.setattr(serving, "load_serving", forbidden("serving.load_serving"))
    return calls


def _proxy_env(tmp_hfcc_dir, monkeypatch):
    """check_proxy() pointed at a dead-but-configured proxy (KEEPALIVE_NODES
    non-empty), all live surfaces stubbed. Mirrors TestCheckProxy._setup."""
    log_lines = _capture_log(monkeypatch)
    _state_dir(tmp_hfcc_dir, monkeypatch)
    monkeypatch.setattr(serving, "KEEPALIVE_NODES", {WORKER})
    monkeypatch.setattr(serving, "load_serving",
                        lambda *a, **k: {"version": 2, "units": []})
    monkeypatch.setattr(health, "_router_drift_probe",
                        lambda: {"status": "skip", "warn": False, "detail": "stubbed"})
    monkeypatch.setattr(health, "_proxy_models_probe",
                        lambda url, timeout=5: {"ok": False, "models": [],
                                                "error": "connection refused"})
    monkeypatch.setattr(health, "_proxy_backend_probe",
                        lambda url=None, timeout=5: {"ok": False, "backends": set(),
                                                     "error": "stubbed"})
    ran = []
    monkeypatch.setattr(health, "run_cmd",
                        lambda args, **k: ran.append(list(args)) or {"ok": True})
    return log_lines, ran


# ── the two skips ──────────────────────────────────────────────────────────

class TestWorkersStandDown:
    def test_record_set_relaunches_nothing(self, tmp_hfcc_dir, monkeypatch, marker):
        """The 17:51:47 resurrection, pinned as impossible: the check returns ok
        without probing a single unit and without touching sparkrun."""
        _stand_down(marker)
        lines, probed, stopped, spawned = _workers_env(tmp_hfcc_dir, monkeypatch)

        assert health.check_workers() is True

        assert probed == []          # not even a health probe (D2: do not probe)
        assert stopped == []         # no `sparkrun stop`
        assert spawned == []         # no detached `sparkrun run`, no auto-heal
        st = state_mod.read_state("workers")
        assert st["ok"] is True
        assert st["intentional"] == "operator_down"
        assert st["total"] == 1 and st["online"] == 0
        assert st["relaunched"] == [] and st["down"] == []
        # The message names the reason (D2).
        assert st["message"] == _expected_line("workers")
        assert REASON in st["message"]

    def test_the_line_is_logged_exactly_once(self, tmp_hfcc_dir, monkeypatch, marker):
        """ONE INFO line (D2) — no per-unit echo of it, no WARN companion."""
        _stand_down(marker)
        lines, _probed, _stopped, _spawned = _workers_env(tmp_hfcc_dir, monkeypatch)

        assert health.check_workers() is True

        exact = [lvl for lvl, msg in lines if msg == _expected_line("workers")]
        assert exact == ["INFO"], f"expected one INFO line, got {lines}"
        assert [msg for _lvl, msg in lines
                if "fleet deliberately down" in msg] == [_expected_line("workers")]

    def test_reason_is_carried_verbatim(self, tmp_hfcc_dir, monkeypatch, marker):
        """The record's reason reaches both the log line and the stream state —
        it is the operator's own words (autodown hook vs manual CLI differ)."""
        reason = "autodown: manual teardown by operator"
        _stand_down(marker, reason=reason)
        lines, _p, _s, _spawned = _workers_env(tmp_hfcc_dir, monkeypatch)

        assert health.check_workers() is True

        msgs = [msg for _lvl, msg in lines]
        # "Running workers check" still leads (that line is pre-existing and
        # byte-identical); the stand-down line is the ONE operator-wording line
        # and the last thing this tick says.
        assert msgs.count(_expected_line("workers", reason)) == 1
        assert msgs[-1] == _expected_line("workers", reason)
        assert [m for m in msgs if "fleet deliberately down" in m] == \
            [_expected_line("workers", reason)]
        assert state_mod.read_state("workers")["message"] == _expected_line(
            "workers", reason)

    @pytest.mark.parametrize("producer,reason", [
        # The TWO sanctioned callers of clear_expected_up, wordings verbatim
        # (hscc-cluster/hscc.py:547 and hscc_daemon/autodown.py:2319).
        ("cluster-down-verb", "hscc cluster down"),
        ("idle-teardown", "autodown: intentional idle teardown"),
    ])
    def test_both_record_writers_stand_down(self, tmp_hfcc_dir, monkeypatch,
                                            marker, producer, reason):
        """The record means "someone deliberately cleared the footprint", and it
        has TWO writers — the operator verb and autodown's idle teardown. BOTH
        are honoured as a stay-down: the gate branches on the record's
        PRESENCE and never on the `reason` text, because a substring test would
        make an operator-chosen reason string a safety property. (The
        idle-teardown shape normally never reaches this gate — it latches the
        intentional block, so `_intentional_window()` answers first; see
        test_C_autodown_window_gate_still_wins. This pins what happens when it
        does reach here.)"""
        _stand_down(marker, reason=reason)
        lines, probed, stopped, spawned = _workers_env(tmp_hfcc_dir, monkeypatch)

        assert health.check_workers() is True

        assert (probed, stopped, spawned) == ([], [], [])
        st = state_mod.read_state("workers")
        assert st["intentional"] == "operator_down"
        assert st["message"] == _expected_line("workers", reason)
        assert _expected_line("workers", reason) in [m for _l, m in lines]

    def test_record_without_reason_still_stands_down(self, tmp_hfcc_dir,
                                                    monkeypatch, marker):
        """A malformed record (a dict, but no `reason`) suppresses just the same
        — the presence of the record is the signal, and reading it must not
        raise."""
        _write_raw(marker, {"units": [], pb.DELIBERATE_DOWN_KEY: {"set_at": "now"}})
        lines, probed, stopped, spawned = _workers_env(tmp_hfcc_dir, monkeypatch)

        assert health.check_workers() is True

        assert (probed, stopped, spawned) == ([], [], [])
        assert "unspecified" in state_mod.read_state("workers")["message"]


class TestProxyStandDown:
    def test_record_set_does_nothing_at_all(self, tmp_hfcc_dir, monkeypatch, marker):
        """The 17:52:02 resurrection: no catalogue probe, no drift read, no
        orphan reap, no `proxy start` while the operator's down stands."""
        _stand_down(marker)
        lines = _capture_log(monkeypatch)
        _state_dir(tmp_hfcc_dir, monkeypatch)
        monkeypatch.setattr(serving, "KEEPALIVE_NODES", {WORKER})
        calls = _proxy_forbidden(monkeypatch)

        assert health.check_proxy() is True

        assert calls == []
        assert [msg for _lvl, msg in lines] == [_expected_line("proxy")]
        st = state_mod.read_state("proxy")
        assert st["ok"] is True
        assert st["port"] == health.PROXY_PORT
        assert st["intentional"] == "operator_down"
        assert st["message"] == _expected_line("proxy")
        assert REASON in st["message"]
        assert "last_check" in st

    def test_gate_runs_before_the_no_workers_early_return(
            self, tmp_hfcc_dir, monkeypatch, marker):
        """The gate is at the very TOP (card step 3), so even a fleet with no
        keep-alive nodes reports the operator's intent rather than 'proxy not
        needed' — the stream state says why nothing ran."""
        _stand_down(marker)
        lines = _capture_log(monkeypatch)
        _state_dir(tmp_hfcc_dir, monkeypatch)
        monkeypatch.setattr(serving, "KEEPALIVE_NODES", set())
        calls = _proxy_forbidden(monkeypatch)

        assert health.check_proxy() is True

        assert calls == []
        assert state_mod.read_state("proxy")["intentional"] == "operator_down"


# ── negative controls: supervision must proceed, byte-identically ──────────

class TestNegativeControls:
    """Nothing here may change what happens when there is NO record: every one
    of these is the incident-day relaunch, still firing."""

    def _assert_relaunches(self, stopped, spawned):
        assert spawned, "the down unit must still be relaunched"
        argv = spawned[0][0][0]
        assert argv[1] == "run" and WORKER in argv
        assert any("sparkrun" in str(a) for a in argv)

    def test_A_marker_present_with_units_no_record(self, tmp_hfcc_dir, monkeypatch,
                                                   marker):
        """Fleet meant to be up (the shape `cluster up` leaves): relaunch as today."""
        _write_raw(marker, {"units": [{"unit_id": "wk1", "role": "worker",
                                       "nodes": [WORKER], "port": 8000}],
                            "reason": "hscc cluster up", "set_at": "now",
                            "boot_baseline": {}, "boot_attempts": {}})
        assert pb.deliberately_down(marker) is None
        _lines, _probed, stopped, spawned = _workers_env(tmp_hfcc_dir, monkeypatch)

        assert health.check_workers() is True

        self._assert_relaunches(stopped, spawned)

    def test_B_missing_marker(self, tmp_hfcc_dir, monkeypatch, marker):
        """No marker file at all (the pre-existing default): fail-safe None."""
        assert not os.path.exists(marker)
        _lines, _probed, stopped, spawned = _workers_env(tmp_hfcc_dir, monkeypatch)

        assert health.check_workers() is True

        self._assert_relaunches(stopped, spawned)

    def test_B_corrupt_marker(self, tmp_hfcc_dir, monkeypatch, marker):
        """Corrupt JSON is NO EVIDENCE, not a reason to go quiet."""
        _write_raw(marker, "{ not json at all")
        assert pb.deliberately_down(marker) is None
        _lines, _probed, stopped, spawned = _workers_env(tmp_hfcc_dir, monkeypatch)

        assert health.check_workers() is True

        self._assert_relaunches(stopped, spawned)

    @pytest.mark.parametrize("body", [
        "[]",                      # a list where a dict belongs
        '{"deliberate_down": "yes"}',   # present but not a dict
        '{"deliberate_down": {}}',      # present but empty
    ])
    def test_B_non_record_shapes(self, tmp_hfcc_dir, monkeypatch, marker, body):
        _write_raw(marker, body)
        assert pb.deliberately_down(marker) is None
        _lines, _probed, stopped, spawned = _workers_env(tmp_hfcc_dir, monkeypatch)

        assert health.check_workers() is True

        self._assert_relaunches(stopped, spawned)

    def test_B_raising_predicate_proceeds(self, tmp_hfcc_dir, monkeypatch):
        """health._operator_down() swallows ANY exception — a predicate that
        explodes must never silence supervision."""
        def boom(*a, **k):
            raise RuntimeError("post_boot is on fire")
        monkeypatch.setattr(pb, "deliberately_down", boom)
        assert health._operator_down() is None
        _lines, _probed, stopped, spawned = _workers_env(tmp_hfcc_dir, monkeypatch)

        assert health.check_workers() is True

        self._assert_relaunches(stopped, spawned)

    def test_B_missing_predicate_proceeds(self, tmp_hfcc_dir, monkeypatch):
        """An older post_boot without the predicate (AttributeError under the
        deferred import) reads as no record, not as down."""
        monkeypatch.delattr(pb, "deliberately_down")
        assert health._operator_down() is None
        _lines, _probed, stopped, spawned = _workers_env(tmp_hfcc_dir, monkeypatch)

        assert health.check_workers() is True

        self._assert_relaunches(stopped, spawned)

    def test_C_autodown_window_gate_still_wins(self, tmp_hfcc_dir, monkeypatch,
                                               marker):
        """Both gates would fire: the AUTO DOWN path stays first and keeps its
        own wording + `intentional: "autodown"` marker (verify reads it)."""
        from hscc_daemon import autodown as _ad
        from hscc_daemon import lifecycle as _lc
        _stand_down(marker)
        monkeypatch.setattr(_lc, "WATCHDOG_BLOCK_FILE",
                            str(tmp_hfcc_dir / "watchdog-block.json"))
        monkeypatch.setattr(_ad, "AUTODOWN_FILE", str(tmp_hfcc_dir / "autodown.json"))
        _lc.save_watchdog_block({"blocked": True, "intentional": "autodown",
                                 "reason": "autodown: intentional idle teardown"})
        _ad.save_config({**_ad.DEFAULT_CONFIG, "enabled": True, "state": "down",
                         "down_since": "2026-01-01T00:00:00+00:00"})
        lines, probed, stopped, spawned = _workers_env(tmp_hfcc_dir, monkeypatch)

        assert health.check_workers() is True

        assert (probed, stopped, spawned) == ([], [], [])
        st = state_mod.read_state("workers")
        assert st["intentional"] == "autodown"          # not operator_down
        msgs = [msg for _lvl, msg in lines]
        assert any("intentional autodown window in effect" in m for m in msgs)
        assert not any("fleet deliberately down" in m for m in msgs)

    def test_proxy_still_repairs_when_no_record(self, tmp_hfcc_dir, monkeypatch,
                                                marker):
        """The proxy gate is the only new behaviour on that path: with the
        marker absent, the unhealthy proxy is still relaunched."""
        _write_raw(marker, {"units": [], "reason": "hscc cluster up"})
        lines, ran = _proxy_env(tmp_hfcc_dir, monkeypatch)

        assert health.check_proxy() is True

        assert ran, "the unhealthy proxy must still be relaunched"
        cmd = ran[0]
        assert cmd[1:3] == ["proxy", "start"] and WORKER in cmd
        assert state_mod.read_state("proxy").get("intentional") != "operator_down"
        assert not any("fleet deliberately down" in m for _l, m in lines)


# ── the escape hatch, from the health side ─────────────────────────────────

class TestRecordLiftsSupervision:
    def test_cluster_up_resumes_supervision(self, tmp_hfcc_dir, monkeypatch, marker):
        """`hscc cluster up` (record_expected_up) pops the record — the ONLY
        override — and the very next tick heals again. Without this the fix
        would be a permanent off-switch."""
        _stand_down(marker)
        lines, _p, _s, spawned = _workers_env(tmp_hfcc_dir, monkeypatch)
        assert health.check_workers() is True
        assert spawned == [] and state_mod.read_state("workers")["intentional"] \
            == "operator_down"

        pb.record_expected_up(
            [{"unit_id": "wk1", "kind": "worker", "nodes": [WORKER], "port": 8000}],
            "hscc cluster up", path=marker)
        assert pb.deliberately_down(marker) is None

        assert health.check_workers() is True

        assert len(spawned) == 1
        assert spawned[0][0][0][1] == "run"
        st = state_mod.read_state("workers")
        assert "intentional" not in st
