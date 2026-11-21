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
    """in-sync / not-a-copy / missing-repo are all fail-OPEN. A detector
    that inferred staleness from a missing comparison would nag every host that
    never installed a watcher — the false alarm the GAP-B-4 rows forbid."""
    same = "the same code\n"
    hermes_home, repo_dir = _homes(tmp_path, deployed=same, repo=same)
    assert cw.deployed_copy_drift(
        "dep_pr_watcher", hermes_home=str(hermes_home),
        repo_dir=str(repo_dir))["status"] == "in-sync"
    hermes_home, repo_dir = _homes(tmp_path, deployed=None, repo=same)
    # dep_pr_watcher deploys as a wrapper now (t_accbc5da), so an empty scripts
    # dir is the healthy answer rather than `missing-deployed`; the copy-kind
    # half of this pair is test_the_retired_copy_kind_is_still_understood.
    assert cw.deployed_copy_drift(
        "dep_pr_watcher", hermes_home=str(hermes_home),
        repo_dir=str(repo_dir))["status"] == "not-a-copy"
    hermes_home, repo_dir = _homes(tmp_path, deployed=same, repo=None)
    assert cw.deployed_copy_drift(
        "dep_pr_watcher", hermes_home=str(hermes_home),
        repo_dir=str(repo_dir))["status"] == "missing-repo"


def test_a_wrapper_stream_has_no_deployed_copy_to_go_stale(tmp_path):
    """install_alone_sweep_gate.sh generates a wrapper that runs the repo's gate,
    and since t_accbc5da the other two installers generate the same kind of stub.
    There is then no second copy of the code on the host, so comparing a copy is
    meaningless: `not-a-copy`, never `drifted`."""
    assert cw.WATCHERS["dep_pr_watcher"]["deploy"] == cw.DEPLOY_WRAPPER
    assert cw.WATCHERS["escalate_watcher"]["deploy"] == cw.DEPLOY_WRAPPER
    assert cw.WATCHERS["alone_sweep_gate"]["deploy"] == cw.DEPLOY_WRAPPER
    d = cw.deployed_copy_drift("alone_sweep_gate", hermes_home=str(tmp_path),
                               repo_dir=str(tmp_path))
    assert d["status"] == "not-a-copy" and "wrapper" in d["detail"]


def test_a_wrapper_stream_still_compares_the_copy_the_old_installer_left(tmp_path):
    """The transition window — the reason `deploy: wrapper` is not simply "stop
    looking". git says wrapper; the HOST says whatever it installed. An operator
    who has not re-run the installer yet still has the `cp`-ed file under
    <bound home>/scripts and its cron job still runs it, which is the exact state
    t_95eb9f15 made loud. So a leftover at the copy-shape path is still hashed:
    mismatch → `drifted` (the warn survives the conversion), match → `in-sync`
    (honest advice, no warn)."""
    hermes_home, repo_dir = _homes(tmp_path, deployed="old code\n",
                                   repo="new code\n")
    d = cw.deployed_copy_drift("dep_pr_watcher", hermes_home=str(hermes_home),
                               repo_dir=str(repo_dir))
    assert d["status"] == "drifted", d
    assert "re-run scripts/install_dep_watcher.sh" in d["detail"]
    # The sentence must also say why re-running fixes it for good, or the
    # operator re-runs the same `cp` forever.
    assert "wrapper" in d["detail"]
    same = "the same code\n"
    hermes_home, repo_dir = _homes(tmp_path, deployed=same, repo=same)
    d = cw.deployed_copy_drift("dep_pr_watcher", hermes_home=str(hermes_home),
                               repo_dir=str(repo_dir))
    assert d["status"] == "in-sync", d


def test_the_retired_copy_kind_is_still_understood(tmp_path, monkeypatch):
    """`deploy: copy` has no shipping user since t_accbc5da and must keep
    working anyway: a host's DEPLOYED plugin payload is a release copy whose
    cron_watchers still declares these streams as copies, and `missing-deployed`
    for a stream that genuinely was installed by copy is what `not-configured`
    means. Pinned on a fabricated row instead of being deleted with the `cp`."""
    hermes_home, repo_dir = _homes(tmp_path, deployed=None, repo="code\n")
    monkeypatch.setitem(cw.WATCHERS["dep_pr_watcher"], "deploy", cw.DEPLOY_COPY)
    assert cw.deployed_copy_drift(
        "dep_pr_watcher", hermes_home=str(hermes_home),
        repo_dir=str(repo_dir))["status"] == "missing-deployed"
    # Same empty tree, wrapper kind: the absence is the healthy answer.
    monkeypatch.setitem(cw.WATCHERS["dep_pr_watcher"], "deploy",
                        cw.DEPLOY_WRAPPER)
    assert cw.deployed_copy_drift(
        "dep_pr_watcher", hermes_home=str(hermes_home),
        repo_dir=str(repo_dir))["status"] == "not-a-copy"


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
    a host with nothing deployed gets `not-a-copy` (this stream deploys as a
    wrapper now), never `drifted`."""
    assert cw.checkout_root() == REPO_ROOT
    d = cw.deployed_copy_drift("dep_pr_watcher",
                               hermes_home=str(tmp_path / "nothing-here"))
    assert d["status"] == "not-a-copy" and d["repo"] is None


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
    HERMES_HOME set to that profile's home. Consulting only the caller's own home
    reported an absence — a confident clean verdict about a copy one directory
    away, which is worse than no check. Both homes get compared."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile-home"))
    (tmp_path / "profile-home" / "scripts").mkdir(parents=True)
    hermes_home, repo_dir = _homes(tmp_path, deployed="old code\n",
                                   repo="new code\n")
    d = cw.deployed_copy_drift("dep_pr_watcher", repo_dir=str(repo_dir))
    assert d["status"] == "drifted", d
    assert d["deployed"] == str(hermes_home / "scripts" / DRIFT_SCRIPT)
    assert str(tmp_path / "profile-home" / "scripts" / DRIFT_SCRIPT) in d["candidates"]
    # And the narrow seam is what hides it: same tree, one home only. The status
    # reads `not-a-copy` because this stream is a wrapper stream now
    # (t_accbc5da) — an absence either way, and the point of the test is that the
    # narrow seam MISSES the drift, not which absence word it prints.
    narrow = cw.deployed_copy_drift("dep_pr_watcher",
                                    hermes_home=str(tmp_path / "profile-home"),
                                    repo_dir=str(repo_dir))
    assert narrow["status"] == "not-a-copy"


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
