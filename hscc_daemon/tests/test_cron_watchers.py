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
    monkeypatch.delenv("HSCC_ALONE_SWEEP_CADENCE_S", raising=False)
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
    """Every row must equal its installer's baked schedule, or the warn window
    silently re-times itself. Read the installers, do not trust these numbers."""
    assert cw.cadence_s("dep_pr_watcher") == 86400
    assert cw.cadence_s("escalate_watcher") == 900
    assert cw.cadence_s("alone_sweep_gate") == 86400


def test_row_installer_paths_exist_and_name_the_real_cron_jobs():
    """The doctor prints ``install_script`` as the fix and ``cron_job`` as the
    thing to look for in `hermes cron list` — a stale name in this roster sends
    an operator to a file or a job that does not exist."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    installers = {}
    for stream, row in cw.WATCHERS.items():
        path = os.path.join(repo, row["install_script"])
        assert os.path.isfile(path), f"{stream}: no installer at {row['install_script']}"
        text = open(path, encoding="utf-8").read()
        assert row["cron_job"] in text, (
            f"{stream}: installer {row['install_script']} never names "
            f"{row['cron_job']} — the doctor would point at a job it cannot find")
        installers[stream] = path
    assert len(installers) == len(cw.WATCHERS)


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


def test_removing_the_stamp_ends_the_warn_for_a_removed_watcher(sandbox):
    """--uninstall clears the marker AND the stamp: an operator who removed the
    job deliberately must land back on `not-configured`, not on a `stale` warn
    that gets louder every day for a job nobody wants any more."""
    cw.stamp("dep_pr_watcher", prs=0, idle=True)
    cw.mark_installed("dep_pr_watcher", "0 8 * * *")
    assert cw.remove_stamp("dep_pr_watcher") is True
    assert cw.mark_removed("dep_pr_watcher") is True
    assert cw.verdict("dep_pr_watcher", now=NOW)["status"] == "not-configured"
    # idempotent: nothing left to remove is still "no stamp present"
    assert cw.remove_stamp("dep_pr_watcher") is True
    assert cw.remove_stamp("nope_watcher") is False


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


# ── the alone-sweep gate row: LIVENESS, with the verdict riding along ───────
# t_13a70290 decided the sweep nightly belongs in this roster. What had to be
# settled first is what the row MEANS: the gate produces a verdict (GREEN / RED /
# NOT MEASURED), and the failure the card refuses to accept is a red sweep
# reading as "watcher alive and well". So the row stays a liveness row — stale /
# never-ran still mean "the cron job is not firing" — and a pass that was not
# clean stamps ok=False, which reads `failing`: warned, and worded so nobody can
# mistake it for a dead job.

def test_a_green_sweep_pass_is_ok_and_carries_the_gate_fields(sandbox):
    """A pass that swept and found nothing to fix advances the stamp — the
    idle-pass rule of GAP-B-4, in the shape this gate has."""
    assert cw.stamp("alone_sweep_gate", gate_rc=0, verdict="green",
                    interpreters=2, head="abc1234") is True
    v = cw.verdict("alone_sweep_gate", now=NOW)
    assert v["status"] == "ok" and v["warn"] is False
    assert "WARN" not in v["detail"] and "hscc-alone-sweep-gate" in v["detail"]
    stamp = cw.read_stamp("alone_sweep_gate") or {}
    assert stamp.get("verdict") == "green" and stamp.get("interpreters") == 2
    assert stamp.get("head") == "abc1234" and stamp.get("ok") is True


def test_a_red_sweep_never_reads_healthy(sandbox):
    """THE constraint: RED must not look like a fresh, healthy stamp."""
    assert cw.stamp_failure(
        "alone_sweep_gate",
        "GATE RED: at least one file answers alone differently than inside "
        "the leg", verdict="red", gate_rc=1, head="abc1234") is True
    v = cw.verdict("alone_sweep_gate", now=NOW)
    assert v["status"] == "failing" and v["warn"] is True
    assert v["detail"].startswith("WARN:")
    assert "GATE RED" in v["detail"], (
        "the warn line must name the verdict, not just 'the pass failed'")
    # the stamp MOVED (the job fired) — this is not the dead-job verdict
    assert v["age_s"] is not None and v["age_s"] < v["window_s"]
    assert cw.read_stamp("alone_sweep_gate")["verdict"] == "red"


def test_a_dead_nightly_and_a_red_nightly_read_differently(sandbox):
    """The two sentences the row must keep apart: 'stopped firing' vs
    'fired, and the sweep was red'. Both warn; neither may be confused."""
    _write_stamp("alone_sweep_gate", age_s=4 * 86400)
    dead = cw.verdict("alone_sweep_gate", now=NOW)
    cw.stamp_failure("alone_sweep_gate", "GATE RED: one file differs",
                     verdict="red")
    red = cw.verdict("alone_sweep_gate", now=NOW)
    assert dead["status"] == "stale" and red["status"] == "failing"
    assert dead["warn"] and red["warn"]
    assert "not firing" in dead["detail"] and "GATE RED" in red["detail"]


def test_a_pass_mid_flight_does_not_false_warn(sandbox):
    """The cadence window has to absorb the pass itself: the stamp is written at
    the END of a ~25 min pass, so the previous pass can legitimately be
    ~24h + pass-length old while this one is still running."""
    _write_stamp("alone_sweep_gate", age_s=86400 + 1800)
    v = cw.verdict("alone_sweep_gate", now=NOW)
    assert v["status"] == "ok" and v["warn"] is False
    assert v["window_s"] == 3 * 86400


def test_installed_but_never_swept_warns_the_same_way(sandbox):
    """The t_3aadd2c0 shape, this job: the installer registered the cron job, so
    a report dir with nothing in it is a failing nightly, not an absent one."""
    assert cw.mark_installed("alone_sweep_gate", "30 4 * * *") is True
    v = cw.verdict("alone_sweep_gate", now=NOW)
    assert v["status"] == "never-ran" and v["warn"] is True
    assert "install_alone_sweep_gate.sh" in v["detail"]
    assert cw.mark_removed("alone_sweep_gate") is True
    assert cw.verdict("alone_sweep_gate", now=NOW)["status"] == "not-configured"


def test_cadence_env_override_for_a_custom_sweep_schedule(sandbox, monkeypatch):
    """An operator who moves the nightly (HSCC_ALONE_SWEEP_SCHEDULE) must be
    able to move the window with it, or a weekly pass is stale by Wednesday."""
    monkeypatch.setenv("HSCC_ALONE_SWEEP_CADENCE_S", "604800")
    assert cw.cadence_s("alone_sweep_gate") == 604800
    assert cw.stale_window_s("alone_sweep_gate") == 3 * 604800


def test_doctor_row_covers_the_sweep_gate_without_going_fatal(sandbox):
    """A red sweep is a finding about the test suite, never a broken host:
    warn-shape in the doctor row, and the fix offers the seconds-cheap
    preflight rather than a 25-minute re-run."""
    cw.stamp_failure("alone_sweep_gate", "GATE RED: one file differs",
                     verdict="red")
    row = cw.doctor_check(now=NOW)
    assert row["ok"] is False and row["fatal"] is False
    assert row["name"] == cw.CHECK_NAME
    assert "alone_sweep_gate" in row["detail"]
    assert "--preflight" in row["fix"]


def test_a_red_sweep_stamp_never_turns_verify_red(sandbox):
    """Negative control on the exclusion: the sweep stamp is a doctor-row
    subject, so neither its age (daily, vs the flat 600s stream window) nor its
    ok=False may fail the cluster smoke test."""
    from hscc_daemon import verify
    cw.stamp_failure("alone_sweep_gate", "GATE RED: one file differs",
                     verdict="red")
    _write_stamp("dep_pr_watcher", age_s=2 * 86400)
    r = verify.check_daemon_streams(state_dir=state.STATE_DIR)
    assert r["ok"] is True
    assert "excluded" in r["detail"]


# ── the WRITE seam: a relocated root a watcher can actually stamp ───────────
# t_e9b0f048: every read took `directory`, neither write did, and `**fields`
# swallowed `directory=` into the stamp PAYLOAD. So `stamp(stream, directory=X)`
# returned True, created nothing at X, and left behind a stamp advertising a root
# the writer never touched — which made a relocated HSCC_DIR a root no watcher
# could stamp through the supported API. Measured on the live host: doctor_check
# at a relocated root read `not-configured` for all three streams while
# hscc-escalate-watcher had stamped successfully minutes earlier, ok=True, no
# warn. These are the two pins the card owes: (a) the seam moves the write,
# (b) a keyword that names a parameter or an envelope key raises instead of
# becoming payload. Both were measured as absent/survivors before the fix.
#
# A live stamp carries the REAL clock, so every verdict below is judged at
# now=None (real now) rather than the fixed NOW this file uses elsewhere — an
# age judgement against a 2026-11-01 NOW would be meaningless here.

def _relocated(tmp_path):
    """A stamp root OUTSIDE the sandbox's canonical STATE_DIR (tmp/.hscc/state),
    so "it landed at X" and "it landed at STATE_DIR" cannot be confused."""
    return tmp_path / "relocated-hscc" / "state"


def test_stamp_directory_seam_moves_the_write(sandbox):
    """(a) stamp(stream, directory=X) creates X/<stream>.json and NOTHING under
    state.STATE_DIR — the seam relocates the write, it does not annotate it."""
    other = _relocated(sandbox)
    assert cw.stamp("dep_pr_watcher", directory=str(other), prs=0,
                    idle=True) is True
    assert (other / "dep_pr_watcher.json").is_file()
    assert not (sandbox / ".hscc" / "state" / "dep_pr_watcher.json").exists()
    # same format, not a second one: the canonical envelope is intact at X
    stamp = cw.read_stamp("dep_pr_watcher", directory=str(other))
    assert stamp is not None
    assert stamp["ok"] is True and stamp["watcher"] == "dep_pr_watcher"
    assert stamp["cron_job"] == "hscc-dep-watcher" and "timestamp" in stamp
    assert stamp["prs"] == 0 and stamp["idle"] is True


def test_stamp_failure_uses_the_same_root_as_stamp(sandbox):
    """One root, chosen once (module invariant "one writer, one format, one
    place"): the red pass overwrites the clean stamp AT X — a second location a
    watcher could pick between is exactly what this card must not create."""
    other = _relocated(sandbox)
    assert cw.stamp("alone_sweep_gate", directory=str(other), gate_rc=0,
                    verdict="green") is True
    assert cw.stamp_failure("alone_sweep_gate", "GATE RED: one file differs",
                            directory=str(other), verdict="red") is True
    assert sorted(p.name for p in other.iterdir()) == ["alone_sweep_gate.json"]
    assert not (sandbox / ".hscc" / "state" / "alone_sweep_gate.json").exists()
    row = cw.verdict("alone_sweep_gate", directory=str(other), now=None)
    assert row["status"] == "failing" and row["warn"] is True
    assert "GATE RED" in row["detail"]


def test_a_stamp_at_a_relocated_root_reads_ok_through_the_doctor(sandbox):
    """THE round trip the defect broke: stamp at X, then ask the doctor about X.
    Before the fix every row here read `not-configured` — "cron job alive" and
    "nobody installed this" one environment variable apart."""
    other = _relocated(sandbox)
    assert cw.stamp("escalate_watcher", directory=str(other), actions=0,
                    idle=True) is True
    row = cw.doctor_check(hscc_dir=str(other.parent))
    by_stream = {r["stream"]: r for r in row["rows"]}
    assert by_stream["escalate_watcher"]["status"] == "ok"
    assert by_stream["escalate_watcher"]["warn"] is False
    # the other two really are judged on the RELOCATED root, not the sandbox one
    assert by_stream["dep_pr_watcher"]["status"] == "not-configured"
    assert by_stream["alone_sweep_gate"]["status"] == "not-configured"
    assert row["ok"] is True and row["fatal"] is False


def test_the_stamp_payload_can_never_carry_a_directory_key(sandbox):
    """The literal leak: `directory` landed INSIDE the JSON, so the stamp
    advertised a root the writer never wrote to (and verdict() read it back as
    an ordinary field). It is a parameter now, so it cannot be payload."""
    other = _relocated(sandbox)
    assert cw.stamp("dep_pr_watcher", directory=str(other)) is True
    stamp = cw.read_stamp("dep_pr_watcher", directory=str(other)) or {}
    assert "directory" not in stamp
    assert cw.verdict("dep_pr_watcher", directory=str(other),
                      now=None)["status"] == "ok"


@pytest.mark.parametrize("field", ["stream", "ok", "timestamp",
                                   "watcher", "cron_job"])
def test_a_reserved_keyword_raises_instead_of_becoming_payload(sandbox, field):
    """(b) `**fields` used to absorb ANY keyword. A stamp that fakes its own
    envelope (`ok=False` from a clean pass, a frozen `timestamp`) is the same
    class of silent failure as a stamp that names a root it never wrote to, so
    the write functions reject these — loudly, and BEFORE the never-raises try,
    which would otherwise swallow them into a bare False. (`stream` is rejected
    by the signature itself; the rest by the reserved-keyword guard.)"""
    with pytest.raises(TypeError) as exc:
        cw.stamp("dep_pr_watcher", **{field: "nope"})
    assert field in str(exc.value)
    # and the rejection costs no write at all — nothing half-landed anywhere
    assert not (sandbox / ".hscc" / "state" / "dep_pr_watcher.json").exists()


def test_directory_is_a_real_parameter_of_both_writers():
    """The structural reason `directory` can no longer be swallowed: it is a
    named parameter of BOTH writers (default None, like every read), so a
    `directory=` kwarg binds to the seam instead of riding into the payload.
    Introspection pin — a future edit that drops it back into **fields goes red
    here, not at a doctor that silently reads `not-configured`."""
    import inspect
    for fn in (cw.stamp, cw.stamp_failure):
        params = inspect.signature(fn).parameters
        assert "directory" in params, fn.__name__
        assert params["directory"].default is None, fn.__name__


def test_stamp_failure_rejects_a_reserved_keyword_too(sandbox):
    """Same guard on the other writer: a reserved envelope name raises there too.
    Asserted with `ok=`, deliberately NOT with `verdict=` — `verdict` is
    legitimate payload (the sweep gate stamps `verdict="green"/"red"`, and
    `test_a_green_sweep_pass_is_ok_and_carries_the_gate_fields` above pins it), so
    reserving it would break the one row this module exists to render."""
    with pytest.raises(TypeError) as exc:
        cw.stamp_failure("alone_sweep_gate", "boom", ok=True)
    assert "ok" in str(exc.value)
    assert not (sandbox / ".hscc" / "state" / "alone_sweep_gate.json").exists()


def test_an_ordinary_payload_field_is_still_accepted(sandbox):
    """The guard is a RESERVED list, not a whitelist: watchers own their payload
    (gate_rc / head / report / fail_limit were never parameters of anything)."""
    assert cw.stamp("alone_sweep_gate", gate_rc=0, interpreters=2,
                    head="abc1234", report="/tmp/r.txt") is True
    stamp = cw.read_stamp("alone_sweep_gate") or {}
    assert stamp["head"] == "abc1234" and stamp["report"] == "/tmp/r.txt"


def test_stamp_failure_honours_persist_disabled(sandbox):
    """Symmetry with stamp(), which already refused this (t_06ead443 follow-up):
    an ad-hoc `hscc check` is not a watcher pass, so it must stamp NOTHING —
    before this it could stamp a FAILURE on the operator's live tree at a moment
    a clean pass was refused."""
    with state.persist_disabled():
        assert cw.stamp_failure("escalate_watcher", RuntimeError("boom")) is False
        assert not os.path.isfile(
            os.path.join(state.STATE_DIR, "escalate_watcher.json"))
    assert state.read_state("escalate_watcher") is None


# ── the split pairing: new writer code, an OLD state.py behind it ───────────
# Review round 1 on this card measured a regression the legs could not see: the
# writers passed `directory=` to `state.write_state` UNCONDITIONALLY, and
# `_state_writer()` prefers `from hscc_daemon import state` — which in the live
# cron runtime resolves to the DEPLOYED plugin copy
# (~/.hermes/plugins/hscc_daemon/state.py, 5573 B, `write_state(stream_name,
# data)`, no `directory`). Its TypeError was swallowed by these writers' own
# never-raises try, so every stamp silently vanished:
# scripts/tests/test_escalate_watcher_run.py went 10 passed -> 1 failed with a
# stderr `liveness stamp write failed`, i.e. the live */15 escalate job would
# have stopped stamping post-merge -> doctor `missing` -> `never-ran`. Under
# run_tests.sh BOTH sides resolve to the checkout, so no leg could observe the
# split. That pairing is a deployment fact, so the pin must not depend on what
# happens to be deployed: it installs an old-shape writer on purpose.
#
# Two old shapes matter and they fail differently:
#   STRICT   — `def write_state(stream_name, data)`: a `directory=` kwarg raises
#              TypeError. Must stay invisible to a caller that asked for nothing.
#   ABSORBER — `def write_state(stream_name, data, **kwargs)`: swallows the
#              kwarg, returns success, writes to ITS root. The worst shape (True,
#              wrong place), and the one a call-and-catch feature-detect misses.

def _strict_old_writer(monkeypatch):
    """write_state exactly as the deployed copy has it: two params, no seam."""
    calls = []

    def write_state(stream_name, data):
        calls.append((stream_name, dict(data)))
        os.makedirs(state.STATE_DIR, exist_ok=True)
        path = os.path.join(state.STATE_DIR, stream_name + ".json")
        with open(path, "w") as f:
            json.dump({"timestamp": state.now_iso(), "stream": stream_name,
                       **data}, f)
        return data

    monkeypatch.setattr(state, "write_state", write_state)
    return calls


def _absorbing_old_writer(monkeypatch):
    """An old writer that takes ANY keyword and ignores it — it reports success
    while writing to its own root, so a naive try/except feature-detect sees no
    error at all."""
    calls = []

    def write_state(stream_name, data, **kwargs):
        calls.append((stream_name, dict(data), kwargs))
        os.makedirs(state.STATE_DIR, exist_ok=True)
        path = os.path.join(state.STATE_DIR, stream_name + ".json")
        with open(path, "w") as f:
            json.dump({"timestamp": state.now_iso(), "stream": stream_name,
                       **data}, f)
        return data

    monkeypatch.setattr(state, "write_state", write_state)
    return calls


def test_a_writer_without_the_seam_still_gets_the_base_two_arg_call(sandbox,
                                                                    monkeypatch):
    """Req (1) of the rework, and the regression itself: with `directory=None`
    the call must keep the shape it had before this card existed, so an older
    resolved writer still takes the stamp. At 3c28aa73 this returned False and
    wrote nothing — the reviewer's one-command repro (test_escalate_watcher_run
    10 passed -> 1 failed) is the same event one level up."""
    calls = _strict_old_writer(monkeypatch)
    assert cw.stamp("escalate_watcher", actions=0, idle=True) is True
    assert len(calls) == 1
    stream, payload = calls[0]
    assert stream == "escalate_watcher" and payload["idle"] is True
    assert payload["ok"] is True and payload["cron_job"] == "hscc-escalate-watcher"
    assert os.path.isfile(os.path.join(state.STATE_DIR, "escalate_watcher.json"))


def test_stamp_failure_with_an_old_writer_also_keeps_the_base_call(sandbox,
                                                                  monkeypatch):
    """Same rule on the other writer — the red pass is the same writer and must
    not be the half that breaks against a deployed copy."""
    calls = _strict_old_writer(monkeypatch)
    assert cw.stamp_failure("alone_sweep_gate", "GATE RED",
                            verdict="red") is True
    assert len(calls) == 1
    assert calls[0][0] == "alone_sweep_gate"
    assert calls[0][1]["ok"] is False and calls[0][1]["error"] == "GATE RED"


def test_no_directory_keyword_reaches_a_writer_that_never_asks(sandbox,
                                                              monkeypatch):
    """The call-shape pin, stated directly: an ABSORBING old writer accepts any
    keyword, so it is the only writer shape that can tell "we did not send
    `directory`" from "we sent it and it was ignored". At 3c28aa73 the recorded
    kwargs were `{'directory': None}` — invisible to a strict stub (which just
    raises) and silently relocated by an absorber."""
    calls = _absorbing_old_writer(monkeypatch)
    assert cw.stamp("dep_pr_watcher", prs=0) is True
    assert cw.stamp_failure("dep_pr_watcher", "boom") is True
    assert [c[2] for c in calls] == [{}, {}], calls


def test_an_explicit_directory_the_writer_cannot_honour_raises_loudly(sandbox,
                                                                     monkeypatch):
    """Req (2): asking for a root the resolved writer cannot write to is a
    deployment/API mismatch, not an operational hiccup, so it must NOT come back
    as the same bare False as a full disk. Strict stub -> the writer itself would
    raise TypeError; the guard turns that into a named refusal BEFORE the try and
    spends no write at either root."""
    calls = _strict_old_writer(monkeypatch)
    other = _relocated(sandbox)
    with pytest.raises(TypeError) as exc:
        cw.stamp("dep_pr_watcher", directory=str(other))
    msg = str(exc.value)
    assert "directory" in msg and "write_state" in msg
    assert not (other / "dep_pr_watcher.json").exists()
    assert not os.path.isfile(os.path.join(state.STATE_DIR,
                                          "dep_pr_watcher.json"))
    assert calls == []                       # nothing was attempted


def test_a_writer_that_absorbs_the_keyword_is_refused_too(sandbox, monkeypatch):
    """The absorber is the case a call-and-catch feature-detect misses: passing
    `directory=` raises nothing, the writer ignores it, and stamp() returns True
    after writing to the writer's root — the original defect wearing a different
    hat. Detection must read the SIGNATURE, not the outcome."""
    calls = _absorbing_old_writer(monkeypatch)
    other = _relocated(sandbox)
    with pytest.raises(TypeError) as exc:
        cw.stamp("escalate_watcher", directory=str(other), actions=0)
    assert "directory" in str(exc.value)
    assert calls == []
    assert not (other / "escalate_watcher.json").exists()


def test_a_positional_only_directory_is_not_mistaken_for_the_seam(sandbox,
                                                                  monkeypatch):
    """The capability check honours a name only where it can be passed BY KEYWORD.
    `def write_state(stream, data, directory, /)` does have a `directory`, and
    still cannot take `directory=` — an "is the name there" check would send the
    keyword anyway, take the TypeError the writer raises for it, and lose the
    stamp in exactly the silence the deployed copy produces."""
    calls = []

    def write_state(stream_name, data, directory, /):
        calls.append(directory)
        return data

    monkeypatch.setattr(state, "write_state", write_state)
    with pytest.raises(TypeError) as exc:
        cw.stamp("dep_pr_watcher", directory=str(_relocated(sandbox)))
    assert "directory" in str(exc.value)
    assert calls == []


def test_stamp_failure_refuses_an_unhonourable_root_as_well(sandbox, monkeypatch):
    """Symmetry: a red pass must not quietly relocate to STATE_DIR when the clean
    pass refused — one root, chosen once, including when there is no root to
    choose."""
    _strict_old_writer(monkeypatch)
    with pytest.raises(TypeError):
        cw.stamp_failure("alone_sweep_gate", "boom",
                         directory=str(_relocated(sandbox)))
    assert not os.path.isfile(os.path.join(state.STATE_DIR,
                                          "alone_sweep_gate.json"))


def test_a_genuine_write_failure_is_still_a_bare_false(sandbox, monkeypatch):
    """The other half of "must not become the same bare False as a full disk":
    the disk failure keeps its bare False. A writer that raises OSError is an
    operational failure the watcher survives (it warns to stderr and keeps
    working); only the seam mismatch is loud."""
    def write_state(stream_name, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(state, "write_state", write_state)
    assert cw.stamp("escalate_watcher", actions=0) is False
    assert cw.stamp_failure("escalate_watcher", RuntimeError("x")) is False


def test_the_loud_refusal_is_a_typeerror_and_names_itself(sandbox):
    """Callers catch Exception and warn (escalate_watcher_run._stamp_pass), so
    the class only has to be a TypeError for them — but it must be its OWN class
    so a future `except TypeError:` around the writer cannot re-silence it, and
    so the checkout's own state.py is proven to satisfy it."""
    assert issubclass(cw.WriterSeamUnsupported, TypeError)
    import inspect
    params = inspect.signature(state.write_state).parameters
    assert "directory" in params, (
        "the checkout writer must carry the seam the writers detect")
    assert params["directory"].default is None


# ── deployed-copy drift (t_95eb9f15) ────────────────────────────────────────
#
# The incident this section pins: `hscc-dep-watcher` was installed, [active] and
# had run OK at 08:00, yet `python3 hscc-bootstrap/doctor.py` reported
# `dep_pr_watcher: not configured (no stamp …)` with ok=True and no warn. The
# job WAS running — from a copy of dep_pr_watcher.py that `install_dep_watcher.sh`
# had `cp`-ied into <bound home>/scripts and nothing had compared to the repo
# since (Sep 29, 6416 B, zero references to `cron_watchers`), so it did its work
# and left no stamp. Nothing in the tree could tell "nobody installed this" from
# "the install is older than the code it is supposed to run" — the same
# invisibility as the 17-consecutive-failures incident, arriving by a new route.

DRIFT_SCRIPT = "dep_pr_watcher.py"
REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))


def _homes(tmp_path, *, deployed, repo, script=DRIFT_SCRIPT):
    """Build a fake bound home + a fake checkout: (hermes_home, repo_dir).

    `deployed` / `repo` are the file contents for ``scripts/<script>`` on each
    side, None meaning "no file". Both sides are tmp_path trees reached through
    the explicit seams — nothing here may read or write the operator's
    ~/.hermes, which is what the hermes_home / repo_dir seams are for.
    """
    hermes_home = tmp_path / ".hermes"
    repo_dir = tmp_path / "checkout"
    for base, blob in ((hermes_home, deployed), (repo_dir, repo)):
        d = base / "scripts"
        d.mkdir(parents=True, exist_ok=True)
        target = d / script
        if blob is None:
            # None means "no file on this side" — and a previous _homes() call
            # on the same tmp_path may have created one. Absent must be absent.
            target.unlink(missing_ok=True)
            continue
        target.write_text(blob)
    return hermes_home, repo_dir


def _copy(status="drifted"):
    """A pre-resolved deployed_copy_drift() row, as a caller would pass it."""
    return {"stream": "dep_pr_watcher", "status": status,
            "deployed": "/bound/scripts/dep_pr_watcher.py",
            "repo": "/checkout/scripts/dep_pr_watcher.py",
            "detail": ("the deployed copy /bound/scripts/dep_pr_watcher.py "
                       "(6416 B, aaaa) is NOT the repo copy "
                       "/checkout/scripts/dep_pr_watcher.py (13162 B, bbbb) -- "
                       "the job runs stale code; re-run "
                       "scripts/install_dep_watcher.sh")}


def test_a_stale_deployed_copy_is_named_as_not_the_repo_copy(tmp_path):
    hermes_home, repo_dir = _homes(tmp_path, deployed="old code\n",
                                   repo="new code\n")
    d = cw.deployed_copy_drift("dep_pr_watcher", hermes_home=str(hermes_home),
                               repo_dir=str(repo_dir))
    assert d["status"] == "drifted"
    assert d["deployed"] == str(hermes_home / "scripts" / DRIFT_SCRIPT)
    assert d["repo"] == str(repo_dir / "scripts" / DRIFT_SCRIPT)
    # The sentence must carry the fix, not just the disagreement, and the sizes
    # so an operator can see which side is the old one.
    assert "re-run scripts/install_dep_watcher.sh" in d["detail"]
    assert f"{len('old code\n')} B" in d["detail"]
    assert f"{len('new code\n')} B" in d["detail"]


def test_only_a_proven_mismatch_is_a_drift(tmp_path):
    """in-sync / missing-deployed / missing-repo are all fail-OPEN. A detector
    that inferred staleness from a missing comparison would nag every host that
    never installed a watcher — the false alarm the GAP-B-4 rows forbid."""
    same = "the same code\n"
    hermes_home, repo_dir = _homes(tmp_path, deployed=same, repo=same)
    assert cw.deployed_copy_drift(
        "dep_pr_watcher", hermes_home=str(hermes_home),
        repo_dir=str(repo_dir))["status"] == "in-sync"
    hermes_home, repo_dir = _homes(tmp_path, deployed=None, repo=same)
    assert cw.deployed_copy_drift(
        "dep_pr_watcher", hermes_home=str(hermes_home),
        repo_dir=str(repo_dir))["status"] == "missing-deployed"
    hermes_home, repo_dir = _homes(tmp_path, deployed=same, repo=None)
    assert cw.deployed_copy_drift(
        "dep_pr_watcher", hermes_home=str(hermes_home),
        repo_dir=str(repo_dir))["status"] == "missing-repo"


def test_a_wrapper_stream_has_no_deployed_copy_to_go_stale(tmp_path):
    """install_alone_sweep_gate.sh generates a one-line wrapper that execs the
    repo's gate — the shape that cannot drift, and the one the two copying
    installers should move to. Comparing a copy for that stream is meaningless,
    so it must answer `not-a-copy` and never `drifted`."""
    assert cw.WATCHERS["dep_pr_watcher"]["deploy"] == cw.DEPLOY_COPY
    assert cw.WATCHERS["escalate_watcher"]["deploy"] == cw.DEPLOY_COPY
    assert cw.WATCHERS["alone_sweep_gate"]["deploy"] == cw.DEPLOY_WRAPPER
    d = cw.deployed_copy_drift("alone_sweep_gate", hermes_home=str(tmp_path),
                               repo_dir=str(tmp_path))
    assert d["status"] == "not-a-copy" and "wrapper" in d["detail"]


def test_the_bound_home_resolves_the_way_the_installers_resolve_it(tmp_path,
                                                                   monkeypatch):
    """$HERMES_HOME wins (a bound profile home is exactly that case), else
    ~/.hermes — the t_3aadd2c0 rule, shared with the installers rather than
    re-derived, and read through an env seam."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert cw.bound_hermes_home() == str(tmp_path / ".hermes")
    assert cw.bound_hermes_home({}) == str(tmp_path / ".hermes")
    assert cw.bound_hermes_home({"HERMES_HOME": "/profiles/work"}) == "/profiles/work"


def test_the_default_comparison_target_is_this_checkout(sandbox, tmp_path):
    """No repo_dir passed == compare against the checkout this module runs from
    (the doctor's own tree). The deployed plugin payload is not a checkout, and
    a host with nothing deployed gets `missing-deployed`, never `drifted`."""
    assert cw.checkout_root() == REPO_ROOT
    d = cw.deployed_copy_drift("dep_pr_watcher",
                               hermes_home=str(tmp_path / "nothing-here"))
    assert d["status"] == "missing-deployed" and d["repo"] is None


def test_a_drifted_copy_may_no_longer_read_as_not_configured(sandbox):
    """THE bug, pinned. No stamp + no marker used to mean "nobody installed
    this"; for a stale deployed copy that was a lie about the operator. It now
    reads `copy-drifted` and warns."""
    v = cw.verdict("dep_pr_watcher", now=NOW, copy=_copy())
    assert v["status"] == "copy-drifted" and v["warn"] is True
    assert v["copy"] == "drifted"
    assert v["detail"].startswith("WARN:")
    assert "re-run scripts/install_dep_watcher.sh" in v["detail"]


def test_the_not_configured_rule_stands_when_no_comparison_is_asked(sandbox):
    """The constraint on this card, pinned as the pair of the test above: the
    read seam's `not-configured`-when-no-stamp rule is UNCHANGED unless the
    caller hands in a comparison — and a comparison that found no mismatch
    (any status but `drifted`) must not change the verdict either."""
    v = cw.verdict("dep_pr_watcher", now=NOW)
    assert v["status"] == "not-configured" and v["warn"] is False
    assert v["copy"] is None and "WARN" not in v["detail"]
    for st in ("in-sync", "missing-deployed", "missing-repo", "unreadable",
               "not-a-copy"):
        v2 = cw.verdict("dep_pr_watcher", now=NOW, copy=_copy(status=st))
        assert v2["status"] == "not-configured" and v2["warn"] is False, st


def test_a_fresh_stamp_outranks_a_drifted_copy(sandbox):
    """Anti-nag: a watcher whose stamp proves it ran must not warn because the
    checkout moved ahead. The drift is carried on the row (`copy`), not raised
    as a fault — otherwise every `git pull` would light up the doctor."""
    _write_stamp("dep_pr_watcher", age_s=10)
    v = cw.verdict("dep_pr_watcher", now=NOW, copy=_copy())
    assert v["status"] == "ok" and v["warn"] is False
    assert v["copy"] == "drifted"


def test_an_installed_never_ran_watcher_names_the_drift_as_the_cause(sandbox):
    """Marker present keeps its own (stronger) status; the drift rides along as
    the likely reason, so the fix is the installer and not a cron chase."""
    cw.mark_installed("dep_pr_watcher", "0 8 * * *")
    v = cw.verdict("dep_pr_watcher", now=NOW, copy=_copy())
    assert v["status"] == "never-ran" and v["warn"] is True
    assert "never completed a pass" in v["detail"]
    assert "re-run scripts/install_dep_watcher.sh" in v["detail"]


def test_doctor_reports_a_stale_install_and_points_at_that_installer(sandbox,
                                                                      tmp_path):
    hermes_home, repo_dir = _homes(tmp_path, deployed="old\n", repo="new\n")
    row = cw.doctor_check(now=NOW, check_deployed_copies=True,
                          env={"HOME": str(tmp_path)}, repo_dir=str(repo_dir))
    assert row["ok"] is False and row["fatal"] is False
    assert "dep_pr_watcher" in row["detail"]
    assert "NOT the repo copy" in row["detail"]
    # the fix must say STALE-not-MISSING and name the one installer that fixes
    # it, or the operator goes hunting through `hermes cron list` for a job
    # that is fine.
    assert "STALE, not missing" in row["fix"]
    assert "scripts/install_dep_watcher.sh" in row["fix"]


def test_a_stale_copy_in_the_shared_home_is_found_from_a_profile_shell(tmp_path,
                                                                        monkeypatch):
    """The live case, measured while writing this: the drifted copy lives in the
    SHARED ~/.hermes/scripts, but a doctor run from a profile shell has
    HERMES_HOME set to that profile's home. Consulting only the caller's own
    home reported `missing-deployed` — a confident clean verdict about a copy
    one directory away, which is worse than no check. Both homes get compared."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile-home"))
    (tmp_path / "profile-home" / "scripts").mkdir(parents=True)
    hermes_home, repo_dir = _homes(tmp_path, deployed="old code\n",
                                   repo="new code\n")
    d = cw.deployed_copy_drift("dep_pr_watcher", repo_dir=str(repo_dir))
    assert d["status"] == "drifted", d
    assert d["deployed"] == str(hermes_home / "scripts" / DRIFT_SCRIPT)
    assert str(tmp_path / "profile-home" / "scripts" / DRIFT_SCRIPT) in d["candidates"]
    # And the narrow seam is what hides it: same tree, one home only.
    narrow = cw.deployed_copy_drift("dep_pr_watcher",
                                    hermes_home=str(tmp_path / "profile-home"),
                                    repo_dir=str(repo_dir))
    assert narrow["status"] == "missing-deployed"


def test_the_candidate_homes_are_the_bound_home_and_the_shared_one(tmp_path,
                                                                    monkeypatch):
    """Two homes, in that order, deduped: the installer's own binding first."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert cw.hermes_home_candidates() == [str(tmp_path / ".hermes")]
    monkeypatch.setenv("HERMES_HOME", "/profiles/work")
    assert cw.hermes_home_candidates() == ["/profiles/work",
                                           str(tmp_path / ".hermes")]


def test_doctor_must_not_read_a_host_scripts_dir_it_was_not_given(tmp_path,
                                                                  monkeypatch):
    """The doctrine that keeps a sandboxed read off the operator's live tree
    (t_06ead443), applied to the NEW filesystem read: a redirected `hscc_dir`
    does not grant permission to resolve the bound home on its own. With a
    genuinely drifted copy planted at $HOME/.hermes/scripts, the redirected row
    stays the clean stamp read — and the SAME tree read with an explicit
    hermes_home does see the drift, so it is the seam that enables the check,
    never an accident of HOME."""
    _homes(tmp_path, deployed="old\n", repo="new\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    root = tmp_path / "sandbox-hscc"
    (root / "state").mkdir(parents=True)

    row = cw.doctor_check(hscc_dir=str(root), now=NOW)
    assert row["ok"] is True and row["fatal"] is False
    assert "not configured" in row["detail"] and "WARN" not in row["detail"]

    seen = cw.doctor_check(hscc_dir=str(root), now=NOW,
                           hermes_home=str(tmp_path / ".hermes"),
                           repo_dir=str(tmp_path / "checkout"))
    assert seen["ok"] is False and "NOT the repo copy" in seen["detail"]
