"""RED tests for GAP-F-1 + GAP-F-2 in hscc_daemon/verify.py::check_daemon_streams.

Fixtures are copied shape-for-shape from the operator's REAL ~/.hscc/state
(values redacted: IPs -> the sanctioned 10.0.0.x placeholder, token numbers
kept in shape but not in value). Everything runs under pytest's tmp_path —
the live state dir is never read or written.

Run (fail-then-pass evidence lives in the card workspace):
    .venv/pytest-writer/bin/pytest -c pytest.ini -p no:cacheprovider \
        --basetemp=<fresh> hscc_daemon/tests/test_verify.py -k SchemaTolerance
"""

import json
import os
from datetime import datetime, timezone

import pytest


# ── live-shape fixtures (verbatim schemas from ~/.hscc/state on 2026-11-01) ──

# dgx.json: dict, ok + timestamp, no last_check.
LIVE_DGX = {
    "timestamp": None,  # filled with "now" by the helper
    "stream": "dgx",
    "ok": True,
    "details": {
        "nodes": [
            {"name": "/Users/x/.sparkrun-local/recipes/local-fixed/qwen3.8-flash-next-nvfp4-solo-vllm.yaml (10.0.0.1)", "ok": True},
            {"name": "/Users/x/.sparkrun-local/recipes/local-fixed/qwen3.8-flash-next-nvfp4-solo-vllm.yaml (10.0.0.2)", "ok": True},
        ],
    },
}

# kmon_orch_tokens.json — GAP-F-1's trigger. cards-monitor's token ledger
# (kanban_live.py:250 LEDGER_PATH) appends [unix_ts, tokens_in, tokens_out]
# triples at a top-level JSON list. Shape preserved, numbers invented.
LIVE_KMON_TOKENS = [
    [1793267614, 7173789216, 46372840],
    [1793267914, 7174001024, 46380001],
    [1793268214, 7174220033, 46391277],
]

# orch_waker.json — GAP-F-2 leg 1: the stopgap waker's own schema. No `ok`,
# no `last_check`, no `timestamp` — verify asserts NOTHING about it today.
LIVE_ORCH_WAKER = {
    "last": {"flightdeck": 3380, "hscc": 78375, "kronos": 2264},
    "pending": {},
    "sent": {"hscc": 1793518940.5806952, "kronos": 1793423751.973366},
}

# kanban_paused.json — GAP-F-2 leg 2: literally `{}` on the live box.
LIVE_KANBAN_PAUSED = {}

# reasoning_policy.json — GAP-F-2 leg 3: config snapshot, no ok, no timestamp.
LIVE_REASONING_POLICY = {
    "template": "4node-flash-next-all",
    "tiers": {"max": "xhigh", "standard": "medium", "light": "low"},
}

# The healthy daemon streams the live dir really carries.
LIVE_STREAM_NAMES = (
    "dispatcher", "engine_wedge", "gateway", "heartbeat", "idle", "local",
    "nas", "post_boot", "proxy", "triggers", "watchdog", "workers",
)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def build_live_state_dir(path, *, with_ledger=True, with_ok_less=True,
                         streams=True):
    """Populate ``path`` with the exact shapes the operator's ~/.hscc/state
    holds. Returns the dir path."""
    os.makedirs(path, exist_ok=True)
    if streams:
        for name in LIVE_STREAM_NAMES:
            doc = {"timestamp": _now_iso(), "stream": name, "ok": True,
                   "message": "ok"}
            # half the live streams also carry last_check (dispatcher, idle,
            # post_boot, proxy, triggers, watchdog, workers, engine_wedge)
            if name in ("dispatcher", "idle", "post_boot", "proxy", "triggers",
                        "watchdog", "workers", "engine_wedge"):
                doc["last_check"] = _now_iso()
            with open(os.path.join(path, f"{name}.json"), "w") as fh:
                json.dump(doc, fh)
        # dgx carries ok+timestamp only
        dgx = dict(LIVE_DGX, timestamp=_now_iso())
        with open(os.path.join(path, "dgx.json"), "w") as fh:
            json.dump(dgx, fh)
    if with_ledger:
        with open(os.path.join(path, "kmon_orch_tokens.json"), "w") as fh:
            json.dump(LIVE_KMON_TOKENS, fh)
    if with_ok_less:
        with open(os.path.join(path, "orch_waker.json"), "w") as fh:
            json.dump(LIVE_ORCH_WAKER, fh)
        with open(os.path.join(path, "kanban_paused.json"), "w") as fh:
            json.dump(LIVE_KANBAN_PAUSED, fh)
        with open(os.path.join(path, "reasoning_policy.json"), "w") as fh:
            json.dump(LIVE_REASONING_POLICY, fh)
    return path


class TestCheckDaemonStreamsSchemaTolerance:
    """GAP-F-1 (crash on non-dict state file) + GAP-F-2 (assert-nothing files
    reported healthy). See card t_9ece1a8d / audit F."""

    def test_f1_list_shaped_state_file_never_crashes(self, tmp_path):
        """GAP-F-1: a LIST-shaped state file (the real kmon_orch_tokens.json)
        must be reported, not crash the whole verify table."""
        from hscc_daemon.verify import check_daemon_streams
        state_dir = build_live_state_dir(
            tmp_path / "state", with_ok_less=False)

        result = check_daemon_streams(state_dir=str(state_dir))  # pre-fix: AttributeError

        assert result["name"] == "daemon_streams"
        detail = result["detail"]
        assert "kmon_orch_tokens.json" in detail
        assert "unrecognized schema" in detail
        # Per-check isolation: the crash used to kill every OTHER verify row.
        # The remaining 13 healthy streams must still have been evaluated.
        assert "checked=13" in detail

    def test_f1_non_dict_scalars_reported_not_crashed(self, tmp_path):
        """Same crash class for any non-dict JSON (a list of ints, a string,
        a bare number) — none may raise."""
        from hscc_daemon.verify import check_daemon_streams
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "weird_list.json").write_text("[1, 2, 3]")
        (state_dir / "weird_str.json").write_text('"hello"')
        (state_dir / "weird_int.json").write_text("42")
        (state_dir / "weird_null.json").write_text("null")

        result = check_daemon_streams(state_dir=str(state_dir))  # pre-fix: AttributeError

        assert result["name"] == "daemon_streams"
        for fn in ("weird_list.json", "weird_str.json", "weird_int.json",
                   "weird_null.json"):
            assert fn in result["detail"]

    def test_f1_non_dict_never_aborts_remaining_checks(self, tmp_path):
        """A stream that WOULD fail (ok=False) after the non-dict file in
        sort order must still be reported — the loop must not abort."""
        from hscc_daemon.verify import check_daemon_streams
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # sorts before "zz_*.json"
        (state_dir / "aa_ledger.json").write_text("[[1, 2, 3]]")
        now = _now_iso()
        (state_dir / "zz_real.json").write_text(json.dumps(
            {"ok": False, "last_check": now, "timestamp": now}))

        result = check_daemon_streams(state_dir=str(state_dir))

        assert result["ok"] is False
        assert "aa_ledger.json: unrecognized schema" in result["detail"]
        assert "zz_real.json: ok=False" in result["detail"]

    def test_f2_ok_less_files_are_not_reported_healthy(self, tmp_path):
        """GAP-F-2: orch_waker.json (stopgap schema), kanban_paused.json ({}),
        reasoning_policy.json carry no ok and no timestamp — the check asserts
        nothing about them, so it may not claim they are healthy."""
        from hscc_daemon.verify import check_daemon_streams
        state_dir = build_live_state_dir(
            tmp_path / "state", with_ledger=False)

        result = check_daemon_streams(state_dir=str(state_dir))

        assert result["ok"] is None, result["detail"]
        detail = result["detail"]
        for fn in ("orch_waker.json", "kanban_paused.json",
                   "reasoning_policy.json"):
            assert fn in detail
        assert "not-checked" in detail
        # The 13 real streams ARE checked and healthy — say so.
        assert "checked=13" in detail

    def test_live_state_dir_verdict_is_never_green(self, tmp_path):
        """The exact live directory (ledger + ok-less files + healthy streams):
        survive it (no crash) and report non-green, naming every file the
        check could not assert anything about."""
        from hscc_daemon.verify import check_daemon_streams
        state_dir = build_live_state_dir(tmp_path / "state")

        result = check_daemon_streams(state_dir=str(state_dir))

        assert result["ok"] is not True          # never "all healthy"
        assert result["ok"] is not False         # unrecognised != a fault
        for fn in ("kmon_orch_tokens.json", "orch_waker.json",
                   "kanban_paused.json", "reasoning_policy.json"):
            assert fn in result["detail"]

    def test_run_all_survives_live_shaped_state_and_other_rows_report(
            self, tmp_path):
        """End-to-end: run_all with the live-shaped state dir must return
        every check row — the GAP-F-1 crash took the whole table (and rc 1
        with no JSON) with it."""
        from hscc_daemon.verify import run_all
        # Load the sibling test module's hermetic helper by PATH (not via
        # sys.path) so this test works whether pytest is invoked from the
        # repo root (scripts/run_tests.sh) or from hscc_daemon/ directly.
        import importlib.util
        _tv = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "test_verify.py")
        _spec = importlib.util.spec_from_file_location("_tv_helpers", _tv)
        assert _spec is not None and _spec.loader is not None
        _tvmod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_tvmod)
        _hermetic_new_checks_overrides = _tvmod._hermetic_new_checks_overrides
        assert_run_all_rows = _tvmod.assert_run_all_rows

        state_dir = build_live_state_dir(tmp_path / "state")

        result = run_all(
            state_dir=str(state_dir),
            plugins_dir=str(tmp_path / "missing-plugins"),
            config=str(tmp_path / "missing.yaml"),
            profiles_dir=str(tmp_path / "empty-profiles"),
            serving_path=str(tmp_path / "missing-serving.json"),
            **_hermetic_new_checks_overrides(tmp_path),
        )

        # Roster-based presence assertion (test_verify.EXPECTED_RUN_ALL_ROWS),
        # not a bare count: this test once hard-asserted len == 13 and the
        # combined stamp for GAP-E-2 (row 14) broke it — a count pin on a
        # table whose rows other cards legitimately add is a collision
        # generator. GAP-F-1's actual failure class — the whole table missing
        # or a row losing its shape — is what this asserts now.
        names = assert_run_all_rows(result)
        assert "daemon_streams" in names
        assert "profile_endpoints" in names
        row = [c for c in result["checks"] if c["name"] == "daemon_streams"][0]
        assert row["ok"] is not True
        assert "kmon_orch_tokens.json" in row["detail"]
