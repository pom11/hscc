"""Tests for the STAY-DOWN stream stamp on dgx + gateway (t_a19fd5d9).

The bug: after t_ab75e987 (the durable record) and t_ba1424b7 (the workers/proxy
gates), a deliberate `hscc cluster down` still false-failed `hscc verify`.
`check_workers`/`check_proxy` stood down and the notifier stopped paging, but
`check_dgx` and `check_gateway` — which probe, and therefore correctly report a
unit that is not serving — wrote `ok: False` with NO explanation. verify excuses
a stream only when the stream data carries its own ``intentional`` marker AND
``autodown.classify`` confirms the window (verify.py ``_intentional_window_verdict``),
and a manual down latches no watchdog block, so neither half held: the fleet read
RED for doing exactly what it was told.

The fix under test: both writers read t_ba1424b7's ``health._operator_down()``
seam and, ONLY when the check is already failing, stamp
``intentional: "operator_down"`` + a message naming the record's reason.

What these tests pin (the card's acceptance list):
  * record set + unit not serving -> the check returns EXACTLY as before with
    ok False, and the stream carries intentional=operator_down + the message;
  * no record (fleet meant to be up) -> NO intentional key at all, verdict and
    state otherwise byte-identical to today;
  * autodown window AND record both present -> intentional == "autodown"
    (precedence — one marker, the existing wording);
  * ``deliberately_down`` raising -> no stamp, the check behaves exactly as
    before (fail-safe: no verifiable evidence must never mean "go quiet");
  * the two invariants this card must NOT disturb: a degraded fleet-visibility
    read is still never excused, and a HEALTHY stream during a stay-down is
    never stamped (the stamp explains a failure, it does not launder one).

Discipline: every probe is stubbed (ssh_cmd, http_check, _sparkrun_workloads,
_gateway_job_alive, _check_multiplex_profiles), so nothing here shells out,
touches a node, or waits on a real timeout — same setup as
test_health.TestCheckGatewayWithMultiplex / test_sparkrun_resolution.
Test 4's fail-safe drives the REAL ``_operator_down`` seam (it swallows the
exception into None) rather than stubbing the seam itself.
Address doctrine: sanctioned 10.0.0.x placeholder range only.
"""

import json

import pytest

from hscc_daemon import autodown as ad
from hscc_daemon import health, lifecycle as lc
from hscc_daemon import post_boot as pb
from hscc_daemon import serving, state as state_mod

REASON = "hscc cluster down"
PRIMARY = "10.0.0.2"

# The wording D2 asks for, pinned in ONE place so a reword fails loudly here
# (same convention as tests/test_stay_down_health.py::_expected_line).
def _expected_message(area, reason=REASON):
    return (f"fleet deliberately down — {area} unhealthy is expected while the "
            f"fleet is down, not a fault (reason: {reason})")


# ── fixtures / helpers ─────────────────────────────────────────────────────

@pytest.fixture
def marker(tmp_path, monkeypatch):
    """The expected-up marker holding the stay-down record. conftest redirects
    post_boot's constant suite-wide; pin it explicitly (test_post_boot.py's
    convention) so each test says which file it is about."""
    path = str(tmp_path / "expected-up.json")
    monkeypatch.setattr(pb, "EXPECTED_UP_FILE", path)
    return path


@pytest.fixture
def streams(tmp_path, monkeypatch):
    """Redirect the stream dir + silence the log, and hand back the dir."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
    monkeypatch.setattr(health, "log", lambda *a, **k: None)
    return state_dir


def _stand_down(marker, reason=REASON):
    """Put the fleet in the state `hscc cluster down` leaves on disk — written
    by the REAL producer, not hand-planted JSON, so the key shape cannot drift
    from post_boot without these tests noticing."""
    pb.clear_expected_up(reason, path=marker)
    assert pb.deliberately_down(marker)["reason"] == reason


def _arm_autodown_window(tmp_path, monkeypatch):
    """Latch the intentional-autodown block + confirmed-down state, which is
    what opens ``health._intentional_window()``."""
    monkeypatch.setattr(lc, "WATCHDOG_BLOCK_FILE",
                        str(tmp_path / "watchdog-block.json"))
    monkeypatch.setattr(ad, "AUTODOWN_FILE", str(tmp_path / "autodown.json"))
    lc.save_watchdog_block({"blocked": True, "intentional": "autodown",
                            "reason": "autodown: intentional idle teardown"})
    ad.save_config({**ad.DEFAULT_CONFIG, "enabled": True, "state": "down"})
    assert health._intentional_window() is True


def _stub_dgx(monkeypatch, *, serving_up=False, degraded=False):
    """Pin check_dgx's four probes. `serving_up` drives ssh+vLLM; the workload
    read stays verified (`degraded=False`) unless the test asks for a loss."""
    monkeypatch.setattr(health, "ssh_cmd",
                        lambda *a, **k: {"ok": serving_up,
                                         "output": "reachable" if serving_up else ""})
    monkeypatch.setattr(health, "http_check",
                        lambda url, timeout=5: {"ok": serving_up})
    monkeypatch.setattr(health, "_sparkrun_workloads",
                        lambda: ([], degraded, "degraded" if degraded else "ok"))
    monkeypatch.setattr(serving, "PRIMARY_NODE", PRIMARY)
    monkeypatch.setattr(serving, "VLLM_HEALTH_URL", f"http://{PRIMARY}:8000/health")


def _stub_gateway(monkeypatch, *, backend_up=False, job=None, degraded=None):
    """Pin check_gateway's three probes. `job=None` = the job bit could not be
    verified, `degraded` the named reason for it."""
    monkeypatch.setattr(health, "_gateway_job_alive", lambda: job)
    monkeypatch.setattr(health, "_GATEWAY_PROBE_UNAVAILABLE", degraded)
    monkeypatch.setattr(health, "http_check",
                        lambda url, timeout=5: {"ok": backend_up})
    monkeypatch.setattr(health, "_check_multiplex_profiles",
                        lambda: {"ok": True, "message": "no mux profiles"})
    monkeypatch.setattr(serving, "VLLM_HEALTH_URL", f"http://{PRIMARY}:8000/health")


def _read(streams, name):
    return json.loads((streams / f"{name}.json").read_text())


# ── dgx ────────────────────────────────────────────────────────────────────

class TestDgxStream:
    def test_record_set_stamps_the_failure(self, marker, streams, monkeypatch):
        """Card case 1: a deliberate down + a unit that is not serving. The
        verdict and `ok` are UNCHANGED (still a truthful False); the stream now
        says why."""
        _stub_dgx(monkeypatch, serving_up=False)
        _stand_down(marker)

        assert health.check_dgx() is False
        entry = _read(streams, "dgx")
        assert entry["ok"] is False, (
            "a unit that is not serving is not serving — the stamp must never "
            "flip a failing check to ok=True to make a tool quiet")
        assert entry["intentional"] == "operator_down"
        assert entry["message"] == _expected_message("dgx")
        assert REASON in entry["message"]

    def test_no_record_leaves_the_stream_unexplained(self, marker, streams,
                                                     monkeypatch):
        """Card case 2 (the control): fleet meant to be up, unit not serving =
        a REAL fault. No marker, no explanation, same verdict."""
        _stub_dgx(monkeypatch, serving_up=False)
        assert pb.deliberately_down(marker) is None

        assert health.check_dgx() is False
        entry = _read(streams, "dgx")
        assert entry["ok"] is False
        assert "intentional" not in entry, (
            "a genuine fault must never arrive pre-excused")
        assert "message" not in entry

    def test_healthy_stream_is_never_stamped(self, marker, streams, monkeypatch):
        """The stamp explains a failure; it does not decorate a healthy read.
        A serving unit during a stay-down writes exactly what it wrote before."""
        _stub_dgx(monkeypatch, serving_up=True)
        _stand_down(marker)

        assert health.check_dgx() is True
        entry = _read(streams, "dgx")
        assert entry["ok"] is True
        assert "intentional" not in entry
        assert "message" not in entry

    def test_autodown_window_outranks_the_record(self, marker, streams, monkeypatch,
                                                 tmp_path):
        """Card case 3 (precedence): an autodown window AND a leftover stay-down
        record. The autodown stamp wins — ONE marker, its own existing wording."""
        _stub_dgx(monkeypatch, serving_up=False)
        _stand_down(marker)
        _arm_autodown_window(tmp_path, monkeypatch)

        assert health.check_dgx() is False
        entry = _read(streams, "dgx")
        assert entry["ok"] is False
        assert entry["intentional"] == "autodown"
        assert "intentional autodown" in entry["message"]
        assert "operator_down" not in json.dumps(entry)

    def test_degraded_visibility_is_still_never_excused(self, marker, streams,
                                                        monkeypatch):
        """t_b543e530's invariant survives the new branch: an UNVERIFIED fleet
        read must not be claimed down by design, record or no record."""
        _stub_dgx(monkeypatch, serving_up=False, degraded=True)
        _stand_down(marker)

        assert health.check_dgx() is False
        entry = _read(streams, "dgx")
        assert entry["ok"] is False
        assert "intentional" not in entry
        assert "VISIBILITY" in entry["message"].upper()

    def test_raising_predicate_is_fail_silent(self, marker, streams, monkeypatch):
        """Card case 4: a predicate that blows up must not stamp, must not
        raise, and must not change the verdict — the check behaves as if the
        record had never existed. Driven through the REAL _operator_down seam."""
        _stub_dgx(monkeypatch, serving_up=False)
        _stand_down(marker)

        def boom(*a, **k):
            raise RuntimeError("post_boot too old / disk on fire")
        monkeypatch.setattr(pb, "deliberately_down", boom)
        assert health._operator_down() is None   # the seam swallows it

        assert health.check_dgx() is False
        entry = _read(streams, "dgx")
        assert entry["ok"] is False
        assert "intentional" not in entry

    def test_reasonless_record_says_unspecified(self, marker, streams, monkeypatch):
        """A record with an empty reason must still name the state honestly —
        no `None`, no blank hole in the message an operator reads."""
        _stub_dgx(monkeypatch, serving_up=False)
        _stand_down(marker)
        monkeypatch.setattr(health, "_operator_down",
                            lambda: {"set_at": "2026-11-09T10:00:00", "reason": ""})

        assert health.check_dgx() is False
        entry = _read(streams, "dgx")
        assert entry["intentional"] == "operator_down"
        assert entry["message"] == _expected_message("dgx", reason="unspecified")


# ── gateway ────────────────────────────────────────────────────────────────

class TestGatewayStream:
    def test_record_set_stamps_the_failure(self, marker, streams, monkeypatch):
        """Card case 1 for the gateway: backend down because the fleet is down
        by instruction. ok stays a truthful False, the stream says why."""
        _stub_gateway(monkeypatch, backend_up=False, job=True)
        _stand_down(marker)

        assert health.check_gateway() is False
        entry = _read(streams, "gateway")
        assert entry["ok"] is False
        assert entry["vllm_healthy"] is False
        assert entry["intentional"] == "operator_down"
        assert entry["message"] == _expected_message("gateway")
        assert REASON in entry["message"]

    def test_no_record_leaves_the_stream_unexplained(self, marker, streams,
                                                     monkeypatch):
        """Card case 2 (the control) for the gateway."""
        _stub_gateway(monkeypatch, backend_up=False, job=True)
        assert pb.deliberately_down(marker) is None

        assert health.check_gateway() is False
        entry = _read(streams, "gateway")
        assert entry["ok"] is False
        assert "intentional" not in entry
        assert "message" not in entry

    def test_healthy_stream_is_never_stamped(self, marker, streams, monkeypatch):
        _stub_gateway(monkeypatch, backend_up=True, job=True)
        _stand_down(marker)

        assert health.check_gateway() is True
        entry = _read(streams, "gateway")
        assert entry["ok"] is True
        assert "intentional" not in entry

    def test_autodown_window_outranks_the_record(self, marker, streams, monkeypatch,
                                                 tmp_path):
        """Card case 3 (precedence) for the gateway."""
        _stub_gateway(monkeypatch, backend_up=False, job=True)
        _stand_down(marker)
        _arm_autodown_window(tmp_path, monkeypatch)

        assert health.check_gateway() is False
        entry = _read(streams, "gateway")
        assert entry["intentional"] == "autodown"
        assert "intentional autodown" in entry["message"]
        assert "operator_down" not in json.dumps(entry)

    def test_unverified_job_still_published_alongside_the_stamp(
            self, marker, streams, monkeypatch):
        """The stamp must not swallow GAP-F-3's honesty key. With no transport
        answering AND the fleet deliberately down, the message names the
        deliberate down (the actionable cause) while `gateway_job: null` + the
        named degradation stay on the stream — the same precedence the autodown
        branch has had all along."""
        _stub_gateway(monkeypatch, backend_up=False, job=None,
                      degraded="no launchctl here")
        _stand_down(marker)

        assert health.check_gateway() is False
        entry = _read(streams, "gateway")
        assert entry["ok"] is False
        assert entry["gateway_job"] is None
        assert entry["gateway_job_degraded"] == "no launchctl here"
        assert entry["intentional"] == "operator_down"
        assert entry["message"] == _expected_message("gateway")

    def test_unverified_job_without_a_record_keeps_its_own_message(
            self, marker, streams, monkeypatch):
        """Control for the case above: no record -> the degraded-read message
        still wins the `message` slot and nothing is stamped."""
        _stub_gateway(monkeypatch, backend_up=False, job=None,
                      degraded="no launchctl here")

        assert health.check_gateway() is False
        entry = _read(streams, "gateway")
        assert entry["ok"] is False
        assert "intentional" not in entry
        assert "UNVERIFIED" in entry["message"]

    def test_raising_predicate_is_fail_silent(self, marker, streams, monkeypatch):
        """Card case 4 for the gateway, again through the real seam."""
        _stub_gateway(monkeypatch, backend_up=False, job=True)
        _stand_down(marker)

        def boom(*a, **k):
            raise RuntimeError("post_boot too old / disk on fire")
        monkeypatch.setattr(pb, "deliberately_down", boom)
        assert health._operator_down() is None

        assert health.check_gateway() is False
        entry = _read(streams, "gateway")
        assert entry["ok"] is False
        assert "intentional" not in entry
