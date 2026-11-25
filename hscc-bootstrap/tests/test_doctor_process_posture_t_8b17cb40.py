"""The scheduling-posture row (card t_8b17cb40).

The gap this row covers: PR #791 pinned the three plist EMITTERS to emit
``ProcessType => Standard``, but nothing read the plist that is actually
INSTALLED. A node keeps whatever posture its file carries until it
re-bootstraps, and while it keeps ``Background`` every merge-queue gate leg pays
~7.6x with nothing saying so (measured in that PR: ``ps -o pri`` 4 vs 20,
4.96 M vs 37.7 M it/s through the real ``merge_queue._run_gate``).

Two legs, as every doctor row has them:

1. the FEATURE side (here): every posture rendered against artefacts a real
   ``plistlib.dump`` wrote (never a fixture's guess at plist syntax), every
   service-manager fact injected, and the fence proven to refuse an ambient read
   — including a canary that plants a red posture under a redirected HOME and
   asserts ``run_doctor()`` neither reads nor writes it.
2. the REGISTRY side: the seam routes, the fallback name cannot drift, the row
   declares no per-row root (its writers resolve from HOME: t_be690204).

There is NO systemd on this host, so the Linux half is exercised through the
``_systemd`` seam only, and the test that does it says so in its own words.
"""

import json
import os
import plistlib
import sys

import doctor
import doctor_process_posture as pp
import doctor_rows
import pytest

CHECK = pp.CHECK_NAME

# A probe output for a loaded job, in the shape `launchctl print` really prints
# (verified against `launchctl print gui/<uid>/com.hermes.hscc_daemon` on this
# host, with the home replaced by the sanctioned fixture user). It carries NO
# process-type line — the card measured that too, and the whole design rests on
# it: the plist is the authority, `spawn type` corroborates.
LAUNCHCTL_LOADED = """gui/501/com.hermes.hscc_daemon = {
\tactive count = 1
\tpath = /Users/hscc-fixture/Library/LaunchAgents/com.hermes.hscc_daemon.plist
\ttype = LaunchAgent
\tstate = running
\tpid = 45663
\tjetsam priority = 40
\tspawn type = daemon (3)
\tjetsamproperties category = daemon
}
"""


def test_the_probe_pulls_spawn_type_and_plist_path_out_of_real_print_output():
    """The two regexes, driven by the text `launchctl print` really emits.

    ``spawn type`` is what tells the loaded posture apart and ``path`` is which
    plist launchd read; both are parsed out of a dump that contains no
    process-type line at all, which is the fact the design rests on.
    """
    seen = []

    def runner(argv, timeout=10):
        seen.append(list(argv))
        return 0, LAUNCHCTL_LOADED

    available, loaded, spawn, path, _note = pp.launchd_probe(_run_probe=runner)
    assert (available, loaded, spawn) == (True, True, "daemon")
    assert path.endswith("Library/LaunchAgents/com.hermes.hscc_daemon.plist")
    assert len(seen) == 1 and seen[0][0] == "launchctl"
    assert seen[0][1] == "print" and seen[0][2].startswith("gui/")
    assert "com.hermes.hscc_daemon" in seen[0][2]

    # rc != 0 is "not loaded", not "broken": the caller must not call a missing
    # job a posture fault. (The runner's contract is (returncode, output), the
    # same shape doctor_supervision._run returns.)
    assert pp.launchd_probe(
        _run_probe=lambda argv, timeout=10: (3, "Could not find service")
    )[:2] == (True, False)
    # and a missing binary is an unavailable probe, not a verdict.
    assert pp.launchd_probe(
        _run_probe=lambda argv, timeout=10: (None, "command not found: launchctl")
    )[0] is False


def write_plist(path, **keys):
    """A REAL plist, written by plistlib (a grep-bait fixture proves nothing)."""
    body = {"Label": "com.hermes.hscc_daemon",
            "ProgramArguments": ["/bin/python", "-m", "hscc_daemon"]}
    body.update(keys)
    with open(path, "wb") as f:
        plistlib.dump(body, f)
    return str(path)


def launchd(available=True, loaded=True, spawn="daemon", plist_path=None,
            note="job loaded"):
    """The tuple ``launchd_probe`` returns, injected."""
    return (available, loaded, spawn, plist_path, note)


def systemd(available=True, active=True, props=None, note="unit active"):
    return (available, active, props if props is not None else {}, note)


# ── the plist is the authority: every ProcessType posture ──────────────────

def test_standard_is_ok_and_names_the_measured_priority(tmp_path):
    path = write_plist(tmp_path / "p.plist", ProcessType="Standard")
    res = pp.doctor_check(plist_path=path, _launchd=launchd(),
                          _systemd=systemd(), _platform="darwin")
    assert res["ok"] is True and res["fatal"] is False
    assert res["name"] == CHECK
    # The green row prints the number that matters, not just "ok".
    assert "Standard" in res["detail"] and "20" in res["detail"]


@pytest.mark.parametrize("value,pri,word", [
    ("Background", 4, "DEPRIORITISED"),
    ("Adaptive", 4, "DEPRIORITISED"),
    ("Interactive", 31, "OVER-PRIORITISED"),
])
def test_a_non_standard_posture_warns_and_says_what_it_costs(tmp_path, value,
                                                             pri, word):
    path = write_plist(tmp_path / "p.plist", ProcessType=value)
    res = pp.doctor_check(plist_path=path, _launchd=launchd(),
                          _systemd=systemd(), _platform="darwin")
    assert res["ok"] is False and res["fatal"] is False, "warn, never fatal"
    assert word in res["detail"]
    assert value in res["detail"]
    assert str(pri) in res["detail"], "the measured pri is the evidence"
    assert "Standard" in res["fix"], "the fix names the posture to restore"


def test_a_keyless_plist_warns_as_unpinned_not_as_standard(tmp_path):
    """Absent is NOT the same as pinned. It measures like Standard today, and
    the row says so — but it warns, because one hand-edit to Background would
    cost every gate 3-7x with nothing left pinning the posture."""
    path = write_plist(tmp_path / "p.plist")
    res = pp.doctor_check(plist_path=path, _launchd=launchd(),
                          _systemd=systemd(), _platform="darwin")
    assert res["ok"] is False and res["fatal"] is False
    assert "UNPINNED" in res["detail"]
    assert "ProcessType" in res["detail"]


def test_an_unknown_value_is_reported_not_guessed(tmp_path):
    path = write_plist(tmp_path / "p.plist", ProcessType="Maintenence")
    res = pp.doctor_check(plist_path=path, _launchd=launchd(),
                          _systemd=systemd(), _platform="darwin")
    assert res["ok"] is False and res["fatal"] is False
    assert "UNKNOWN" in res["detail"] and "Maintenence" in res["detail"]


@pytest.mark.parametrize("value", ["Standard", "Background", "Adaptive",
                                   "Interactive", None, "Nonsense"])
def test_no_verdict_is_ever_fatal_and_always_has_five_fields(tmp_path, value):
    """The card's ruling as a blanket property: a node may legitimately run
    another posture, so nothing here may sink bootstrap Stage 1."""
    keys = {} if value is None else {"ProcessType": value}
    path = write_plist(tmp_path / "p.plist", **keys)
    res = pp.doctor_check(plist_path=path, _launchd=launchd(),
                          _systemd=systemd(), _platform="darwin")
    assert set(res) == {"name", "ok", "detail", "fix", "fatal"}
    assert res["fatal"] is False
    assert res["name"] == CHECK and res["detail"], "a row never goes quiet"


# ── the loaded job: what a plist-only read would miss ──────────────────────

def test_a_fixed_file_with_a_stale_bootstrap_still_warns(tmp_path):
    """THE case this row exists for.

    The operator has already written Standard into the file (or bootstrap did),
    but launchd still runs the job from the file it loaded earlier: the node
    keeps paying the tax and a plist-only row would read GREEN. ``spawn type``
    is how the row sees it — measured to discriminate daemon(3)/background(5)/
    adaptive(6)/interactive(4) across the five postures.
    """
    path = write_plist(tmp_path / "p.plist", ProcessType="Standard")
    res = pp.doctor_check(plist_path=path, _launchd=launchd(spawn="background"),
                          _systemd=systemd(), _platform="darwin")
    assert res["ok"] is False and res["fatal"] is False
    assert "STALE BOOTSTRAP" in res["detail"]
    assert "background" in res["detail"]
    assert "bootstrap" in res["fix"], "the fix is a re-bootstrap, not an edit"


def test_a_loaded_job_uses_the_plist_launchd_actually_read(tmp_path):
    """The file launchd names is the file that decides the posture.

    A second, RED plist sitting in the conventional location must not move the
    verdict: one file per question, and the question is about the loaded job.
    """
    real = write_plist(tmp_path / "real.plist", ProcessType="Standard")
    write_plist(tmp_path / "com.hermes.hscc_daemon.plist",
                ProcessType="Background")
    res = pp.doctor_check(_launchd=launchd(plist_path=real),
                          _systemd=systemd(), _platform="darwin")
    assert res["ok"] is True, "the loaded job's own plist is the subject"
    # …and the isolation runs the other way too: the same file loaded as
    # Background is red even though nothing else changed.
    bad = write_plist(tmp_path / "bad.plist", ProcessType="Background")
    res2 = pp.doctor_check(_launchd=launchd(plist_path=bad),
                           _systemd=systemd(), _platform="darwin")
    assert res2["ok"] is False and "DEPRIORITISED" in res2["detail"]


def test_absent_plist_and_no_loaded_job_is_a_skip_not_a_failure(tmp_path):
    """Nothing is installed here (a dev checkout, a systemd node): the
    supervision row owns "is the daemon running"; this row stays quiet-green."""
    res = pp.doctor_check(plist_path=str(tmp_path / "absent.plist"),
                          _launchd=launchd(loaded=False), _systemd=systemd(),
                          _platform="darwin")
    assert res["ok"] is True and res["fatal"] is False
    assert "skipped" in res["detail"]


def test_loaded_job_with_an_unreadable_plist_warns(tmp_path):
    """launchd says the job is loaded; nothing can say what it declares. That
    is "cannot verify", never ok and never a guessed posture."""
    junk = tmp_path / "junk.plist"
    junk.write_text("<plist not really\n")
    res = pp.doctor_check(plist_path=str(junk), _launchd=launchd(),
                          _systemd=systemd(), _platform="darwin")
    assert res["ok"] is False and res["fatal"] is False
    assert "UNREADABLE" in res["detail"]


def test_no_launchctl_at_all_is_a_skip(tmp_path):
    """A machine with no launchd (CI container) has no launchd posture to have
    (the same fail-open rule doctor_supervision.check applies)."""
    path = write_plist(tmp_path / "p.plist", ProcessType="Background")
    res = pp.doctor_check(plist_path=path,
                          _launchd=launchd(available=False, loaded=False,
                                           spawn=None,
                                           note="command not found: launchctl"),
                          _systemd=systemd(), _platform="darwin")
    assert res["ok"] is True and "skipped" in res["detail"]


def test_the_verdict_does_not_come_from_launchctl_text(tmp_path):
    """The row reads ProcessType from the FILE, not from launchd's output.

    ``launchctl print`` has no process-type line (measured: its
    ``jetsam priority = 40`` is identical for Standard, Background, Adaptive,
    Interactive and a keyless job), and a row that grepped it would report a
    posture it cannot see. The plist says Standard, the probe output names no
    such thing, and the verdict is still Standard.
    """
    path = write_plist(tmp_path / "p.plist", ProcessType="Standard")
    seen = []

    def probe(_run_probe=None):
        seen.append(1)
        return launchd(spawn="daemon")

    res = pp.doctor_check(plist_path=path, _launchd=probe, _systemd=systemd(),
                          _platform="darwin")
    assert res["ok"] is True and "Standard" in res["detail"]
    assert seen == [1], "the probe is asked exactly once (no per-key probe storm)"

    # And the other way round, which is the mutation bait: a RED plist with a
    # launchd that looks perfectly healthy must still be red. A row that read
    # its verdict out of `launchctl print` would call this fine.
    red = write_plist(tmp_path / "red.plist", ProcessType="Background")
    res = pp.doctor_check(plist_path=red, _launchd=launchd(spawn="daemon"),
                          _systemd=systemd(), _platform="darwin")
    assert res["ok"] is False and "DEPRIORITISED" in res["detail"]


# ── the systemd half (no systemd on this host: injected probes only) ───────

def test_systemd_deprioritising_knobs_warn():
    """``Nice=``/``CPUWeight=`` are systemd's comparable knobs and NOTHING in
    this repo writes either, so the default (Nice=0 / CPUWeight=100) is the
    fleet's posture. Exercised through the seam: this host has no systemctl."""
    res = pp.doctor_check(_launchd=launchd(available=False, loaded=False),
                          _systemd=systemd(props={"Nice": 10,
                                                  "CPUWeight": 100}),
                          _platform="linux")
    assert res["ok"] is False and res["fatal"] is False
    assert "DEPRIORITISED" in res["detail"] and "Nice=10" in res["detail"]

    res = pp.doctor_check(_launchd=launchd(available=False, loaded=False),
                          _systemd=systemd(props={"Nice": 0,
                                                  "CPUWeight": 10}),
                          _platform="linux")
    assert res["ok"] is False and "CPUWeight=10" in res["detail"]


def test_systemd_defaults_are_ok_and_an_absent_unit_is_a_skip():
    res = pp.doctor_check(_launchd=launchd(available=False, loaded=False),
                          _systemd=systemd(props={"Nice": 0,
                                                  "CPUWeight": 100}),
                          _platform="linux")
    assert res["ok"] is True and "posture ok" in res["detail"]

    res = pp.doctor_check(_launchd=launchd(available=False, loaded=False),
                          _systemd=systemd(active=False, props={}),
                          _platform="linux")
    assert res["ok"] is True and "skipped" in res["detail"]

    res = pp.doctor_check(_launchd=launchd(available=False, loaded=False),
                          _systemd=systemd(available=False, props={}),
                          _platform="linux")
    assert res["ok"] is True and "skipped" in res["detail"]


# ── the systemd half against REAL output (card t_011ae619) ─────────────────
#
# Byte-for-byte captures from `systemctl --user show hscc_daemon.service
# -p Nice -p CPUWeight` on a live fleet node (Ubuntu 24.04.4, systemd 255.4,
# 2026-11-25), against a throwaway user unit in four states. The unit did NOT
# exist on any fleet node (the daemon has no Linux emitter), so the captures
# come from a controlled unit created for the measurement. `od -c` verified:
# every line is `\n`-terminated, no trailing whitespace, no carriage returns.

SYSTEMD_CAPTURE_DEFAULT = "CPUWeight=[not set]\nNice=0\n"
SYSTEMD_CAPTURE_DEPRIORITISED = "CPUWeight=20\nNice=5\n"
SYSTEMD_CAPTURE_NEGATIVE_NICE = "CPUWeight=[not set]\nNice=-5\n"
SYSTEMD_CAPTURE_NICE_ONLY = "CPUWeight=[not set]\nNice=5\n"


def fake_systemctl(raw_show):
    """A `_run_probe` for ``systemd_probe`` answering from one capture.

    ``systemctl --version`` and ``is-active`` answer rc 0 for any unit, so the
    probe runs its real argv and real parse; only the SHOW line is replayed.
    """
    def run(argv, timeout=10):
        argv = [str(a) for a in argv]
        if "--version" in argv:
            return 0, "systemd 255 (255.4-1ubuntu8.16)\n"
        if "is-active" in argv:
            return 0, ""
        if "show" in argv:
            return 0, raw_show
        raise AssertionError(f"unexpected argv: {argv}")
    return run


def test_real_output_default_unit_probe_contract():
    """The probe contract, driven by the bytes a live systemd actually emits.

    A DEFAULT unit (no Nice=, no CPUWeight= in the unit file) still REPORTS
    both knobs: ``Nice=0`` and the literal ``CPUWeight=[not set]``. The row
    renders its green posture, not a skip.
    """
    avail, active, props, note = pp.systemd_probe(
        _run_probe=fake_systemctl(SYSTEMD_CAPTURE_DEFAULT))
    assert (avail, active) == (True, True) and "hscc_daemon" in note
    assert props == {"CPUWeight": pp.SYSTEMD_NOT_SET, "Nice": 0}
    res = pp.verdict(None, "systemd", True, None, None, systemd_props=props,
                     systemd_active=True, manager="systemd")
    assert res["ok"] is True and "posture ok" in res["detail"]
    assert "reported neither" not in res["detail"]


def test_real_output_negative_nice_is_reported_not_skipped():
    """THE measured silent-green (t_011ae619): a ``Nice=-5`` unit answers
    ``CPUWeight=[not set]`` + ``Nice=-5``. The old ``(\\d+)`` matched NEITHER
    line, so the row printed "systemd reported neither Nice= nor CPUWeight="
    — green — on a unit that HAD answered both knobs. Now: the line parses,
    the row says what it measured, and the skip cannot fire here."""
    avail, active, props, _ = pp.systemd_probe(
        _run_probe=fake_systemctl(SYSTEMD_CAPTURE_NEGATIVE_NICE))
    assert props == {"CPUWeight": pp.SYSTEMD_NOT_SET, "Nice": -5}
    res = pp.verdict(None, "systemd", True, None, None, systemd_props=props,
                     systemd_active=True, manager="systemd")
    assert "reported neither" not in res["detail"]
    assert "Nice=-5" in res["detail"]  # over-prioritised is not the taxed
    assert res["ok"] is True           # case by design: the row hunts LESS cpu


def test_real_output_deprioritised_still_warns_from_real_bytes():
    """The warn half on measured bytes: both knobs numeric, both low."""
    _a, _b, props, _n = pp.systemd_probe(
        _run_probe=fake_systemctl(SYSTEMD_CAPTURE_DEPRIORITISED))
    assert props == {"CPUWeight": 20, "Nice": 5}
    res = pp.verdict(None, "systemd", True, None, None, systemd_props=props,
                     systemd_active=True, manager="systemd")
    assert res["ok"] is False and res["fatal"] is False
    assert "DEPRIORITISED" in res["detail"]
    assert "Nice=5 (default 0)" in res["detail"]
    assert "CPUWeight=20 (default 100)" in res["detail"]


def test_real_output_half_reported_is_shown_wholly():
    """``Nice=5`` alone (weight unset): the probe records BOTH reports and
    the warn names the low knob while the shown list carries both."""
    _a, _b, props, _n = pp.systemd_probe(
        _run_probe=fake_systemctl(SYSTEMD_CAPTURE_NICE_ONLY))
    res = pp.verdict(None, "systemd", True, None, None, systemd_props=props,
                     systemd_active=True, manager="systemd")
    assert res["ok"] is False and "Nice=5 (default 0)" in res["detail"]
    assert "CPUWeight=[not set]" in res["detail"]  # the shown list tells all


def test_reported_neither_skip_only_fires_when_both_properties_absent():
    """The regression the card demands: the skip is for properties systemd
    really did not report (measured: an unknown ``-p`` prints nothing, rc 0),
    never for the ``[not set]`` / negative spellings it DID report. And an
    output whose values cannot be parsed at all stays honest-as-skip, so the
    branch is not dead either."""
    for raw, must_skip in (
            ("", True),                                     # printed nothing
            ("MainPID=4242\n", True),                       # other props only
            ("Nice=auto\nCPUWeight=huge\n", True),          # unparsable values
            (SYSTEMD_CAPTURE_DEFAULT, False),               # reported both
            (SYSTEMD_CAPTURE_NEGATIVE_NICE, False),         # negative parsed
    ):
        _a, _b, props, _n = pp.systemd_probe(_run_probe=fake_systemctl(raw))
        res = pp.verdict(None, "systemd", True, None, None,
                         systemd_props=props, systemd_active=True,
                         manager="systemd")
        fired = "reported neither" in res["detail"]
        assert fired is must_skip, (raw, res["detail"])


# ── the fence: no suite reads the operator's LaunchAgents ──────────────────

def test_the_check_refuses_inside_pytest_with_nothing_injected(monkeypatch):
    """An uninjected posture check inside a suite is an ERROR naming the seams.

    Its default targets are the OPERATOR's live job and their real
    ``~/Library/LaunchAgents`` — the ambient read the card explicitly forbids.
    """
    res = pp.doctor_check()
    assert res["ok"] is False and res["fatal"] is False
    assert "refused" in res["detail"] and "plist_path" in res["detail"]


def test_the_fence_refuses_a_planted_live_plist_and_writes_nothing(tmp_path,
                                                                   monkeypatch):
    """The canary: a full plausible LaunchAgents under a redirected HOME.

    A red (Background) posture planted at the conventional path must be neither
    read nor written by the refusal. Guarded against the real path first,
    because the conftest seal redirects ``expanduser`` too, so comparing
    ``expanduser`` against itself would pass anywhere.
    """
    real = os.path.realpath(os.path.expanduser(
        "~/Library/LaunchAgents/com.hermes.hscc_daemon.plist"))
    home = tmp_path / "home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    planted = agents / "com.hermes.hscc_daemon.plist"
    write_plist(planted, ProcessType="Background")
    before = planted.read_bytes()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: p.replace("~", str(home)))
    assert os.path.realpath(os.path.expanduser(
        "~/Library/LaunchAgents/com.hermes.hscc_daemon.plist")) != real

    res = pp.doctor_check()                       # nothing injected
    assert res["ok"] is False and "refused" in res["detail"]
    assert planted.read_bytes() == before
    assert os.listdir(agents) == ["com.hermes.hscc_daemon.plist"]

    # …and pointed AT the planted file (a human CLI run on that box), the row
    # sees the red posture — the canary is a fixture, not a fiction.
    hit = pp.doctor_check(plist_path=str(planted), _launchd=launchd(),
                          _systemd=systemd(), _platform="darwin")
    assert hit["ok"] is False and "DEPRIORITISED" in hit["detail"]


def test_a_probe_that_raises_is_data_not_a_crash(tmp_path):
    """The row never raises: a broken probe is a note, the way
    doctor_supervision.check treats a probe fault."""
    def boom():
        raise RuntimeError("launchd exploded")

    path = write_plist(tmp_path / "p.plist", ProcessType="Standard")
    res = pp.doctor_check(plist_path=path, _launchd=boom, _systemd=systemd(),
                          _platform="darwin")
    assert set(res) == {"name", "ok", "detail", "fix", "fatal"}


def test_label_and_plist_path_come_from_one_vocabulary():
    """The row asks about the SAME job the supervision row asks about."""
    import doctor_supervision
    assert pp._launchd_label() == doctor_supervision.LAUNCHD_LABEL
    assert pp.installed_plist_path().endswith(
        f"{doctor_supervision.LAUNCHD_LABEL}.plist")


# ── the registry side ─────────────────────────────────────────────────────

def by_key():
    return {s.key: s for s in doctor.FEATURE_CHECK_ROWS}


def test_the_fallback_name_equals_the_modules_check_name():
    """doctor.py carries the name as a STRING constant for the case where this
    module cannot be imported; the two must not be allowed to drift."""
    assert doctor._PROCESS_POSTURE_CHECK_NAME == CHECK


def test_the_row_declares_no_root_context():
    """Its writers resolve from HOME (t_be690204).

    ``install.py``, ``flightdeck/commands/daemon_install.py`` and
    ``launchd-setup.sh`` all write ``~/Library/LaunchAgents`` from ``~``, and
    none of them honours ``HSCC_DIR`` — so a root handed to this row would be a
    directory no emitter can reach, and a live job would read as "nothing
    installed".
    """
    spec = by_key()["process_posture"]
    assert spec.row_kwargs == ()
    assert spec.row_fn == "", "the generic builder; no run_doctor() edit"
    assert spec.seam_kwarg == "_process_posture_check"


def test_the_seam_short_circuits_the_loader(tmp_path, monkeypatch):
    """An injected verdict wins over the real read, in a pytest process too."""
    monkeypatch.setattr(doctor, "_load_doctor_process_posture",
                        lambda: pytest.fail("the loader must not run"))
    res = doctor.run_doctor(
        str(tmp_path / ".hermes"),
        _cluster_runner=lambda: json.dumps([{"name": "hscc",
                                             "hosts": ["192.0.2.1"]}]),
        fleet_checks=False, hscc_dir=str(tmp_path / ".hscc"),
        _process_posture_check=lambda: {"name": CHECK, "ok": True,
                                        "fatal": False, "detail": "injected",
                                        "fix": ""})
    row = {c["name"]: c for c in res["checks"]}[CHECK]
    assert row["detail"] == "injected" and row["fatal"] is False


def test_uninjected_inside_pytest_it_refuses_and_never_sinks_the_preflight(
        tmp_path, monkeypatch):
    """The fence at the registry level: ERROR naming the seam, ok False, and
    the run still passes (a refusal is never fatal)."""
    res = doctor.run_doctor(
        str(tmp_path / ".hermes"),
        _cluster_runner=lambda: json.dumps([{"name": "hscc",
                                             "hosts": ["192.0.2.1"]}]),
        fleet_checks=False, hscc_dir=str(tmp_path / ".hscc"))
    row = {c["name"]: c for c in res["checks"]}[CHECK]
    assert row["ok"] is False and row["fatal"] is False
    assert "refused" in row["detail"] and "_process_posture_check" in row["detail"]


def test_a_missing_module_reports_unverifiable_instead_of_vanishing(monkeypatch):
    """The generic builder's UNVERIFIABLE path for THIS row."""
    monkeypatch.setattr(doctor, "_load_doctor_process_posture", lambda: None)
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    spec = by_key()["process_posture"]
    row = doctor_rows.build_row(doctor, spec, None)
    assert row.ok is False and row.fatal is False
    assert "not importable" in row.detail
