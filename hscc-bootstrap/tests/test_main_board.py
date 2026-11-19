"""The main orchestrator's board: bootstrap ensure + doctor check (M3).

Every fixture pins HERMES_HOME / HSCC_DIR under tmp_path, and the Hermes
surface is faked (a fake ``hermes`` executable for the CLI path, a fake kanban
object for the doctor read). Nothing here can read or switch the operator's
real board.
"""

import json
import os

import doctor
import main_board as mb


def _pin(tmp_path, monkeypatch, name=None):
    home = tmp_path / "home" / ".hermes"
    home.mkdir(parents=True)
    hscc = tmp_path / "hscc"
    hscc.mkdir()
    if name is not None:
        (hscc / "main-orchestrator.json").write_text(
            json.dumps({"name": name}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HSCC_DIR", str(hscc))
    monkeypatch.delenv("HERMES_BIN", raising=False)
    return home, hscc


class _FakeKanban:
    def __init__(self, current):
        self.current = current
        self.calls = 0

    def get_current_board(self):
        self.calls += 1
        if isinstance(self.current, Exception):
            raise self.current
        return self.current


def _recording_runner(calls, *, fail_on=None):
    def run(argv):
        calls.append(list(argv))
        if fail_on and fail_on in argv:
            return 1, "", f"{fail_on} failed"
        if "create" in argv:
            return 0, "Board created.\n", ""
        return 0, "Active board is now.\n", ""
    return run


# ── ensure: create then switch, by the resolver's board ─────────────────────

def test_ensure_creates_then_switches_the_default_board(tmp_path, monkeypatch):
    _pin(tmp_path, monkeypatch)
    calls = []
    res = mb.ensure(hermes_bin="/bin/sh",
                    runner=_recording_runner(calls))
    assert res["ok"] is True and res["board"] == "general"
    assert calls[0][1:] == ["kanban", "boards", "create", "general"]
    assert calls[1][1:] == ["kanban", "boards", "switch", "general"]


def test_ensure_uses_the_stored_main_name(tmp_path, monkeypatch):
    _pin(tmp_path, monkeypatch, name="ops")
    calls = []
    res = mb.ensure(hermes_bin="/bin/sh",
                    runner=_recording_runner(calls))
    assert res["ok"] is True and res["board"] == "ops"
    assert calls[0][-1] == "ops" and calls[1][-1] == "ops"


def test_ensure_is_idempotent_when_the_board_already_exists(tmp_path,
                                                           monkeypatch):
    _pin(tmp_path, monkeypatch)
    calls = []

    def runner(argv):
        calls.append(list(argv))
        if "create" in argv:
            return 0, "Board 'general' already exists.\n", ""
        return 0, "Active board is now 'general'.\n", ""

    res = mb.ensure(hermes_bin="/bin/sh", runner=runner)
    assert res["ok"] is True and res["action"] == "already-current"
    assert len(calls) == 2, "re-running still switches to the same board"


def test_ensure_failure_names_the_failed_command_and_does_not_switch(
        tmp_path, monkeypatch):
    _pin(tmp_path, monkeypatch)
    calls = []
    res = mb.ensure(hermes_bin="/bin/sh",
                    runner=_recording_runner(calls, fail_on="create"))
    assert res["ok"] is False and res["action"] == "create-failed"
    assert "create failed" in res["detail"]
    assert [c[3] for c in calls] == ["create"], "no switch after create fails"


def test_ensure_switch_failure_is_reported(tmp_path, monkeypatch):
    _pin(tmp_path, monkeypatch)
    calls = []
    res = mb.ensure(hermes_bin="/bin/sh",
                    runner=_recording_runner(calls, fail_on="switch"))
    assert res["ok"] is False and res["action"] == "switch-failed"


def test_ensure_missing_hermes_says_so(tmp_path, monkeypatch):
    _pin(tmp_path, monkeypatch)
    monkeypatch.setattr(mb, "find_hermes_bin", lambda explicit=None: None)
    res = mb.ensure(runner=lambda argv: (0, "", ""))
    assert res["ok"] is False and res["action"] == "missing-hermes"
    assert "HERMES_BIN" in res["detail"]


def test_find_hermes_bin_prefers_the_hermes_home_venv(tmp_path, monkeypatch):
    home, _ = _pin(tmp_path, monkeypatch)
    binary = home / "hermes-agent" / "venv" / "bin" / "hermes"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setattr(mb.shutil, "which", lambda name: None)
    assert mb.find_hermes_bin() == str(binary)


def test_ensure_runs_a_real_fake_hermes_cli_from_the_temp_home(
        tmp_path, monkeypatch):
    """The production path (find binary + subprocess), not the runner seam:
    a fake hermes under HERMES_HOME records the two board verbs."""
    home, _ = _pin(tmp_path, monkeypatch)
    binary = home / "hermes-agent" / "venv" / "bin" / "hermes"
    binary.parent.mkdir(parents=True)
    log = tmp_path / "hermes.log"
    binary.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HERMES_CALL_LOG\"\n")
    binary.chmod(0o755)
    monkeypatch.setenv("HERMES_CALL_LOG", str(log))
    monkeypatch.setattr(mb.shutil, "which", lambda name: None)

    res = mb.ensure()
    assert res["ok"] is True and res["board"] == "general"
    lines = log.read_text().splitlines()
    assert lines == ["kanban boards create general",
                     "kanban boards switch general"]


# ── doctor check: ok / warn / unverifiable ─────────────────────────────────

def test_doctor_ok_when_current_board_is_the_main_board(tmp_path, monkeypatch):
    _pin(tmp_path, monkeypatch)
    fake = _FakeKanban("general")
    res = mb.doctor_check(kanban_db=fake)
    assert res["name"] == mb.CHECK_NAME
    assert res["ok"] is True and res["fatal"] is False
    assert res["fix"] == ""
    assert fake.calls == 1


def test_doctor_warns_with_the_one_line_fix_on_drift(tmp_path, monkeypatch):
    _pin(tmp_path, monkeypatch, name="ops")
    res = mb.doctor_check(kanban_db=_FakeKanban("headless-display"))
    assert res["ok"] is False and res["fatal"] is False
    assert "headless-display" in res["detail"] and "'ops'" in res["detail"]
    assert res["fix"].startswith("hermes kanban boards switch ops")


def test_doctor_warns_not_fatal_when_the_kanban_lib_is_missing(tmp_path,
                                                               monkeypatch):
    _pin(tmp_path, monkeypatch)
    monkeypatch.setattr(mb, "_load_kanban_db", lambda: None)
    res = mb.doctor_check()
    assert res["ok"] is False and res["fatal"] is False
    assert "not importable" in res["detail"]


def test_doctor_warns_when_the_current_board_cannot_be_read(tmp_path,
                                                            monkeypatch):
    _pin(tmp_path, monkeypatch)
    res = mb.doctor_check(kanban_db=_FakeKanban(RuntimeError("db locked")))
    assert res["ok"] is False and res["fatal"] is False
    assert "db locked" in res["detail"]


# ── the doctor row's wiring ─────────────────────────────────────────────────

FAKE_CLUSTER = json.dumps([{"name": "hscc", "hosts": ["192.0.2.1"]}])


def _doctor(tmp_path, monkeypatch, **kw):
    home, _ = _pin(tmp_path, monkeypatch)
    kw.setdefault("_orch_waker_check", lambda: {
        "name": "orch-waker (orchestrator waker)", "ok": True,
        "fatal": False, "detail": "ok", "fix": ""})
    return doctor.run_doctor(
        str(home), _cluster_runner=lambda: FAKE_CLUSTER, fleet_checks=True,
        _ssh_factory=lambda h: (lambda c, t: {"ok": False, "stdout": "",
                                              "code": 127, "stderr": "stub"}),
        hscc_dir=str(tmp_path / "hscc"),
        _cluster_default_runner=lambda: '{"hosts": ["192.0.2.1"]}',
        _status_runner=lambda cl: "",
        _serving_raw=json.dumps({"units": []}),
        _venv_python="/home/u/.venvs/sparkrun/bin/python",
        _lan_probe=lambda i, n: None, **kw)


def test_doctor_routes_the_main_board_row(tmp_path, monkeypatch):
    verdict = {"name": mb.CHECK_NAME, "ok": False, "fatal": False,
               "detail": "kanban current board is 'x', expected 'general'",
               "fix": "hermes kanban boards switch general"}
    res = _doctor(tmp_path, monkeypatch, _main_board_check=lambda: dict(verdict))
    rows = [c for c in res["checks"] if c["name"] == mb.CHECK_NAME]
    assert len(rows) == 1
    assert rows[0]["ok"] is False and rows[0]["fatal"] is False
    assert res["ok"] is True, "board drift must never sink preflight"


def test_doctor_main_board_row_is_not_injected_by_default(tmp_path,
                                                          monkeypatch):
    res = _doctor(tmp_path, monkeypatch)
    row = {c["name"]: c for c in res["checks"]}[mb.CHECK_NAME]
    assert row["ok"] is False and row["fatal"] is False
    assert "refused" in row["detail"] and "_main_board_check" in row["detail"]
