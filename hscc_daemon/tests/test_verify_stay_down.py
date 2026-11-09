"""Tests for the STAY-DOWN verify reader (t_97f2b38c) — the READER half.

The bug (daemon.log 2026-11-08, addresses redacted to the sanctioned 10.0.0.x
range): after the operator typed `hscc cluster down`, `hscc verify` still
printed ``✗ daemon_streams ... dgx.json: ok=False; gateway.json: ok=False`` —
the serving layer reported as faulty for an instruction the operator gave on
purpose. The excuse that exists for AUTO DOWN
(``_intentional_window_verdict`` + ``intentional == "autodown"``) never fires
for a manual down, because a manual down never latches the intentional watchdog
block. The writers (t_ba1424b7: workers/proxy; t_a19fd5d9: dgx/gateway) now
stamp ``intentional: "operator_down"`` on the streams they stand down; this
module reads it.

Proof shape (the card's acceptance list, one test each):
  * marker + confirmed record      -> not a fault, and the human detail says
    ``fleet deliberately down (<reason>)`` naming the record's own reason;
  * marker + NO record (file missing / corrupt / shapeless / a raising
    predicate) -> STILL a fault (fail-safe both ways: a lost marker must never
    make a real fault invisible);
  * NO marker + confirmed record   -> STILL a fault (a stream that does not
    explain itself is a fault — the existing contract);
  * the autodown window case       -> byte-identical labels and verdict to what
    this check produced before the second excuse existed;
  * both markers apply             -> the autodown wording wins (branch order).

Discipline: the record is seeded through the PRODUCTION writer
(``post_boot.clear_expected_up``), never hand-planted JSON, so the reader cannot
drift from the writer's shape without failing here. The marker path is
redirected suite-wide by conftest and pinned per test (test_post_boot.py /
test_stay_down_health.py's convention). Nothing here shells out, sshes, calls
sparkrun, or touches a live fleet — unit tests over a tmp state dir only.
"""

import json

import pytest

from hscc_daemon import post_boot as pb
from hscc_daemon.state import now_iso

# The three sanctioned `clear_expected_up(reason)` wordings on current main,
# verbatim: the CLI verb, the slash verb, and the idle teardown. They are data
# here (a label, never a discriminator) — see _operator_down_verdict's
# "WHICH WRITERS" decision.
REASON_OPERATOR = "hscc cluster down"            # hscc-cluster/hscc.py
REASON_SLASH = "/cluster-down"                   # hscc-commands/__init__.py
REASON_IDLE = "autodown: intentional idle teardown"  # hscc_daemon/autodown.py

LABEL_OPERATOR = "fleet deliberately down (hscc cluster down)"
LABEL_BARE = "fleet deliberately down"
# The pre-existing autodown wording, unchanged by this card and pinned here so a
# reword of EITHER excuse fails loudly in this file.
AUTO_LABEL = "intentionally down by autodown"
SERVING = ("dgx", "gateway", "workers", "proxy")


# ── fixtures / helpers ─────────────────────────────────────────────────────

@pytest.fixture
def marker(tmp_path, monkeypatch):
    """The expected-up marker. conftest already redirects post_boot's constant
    for the whole suite; pin it explicitly (test_post_boot.py's convention) so
    these tests say which file they are about."""
    path = str(tmp_path / "expected-up.json")
    monkeypatch.setattr(pb, "EXPECTED_UP_FILE", path)
    return path


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


def _stand_down(marker, reason=REASON_OPERATOR):
    """Put the fleet in the state `hscc cluster down` leaves on disk — written
    by the REAL producer, not hand-planted JSON."""
    pb.clear_expected_up(reason, path=marker)
    assert pb.deliberately_down(marker)["reason"] == reason


def _write_raw(path, data):
    """Plant arbitrary marker bytes (negative controls: present-but-not-a-record)."""
    with open(path, "w") as f:
        if isinstance(data, str):
            f.write(data)
        else:
            json.dump(data, f)


def _stream(state_dir, name, ok, **extra):
    data = {"ok": ok, "timestamp": now_iso(), "last_check": now_iso(),
            "stream": name, **extra}
    (state_dir / f"{name}.json").write_text(json.dumps(data))


def _arm_intentional(tmp_path, monkeypatch, state="down"):
    """Latched intentional watchdog block + autodown config at `state` — the
    same arming test_verify.py uses, so the two excuses are compared under the
    same conditions."""
    from hscc_daemon import autodown as _ad
    from hscc_daemon import lifecycle as _lc
    block_file = str(tmp_path / "watchdog-block.json")
    monkeypatch.setattr(_lc, "WATCHDOG_BLOCK_FILE", block_file)
    _lc.save_watchdog_block({
        "blocked": True, "intentional": "autodown",
        "reason": REASON_IDLE})
    ad_file = str(tmp_path / "autodown.json")
    monkeypatch.setattr(_ad, "AUTODOWN_FILE", ad_file)
    _ad.save_config({**_ad.DEFAULT_CONFIG, "enabled": True,
                     "state": state,
                     "down_since": "2026-01-01T00:00:00+00:00"})


# ── the card's acceptance cases ────────────────────────────────────────────

class TestConfirmedStayDownExcuses:
    """Stream marked `operator_down` + `deliberately_down()` confirms ⇒ the
    stream is NOT a fault, and the operator is told why."""

    def test_serving_layer_stay_down_is_not_a_fault(self, marker, state_dir):
        from hscc_daemon.verify import check_daemon_streams
        _stand_down(marker)
        for name in SERVING:
            _stream(state_dir, name, False, intentional="operator_down",
                    message=f"fleet deliberately down — not relaunching ({name})")
        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is True
        assert "REAL FAILURES" not in result["detail"]
        assert "ok=False" not in result["detail"]

    def test_human_detail_names_the_real_cause(self, marker, state_dir):
        """The deliverable the operator reads: the label carries the RECORD's
        reason, per excused stream."""
        from hscc_daemon.verify import check_daemon_streams
        _stand_down(marker)
        _stream(state_dir, "dgx", False, intentional="operator_down")
        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["detail"] == (
            f"ok=0 healthy, {LABEL_OPERATOR}: "
            f"dgx.json: {LABEL_OPERATOR}")

    def test_every_stream_says_so_gets_the_label(self, marker, state_dir):
        from hscc_daemon.verify import check_daemon_streams
        _stand_down(marker)
        for name in SERVING:
            _stream(state_dir, name, False, intentional="operator_down")
        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is True
        for name in SERVING:
            assert f"{name}.json: {LABEL_OPERATOR}" in result["detail"]

    @pytest.mark.parametrize("reason", [REASON_OPERATOR, REASON_SLASH, REASON_IDLE])
    def test_every_record_producer_wording_excuses(self, marker, state_dir,
                                                   reason):
        """All THREE sanctioned `clear_expected_up` wordings stand the fleet down
        for verify. The reason is copied into the label and is never a
        discriminator — no substring of it is tested anywhere in verify.py."""
        from hscc_daemon.verify import check_daemon_streams
        _stand_down(marker, reason=reason)
        _stream(state_dir, "gateway", False, intentional="operator_down")
        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is True
        assert f"fleet deliberately down ({reason})" in result["detail"]

    def test_record_without_reason_renders_the_bare_label(self, marker,
                                                          state_dir):
        """A record with a timestamp and no reason is still a stay-down; the
        label must not invent a reason or print empty parentheses."""
        from hscc_daemon.verify import check_daemon_streams
        _write_raw(marker, {pb.DELIBERATE_DOWN_KEY: {"set_at": now_iso()}})
        _stream(state_dir, "proxy", False, intentional="operator_down")
        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is True
        assert f"proxy.json: {LABEL_BARE}" in result["detail"]
        assert "()" not in result["detail"]

    def test_excused_stream_that_goes_silent_is_still_a_fault(self, marker,
                                                              state_dir):
        """The excuse explains an unhealthy tick, not a dead check: the recency
        rule still applies to an excused stream."""
        from hscc_daemon.verify import check_daemon_streams
        _stand_down(marker)
        _stream(state_dir, "dgx", False, intentional="operator_down",
                last_check="2020-01-01T00:00:00+00:00",
                timestamp="2020-01-01T00:00:00+00:00")
        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is False
        assert "dgx.json: stale" in result["detail"]


class TestFailSafeStillReportsFaults:
    """Either key alone ⇒ still a fault. A lost marker must never make a real
    fault invisible (D1), and a stream that does not explain itself is a fault
    (the pre-existing contract)."""

    def test_marker_with_no_record_file_is_a_fault(self, marker, state_dir):
        from hscc_daemon.verify import check_daemon_streams
        # No _stand_down: the marker file does not exist at all.
        _stream(state_dir, "dgx", False, intentional="operator_down")
        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is False
        assert "dgx.json: ok=False" in result["detail"]

    def test_marker_with_corrupt_record_file_is_a_fault(self, marker,
                                                        state_dir):
        from hscc_daemon.verify import check_daemon_streams
        _write_raw(marker, "{not json at all")
        _stream(state_dir, "dgx", False, intentional="operator_down")
        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is False
        assert "dgx.json: ok=False" in result["detail"]

    @pytest.mark.parametrize("record", [
        {},                                # empty: asserts nothing
        {"reason": REASON_OPERATOR},       # no set_at: not a record we vouch for
        "fleet down",                      # not a dict
        None,                              # key present, value null
    ])
    def test_shapeless_record_is_a_fault(self, marker, state_dir, record):
        from hscc_daemon.verify import check_daemon_streams
        _write_raw(marker, {pb.DELIBERATE_DOWN_KEY: record})
        _stream(state_dir, "dgx", False, intentional="operator_down")
        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is False
        assert "dgx.json: ok=False" in result["detail"]

    def test_raising_predicate_is_a_fault_and_never_raises(self, monkeypatch,
                                                           state_dir):
        """The seam must swallow its predicate's failure into "no evidence" —
        verify still reports the fault, and the row never becomes a traceback."""
        from hscc_daemon import post_boot
        from hscc_daemon.verify import check_daemon_streams

        def _boom(*a, **kw):
            raise RuntimeError("predicate exploded")
        monkeypatch.setattr(post_boot, "deliberately_down", _boom)
        _stream(state_dir, "workers", False, intentional="operator_down")
        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is False
        assert "workers.json: ok=False" in result["detail"]

    def test_record_confirms_but_stream_does_not_explain_itself(self, marker,
                                                                state_dir):
        """Control A: the record is set (fleet deliberately down) but this stream
        carries no marker ⇒ its `ok: False` is a REAL fault."""
        from hscc_daemon.verify import check_daemon_streams
        _stand_down(marker)
        _stream(state_dir, "gateway", False)
        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is False
        assert "gateway.json: ok=False" in result["detail"]
        assert LABEL_OPERATOR not in result["detail"]

    def test_record_confirms_but_a_real_failure_sits_alongside(self, marker,
                                                               state_dir):
        """An excused stay-down stream never hides a genuine failure in another
        one, and the real failure stays visible next to the label."""
        from hscc_daemon.verify import check_daemon_streams
        _stand_down(marker)
        _stream(state_dir, "dgx", False, intentional="operator_down")
        _stream(state_dir, "heartbeat", False)
        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is False
        assert "heartbeat.json: ok=False" in result["detail"]
        assert "REAL FAILURES" in result["detail"]
        assert LABEL_OPERATOR in result["detail"]

    def test_marker_with_a_foreign_value_is_a_fault(self, marker, state_dir):
        """Only the exact `operator_down` marker is this excuse; anything else
        in that key is not evidence of anything."""
        from hscc_daemon.verify import check_daemon_streams
        _stand_down(marker)
        _stream(state_dir, "proxy", False, intentional="operator_offline")
        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is False
        assert "proxy.json: ok=False" in result["detail"]


class TestAutodownPathUnchanged:
    """D2: the two excuses are independent. The autodown wording, labels and
    verdicts stay byte-identical — including when a stay-down record is ALSO
    present, which is the normal idle-teardown state (autodown.teardown calls
    clear_expected_up itself)."""

    def _autodown_state(self, state_dir):
        for name in ("watchdog", "dgx", "gateway"):
            _stream(state_dir, name, False, intentional="autodown",
                    message=f"intentional autodown ({name})")

    def test_labels_and_verdict_are_byte_identical_with_a_record_present(
            self, tmp_path, marker, state_dir, monkeypatch):
        from hscc_daemon.verify import check_daemon_streams
        _arm_intentional(tmp_path, monkeypatch)
        self._autodown_state(state_dir)
        without_record = check_daemon_streams(state_dir=str(state_dir))

        _stand_down(marker)          # the idle teardown ALSO stamps the record
        with_record = check_daemon_streams(state_dir=str(state_dir))
        assert with_record == without_record
        assert with_record["ok"] is True
        assert "intentionally down by autodown" in with_record["detail"]
        assert LABEL_BARE not in with_record["detail"]

    def test_autodown_marker_outside_a_window_is_not_excused_by_the_record(
            self, marker, state_dir):
        """No window ⇒ the autodown branch does not fire, and the stay-down
        record must NOT borrow it: a stream tagged `autodown` while the fleet is
        not in a window is still an unexplained fault."""
        from hscc_daemon.verify import check_daemon_streams
        _stand_down(marker)
        _stream(state_dir, "dgx", False, intentional="autodown",
                message="intentional autodown (dgx)")
        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is False
        assert "dgx.json: ok=False" in result["detail"]

    def test_waking_wording_is_untouched_by_the_record(self, tmp_path, marker,
                                                       state_dir, monkeypatch):
        """A wake renders 'waking from autodown (serving layer starting)' — the
        card says the waking wording does NOT extend to a deliberate down."""
        from hscc_daemon.verify import check_daemon_streams
        _arm_intentional(tmp_path, monkeypatch, state="waking")
        _stand_down(marker)
        _stream(state_dir, "gateway", False, intentional="autodown",
                message="intentional autodown (gateway)")
        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is True
        assert "waking from autodown (serving layer starting)" in result["detail"]
        assert LABEL_BARE not in result["detail"]

    def test_mixed_fleet_keeps_both_labels_and_both_excuses(self, tmp_path,
                                                            marker, state_dir,
                                                            monkeypatch):
        """The proxy's stay-down gate sits ABOVE its own autodown tagging, so one
        tick of an idle teardown really can tag proxy `operator_down` while the
        other streams are tagged `autodown`. Both excuses render, each with its
        own wording, and nothing fails."""
        from hscc_daemon.verify import check_daemon_streams
        _arm_intentional(tmp_path, monkeypatch)
        _stand_down(marker)
        _stream(state_dir, "dgx", False, intentional="autodown",
                message="intentional autodown (dgx)")
        _stream(state_dir, "proxy", False, intentional="operator_down")
        result = check_daemon_streams(state_dir=str(state_dir))
        assert result["ok"] is True
        assert "intentionally down by autodown: dgx.json" in result["detail"]
        assert f"{LABEL_OPERATOR}: proxy.json" in result["detail"]
        assert "REAL FAILURES" not in result["detail"]


class TestJsonRecordShape:
    """Card item 3: `hscc verify --json` must emit the SAME KIND of record for
    the operator-down excuse as for the autodown one — only the label differs."""

    def test_operator_excused_row_matches_the_autodown_row_shape(
            self, tmp_path, marker, state_dir, monkeypatch):
        """Same keys, same name, same `ok`, same detail TEMPLATE — one label and
        one stream each. `hscc verify --json` is a raw dump of these dicts, so a
        new key here would be a machine-view change, which the card forbids."""
        from hscc_daemon.verify import check_daemon_streams
        # autodown-excused row.
        _arm_intentional(tmp_path, monkeypatch)
        _stream(state_dir, "dgx", False, intentional="autodown")
        auto = check_daemon_streams(state_dir=str(state_dir))
        # operator-excused row: same single failing stream, no window.
        (state_dir / "dgx.json").unlink()
        (tmp_path / "watchdog-block.json").unlink()   # window closed again
        _stand_down(marker)
        _stream(state_dir, "dgx", False, intentional="operator_down")
        oper = check_daemon_streams(state_dir=str(state_dir))

        assert set(oper) == set(auto) == {"name", "ok", "detail"}
        assert oper["name"] == auto["name"] == "daemon_streams"
        assert oper["ok"] is auto["ok"] is True
        # Same template — "<label>: <stream>.json: <label>" — with exactly one
        # intended difference: the autodown line ALSO carries that stream's own
        # `message`/`reason` in parentheses (its label names no reason), while
        # the stay-down label already carries the record's reason, so its line
        # does not repeat it. Nothing else differs, and no new key appears.
        assert auto["detail"] == (
            f"ok=0 healthy, {AUTO_LABEL}: dgx.json: {AUTO_LABEL} (autodown)")
        assert oper["detail"] == (
            f"ok=0 healthy, {LABEL_OPERATOR}: dgx.json: {LABEL_OPERATOR}")
        assert (oper["detail"].replace(LABEL_OPERATOR, "<L>") ==
                auto["detail"].replace(f"{AUTO_LABEL} (autodown)", "<L>")
                .replace(AUTO_LABEL, "<L>"))


class TestVerdictHelpers:
    """The seam itself: injectable, never raising, fail-safe toward reporting
    faults, and independent of the window verdict."""

    def test_injected_predicate_result_is_returned_verbatim(self):
        from hscc_daemon.verify import _operator_down_verdict
        record = {"set_at": "2026-11-09T12:00:00+00:00",
                  "reason": REASON_OPERATOR}
        assert _operator_down_verdict(lambda: record) is record

    @pytest.mark.parametrize("value", [
        None, {}, [], "down", 0, 1, True,
        {}, {"reason": "x"}, {"set_at": ""}, {"set_at": None},
    ])
    def test_anything_that_is_not_a_record_reads_as_no_evidence(self, value):
        from hscc_daemon.verify import _operator_down_verdict
        assert _operator_down_verdict(lambda: value) is None

    def test_predicate_raising_is_swallowed_into_no_evidence(self):
        from hscc_daemon.verify import _operator_down_verdict

        def _boom():
            raise OSError("unreadable")
        assert _operator_down_verdict(_boom) is None

    def test_default_predicate_is_the_real_post_boot_accessor(self, marker):
        """No injection: the helper must read post_boot's OWN predicate, so the
        reader cannot drift from the writer's key or file."""
        from hscc_daemon.verify import _operator_down_verdict
        assert _operator_down_verdict() is None
        _stand_down(marker)
        assert _operator_down_verdict() == pb.deliberately_down(marker)

    @pytest.mark.parametrize("record,expected", [
        ({"set_at": "x", "reason": "hscc cluster down"},
         "fleet deliberately down (hscc cluster down)"),
        ({"set_at": "x", "reason": "  /cluster-down  "},
         "fleet deliberately down (/cluster-down)"),
        ({"set_at": "x"}, LABEL_BARE),
        ({"set_at": "x", "reason": ""}, LABEL_BARE),
        ({"set_at": "x", "reason": "   "}, LABEL_BARE),
        ({"set_at": "x", "reason": None}, LABEL_BARE),
        ({"set_at": "x", "reason": 42}, LABEL_BARE),
    ])
    def test_label_derives_from_the_record_and_never_invents_a_reason(
            self, record, expected):
        from hscc_daemon.verify import _operator_down_label
        assert _operator_down_label(record) == expected
