"""Doctor coverage for the cron-watchers check (GAP-B-4, t_87663869).

Two layers, same shape as test_doctor_orch_waker.py:

1. The BRIDGE helpers inside doctor.py (loader + row builder): a missing
   hscc_daemon module must surface as an honest UNVERIFIABLE row, never a
   silent vanishing of the check.

2. The SEAM inside run_doctor(): the check's real targets are
   ~/.hscc/state/dep_pr_watcher.json, ~/.hscc/state/escalate_watcher.json,
   ~/.hscc/state/alone_sweep_gate.json and the install marker beside them — the
   OPERATOR's live tree when this suite runs from a dispatched worker. Inside a
   pytest process the verdict must be injected (``_cron_watchers_check``) or the
   row renders as a refusal naming the seam; it must never read the operator's
   watcher stamps.

The verdicts themselves (ok / stale-warn / not-configured / never-ran) are
pinned against the real cron_watchers.doctor_check in
hscc_daemon/tests/test_cron_watchers.py — here we pin only that doctor
ROUTES the verdict and honours the fatal flag. GAP-B-4 is warn-only by
contract: a stale watcher must never sink the doctor (fatal=False).
"""

import json
import sys

import doctor

CHECK_NAME = "cron watchers (dep_pr/escalate/alone_sweep)"

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
    # sparkrun is a FATAL preflight check. A CI/dev box that has not installed
    # it must not sink this unrelated row's verdict; the probe itself is
    # covered by test_doctor.TestSparkrunCheck.
    monkeypatch.setattr(doctor, "_sparkrun_ok",
                        lambda: doctor.Check("sparkrun", True, detail="stubbed"))
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
    # The fence is shared and child-visible now (doctor_rows.under_pytest),
    # so the marker pytest exports for THIS item has to go too, or the row
    # refuses and the branch under test is never taken.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    row = doctor._cron_watchers_check_row(None)
    assert row.ok is False and row.fatal is False
    assert "not importable" in row.detail
    assert "re-run bootstrap" in row.fix


def test_row_uses_the_real_check_outside_pytest_and_never_a_relocated_root(
        tmp_path, monkeypatch):
    """The human-CLI/sandbox path: the row delegates to
    cron_watchers.doctor_check(check_deployed_copies=True) — the stub proves the
    delegation AND that it carries NO root (t_adb66608).

    This is the writer/reader half of the HSCC_DIR rule: the watcher ledger is
    written by Hermes cron jobs and the installers, none of which see
    ``HSCC_DIR``, so a row that forwarded the ambient root would read a
    directory nothing can write to and call live watchers "not configured"
    (audit t_572b4856 §10.1, the measured live-host case). Passing an hscc_dir
    here and being ignored is the point — a row whose subject has one root is
    not handed another one. ``check_deployed_copies`` stays the deliberate
    opt-in (t_95eb9f15): on the live host the doctor asks for it, so a stale
    installed copy reads as stale instead of as "not configured"."""
    calls = []

    def fake_check(hscc_dir=None, check_deployed_copies=False):
        calls.append((hscc_dir, check_deployed_copies))
        return {"name": CHECK_NAME, "ok": True, "fatal": False,
                "detail": "from the real check", "fix": ""}

    class CW:
        doctor_check = staticmethod(fake_check)

    monkeypatch.setattr(doctor, "_load_cron_watchers", lambda: CW())
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    # The fence is shared and child-visible now (doctor_rows.under_pytest),
    # so the marker pytest exports for THIS item has to go too, or the row
    # refuses and the branch under test is never taken.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    row = doctor._cron_watchers_check_row(None, hscc_dir="/sandbox/.hscc")
    assert calls == [(None, True)], "the cron row must not be handed a root"
    assert row.detail == "from the real check"
    assert row.ok is True


def test_the_cron_row_declares_no_hscc_dir_in_the_registry():
    """The wiring half of the same rule (t_adb66608), pinned where it is set.

    ``row_kwargs`` is how run_doctor hands a row its context, so a row that
    declares ("hscc_dir",) will receive os.environ["HSCC_DIR"] no matter what
    its own function does. The cron row declares NOTHING: the disagreement this
    card closes cannot be re-wired by editing one call site, and the default
    ("hscc_dir",) on the dataclass makes an omission loud rather than silent.
    """
    spec = {s.key: s for s in doctor.FEATURE_CHECK_ROWS}["cron_watchers"]
    assert spec.row_kwargs == ()
    # And the row function still takes the kwarg (every feature row shares the
    # shape), so the pin above is the only path a root could arrive by.
    import inspect as _inspect
    assert "hscc_dir" in _inspect.signature(
        doctor._cron_watchers_check_row).parameters


def test_row_survives_a_cron_watchers_without_the_drift_seam(monkeypatch):
    """An OLDER deployed cron_watchers takes no check_deployed_copies (the
    deployed plugin payload is a release, not this checkout). The row must fall
    back to the stamp read rather than crash the doctor — a missing feature is
    allowed, an exception in a warn-only row is not."""
    calls = []

    def fake_check(hscc_dir=None):          # old signature: one seam only
        calls.append(hscc_dir)
        return {"name": CHECK_NAME, "ok": True, "fatal": False,
                "detail": "stamp read only", "fix": ""}

    class CW:
        doctor_check = staticmethod(fake_check)   # no check_deployed_copies

    monkeypatch.setattr(doctor, "_load_cron_watchers", lambda: CW())
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    # The fence is shared and child-visible now (doctor_rows.under_pytest),
    # so the marker pytest exports for THIS item has to go too, or the row
    # refuses and the branch under test is never taken.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    row = doctor._cron_watchers_check_row(None, hscc_dir="/sandbox/.hscc")
    assert calls == [None] and row.detail == "stamp read only"


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
