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
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from hscc_daemon import post_boot as pb
from hscc_daemon import state as st
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


# ── t_374d7363: the two LIVE-PROBE rows (proxy + chat round-trip) ──────────
#
# Same instruction, different shape of evidence. These two rows do not read a
# stream — they hit the serving layer over HTTP right now, so they have no
# `intentional` key of their own to agree with the durable record. The card's
# design question is what the second key is; the answer pinned here is THE
# DAEMON'S OWN CURRENT STAY-DOWN STAMP on the stream that supervises what the
# row probes (`proxy` for the proxy row; the serving-layer supervisors for the
# chat row), which is a DIFFERENT writer from post_boot's record, so the two
# really are independent. Both keys, or the row reports its fault.
#
# The shape a deliberate `hscc cluster down` actually leaves (read, not run —
# see the card/PR evidence): `cmd_cluster_down` runs one
# `sparkrun stop --all --cluster …`, which sweeps the DGX hosts for containers
# (sparkrun/api/_stop_all.py) and never touches the proxy, a LOCAL process on
# :4000 (health.check_proxy). So the proxy SURVIVES, its discovery sweep prunes
# the dead backends, and it answers 200 `{"data": []}` ⇒
# `detail == "no models in data list"` (health.py:1108-1113 documents that exact
# body). The `connection error:` shape only happens if the proxy itself died,
# which `cluster down` does not do — both shapes are covered below anyway.

PROXY_EXCUSED = (f"{LABEL_OPERATOR}: serving layer not serving "
                 f"models, none expected")
PROXY_WINDOW = (f"{AUTO_LABEL}: serving layer not serving "
                f"models, none expected")
CHAT_EXCUSED = f"{LABEL_OPERATOR}: serving layer not serving, chat not expected"
CHAT_WINDOW = f"{AUTO_LABEL}: serving layer not serving, chat not expected"
SERVING_SUPERVISORS = ("workers", "dgx", "gateway", "proxy")


@pytest.fixture
def daemon_state(tmp_path, monkeypatch):
    """The daemon's OWN state funnel (`state.STATE_DIR`) redirected to a tmp dir
    — the directory `state.read_state` reads, which is what the stay-down witness
    is read through. Deliberately a different fixture from `state_dir` above:
    that one is `check_daemon_streams`'s argument, this one is the funnel."""
    d = tmp_path / "daemon-state"
    d.mkdir()
    monkeypatch.setattr(st, "STATE_DIR", str(d))
    return d


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _daemon_stamp(name, age_s=0, **extra):
    """`~/.hscc/state/<name>.json` the way the daemon's stay-down supervisor
    writes it (health.check_proxy / check_workers / check_dgx / check_gateway),
    through the production `state.write_state` funnel."""
    data = {"ok": True, "intentional": "operator_down",
            "last_check": _ago(age_s),
            "message": f"fleet deliberately down — not relaunching ({name})",
            **extra}
    st.write_state(name, data)
    return data


def _proxy_http(data_list, calls=None):
    """Stand in for `urlopen`: 200 + a parseable /v1/models body."""
    def _fake(req, timeout=None):
        if calls is not None:
            calls.append(req)
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = json.dumps({"data": data_list}).encode()
        resp.__enter__ = lambda self: self
        resp.__exit__ = lambda self, *a: None
        return resp
    return _fake


def _dead_proxy(calls=None):
    """Stand in for a proxy process that is not there at all."""
    def _fake(req, timeout=None):
        if calls is not None:
            calls.append(req)
        raise urllib.error.URLError("Connection refused")
    return _fake


def _serving_file(tmp_path, units=({"nodes": ["10.0.0.1"], "port": 8000},)):
    p = tmp_path / "serving.json"
    p.write_text(json.dumps({"units": list(units)}))
    return str(p)


def _failing_probe(calls):
    """Every unit answers the way a stopped container answers: nothing."""
    def _probe(node, port, timeout=None, max_tokens=None):
        calls.append((node, port))
        return {"ok": False, "error": "Connection refused", "status": None}
    return _probe


class TestProxyRowDuringDeliberateDown:
    """check_proxy — the row that survives the down with an empty catalogue."""

    def test_empty_catalogue_is_excused_without_touching_the_proxy(
            self, marker, daemon_state):
        from hscc_daemon.verify import check_proxy
        _stand_down(marker)
        _daemon_stamp("proxy", port=4000)
        calls = []
        with patch("hscc_daemon.verify.urllib.request.urlopen",
                   side_effect=_proxy_http([{"id": "m"}], calls)):
            result = check_proxy(url="http://localhost:4000/v1/models")
        assert result == {"name": "proxy", "ok": True, "detail": PROXY_EXCUSED}
        # The excuse is decided BEFORE probing — the daemon's own stay-down gates
        # all refuse to touch the serving layer while the record stands.
        assert calls == []

    def test_human_row_is_the_exact_line_the_operator_reads(self, marker,
                                                            daemon_state):
        """The deliverable: one line, the record's own reason, no invented text,
        no empty parentheses, no second parenthetical."""
        from hscc_daemon.verify import check_proxy
        _stand_down(marker, reason=REASON_OPERATOR)
        _daemon_stamp("proxy")
        calls = []
        with patch("hscc_daemon.verify.urllib.request.urlopen",
                   side_effect=_proxy_http([], calls)):
            result = check_proxy(url="http://localhost:4000/v1/models")
        assert result["detail"] == PROXY_EXCUSED
        assert "()" not in result["detail"]

    @pytest.mark.parametrize("reason", [REASON_OPERATOR, REASON_SLASH,
                                        REASON_IDLE])
    def test_every_record_producer_wording_excuses_the_proxy_row(
            self, marker, daemon_state, reason):
        """Same rule as the stream reader: all three sanctioned writers of the
        record stand the proxy row down; the reason is data, never a test."""
        from hscc_daemon.verify import check_proxy
        _stand_down(marker, reason=reason)
        _daemon_stamp("proxy")
        with patch("hscc_daemon.verify.urllib.request.urlopen",
                   side_effect=_proxy_http([])):
            result = check_proxy(url="http://localhost:4000/v1/models")
        assert result["ok"] is True
        assert f"fleet deliberately down ({reason})" in result["detail"]

    def test_record_without_reason_renders_the_bare_label(self, marker,
                                                          daemon_state):
        from hscc_daemon.verify import check_proxy
        _write_raw(marker, {pb.DELIBERATE_DOWN_KEY: {"set_at": now_iso()}})
        _daemon_stamp("proxy")
        with patch("hscc_daemon.verify.urllib.request.urlopen",
                   side_effect=_proxy_http([])):
            result = check_proxy(url="http://localhost:4000/v1/models")
        assert result["detail"] == (
            f"{LABEL_BARE}: serving layer not serving models, none expected")

    def test_excused_row_has_no_new_json_key(self, marker, daemon_state):
        """`hscc verify --json` dumps these dicts — same keys as the row it
        mirrors (the autodown-excused proxy row), or the machine view changed."""
        from hscc_daemon.verify import check_proxy
        _stand_down(marker)
        _daemon_stamp("proxy")
        with patch("hscc_daemon.verify.urllib.request.urlopen",
                   side_effect=_proxy_http([])):
            stay_down = check_proxy(url="http://localhost:4000/v1/models")
        with patch("hscc_daemon.verify.urllib.request.urlopen",
                   side_effect=_proxy_http([{"id": "m"}])):
            healthy = check_proxy(url="http://localhost:4000/v1/models")
        assert set(stay_down) == set(healthy) == {"name", "ok", "detail"}

    # ── the negative controls: either key alone is the shape of the bug ─────

    def test_empty_catalogue_still_fails_without_the_witness(self, marker,
                                                             daemon_state):
        """Record present, but the daemon has NOT stamped this process down ⇒
        an unexplained empty catalogue is a real fault (D1)."""
        from hscc_daemon.verify import check_proxy
        _stand_down(marker)
        with patch("hscc_daemon.verify.urllib.request.urlopen",
                   side_effect=_proxy_http([])):
            result = check_proxy(url="http://localhost:4000/v1/models")
        assert result == {"name": "proxy", "ok": False,
                          "detail": "no models in data list"}

    def test_empty_catalogue_still_fails_without_the_record(self, marker,
                                                            daemon_state):
        """Witness present, record missing ⇒ a stale claim of a stay-down must
        not hide a proxy that serves nothing."""
        from hscc_daemon.verify import check_proxy
        _daemon_stamp("proxy")
        with patch("hscc_daemon.verify.urllib.request.urlopen",
                   side_effect=_proxy_http([])):
            result = check_proxy(url="http://localhost:4000/v1/models")
        assert result["ok"] is False
        assert result["detail"] == "no models in data list"

    def test_dead_proxy_still_fails_without_the_witness(self, marker,
                                                        daemon_state):
        """The other shape the card names (proxy process gone). Excused only on
        both keys; here the witness is absent, so `connection error` still
        reports."""
        from hscc_daemon.verify import check_proxy
        _stand_down(marker)
        with patch("hscc_daemon.verify.urllib.request.urlopen",
                   side_effect=_dead_proxy()):
            result = check_proxy(url="http://localhost:4000/v1/models")
        assert result["ok"] is False
        assert result["detail"].startswith("connection error:")

    def test_dead_proxy_is_excused_when_both_keys_agree(self, marker,
                                                        daemon_state):
        """A `cluster down` does not kill the proxy, but a down the operator
        caused that ALSO lost the process is still the operator's instruction —
        the excuse is about the fleet, not about which shape came back."""
        from hscc_daemon.verify import check_proxy
        _stand_down(marker)
        _daemon_stamp("proxy")
        calls = []
        with patch("hscc_daemon.verify.urllib.request.urlopen",
                   side_effect=_dead_proxy(calls)):
            result = check_proxy(url="http://localhost:4000/v1/models")
        assert result == {"name": "proxy", "ok": True, "detail": PROXY_EXCUSED}
        assert calls == []

    def test_stamp_the_daemon_stopped_renewing_is_not_evidence(self, marker,
                                                               daemon_state):
        """A stay-down stamp days old means the daemon stopped supervising — the
        row must report, not trust a leftover."""
        from hscc_daemon.verify import check_proxy
        _stand_down(marker)
        _daemon_stamp("proxy", age_s=3 * 24 * 3600)
        with patch("hscc_daemon.verify.urllib.request.urlopen",
                   side_effect=_proxy_http([])):
            result = check_proxy(url="http://localhost:4000/v1/models")
        assert result["ok"] is False
        assert result["detail"] == "no models in data list"

    def test_an_autodown_stamp_is_not_a_stay_down_witness(self, marker,
                                                          daemon_state):
        """Two independent excuses, two markers. No window is in effect here, so
        `autodown` in that key explains nothing and the fault still reports."""
        from hscc_daemon.verify import check_proxy
        _stand_down(marker)
        _daemon_stamp("proxy", intentional="autodown")
        with patch("hscc_daemon.verify.urllib.request.urlopen",
                   side_effect=_proxy_http([])):
            result = check_proxy(url="http://localhost:4000/v1/models")
        assert result["ok"] is False

    def test_a_foreign_marker_value_is_not_evidence(self, marker,
                                                    daemon_state):
        from hscc_daemon.verify import check_proxy
        _stand_down(marker)
        _daemon_stamp("proxy", intentional="operator_offline")
        with patch("hscc_daemon.verify.urllib.request.urlopen",
                   side_effect=_proxy_http([])):
            result = check_proxy(url="http://localhost:4000/v1/models")
        assert result["ok"] is False

    def test_window_wording_still_wins_and_stays_byte_identical(
            self, tmp_path, marker, daemon_state, monkeypatch):
        """D2/precedence: with an autodown window AND a stay-down record AND a
        stay-down stamp, the row renders exactly what it rendered before this
        excuse existed."""
        from hscc_daemon.verify import check_proxy
        _arm_intentional(tmp_path, monkeypatch)
        _stand_down(marker)
        _daemon_stamp("proxy")
        with patch("hscc_daemon.verify.urllib.request.urlopen",
                   side_effect=_proxy_http([])):
            result = check_proxy(url="http://localhost:4000/v1/models")
        assert result == {"name": "proxy", "ok": True, "detail": PROXY_WINDOW}
        assert LABEL_BARE not in result["detail"]

    def test_a_serving_proxy_reports_the_stay_down_before_its_models(
            self, marker, daemon_state):
        """The excuse is judged BEFORE the probe (nothing is touched while the
        record stands), so during a deliberate down the row says the fleet is
        off on purpose even if the LB still lists a stale catalogue. That is the
        same consequence the autodown window gate above already has, and the
        daemon's own proxy supervisor made the same trade-off on purpose
        (health.check_proxy: a hand-started proxy is not health-checked while the
        record stands). Without the witness the very same body is the
        `2 models available` row it always was — the excuse never rewrites a
        healthy verdict into a failing one."""
        from hscc_daemon.verify import check_proxy
        body = _proxy_http([{"id": "m1"}, {"id": "m2"}])
        _stand_down(marker)
        _daemon_stamp("proxy")
        with patch("hscc_daemon.verify.urllib.request.urlopen", side_effect=body):
            result = check_proxy(url="http://localhost:4000/v1/models")
        assert result == {"name": "proxy", "ok": True, "detail": PROXY_EXCUSED}
        (daemon_state / "proxy.json").unlink()
        with patch("hscc_daemon.verify.urllib.request.urlopen", side_effect=body):
            result = check_proxy(url="http://localhost:4000/v1/models")
        assert result == {"name": "proxy", "ok": True,
                          "detail": "2 models available"}


class TestChatRoundtripDuringDeliberateDown:
    """check_chat_roundtrip — the row that probes EVERY serving.json unit."""

    @pytest.mark.parametrize("witness", SERVING_SUPERVISORS)
    def test_excused_by_any_serving_layer_supervisor(self, marker, daemon_state,
                                                     witness):
        """The row's subject is the whole serving layer, so any one of the
        supervisors freshly standing down (plus the record) is the agreement.
        `workers` alone covers an orchestrator-only fleet, which stamps nothing
        else; `dgx` alone covers a fleet with no keep-alive units."""
        from hscc_daemon.verify import check_chat_roundtrip
        _stand_down(marker)
        _daemon_stamp(witness)
        calls = []
        result = check_chat_roundtrip(
            serving_path=_serving_file(daemon_state),
            probe=_failing_probe(calls))
        assert result == {"name": "chat_roundtrip", "ok": True,
                          "detail": CHAT_EXCUSED}
        assert calls == []

    def test_no_witness_probes_every_unit_and_fails(self, marker, daemon_state):
        from hscc_daemon.verify import check_chat_roundtrip
        _stand_down(marker)
        calls = []
        result = check_chat_roundtrip(
            serving_path=_serving_file(daemon_state),
            probe=_failing_probe(calls))
        assert len(calls) == 1
        assert result["ok"] is False
        assert "did not answer" in result["detail"]

    def test_witness_without_the_record_still_probes_and_fails(
            self, marker, daemon_state):
        from hscc_daemon.verify import check_chat_roundtrip
        _daemon_stamp("workers")
        calls = []
        result = check_chat_roundtrip(
            serving_path=_serving_file(daemon_state),
            probe=_failing_probe(calls))
        assert len(calls) == 1
        assert result["ok"] is False

    def test_stale_witness_still_probes_and_fails(self, marker, daemon_state):
        from hscc_daemon.verify import check_chat_roundtrip
        _stand_down(marker)
        _daemon_stamp("workers", age_s=3 * 24 * 3600)
        calls = []
        result = check_chat_roundtrip(
            serving_path=_serving_file(daemon_state),
            probe=_failing_probe(calls))
        assert len(calls) == 1
        assert result["ok"] is False

    def test_window_wording_still_wins_and_stays_byte_identical(
            self, tmp_path, marker, daemon_state, monkeypatch):
        from hscc_daemon.verify import check_chat_roundtrip
        _arm_intentional(tmp_path, monkeypatch)
        _stand_down(marker)
        _daemon_stamp("workers")
        calls = []
        result = check_chat_roundtrip(serving_path=_serving_file(tmp_path),
                                      probe=_failing_probe(calls))
        assert result == {"name": "chat_roundtrip", "ok": True,
                          "detail": CHAT_WINDOW}
        assert calls == []
        assert LABEL_BARE not in result["detail"]

    def test_missing_serving_file_is_excused_before_it_is_even_read(
            self, marker, daemon_state, tmp_path):
        """The stay-down gate sits BEFORE the serving.json load (nothing is
        touched while the record stands), so a serving.json this row could not
        read is part of the down state. Control below: witness gone ⇒ the
        pre-existing `ok: None` unverified row, unchanged."""
        from hscc_daemon.verify import check_chat_roundtrip
        _stand_down(marker)
        _daemon_stamp("workers")
        result = check_chat_roundtrip(
            serving_path=str(tmp_path / "nope.json"),
            probe=_failing_probe([]))
        assert result == {"name": "chat_roundtrip", "ok": True,
                          "detail": CHAT_EXCUSED}
        (daemon_state / "workers.json").unlink()
        result = check_chat_roundtrip(
            serving_path=str(tmp_path / "nope.json"),
            probe=_failing_probe([]))
        assert result["ok"] is None
        assert "unverified" in result["detail"]


class TestStayDownWitnessHelper:
    """The second key itself: fail-safe, per-stream cadence, never raising."""

    def test_a_fresh_stamp_is_current(self, daemon_state):
        from hscc_daemon.verify import _stay_down_witness_current
        _daemon_stamp("proxy")
        assert _stay_down_witness_current("proxy") is True

    def test_a_naive_timestamp_is_read_as_utc(self, daemon_state):
        from hscc_daemon.verify import _stay_down_witness_current
        _daemon_stamp("proxy", last_check=_ago(60).replace("+00:00", ""))
        assert _stay_down_witness_current("proxy") is True

    @pytest.mark.parametrize("case", [
        "no-timestamp", "unparseable-timestamp", "marker-null",
        "marker-missing", "marker-autodown", "marker-foreign-value"])
    def test_a_stamp_that_says_nothing_current_is_not_evidence(
            self, daemon_state, case):
        """Every way a stay-down stamp can fail to be CURRENT evidence reads as
        NO evidence, so the row reports its fault. `write_state` stamps its own
        fresh `timestamp`, so a case that means "no timestamp" has to null BOTH
        keys the reader falls back through."""
        from hscc_daemon.verify import _stay_down_witness_current
        now = now_iso()
        stamp = {
            "no-timestamp": {"intentional": "operator_down",
                             "last_check": None, "timestamp": None},
            "unparseable-timestamp": {"intentional": "operator_down",
                                      "last_check": "yesterday"},
            "marker-null": {"intentional": None, "last_check": now},
            "marker-missing": {"last_check": now},
            "marker-autodown": {"intentional": "autodown", "last_check": now},
            "marker-foreign-value": {"intentional": "operator_offline",
                                     "last_check": now},
        }[case]
        st.write_state("proxy", stamp)
        assert _stay_down_witness_current("proxy") is False

    def test_a_non_dict_stream_is_not_evidence(self, daemon_state):
        from hscc_daemon.verify import _stay_down_witness_current
        (daemon_state / "proxy.json").write_text(json.dumps([1, 2, 3]))
        assert _stay_down_witness_current("proxy") is False

    def test_a_missing_state_dir_is_not_evidence(self, tmp_path, monkeypatch):
        from hscc_daemon.verify import _stay_down_witness_current
        monkeypatch.setattr(st, "STATE_DIR", str(tmp_path / "absent"))
        assert _stay_down_witness_current("proxy") is False

    def test_a_raising_reader_is_swallowed_into_no_evidence(self, monkeypatch):
        from hscc_daemon.verify import _stay_down_witness_current

        def _boom(*a, **kw):
            raise RuntimeError("unreadable")
        monkeypatch.setattr(st, "read_state", _boom)
        assert _stay_down_witness_current("proxy") is False

    def test_the_bound_scales_with_that_streams_own_cadence(self, daemon_state):
        """600 s floor for a fast stream, ~2 cycles for a slow one (nas: 900 s
        ⇒ 2*900+90) — the same rule check_daemon_streams applies to staleness,
        so a slow supervisor is never judged by a fast stream's window."""
        from hscc_daemon.verify import _stay_down_witness_current
        _daemon_stamp("nas", age_s=1200)
        assert _stay_down_witness_current("nas") is True
        _daemon_stamp("proxy", age_s=1200)
        assert _stay_down_witness_current("proxy") is False

    def test_verdict_needs_both_keys_and_injects_its_predicate(self):
        from hscc_daemon.verify import _stay_down_probe_verdict
        record = {"set_at": "2026-11-09T12:00:00+00:00",
                  "reason": REASON_OPERATOR}
        # No witness stream on disk: even a confirmed record excuses nothing.
        assert _stay_down_probe_verdict(("proxy",),
                                        deliberately_down_fn=lambda: record) is None
        # Witness + injected record: excused, through the seam (no ~/.hscc read).
        import hscc_daemon.verify as v
        original = v._stay_down_witness_current
        v._stay_down_witness_current = lambda name, now=None: True
        try:
            assert _stay_down_probe_verdict(
                ("proxy",), deliberately_down_fn=lambda: record) == LABEL_OPERATOR
        finally:
            v._stay_down_witness_current = original
        # Witness agrees, record predicate raises ⇒ no evidence ⇒ report.
        def _boom():
            raise OSError("unreadable")
        assert _stay_down_probe_verdict(("proxy",),
                                        deliberately_down_fn=_boom) is None

    def test_verdict_without_any_witness_name_never_excuses(self, marker,
                                                            daemon_state):
        """An empty witness tuple is a caller bug, and the fail-safe direction
        says it excuses nothing."""
        from hscc_daemon.verify import _stay_down_probe_verdict
        _stand_down(marker)
        _daemon_stamp("proxy")
        assert _stay_down_probe_verdict(()) is None
