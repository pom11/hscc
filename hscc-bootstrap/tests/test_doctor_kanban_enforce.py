"""Doctor coverage for the kanban-enforce check (GAP-F-4, t_51931733).

Two layers, same shape as test_doctor_orch_waker.py:

1. The BRIDGE helpers inside doctor.py (loader + row builder): a missing
   hscc_daemon module must surface as an honest UNVERIFIABLE row, never a
   silent vanishing of the check.

2. The SEAM inside run_doctor(): the check's real targets are
   ``~/.hscc/state/kanban_paused.json``, ``~/.hscc/state/kanban_enforce.json``
   and ``~/.hscc/daemon.pid`` — the OPERATOR's live daemon state when this suite
   runs from a dispatched worker. Inside a pytest process the verdict must be
   injected (``_kanban_enforce_check``) or the row renders as a refusal; it must
   never read the operator's daemon state. A canary trio under a redirected
   dir proves a leak would be visible (and stays untouched by the refusal).

The row's five fields are the doctor Check contract; the verdicts themselves
(idle-ok / paused+fresh-ok / stale-warn / failed-tick-warn / never-stamped
fatal / predates-warn / daemon-down-warn / malformed-entry warn) are pinned
against the real kanban_pause.doctor_check in
hscc_daemon/tests/test_kanban_pause_enforce_stream.py — here we pin only that
doctor ROUTES the verdict, honours the fatal flag, and forwards HSCC_DIR.
"""

import json
import sys

import doctor

CHECK_NAME = "kanban enforce (paused-board hold)"

FAKE_CLUSTER = json.dumps([{"name": "hscc", "hosts": ["192.0.2.1"]}])


def _fleet_health():
    """Stub every other fleet seam so run_doctor() cannot reach anything real."""
    return {"_cluster_default_runner": lambda: '{"hosts": ["192.0.2.1"]}',
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


def _row(**verdict):
    base = {"name": CHECK_NAME, "ok": True, "fatal": False,
            "detail": "no boards paused, last enforce 12s ago (budget 180s)",
            "fix": ""}
    base.update(verdict)
    return lambda: dict(base)


def _waker_row():
    return lambda: {"name": "orch-waker (orchestrator waker)", "ok": True,
                    "fatal": False, "detail": "ok", "fix": ""}


def _doctor(tmp_path, monkeypatch, **kw):
    home = _home(tmp_path, monkeypatch)
    kw.setdefault("_orch_waker_check", _waker_row())
    return doctor.run_doctor(
        str(home), _cluster_runner=lambda: FAKE_CLUSTER, fleet_checks=True,
        _ssh_factory=lambda h: (lambda c, t: {"ok": False, "stdout": "",
                                              "code": 127, "stderr": "stub"}),
        hscc_dir="/nonexistent",
        **_fleet_health(), **kw)


# ── the seam inside run_doctor ─────────────────────────────────────────────

def test_run_doctor_reports_the_enforce_row_when_injected(tmp_path, monkeypatch):
    res = _doctor(tmp_path, monkeypatch, _kanban_enforce_check=_row())
    names = {c["name"]: c for c in res["checks"]}
    assert CHECK_NAME in names, "the enforcer must have its OWN row"
    assert names[CHECK_NAME]["ok"] is True
    assert names[CHECK_NAME]["fatal"] is False


def test_run_doctor_routes_a_warning_without_sinking_the_doctor(tmp_path,
                                                                monkeypatch):
    """A stale enforce tick is ok=False / fatal=False: printed, but preflight
    still passes (same non-fatal posture as the waker and watchers rows —
    doctor is the Stage-1 install gate and the daemon may legitimately be
    between restarts)."""
    res = _doctor(tmp_path, monkeypatch,
                  _kanban_enforce_check=_row(ok=False, detail="stale: 900s"))
    row = {c["name"]: c for c in res["checks"]}[CHECK_NAME]
    assert row["ok"] is False and row["fatal"] is False
    assert "900s" in row["detail"]
    assert res["ok"] is True


def test_run_doctor_honours_the_fatal_never_stamped_verdict(tmp_path, monkeypatch):
    """The one RED case (policy live + daemon running + stream never stamped)
    must be able to sink preflight — doctor's exit code is the operator's
    install gate (the orch-waker fatal class)."""
    res = _doctor(tmp_path, monkeypatch,
                  _kanban_enforce_check=_row(ok=False, fatal=True,
                                            detail="never enforced while the "
                                                   "daemon runs"))
    assert res["ok"] is False
    assert CHECK_NAME in res["fatal_failures"]


def test_run_doctor_inside_pytest_refuses_an_uninjected_check(tmp_path,
                                                              monkeypatch):
    """THE fence. fleet_checks=True with no _kanban_enforce_check must not read
    the operator's ~/.hscc — the row comes back a refusal naming the seam."""
    res = _doctor(tmp_path, monkeypatch)
    row = {c["name"]: c for c in res["checks"]}[CHECK_NAME]
    assert row["ok"] is False
    assert row["fatal"] is False
    assert "refused" in row["detail"] and "pytest" in row["detail"]


def test_refusal_leaves_the_canary_daemon_state_untouched(tmp_path, monkeypatch):
    """The tripwire for the fence above: a full plausible ~/.hscc under the
    redirected HSCC_DIR (paused policy + enforce stamp + pid file). If the
    pytest branch ever regressed into the real kanban_pause.doctor_check, the
    detail would carry this live tick text instead of 'refused'."""
    root = tmp_path / ".hscc"
    state = root / "state"
    state.mkdir(parents=True)
    canary_policy = state / "kanban_paused.json"
    canary_policy.write_text(json.dumps(
        {"_": {"note": "canary"},
         "hscc": {"cards": [1], "paused_at": 1000}}))
    canary_stream = state / "kanban_enforce.json"
    canary_stream.write_text(json.dumps(
        {"ok": True, "last_enforce": 1000, "timestamp": 1000}))
    canary_pid = root / "daemon.pid"
    canary_pid.write_text("999999")

    res = _doctor(tmp_path, monkeypatch)
    row = {c["name"]: c for c in res["checks"]}[CHECK_NAME]
    assert "refused" in row["detail"], (
        f"the row read live daemon state instead of refusing: {row['detail']}")
    # Nothing was written back either — the check is read-only by contract.
    assert json.loads(canary_policy.read_text())["hscc"]["cards"] == [1]
    assert json.loads(canary_stream.read_text())["last_enforce"] == 1000
    assert canary_pid.read_text() == "999999"


def test_run_doctor_forwards_the_hscc_dir_env_to_the_row_builder(
        tmp_path, monkeypatch):
    """Call-site pin: the row builder must receive HSCC_DIR, not just be able
    to take it. Bootstrap's sandbox keeps HOME real and relocates only
    HSCC_DIR, so a builder that dropped this kwarg would read the OPERATOR's
    ~/.hscc from inside a sandboxed Stage-1 run (the historical doctor bug)."""
    seen = {}

    def builder(injected=None, hscc_dir=None):
        seen["hscc_dir"] = hscc_dir
        seen["injected"] = injected
        return doctor.Check(CHECK_NAME, True, detail="stub row", fix="",
                            fatal=False)

    monkeypatch.setattr(doctor, "_kanban_enforce_check_row", builder)
    injected = _row()
    _doctor(tmp_path, monkeypatch, _kanban_enforce_check=injected)
    assert seen["hscc_dir"] == str(tmp_path / ".hscc")
    assert seen["injected"] is injected, "the seam must be forwarded verbatim"


def test_enforce_row_is_in_the_check_list_exactly_once(tmp_path, monkeypatch):
    """The checklist is keyed by name; a duplicated row would double-count the
    verdict and confuse the ○/● rendering."""
    res = _doctor(tmp_path, monkeypatch, _kanban_enforce_check=_row())
    assert [c["name"] for c in res["checks"]].count(CHECK_NAME) == 1


# ── the row builder (the bridge) ────────────────────────────────────────────

def test_row_builder_follows_the_injected_verdict_exactly(tmp_path, monkeypatch):
    """The row must copy the five Check fields and add nothing: doctor's
    renderer and exit logic key off them, so an invented field or a dropped
    one is a contract break at the bridge, not in the check."""
    seen = _row(ok=False, fatal=True, detail="d", fix="f", extra="nope")
    row = doctor._kanban_enforce_check_row(seen)
    assert {f for f in row.__dataclass_fields__} == {"name", "ok", "detail",
                                                     "fix", "fatal"}
    assert row.name == CHECK_NAME and row.fatal is True
    assert row.detail == "d" and row.fix == "f"


def test_row_builder_reports_unverifiable_when_module_missing(tmp_path,
                                                              monkeypatch):
    """A deployment where hscc_daemon/kanban_pause.py is not next to bootstrap:
    the row must say the enforcer's health CANNOT be verified, never vanish and
    never claim ok (the notifier-mask honesty rule)."""
    monkeypatch.setattr(doctor, "_load_kanban_pause", lambda: None)
    # The pytest branch is the very thing this row's non-pytest path bypasses:
    # delitem restores sys.modules["pytest"] at teardown (monkeypatch's own
    # contract), so no other test can ever see the module missing.
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    row = doctor._kanban_enforce_check_row(None)
    assert row.ok is False
    assert "kanban_pause.py" in row.detail and "not importable" in row.detail
    assert row.fatal is False
    assert "re-run bootstrap" in row.fix


def test_row_builder_uses_the_real_check_outside_pytest(tmp_path, monkeypatch):
    """The human-CLI path: with the pytest branch forced off, the row delegates
    to kanban_pause.doctor_check(hscc_dir=...) — the injected stub proves the
    delegation AND that the HSCC_DIR pass-through is wired, so no real policy
    file, stream file or pid file is read."""
    calls = []

    def fake_check(hscc_dir=None):
        calls.append(hscc_dir)
        return {"name": CHECK_NAME, "ok": True, "fatal": False,
                "detail": "from the real check", "fix": ""}

    class KP:
        doctor_check = staticmethod(fake_check)

    monkeypatch.setattr(doctor, "_load_kanban_pause", lambda: KP())
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    row = doctor._kanban_enforce_check_row(None, hscc_dir="/sandbox/.hscc")
    assert calls == ["/sandbox/.hscc"] and row.detail == "from the real check"
    assert row.ok is True and row.fatal is False


# ── the loader ──────────────────────────────────────────────────────────────

def test_load_kanban_pause_resolves_the_repo_sibling():
    """The loader's fallback (repo root on sys.path) is what makes the row work
    in a plain checkout where hscc_daemon is not 'installed'."""
    kp = doctor._load_kanban_pause()
    assert kp is not None and kp.CHECK_NAME == CHECK_NAME


def test_row_name_survives_even_without_the_module():
    """The fallback constant must equal the module's CHECK_NAME, or the
    missing-module row prints a name the fix text cannot match."""
    real = doctor._load_kanban_pause()
    assert real is not None
    assert real.CHECK_NAME == doctor._KANBAN_ENFORCE_CHECK_NAME
