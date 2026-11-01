"""Doctor coverage for the orch-waker check (orch-waker W3, t_2b34b5c7).

Two layers, same shape as test_doctor_role_soul.py:

1. The BRIDGE helpers inside doctor.py (loader + row builder): a missing
   hscc_daemon module must surface as an honest UNVERIFIABLE row, never a
   silent vanishing of the check.

2. The SEAM inside run_doctor(): the check's real targets are
   ~/.hscc/config.json and ~/.hscc/state/orch_waker.json — the OPERATOR's live
   daemon state when this suite runs from a dispatched worker. Inside a pytest
   process the verdict must be injected (``_orch_waker_check``) or the row
   renders as a refusal; it must never read the operator's daemon state. A
   canary state file under a redirected dir proves a leak would be visible.

The row's five fields are the doctor Check contract; the verdicts themselves
(ok / warn / disabled / never-ticked) are pinned against the real
orch_waker.doctor_check in hscc_daemon/tests/test_orch_waker_w3.py — here we
pin only that doctor ROUTES the verdict and honours the fatal flag.
"""

import json
import sys

import doctor

CHECK_NAME = "orch-waker (orchestrator waker)"


def _fleet_health():
    """Stub every other fleet seam so run_doctor() cannot reach anything real."""
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


FAKE_CLUSTER = json.dumps([{"name": "hscc", "hosts": ["10.0.0.1"]}])


def _row(**verdict):
    base = {"name": CHECK_NAME, "ok": True, "fatal": False,
            "detail": "orch-waker: enabled; stream last ticked 12s ago",
            "fix": ""}
    base.update(verdict)
    return lambda: dict(base)


def _doctor(tmp_path, monkeypatch, **kw):
    home = _home(tmp_path, monkeypatch)
    return doctor.run_doctor(
        str(home), _cluster_runner=lambda: FAKE_CLUSTER, fleet_checks=True,
        _ssh_factory=lambda h: (lambda c, t: {"ok": False, "stdout": "",
                                              "code": 127, "stderr": "stub"}),
        hscc_dir="/nonexistent",
        **_fleet_health(), **kw)


# ── the seam inside run_doctor ─────────────────────────────────────────────

def test_run_doctor_reports_the_waker_row_when_injected(tmp_path, monkeypatch):
    res = _doctor(tmp_path, monkeypatch, _orch_waker_check=_row())
    names = {c["name"]: c for c in res["checks"]}
    assert CHECK_NAME in names, "the waker must have its OWN row"
    assert names[CHECK_NAME]["ok"] is True
    assert names[CHECK_NAME]["fatal"] is False


def test_run_doctor_routes_a_warning_without_sinking_the_doctor(tmp_path,
                                                                monkeypatch):
    """A stale waker tick is ok=False / fatal=False: printed, but preflight
    still passes (same non-fatal posture as the role-SOUL mismatch)."""
    res = _doctor(tmp_path, monkeypatch,
                  _orch_waker_check=_row(ok=False, detail="stale: 500s"))
    row = {c["name"]: c for c in res["checks"]}[CHECK_NAME]
    assert row["ok"] is False and row["fatal"] is False
    assert "500s" in row["detail"]
    assert res["ok"] is True


def test_run_doctor_honours_the_fatal_never_ticked_verdict(tmp_path, monkeypatch):
    """The one RED case (enabled + daemon running + never ticked) must be able
    to sink preflight — doctor's exit code is the operator's install gate."""
    res = _doctor(tmp_path, monkeypatch,
                  _orch_waker_check=_row(ok=False, fatal=True,
                                        detail="enabled but never ticked"))
    assert res["ok"] is False


def test_run_doctor_inside_pytest_refuses_an_uninjected_check(tmp_path,
                                                              monkeypatch):
    """THE fence. fleet_checks=True with no _orch_waker_check must not read the
    operator's ~/.hscc — the row comes back a refusal naming the seam. No
    canary file is written here on purpose: the refusal assertion alone is the
    tripwire — if the pytest branch ever regressed into running the real
    doctor_check, the detail would carry live tick text instead of 'refused'
    AND would have touched the operator's state dir by then anyway."""
    res = _doctor(tmp_path, monkeypatch)
    row = {c["name"]: c for c in res["checks"]}[CHECK_NAME]
    assert row["ok"] is False
    assert "refused" in row["detail"] and "pytest" in row["detail"]


def test_row_builder_follows_the_injected_verdict_exactly(tmp_path, monkeypatch):
    """The row must copy the five Check fields and add nothing: doctor's
    renderer and exit logic key off them, so an invented field or a dropped
    one is a contract break at the bridge, not in the check."""
    seen = _row(ok=False, fatal=True, detail="d", fix="f", extra="nope")
    row = doctor._orch_waker_check_row(seen)
    assert {f for f in row.__dataclass_fields__} == {"name", "ok", "detail",
                                                     "fix", "fatal"}
    assert row.name == CHECK_NAME and row.fatal is True
    assert row.detail == "d" and row.fix == "f"


def test_row_builder_reports_unverifiable_when_module_missing(tmp_path,
                                                              monkeypatch):
    """A deployment where hscc_daemon/orch_waker.py is not next to bootstrap:
    the row must say the waker's health CANNOT be verified, never vanish and
    never claim ok (the notifier-mask honesty rule)."""
    monkeypatch.setattr(doctor, "_load_orch_waker", lambda: None)
    # The pytest branch is the very thing this row's non-pytest path bypasses:
    # delitem restores sys.modules["pytest"] at teardown (monkeypatch's own
    # contract), so no other test can ever see the module missing.
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    row = doctor._orch_waker_check_row(None)
    assert row.ok is False
    assert "not importable" in row.detail
    assert row.fatal is False
    assert "re-run bootstrap" in row.fix


def test_row_builder_uses_the_real_check_outside_pytest(tmp_path, monkeypatch):
    """The human-CLI path: with the pytest branch forced off, the row delegates
    to orch_waker.doctor_check(hscc_dir=...) — the injected stub proves the
    delegation AND that the HSCC_DIR pass-through is wired, no real config or
    state is read."""
    calls = []

    def fake_check(hscc_dir=None):
        calls.append(hscc_dir)
        return {"name": CHECK_NAME, "ok": True, "fatal": False,
                "detail": "from the real check", "fix": ""}

    class Waker:
        doctor_check = staticmethod(fake_check)

    monkeypatch.setattr(doctor, "_load_orch_waker", lambda: Waker())
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    row = doctor._orch_waker_check_row(None, hscc_dir="/sandbox/.hscc")
    assert calls == ["/sandbox/.hscc"] and row.detail == "from the real check"


def test_waker_row_is_in_the_check_list_exactly_once(tmp_path, monkeypatch):
    """The checklist is keyed by name; a duplicated row would double-count the
    verdict and confuse the ○/● rendering."""
    res = _doctor(tmp_path, monkeypatch, _orch_waker_check=_row())
    assert [c["name"] for c in res["checks"]].count(CHECK_NAME) == 1
