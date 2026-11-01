"""Doctor coverage for the GAP-F-5 supervision row (audit F).

Two layers, same shape as test_doctor_orch_waker.py:

1. ``doctor_supervision.check`` itself, driven by injected supervisor probes
   and a tmp pid file — every verdict (supervised / orphan / mismatch /
   stale-pid-file / not-running / skipped) pinned against fixtures, including
   the exact t_61457fbd incident shape (a live pid-file pid NO supervisor
   owns) and the launchd-over-systemd precedence rule.

2. The seam inside ``run_doctor``: inside a pytest process the check must be
   INJECTED (fixture-backed lambda) or it renders as a refusal naming the
   seam — a suite must never ask the live service manager or read the
   operator's real ``~/.hscc/daemon.pid``.

The probes are the restart_daemon.sh pair (``launchctl print`` pid line,
``systemctl --user is-active`` + ``show -p MainPID``), so the fixtures use
real captured output shapes for the launchd parse leg.
"""

import json
import subprocess

import doctor
import doctor_supervision as ds


def _write_pid(tmp_path, pid, name="daemon.pid"):
    p = tmp_path / name
    p.write_text(str(pid))
    return str(p)


def _stub_launchd(loaded=True, pid=None, available=True):
    """A stand-in for _launchd_probe returning (kind, pid, note)."""
    def probe():
        if not available:
            return (None, None, "launchctl probe unavailable: command not found")
        if not loaded:
            return ("launchd", None, "job not loaded")
        return ("launchd", pid, "job loaded")
    return probe


def _stub_systemd(active=False, pid=None, available=True):
    def probe():
        if not available:
            return (None, None, "systemctl probe unavailable: command not found")
        if not active:
            return ("systemd", None, "no daemon unit active")
        return ("systemd", pid, "unit com.hermes.hscc_daemon.service active")
    return probe


ALIVE = lambda pid: True          # noqa: E731 - fixture liveness
DEAD = lambda pid: False          # noqa: E731


def _alive_pids(*pids):
    """Per-pid liveness stub: only the named pids are alive."""
    s = set(pids)
    return lambda pid: pid in s


def _check(tmp_path, *, pid=None, alive=ALIVE, launchd=None, systemd=None):
    pid_file = _write_pid(tmp_path, pid) if pid is not None else \
        str(tmp_path / "absent.pid")
    return ds.check(pid_file=pid_file,
                    _launchd=launchd or _stub_launchd(available=False),
                    _systemd=systemd or _stub_systemd(available=False),
                    _pid_alive=alive)


# ── the launchd pid-line parse (the restart_daemon.sh sed, Python port) ────

class TestLaunchctlParse:
    SAMPLE = """\
gui/501/com.hermes.hscc_daemon = {
\tactive count = 1
\tpid = 92822
\tstate = running
\tprogram = /opt/homebrew/bin/hscc
}
"""

    def test_pid_line_parsed_from_real_print_shape(self):
        calls = []

        def runner(argv, timeout=10):
            calls.append(argv)
            return (0, self.SAMPLE)
        kind, pid, note = ds._launchd_probe(_run_probe=runner)
        assert kind == "launchd" and pid == 92822
        assert calls[0][:2] == ["launchctl", "print"]

    def test_loaded_job_without_pid_line_is_none_pid(self):
        """Mid-ThrottleInterval launchd reports the job but no pid — the
        restart script polls for exactly this; the check must not invent one."""
        def runner(argv, timeout=10):
            return (0, "gui/501/x = {\n\tstate = not running\n}\n")
        kind, pid, note = ds._launchd_probe(_run_probe=runner)
        assert kind == "launchd" and pid is None and note == "job loaded"

    def test_nonzero_rc_is_job_not_loaded(self):
        def runner(argv, timeout=10):
            return (1, "Could not find service in domain for user")
        kind, pid, note = ds._launchd_probe(_run_probe=runner)
        assert kind == "launchd" and pid is None and note == "job not loaded"

    def test_missing_binary_is_a_probe_fault_not_not_loaded(self):
        """rc=None = the probe could not run at all — an environment fault the
        caller must not read as 'no job' (honest-signal rule)."""
        def runner(argv, timeout=10):
            return (None, "command not found: launchctl")
        kind, pid, note = ds._launchd_probe(_run_probe=runner)
        assert kind is None and pid is None and "unavailable" in note


# ── the verdicts ─────────────────────────────────────────────────────────────

class TestSupervisionVerdicts:
    def test_supervised_ok(self, tmp_path):
        res = _check(tmp_path, pid=111,
                     launchd=_stub_launchd(pid=111))
        assert res["ok"] is True and res["fatal"] is False
        assert "supervised" in res["detail"]

    def test_orphan_daemon_warns(self, tmp_path):
        """THE t_61457fbd class: the pid-file process is ALIVE while the
        loaded job reports no/other pid. Warn (never fatal — doctor is the
        Stage-1 install gate), and the detail must name the class."""
        res = _check(tmp_path, pid=222, alive=ALIVE,
                     launchd=_stub_launchd(loaded=True, pid=None))
        assert res["ok"] is False and res["fatal"] is False
        assert "ORPHAN DAEMON" in res["detail"]
        assert "kickstart" in res["fix"]

    def test_orphan_when_no_job_loaded_at_all(self, tmp_path):
        """Manager EXISTS but no job: an orphan from `hscc start` with the
        service never installed — the TCC-blind bootstrap shape. Warn, do NOT
        skip: a loaded manager proves supervision is possible here."""
        res = _check(tmp_path, pid=222, alive=ALIVE,
                     launchd=_stub_launchd(loaded=False))
        assert res["ok"] is False
        assert "ORPHAN DAEMON" in res["detail"]

    def test_both_live_different_pids_is_the_twin_orphan(self, tmp_path):
        """Two loops: the supervised daemon runs at 444 while the pid file (and
        whatever wrote it) names a live 333 the manager does not own — the
        `hscc start`-beside-the-job twin. ORPHAN is the right class, and the
        detail must name the supervisor's pid so the operator sees both."""
        res = _check(tmp_path, pid=333, alive=ALIVE,
                     launchd=_stub_launchd(pid=444))
        assert res["ok"] is False and res["fatal"] is False
        assert "ORPHAN DAEMON" in res["detail"]
        assert "supervisor reports pid 444" in res["detail"]

    def test_pid_mismatch_warns(self, tmp_path):
        """The supervised daemon runs (pid 444 alive); the pid file names a
        DEAD 333 — the file drifted behind reality, blinding pid-file readers."""
        res = _check(tmp_path, pid=333, alive=_alive_pids(444),
                     launchd=_stub_launchd(pid=444))
        assert res["ok"] is False and res["fatal"] is False
        assert "MISMATCH" in res["detail"]

    def test_missing_pid_file_under_live_supervisor_warns(self, tmp_path):
        """The supervised daemon runs but daemon.pid is gone: every reader
        that keys off the pid file (orch-waker, restart) is blind."""
        res = _check(tmp_path, pid=None,
                     launchd=_stub_launchd(pid=555))
        assert res["ok"] is False
        assert "pid file" in res["detail"]

    def test_systemd_supervision_supported(self, tmp_path):
        res = _check(tmp_path, pid=777,
                     systemd=_stub_systemd(active=True, pid=777))
        assert res["ok"] is True
        assert "systemd" in res["detail"]

    def test_launchd_wins_when_both_claim(self, tmp_path):
        """Same precedence restart_daemon.sh's detect_supervisor uses: a pid
        launchd reports is trusted over a systemd one."""
        res = _check(tmp_path, pid=888,
                     launchd=_stub_launchd(pid=888),
                     systemd=_stub_systemd(active=True, pid=999))
        assert res["ok"] is True
        assert "launchd" in res["detail"]

    def test_no_service_manager_skips_ok(self, tmp_path):
        """CI container / exotic OS: no launchctl, no systemctl — supervision
        cannot be armed here, so skip LOUDLY (fail-open, the same rule as the
        restart script's pgrep guard)."""
        res = _check(tmp_path, pid=123, alive=ALIVE)   # both stubs unavailable
        assert res["ok"] is True and "skipped" in res["detail"]

    def test_nothing_running_warns_not_fatal(self, tmp_path):
        """Manager present, daemon absent: doctor runs before the daemon
        exists by design, so this is a warning (○), not a failed preflight."""
        res = _check(tmp_path, pid=None,
                     launchd=_stub_launchd(loaded=False))
        assert res["ok"] is False and res["fatal"] is False
        assert "not running" in res["detail"]

    def test_check_never_raises_on_probe_fault(self, tmp_path):
        def boom():
            raise RuntimeError("service manager on strike")
        res = _check(tmp_path, pid=None, launchd=boom)
        assert "name" in res and "detail" in res


# ── pid-file helpers ────────────────────────────────────────────────────────

class TestPidFileReading:
    def test_good_pid(self, tmp_path):
        p = tmp_path / "daemon.pid"
        p.write_text("  4242 \n")
        assert ds.read_pid_file_pid(str(p)) == 4242

    def test_absent_or_garbage_is_none(self, tmp_path):
        assert ds.read_pid_file_pid(str(tmp_path / "nope")) is None
        p = tmp_path / "daemon.pid"
        p.write_text("not-a-pid")
        assert ds.read_pid_file_pid(str(p)) is None
        p.write_text("0")
        assert ds.read_pid_file_pid(str(p)) is None

    def test_hscc_dir_relocation_matches_waker_convention(self, tmp_path):
        """Sandboxed bootstrap keeps HOME real and relocates HSCC_DIR; the
        pid file must resolve under the sandbox (orch_waker.doctor_check's
        rule), or doctor fires verdicts over the operator's live daemon."""
        got = ds._pid_file_path(hscc_dir=str(tmp_path / ".hscc"))
        assert got == str(tmp_path / ".hscc" / "daemon.pid")


# ── the run_doctor seam ─────────────────────────────────────────────────────

SUPERVISION_NAME = ds.CHECK_NAME


def _row(**verdict):
    base = {"name": SUPERVISION_NAME, "ok": True, "fatal": False,
            "detail": "supervised — launchd pid 111 == pid-file pid",
            "fix": ""}
    base.update(verdict)
    return lambda: dict(base)


def _fleet_health():
    return {"_cluster_default_runner": lambda: '{"hosts": ["10.0.0.1"]}',
            "_status_runner": lambda cl: "",
            "_serving_raw": json.dumps({"units": []}),
            "_venv_python": "/home/u/.venvs/sparkrun/bin/python",
            "_lan_probe": lambda i, n: None}


def _home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "hermes-agent").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HSCC_DIR", str(tmp_path / ".hscc"))
    return home


def _doctor(tmp_path, monkeypatch, **kw):
    home = _home(tmp_path, monkeypatch)
    return doctor.run_doctor(
        str(home), _cluster_runner=lambda: json.dumps(
            [{"name": "hscc", "hosts": ["10.0.0.1"]}]),
        fleet_checks=True,
        _ssh_factory=lambda h: (lambda c, t: {"ok": False, "stdout": "",
                                              "code": 127, "stderr": "stub"}),
        hscc_dir="/nonexistent",
        **_fleet_health(), **kw)


class TestRunDoctorSeam:
    def test_row_present_when_injected(self, tmp_path, monkeypatch):
        res = _doctor(tmp_path, monkeypatch, _supervision_check=_row())
        names = {c["name"]: c for c in res["checks"]}
        assert SUPERVISION_NAME in names, "supervision must have its OWN row"
        assert names[SUPERVISION_NAME]["ok"] is True
        assert names[SUPERVISION_NAME]["fatal"] is False

    def test_orphan_warning_does_not_sink_the_doctor(self, tmp_path,
                                                     monkeypatch):
        res = _doctor(tmp_path, monkeypatch, _supervision_check=_row(
            ok=False, detail="ORPHAN DAEMON — pid-file pid 222 is alive but "
                             "no supervisor owns the job"))
        assert res["ok"] is True, "warn row must not flip preflight"

    def test_uninjected_inside_pytest_refuses_instead_of_probing(self, tmp_path,
                                                                 monkeypatch):
        """The fence: no seam ⇒ ERROR naming the seam, never a silent pass and
        never a live launchctl/pid-file probe of the operator's box."""
        res = _doctor(tmp_path, monkeypatch)
        row = {c["name"]: c for c in res["checks"]}[SUPERVISION_NAME]
        assert row["ok"] is False
        assert "refused inside a pytest process" in row["detail"]
        assert row["fatal"] is False
        assert res["ok"] is True   # a refusal is not a fatal either

    def test_row_printed_exactly_once(self, tmp_path, monkeypatch):
        res = _doctor(tmp_path, monkeypatch, _supervision_check=_row())
        assert [c["name"] for c in res["checks"]].count(SUPERVISION_NAME) == 1
