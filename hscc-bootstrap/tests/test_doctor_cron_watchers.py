"""Doctor coverage for the cron-watchers check (GAP-B-4, t_87663869).

Two layers, same shape as test_doctor_orch_waker.py:

1. The BRIDGE helpers inside doctor.py (loader + row builder): a missing
   hscc_daemon module must surface as an honest UNVERIFIABLE row, never a
   silent vanishing of the check.

2. The SEAM inside run_doctor(): the check's real targets are
   ~/.hscc/state/dep_pr_watcher.json, ~/.hscc/state/escalate_watcher.json and
   the install marker beside them — the OPERATOR's live tree when this suite
   runs from a dispatched worker. Inside a pytest process the verdict must be
   injected (``_cron_watchers_check``) or the row renders as a refusal naming
   the seam; it must never read the operator's watcher stamps.

The verdicts themselves (ok / stale-warn / not-configured / never-ran) are
pinned against the real cron_watchers.doctor_check in
hscc_daemon/tests/test_cron_watchers.py — here we pin only that doctor
ROUTES the verdict and honours the fatal flag. GAP-B-4 is warn-only by
contract: a stale watcher must never sink the doctor (fatal=False).
"""

import json
import sys

import doctor

CHECK_NAME = "cron watchers (dep_pr/escalate)"

FAKE_CLUSTER = json.dumps([{"name": "hscc", "hosts": ["10.0.0.1"]}])


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


def _row(**verdict):
    base = {"name": CHECK_NAME, "ok": True, "fatal": False,
            "detail": "dep_pr_watcher (hscc-dep-watcher) fresh (12s old); "
                      "escalate_watcher (hscc-escalate-watcher) fresh (60s old)",
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

def test_run_doctor_reports_the_watchers_row_when_injected(tmp_path, monkeypatch):
    res = _doctor(tmp_path, monkeypatch, _cron_watchers_check=_row())
    names = {c["name"]: c for c in res["checks"]}
    assert CHECK_NAME in names, "the cron watchers must have their OWN row"
    assert names[CHECK_NAME]["ok"] is True
    assert names[CHECK_NAME]["fatal"] is False


def test_watchers_row_is_in_the_check_list_exactly_once(tmp_path, monkeypatch):
    """The checklist is keyed by name; a duplicated row would double-count the
    verdict and confuse the ○/● rendering."""
    res = _doctor(tmp_path, monkeypatch, _cron_watchers_check=_row())
    assert [c["name"] for c in res["checks"]].count(CHECK_NAME) == 1


def test_run_doctor_routes_stale_as_warn_without_sinking_the_doctor(
        tmp_path, monkeypatch):
    """THE GAP-B-4 acceptance: a dead watcher is ok=False (reported) but
    fatal=False, so the doctor's overall verdict — the install gate — stands."""
    res = _doctor(tmp_path, monkeypatch, _cron_watchers_check=_row(
        ok=False, fatal=False,
        detail="WARN: dep_pr_watcher last ran 4d ago -- cron not firing"))
    row = {c["name"]: c for c in res["checks"]}[CHECK_NAME]
    assert row["ok"] is False and row["fatal"] is False
    assert "4d ago" in row["detail"]
    assert res["ok"] is True, "a stale cron watcher must never fail preflight"


def test_run_doctor_not_configured_is_a_clean_pass(tmp_path, monkeypatch):
    """Nobody installed the watchers: doctor must say so cleanly and pass —
    no warn, no fix nag, no red."""
    res = _doctor(tmp_path, monkeypatch, _cron_watchers_check=_row(
        ok=True, detail="dep_pr_watcher: not configured (no stamp); "
                        "escalate_watcher: not configured (no stamp)"))
    row = {c["name"]: c for c in res["checks"]}[CHECK_NAME]
    assert row["ok"] is True and row["fatal"] is False
    assert "not configured" in row["detail"]


def test_run_doctor_inside_pytest_refuses_an_uninjected_check(tmp_path, monkeypatch):
    """THE fence. fleet_checks=True with no _cron_watchers_check must not read
    the operator's ~/.hscc — the row comes back a refusal naming the seam."""
    res = _doctor(tmp_path, monkeypatch)
    row = {c["name"]: c for c in res["checks"]}[CHECK_NAME]
    assert row["ok"] is False
    assert "refused" in row["detail"] and "pytest" in row["detail"]


def test_row_builder_follows_the_injected_verdict_exactly(tmp_path, monkeypatch):
    """The row copies the five Check fields and adds nothing: doctor's
    renderer and exit logic key off them, so an invented or dropped field is
    a contract break at the bridge, not in the check."""
    seen = _row(ok=False, fatal=False, detail="d", fix="f", extra="nope")
    row = doctor._cron_watchers_check_row(seen)
    assert {f for f in row.__dataclass_fields__} == {"name", "ok", "detail",
                                                     "fix", "fatal"}
    assert row.name == CHECK_NAME and row.ok is False and row.detail == "d"
    assert row.fix == "f" and row.fatal is False


# ── the bridge (loader + unverifiable posture) ─────────────────────────────

def test_row_reports_unverifiable_when_module_missing(tmp_path, monkeypatch):
    """A deployment where hscc_daemon/cron_watchers.py is not next to
    bootstrap: the row must say liveness CANNOT be verified, never vanish and
    never claim ok (the notifier-mask honesty rule)."""
    monkeypatch.setattr(doctor, "_load_cron_watchers", lambda: None)
    # delitem restores sys.modules["pytest"] at teardown — the same idiom the
    # waker bridge test uses to force the non-pytest path.
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    row = doctor._cron_watchers_check_row(None)
    assert row.ok is False and row.fatal is False
    assert "not importable" in row.detail
    assert "re-run bootstrap" in row.fix


def test_row_uses_the_real_check_outside_pytest_and_forwards_hscc_dir(tmp_path,
                                                                       monkeypatch):
    """The human-CLI/sandbox path: the row delegates to
    cron_watchers.doctor_check(hscc_dir=...) — the stub proves the delegation
    AND the HSCC_DIR pass-through (the bootstrap sandbox reads the SANDBOX's
    ~/.hscc root; reading the operator's live tree here is the historical
    doctor bug)."""
    calls = []

    def fake_check(hscc_dir=None):
        calls.append(hscc_dir)
        return {"name": CHECK_NAME, "ok": True, "fatal": False,
                "detail": "from the real check", "fix": ""}

    class CW:
        doctor_check = staticmethod(fake_check)

    monkeypatch.setattr(doctor, "_load_cron_watchers", lambda: CW())
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    row = doctor._cron_watchers_check_row(None, hscc_dir="/sandbox/.hscc")
    assert calls == ["/sandbox/.hscc"] and row.detail == "from the real check"
    assert row.ok is True


def test_load_cron_watchers_resolves_the_repo_sibling():
    """The loader's fallback (repo root on sys.path) is what makes the row
    work in a plain checkout where hscc_daemon is not 'installed'."""
    cw = doctor._load_cron_watchers()
    assert cw is not None and cw.CHECK_NAME == CHECK_NAME


def test_row_name_survives_even_without_the_module():
    """The fallback constant must equal the module's CHECK_NAME, or the
    missing-module row prints a name the fix text cannot match."""
    # via the loader, which also puts the repo root on sys.path — bootstrap's
    # own test process does not have hscc_daemon importable otherwise.
    real = doctor._load_cron_watchers()
    assert real is not None
    assert real.CHECK_NAME == doctor._CRON_WATCHERS_CHECK_NAME
