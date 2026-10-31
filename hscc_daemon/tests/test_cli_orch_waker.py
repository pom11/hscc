"""Unified-CLI surface for `hscc orch waker …` (orch-waker W3, t_2b34b5c7).

The rendering + honesty half of W3, driven through ``unified.main()`` with a
RECORDING stub engine (the dispatch tests for `orch inject` live in
test_cli_orch_group.py; this file is the waker verbs that card pre-declared).
Nothing here can reach tmux, a daemon or ~/.hscc: the engine is a stub, and
the conftest's autouse fence covers the rest.

What is pinned:
  * dispatch — `waker` routes through cmd_orch with the argv tail intact;
  * rendering — the multi-line human view mirrors the receipt VERBATIM (every
    number on screen comes from the receipt, none is recomputed), and the
    never-ticked / no-boards / no-audit shapes render honestly instead of
    fabricating "0s ago" or a fake board;
  * --json — the raw receipt dump, byte-identical contract with `orch inject`;
  * honesty surfaces — `hscc orch --help`, `hscc help orch`, the main banner
    and the group listing all advertise the waker verbs (F5: a dispatched
    command must not hide from a help surface the operator reads);
  * the --theme dangling-flag guard covers the waker verbs too (a swallowed
    --json would silently switch the machine contract to the human view).
"""

import json
import sys

import pytest

import hscc as unified


def _main(monkeypatch, argv):
    """Drive unified.main() with argv; returns (rc, stdout). Same shape as
    test_cli_orch_group.py's helper: main() exits via SystemExit, which IS the
    exit code here."""
    monkeypatch.setattr(sys, "argv", ["hscc", *argv])
    import io
    import contextlib
    buf = io.StringIO()
    rc = None
    try:
        with contextlib.redirect_stdout(buf):
            unified.main()
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 0
    return rc, buf.getvalue()


STATUS_RECEIPT = {
    "verb": "waker status",
    "enabled": True,
    "config": {"enabled": True, "debounce_s": 90, "min_gap_s": 300,
               "poll_s": 30},
    "stream": {"name": "orch_waker", "interval_s": 30, "scan_every_s": 30,
               "last_tick": 1_800_000_000, "tick_age_s": 12.4},
    "boards": {"hscc": {"last_id": 41, "pending": 2},
               "inc": {"last_id": 7, "pending": 0}},
    "audit": [
        {"ts": 1_799_999_000, "board": "hscc", "profile": "hscc-orch",
         "ok": True, "outcome": "sent", "msg": "wake"},
        {"ts": 1_799_999_500, "board": "inc", "profile": "inc-orch",
         "ok": False, "outcome": "failed: no pane named 'inc-orch'",
         "msg": "wake"},
    ],
}


class StubEngine:
    def __init__(self, result=None, raises=None):
        self.calls = []
        self.result = result if result is not None else dict(STATUS_RECEIPT)
        self.raises = raises

    def cmd_orch(self, argv=None):
        self.calls.append(list(argv or []))
        if self.raises:
            raise self.raises
        return self.result


def _with_engine(monkeypatch, eng):
    loaded = []

    def _load():
        loaded.append(True)
        return eng

    monkeypatch.setattr(unified, "_load_cluster_engine", _load)
    return loaded


# ── dispatch ───────────────────────────────────────────────────────────────

class TestWakerDispatch:
    def test_waker_status_reaches_the_engine_router(self, monkeypatch):
        eng = StubEngine()
        _with_engine(monkeypatch, eng)
        rc, out = _main(monkeypatch, ["orch", "waker", "status"])
        assert rc == 0, out
        assert eng.calls == [["waker", "status"]]

    def test_enable_disable_route(self, monkeypatch):
        eng = StubEngine(result={"verb": "waker enable", "enabled": True,
                                 "config": "/home/u/.hscc/config.json",
                                 "note": "tick resumes on the next daemon tick (~30s)"})
        _with_engine(monkeypatch, eng)
        rc, out = _main(monkeypatch, ["orch", "waker", "enable"])
        assert rc == 0, out
        assert eng.calls == [["waker", "enable"]]
        assert "orch-waker enabled" in out
        assert "/home/u/.hscc/config.json" in out

    def test_disable_receipt_line(self, monkeypatch):
        eng = StubEngine(result={"verb": "waker disable", "enabled": False,
                                 "config": "/x/config.json",
                                 "note": "tick is a no-op from the next daemon tick"})
        _with_engine(monkeypatch, eng)
        rc, out = _main(monkeypatch, ["orch", "waker", "disable"])
        assert rc == 0
        assert "orch-waker disabled" in out
        assert "no-op" in out

    def test_refusal_exit_1_one_line(self, monkeypatch):
        eng = StubEngine(raises=ValueError("unknown waker subcommand: frob "
                                           "(available: status, enable, disable)"))
        _with_engine(monkeypatch, eng)
        rc, out = _main(monkeypatch, ["orch", "waker", "frob"])
        assert rc == 1
        assert "unknown waker subcommand" in out and "Traceback" not in out


# ── rendering: the receipt verbatim, nothing fabricated ────────────────────

class TestWakerStatusRendering:
    def _status(self, monkeypatch, receipt=None):
        eng = StubEngine(result=dict(receipt or STATUS_RECEIPT))
        _with_engine(monkeypatch, eng)
        rc, out = _main(monkeypatch, ["orch", "waker", "status"])
        assert rc == 0
        return out

    def test_header_carries_the_three_knobs(self, monkeypatch):
        out = self._status(monkeypatch)
        assert "orch-waker: ENABLED" in out
        assert "poll 30s" in out and "debounce 90s" in out and "min gap 300s" in out

    def test_tick_line_with_age(self, monkeypatch):
        out = self._status(monkeypatch)
        assert "last tick: 1800000000 (12s ago)" in out

    def test_boards_sorted_with_ids_and_pending(self, monkeypatch):
        out = self._status(monkeypatch)
        lines = out.splitlines()
        hscc = [i for i, l in enumerate(lines) if "hscc: last-id 41 pending 2" in l]
        inc = [i for i, l in enumerate(lines) if "inc: last-id 7 pending 0" in l]
        assert hscc and inc and hscc[0] < inc[0], "boards render sorted"

    def test_audit_lines_show_sent_and_failed(self, monkeypatch):
        out = self._status(monkeypatch)
        assert "hscc -> hscc-orch: sent" in out
        assert "inc -> inc-orch: FAILED" in out
        assert "no pane" in out, "the outcome text reaches the operator"

    def test_no_fabricated_target_line(self, monkeypatch):
        out = self._status(monkeypatch)
        assert "target:" not in out and "{" not in out

    def test_never_ticked_shape_is_honest(self, monkeypatch):
        r = dict(STATUS_RECEIPT)
        r["stream"] = {"name": "orch_waker", "interval_s": 30,
                       "last_tick": None, "tick_age_s": None}
        r["boards"] = {}
        r["audit"] = []
        out = self._status(monkeypatch, r)
        assert "last tick: never" in out
        assert "0s ago" not in out
        assert "none tracked yet" in out
        assert "no deliveries yet" in out

    def test_disabled_header(self, monkeypatch):
        r = dict(STATUS_RECEIPT)
        r["enabled"] = False
        out = self._status(monkeypatch, r)
        assert "orch-waker: DISABLED" in out

    def test_json_flag_dumps_the_raw_receipt(self, monkeypatch):
        eng = StubEngine()
        _with_engine(monkeypatch, eng)
        rc, out = _main(monkeypatch, ["orch", "waker", "status", "--json"])
        assert rc == 0
        doc = json.loads(out)
        assert doc["verb"] == "waker status"
        assert doc["stream"]["tick_age_s"] == 12.4, "raw age, not the rounded view"


# ── help surfaces advertise the verbs (F5) ────────────────────────────────

class TestWakerHelpSurfaces:
    def test_group_help_advertises_waker(self, monkeypatch):
        monkeypatch.setattr(unified, "_load_cluster_engine",
                            lambda: pytest.fail("engine loaded for help"))
        rc, out = _main(monkeypatch, ["orch", "--help"])
        assert rc == 0
        assert "orch waker" in out
        assert "status|enable|disable" in out

    def test_waker_help_token_stays_inert(self, monkeypatch):
        monkeypatch.setattr(unified, "_load_cluster_engine",
                            lambda: pytest.fail("engine loaded for help"))
        rc, out = _main(monkeypatch, ["orch", "waker", "--help"])
        assert rc == 0 and "orch waker" in out

    def test_command_help_entry_advertises_waker(self):
        entry = unified.COMMAND_HELP["orch"]
        assert "waker status|enable|disable" in entry
        for ln in entry.splitlines():
            assert len(ln) <= 78, f"over budget: {ln!r}"

    def test_main_banner_advertises_waker(self, monkeypatch):
        rc, out = _main(monkeypatch, [])
        assert rc == 0 and "orch waker" in out


# ── the theme guard covers waker verbs too ────────────────────────────────

class TestWakerThemeGuard:
    def test_dangling_theme_refuses_without_calling_engine(self, monkeypatch):
        """`--theme --json` would strip --json away as the 'palette name': the
        operator asked for the machine contract and silently got the human
        view. Refuse instead (the F6 rule the inject verbs already honour)."""
        eng = StubEngine()
        loaded = _with_engine(monkeypatch, eng)
        rc, out = _main(monkeypatch, ["orch", "waker", "status",
                                      "--theme", "--json"])
        assert rc == 1, out
        assert "refusing" in out
        assert eng.calls == []
        assert loaded == [], "refuse before the engine load"

    def test_real_theme_still_works(self, monkeypatch):
        eng = StubEngine()
        _with_engine(monkeypatch, eng)
        rc, out = _main(monkeypatch, ["orch", "waker", "status",
                                      "--theme", "light"])
        assert rc == 0
        assert eng.calls == [["waker", "status"]]
