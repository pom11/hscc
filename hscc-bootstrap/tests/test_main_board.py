"""The main orchestrator's board: bootstrap ensure + doctor check (M3).

Every fixture pins HERMES_HOME / HSCC_DIR under tmp_path, and the Hermes
surface is faked (a fake ``hermes`` executable for the CLI path, a fake kanban
object for the doctor read). Nothing here can read or switch the operator's
real board.
"""

import json
import os
import subprocess

import doctor
import main_board as mb


def _pin(tmp_path, monkeypatch, name=None, *, started=True):
    home = tmp_path / "home" / ".hermes"
    home.mkdir(parents=True)
    if started:
        # Hermes writes this on first run; ensure() now refuses to run the CLI
        # (and thus create it) before that (t_6f4895aa).
        (home / "config.yaml").write_text("_config_version: 52\n",
                                          encoding="utf-8")
    hscc = tmp_path / "hscc"
    hscc.mkdir()
    if name is not None:
        (hscc / "main-orchestrator.json").write_text(
            json.dumps({"name": name}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HSCC_DIR", str(hscc))
    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
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


# ── fail-closed kanban-root guard (t_abe94f61) ─────────────────────────────

def test_ensure_refuses_a_kanban_root_pinned_outside_the_target_home(
        tmp_path, monkeypatch):
    """HERMES_HOME alone is not containment: a stale HERMES_KANBAN_HOME wins.

    Hermes' rule is ``HERMES_KANBAN_HOME`` first, then the home-derived root.
    A caller that pinned only HERMES_HOME would have mutated this foreign root.
    """
    home, _ = _pin(tmp_path, monkeypatch)
    foreign = tmp_path / "foreign-kanban"
    foreign.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(foreign))
    calls = []
    res = mb.ensure(hermes_bin="/bin/sh", runner=_recording_runner(calls))
    assert res["ok"] is False and res["action"] == "kanban-root-refused"
    assert str(foreign) in res["detail"] and str(home) in res["detail"]
    assert "HERMES_KANBAN_HOME" in res["detail"]
    assert calls == [], "a refused root must not run create/switch"


def test_ensure_refuses_a_throwaway_home_inside_the_native_root(
        tmp_path, monkeypatch):
    """A HERMES_HOME UNDER ~/.hermes resolves back to the live root (t_d39c720e).

    The target is the throwaway dir the caller asked for; Hermes would use the
    native root instead, so the two disagree and the stage must refuse.
    """
    fake_home = tmp_path / "operator-home"
    native = fake_home / ".hermes"
    throwaway = native / "scratch" / "throwaway"
    throwaway.mkdir(parents=True)
    (native / "config.yaml").write_text("_config_version: 52\n",
                                        encoding="utf-8")
    hscc = tmp_path / "hscc"
    hscc.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("HERMES_HOME", str(throwaway))
    monkeypatch.setenv("HSCC_DIR", str(hscc))
    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    calls = []
    res = mb.ensure(hermes_bin="/bin/sh", runner=_recording_runner(calls))
    assert res["ok"] is False and res["action"] == "kanban-root-refused"
    assert str(native) in res["detail"] and str(throwaway) in res["detail"]
    assert calls == []


def test_ensure_accepts_a_profile_scoped_home(tmp_path, monkeypatch):
    """A live profile session (HERMES_HOME=<root>/profiles/x) targets <root>;
    the resolved kanban root is that same root, so the guard must not fire."""
    root = tmp_path / "live-hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    (root / "config.yaml").write_text("_config_version: 52\n", encoding="utf-8")
    hscc = tmp_path / "hscc"
    hscc.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "operator-home"))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HSCC_DIR", str(hscc))
    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    calls = []
    res = mb.ensure(hermes_bin="/bin/sh", runner=_recording_runner(calls))
    assert res["ok"] is True and res["board"] == "general"
    assert [c[3] for c in calls] == ["create", "switch"]


def test_ensure_defers_when_hermes_has_never_run_in_the_home(
        tmp_path, monkeypatch):
    """t_6f4895aa: creating the board here would write config.yaml as a side
    effect, and the later wiring stage would then truthfully but misleadingly
    print "config wired" on a first install. Defer, warn, keep the home fresh."""
    _pin(tmp_path, monkeypatch, started=False)
    calls = []
    res = mb.ensure(hermes_bin="/bin/sh", runner=_recording_runner(calls))
    assert res["ok"] is False and res["action"] == "hermes-not-started"
    assert "no config.yaml" in res["detail"]
    assert "re-run hscc-bootstrap" in res["detail"]
    assert "hermes kanban boards create general --switch" in res["detail"]
    assert calls == [], "the CLI would have created config.yaml — do not run it"


def test_the_refusal_names_the_root_a_hermes_cli_would_use(tmp_path,
                                                           monkeypatch):
    """The guard's answer is compared against a Hermes-shaped binary, not a
    guess. A real Hermes resolves ``HERMES_KANBAN_HOME`` first; the fake here
    reports exactly that (and is never asked to create anything)."""
    home, _ = _pin(tmp_path, monkeypatch)
    foreign = tmp_path / "foreign-kanban"
    foreign.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(foreign))
    fake = tmp_path / "hermes"
    fake.write_text(
        "#!/bin/sh\n"
        "# kanban_home(): HERMES_KANBAN_HOME else the HERMES_HOME root.\n"
        "if [ -n \"$HERMES_KANBAN_HOME\" ]; then\n"
        "  printf '%s\\n' \"$HERMES_KANBAN_HOME\"\n"
        "else\n"
        "  printf '%s\\n' \"$HERMES_HOME\"\n"
        "fi\n",
        encoding="utf-8")
    fake.chmod(0o755)
    reported = subprocess.run(
        [str(fake), "kanban", "boards", "create", "general"],
        capture_output=True, text=True,
        env={**os.environ, "HERMES_HOME": str(home)}).stdout.strip()
    assert reported == str(foreign)
    calls = []
    res = mb.ensure(hermes_bin=str(fake), runner=_recording_runner(calls))
    assert res["action"] == "kanban-root-refused"
    assert reported in res["detail"]
    assert calls == []


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
