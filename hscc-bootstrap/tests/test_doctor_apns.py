"""Doctor coverage for the APNs push configuration row (card N2).

Two layers:

1. The bridge: a deploy that loses ``hscc_daemon/apns.py`` surfaces as an
   UNVERIFIABLE row, and a pytest process with no injected verdict renders the
   refusal naming ``_apns_check`` — never a read of the operator's ``~/.hscc``.
2. The seam: an injected verdict reaches the check list unchanged (same five
   fields), and a real configured/missing verdict is driven through the feature
   module's own ``doctor_check`` in ``hscc_daemon/tests/test_apns.py``.
"""

import json

import doctor


CHECK_NAME = "APNs push (operator notifications)"


def _fleet_health():
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


def _doctor(tmp_path, monkeypatch, **kw):
    home = _home(tmp_path, monkeypatch)
    return doctor.run_doctor(
        str(home), _cluster_runner=lambda: '{"hosts": ["192.0.2.1"]}',
        fleet_checks=True,
        _ssh_factory=lambda h: (lambda c, t: {"ok": False, "stdout": "",
                                              "code": 127, "stderr": "stub"}),
        hscc_dir="/nonexistent",
        **_fleet_health(), **kw)


def _row(**over):
    base = {"name": CHECK_NAME, "ok": False, "fatal": False,
            "detail": "no APNs key configured — push to the apps is off",
            "fix": "One-time: Apple developer portal -> Keys -> new key with APNs"}
    base.update(over)
    return lambda: dict(base)


def test_registry_owns_the_row_and_its_helpers():
    spec = [r for r in doctor.FEATURE_CHECK_ROWS if r.key == "apns"]
    assert len(spec) == 1
    assert spec[0].seam_kwarg == "_apns_check"
    assert callable(doctor._load_apns)
    assert callable(doctor._refused_apns_check)


def test_injected_verdict_is_rendered_as_a_warning(tmp_path, monkeypatch):
    res = _doctor(tmp_path, monkeypatch, _apns_check=_row())
    row = [c for c in res["checks"] if c["name"] == CHECK_NAME]
    assert len(row) == 1
    assert row[0]["ok"] is False and row[0]["fatal"] is False
    assert "no APNs key configured" in row[0]["detail"]
    assert "Apple developer portal" in row[0]["fix"]


def test_uninjected_check_refuses_inside_pytest(tmp_path, monkeypatch):
    res = _doctor(tmp_path, monkeypatch)
    row = [c for c in res["checks"] if c["name"] == CHECK_NAME]
    assert len(row) == 1
    assert row[0]["ok"] is False
    assert "_apns_check" in row[0]["detail"]
    assert "refused inside a pytest process" in row[0]["detail"]
    assert row[0]["fatal"] is False


def test_missing_module_reports_unverifiable(tmp_path, monkeypatch):
    """The human-CLI path (no pytest fence) turns a missing module into a row."""
    import types
    import doctor_rows

    spec = [r for r in doctor.FEATURE_CHECK_ROWS if r.key == "apns"][0]
    fake = types.SimpleNamespace(
        Check=doctor.Check,
        sys=types.SimpleNamespace(modules={}),  # no "pytest" here
        _load_apns=lambda: None,
        _refused_apns_check=doctor._refused_apns_check,
        _APNS_CHECK_NAME=CHECK_NAME,
    )
    row = doctor_rows.build_row(fake, spec, None)
    assert row.name == CHECK_NAME
    assert row.ok is False and row.fatal is False
    assert "not importable" in row.detail
