"""GAP-B-4: cron-watcher liveness stamps + staleness verdicts.

Acceptance pair for the audit-B gap: a FRESH stamp reads ok, a STALE stamp
warns (never fails), and a watcher nobody installed reports "not configured"
cleanly. Every test runs under a redirected HOME / STATE_DIR — nothing here
may read or write the operator's ~/.hscc.

The stamp itself is exercised through the REAL writer (hscc_daemon.state) on a
tmp STATE_DIR, so the test proves the integration, not just the reader.
"""

import datetime
import json
import os

import pytest

from hscc_daemon import cron_watchers as cw
from hscc_daemon import state


NOW = datetime.datetime(2026, 11, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect the canonical state writer + the marker into tmp_path.

    STATE_DIR is redirected on the REAL state module (the same attribute
    write_state resolves at call time), so stamp() lands in the sandbox; the
    marker file lives one level above it, inside the same tree.
    """
    state_dir = tmp_path / ".hscc" / "state"
    monkeypatch.setattr(state, "STATE_DIR", str(state_dir))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HSCC_DIR", raising=False)
    monkeypatch.delenv("HSCC_DEP_WATCHER_CADENCE_S", raising=False)
    monkeypatch.delenv("HSCC_ESCALATE_CADENCE_S", raising=False)
    return tmp_path


def _write_stamp(stream, *, ok=True, age_s=0, **fields):
    ts = (NOW - datetime.timedelta(seconds=age_s)).isoformat()
    data = {"timestamp": ts, "stream": stream, "ok": ok, "watcher": stream,
            **fields}
    os.makedirs(state.STATE_DIR, exist_ok=True)
    with open(os.path.join(state.STATE_DIR, stream + ".json"), "w") as f:
        json.dump(data, f)
    return data


# ── cadence / window SOT ────────────────────────────────────────────────────

def test_windows_are_at_least_3x_cadence():
    """The card's floor: window >= 3x the watcher's cadence, per watcher."""
    for stream, row in cw.WATCHERS.items():
        assert cw.stale_window_s(stream) == 3 * row["cadence_s"]


def test_cadences_match_the_installer_defaults():
    """dep watcher daily (0 8 * * *), escalate watcher */15 — drift here
    silently re-times the warn window, so pin both."""
    assert cw.cadence_s("dep_pr_watcher") == 86400
    assert cw.cadence_s("escalate_watcher") == 900


def test_cadence_env_override(sandbox, monkeypatch):
    """A customised schedule must not be judged against the installer default."""
    monkeypatch.setenv("HSCC_ESCALATE_CADENCE_S", "3600")
    assert cw.cadence_s("escalate_watcher") == 3600
    assert cw.stale_window_s("escalate_watcher") == 10800
    # garbage falls back to the default rather than to zero (a 0s window would
    # declare every pass stale)
    monkeypatch.setenv("HSCC_ESCALATE_CADENCE_S", "abc")
    assert cw.cadence_s("escalate_watcher") == 900


# ── stamping ────────────────────────────────────────────────────────────────

def test_stamp_lands_through_the_canonical_writer(sandbox):
    """stamp() uses state.write_state — the ONE stamp format on this host."""
    assert cw.stamp("dep_pr_watcher", prs=0, cards=0, idle=True) is True
    entry = state.read_state("dep_pr_watcher") or {}
    assert entry.get("ok") is True
    assert entry.get("watcher") == "dep_pr_watcher"
    assert entry.get("cron_job") == "hscc-dep-watcher"
    assert entry.get("idle") is True
    # the file is exactly where every other stream lives
    assert os.path.isfile(os.path.join(state.STATE_DIR, "dep_pr_watcher.json"))


def test_stamp_never_raises_on_unwritable_dir(sandbox, monkeypatch):
    """A stamp failure must cost the watcher its signal, never its work."""
    monkeypatch.setattr(state, "write_state",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    assert cw.stamp("dep_pr_watcher") is False


def test_stamp_failure_records_ok_false(sandbox):
    assert cw.stamp_failure("escalate_watcher", RuntimeError("boom")) is True
    entry = state.read_state("escalate_watcher")
    assert entry["ok"] is False and "boom" in entry["error"]


def test_stamp_honours_persist_disabled(sandbox):
    """An ad-hoc `hscc check` run is not a watcher pass: it stamps nothing."""
    with state.persist_disabled():
        assert cw.stamp("dep_pr_watcher") is False
    assert state.read_state("dep_pr_watcher") is None


# ── the acceptance pair: ok-on-fresh / warn-on-stale ────────────────────────

def test_fresh_stamp_is_ok(sandbox):
    _write_stamp("escalate_watcher", age_s=60)
    v = cw.verdict("escalate_watcher", now=NOW)
    assert v["status"] == "ok" and v["warn"] is False
    assert "WARN" not in v["detail"]


def test_stale_stamp_warns_not_fails(sandbox):
    """Just past the 3x window: a WARN-shaped detail (repo convention:
    ok=True surface + 'WARN:' prefix) — never a hard failure."""
    _write_stamp("escalate_watcher", age_s=3 * 900 + 60)
    v = cw.verdict("escalate_watcher", now=NOW)
    assert v["status"] == "stale" and v["warn"] is True
    assert v["detail"].startswith("WARN:")
    assert "hscc-escalate-watcher" in v["detail"]
    assert "hermes cron list" in v["detail"]


def test_missing_stamp_is_not_configured(sandbox):
    """No stamp AND no install marker -> the watcher is off. Clean, no WARN:
    nagging an operator who never installed it is exactly the false alarm the
    card forbids."""
    v = cw.verdict("dep_pr_watcher", now=NOW)
    assert v["status"] == "not-configured" and v["warn"] is False
    assert "not configured" in v["detail"]
    assert "WARN" not in v["detail"]


def test_installed_but_never_ran_warns(sandbox):
    """The t_3aadd2c0 class: the installer registered the job, so silence is
    a failed job, not an absent one. The marker is what separates the two."""
    assert cw.mark_installed("dep_pr_watcher", "0 8 * * *") is True
    v = cw.verdict("dep_pr_watcher", now=NOW)
    assert v["status"] == "never-ran" and v["warn"] is True
    assert "never completed a pass" in v["detail"]
    # and un-installing returns it to the clean verdict
    assert cw.mark_removed("dep_pr_watcher") is True
    v = cw.verdict("dep_pr_watcher", now=NOW)
    assert v["status"] == "not-configured" and v["warn"] is False


def test_bad_stamp_is_a_warning(sandbox):
    _write_stamp("escalate_watcher")
    path = os.path.join(state.STATE_DIR, "escalate_watcher.json")
    with open(path) as f:
        data = json.load(f)
    data["timestamp"] = "not-a-timestamp"
    with open(path, "w") as f:
        json.dump(data, f)
    v = cw.verdict("escalate_watcher", now=NOW)
    assert v["status"] == "bad-stamp" and v["warn"] is True


def test_failing_pass_stamps_fresh_but_warns(sandbox):
    """A crash stamped by stamp_failure: fresh stamp, ok=False -> 'failing',
    distinguished from both 'dead' (stale) and 'healthy' (ok)."""
    cw.stamp_failure("dep_pr_watcher", "ValueError('bad pr payload')")
    v = cw.verdict("dep_pr_watcher", now=NOW)
    assert v["status"] == "failing" and v["warn"] is True
    assert "bad pr payload" in v["detail"]


def test_boundary_exactly_window_is_not_stale(sandbox):
    """MISS_FACTOR is a floor, not a hair trigger: exactly 3x cadence is ok."""
    _write_stamp("escalate_watcher", age_s=3 * 900)
    assert cw.verdict("escalate_watcher", now=NOW)["status"] == "ok"


def test_naive_stamp_timestamp_treated_as_utc(sandbox):
    """A stamp without an offset must not be age-judged against local time."""
    ts = (NOW.replace(tzinfo=None) - datetime.timedelta(seconds=60)).isoformat()
    data = {"timestamp": ts, "stream": "escalate_watcher", "ok": True}
    os.makedirs(state.STATE_DIR, exist_ok=True)
    with open(os.path.join(state.STATE_DIR, "escalate_watcher.json"), "w") as f:
        json.dump(data, f)
    assert cw.verdict("escalate_watcher", now=NOW)["status"] == "ok"


def test_verdict_unknown_stream_raises_keyerror(sandbox):
    with pytest.raises(KeyError):
        cw.verdict("nope_watcher")


def test_all_verdicts_covers_every_watcher(sandbox):
    rows = cw.all_verdicts(now=NOW)
    assert [r["stream"] for r in rows] == list(cw.STREAMS)


# ── the doctor row ──────────────────────────────────────────────────────────

def _doctor_row(sandbox, *, now=NOW, hscc_dir=None):
    return cw.doctor_check(hscc_dir=hscc_dir, now=now)


def test_doctor_ok_when_both_fresh(sandbox):
    _write_stamp("dep_pr_watcher", age_s=10)
    _write_stamp("escalate_watcher", age_s=10)
    row = _doctor_row(sandbox)
    assert row["ok"] is True and row["fatal"] is False
    assert row["name"] == cw.CHECK_NAME


def test_doctor_not_configured_is_clean_ok(sandbox):
    row = _doctor_row(sandbox)
    assert row["ok"] is True and row["fatal"] is False
    assert "not configured" in row["detail"]
    assert "WARN" not in row["detail"]


def test_doctor_warns_on_stale_without_going_fatal(sandbox):
    """THE GAP-B-4 verdict: a dead watcher is reported (WARN) but fatal=False
    always, so doctor/bootstrap never die over an out-of-band cron job."""
    _write_stamp("dep_pr_watcher", age_s=4 * 86400)
    row = _doctor_row(sandbox)
    assert row["ok"] is False and row["fatal"] is False
    assert "WARN:" in row["detail"] and "dep_pr_watcher" in row["detail"]
    assert "cron" in row["fix"]


def test_doctor_hscc_dir_redirects_without_env_mutation(tmp_path, monkeypatch):
    """hscc_dir is the bootstrap seam (HOME stays real in a sandboxed
    bootstrap). Redirect must be by PARAMETER — os.environ untouched, and a
    stale stamp under the redirected root is still seen."""
    root = tmp_path / "sandbox-hscc"
    (root / "state").mkdir(parents=True)
    monkeypatch.setenv("HSCC_DIR", "/must-not-be-read")
    ts = (NOW - datetime.timedelta(seconds=4 * 86400)).isoformat()
    with open(root / "state" / "dep_pr_watcher.json", "w") as f:
        json.dump({"timestamp": ts, "ok": True}, f)
    row = cw.doctor_check(hscc_dir=str(root), now=NOW)
    assert row["ok"] is False and row["fatal"] is False
    assert "dep_pr_watcher" in row["detail"]
    assert os.environ.get("HSCC_DIR") == "/must-not-be-read"


def test_doctor_marker_under_hscc_dir_root_not_state_dir(tmp_path):
    """The marker lives one level above the stamp dir — pin the layout, the
    17-failure detection depends on it."""
    root = tmp_path / "hscc"
    (root / "state").mkdir(parents=True)
    cw.mark_installed("escalate_watcher", "*/15 * * * *",
                      directory=str(root / "state"))
    assert (root / cw.MARKER_FILENAME).is_file()
    assert not (root / "state" / cw.MARKER_FILENAME).exists()
    row = cw.doctor_check(hscc_dir=str(root), now=NOW)
    assert row["ok"] is False  # installed, never ran -> warn


# ── verify must NOT judge these stamps (the cry-wolf pair) ─────────────────

def test_verify_excludes_watcher_stamps(sandbox):
    """A 2-day-old dep stamp would be RED 23h of every day under the flat
    600s stream window. The verdict belongs to the doctor row instead."""
    from hscc_daemon import verify
    _write_stamp("dep_pr_watcher", age_s=2 * 86400)
    _write_stamp("escalate_watcher", age_s=2 * 86400)
    r = verify.check_daemon_streams(state_dir=state.STATE_DIR)
    assert r["ok"] is True
    assert "excluded" in r["detail"]


def test_verify_still_judges_real_streams_after_the_exclusion(sandbox):
    """Negative control: the exclusion must not swallow genuine daemon-stream
    staleness sitting next to the watcher stamps."""
    from hscc_daemon import verify
    _write_stamp("dep_pr_watcher", age_s=10)
    _write_stamp("nas", age_s=99999)
    r = verify.check_daemon_streams(state_dir=state.STATE_DIR,
                                    stream_intervals={"nas": 900})
    assert r["ok"] is False and "nas" in r["detail"]


def test_live_watcher_stamps_carry_their_own_verdict(sandbox):
    """End-to-end: stamp through the writer, read through the verdict — the
    two halves meet at the exact canonical file, no private format."""
    cw.stamp("escalate_watcher", actions=0, idle=True)
    # now=None: a live stamp carries the real wall-clock timestamp, so judge
    # it against real now (a fixed NOW here would age-judge it wrong).
    v = cw.verdict("escalate_watcher")
    assert v["status"] == "ok" and v["age_s"] is not None
