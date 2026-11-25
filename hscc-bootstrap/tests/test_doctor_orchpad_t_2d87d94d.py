"""The orchpad doctor row (card t_2d87d94d).

Two halves, as every feature row has them:

* the verdict: a missing plugin is a WARN naming the profile, a worker home is
  never counted, and a home with no orchestrator profiles yet is a clean skip;
* the fence: inside pytest an uninjected call refuses and never reads the
  operator's tree, while an explicit ``hermes_home`` is the test seam.

Every case uses a tmp HERMES_HOME. No test reads ``~/.hermes``.
"""

import os

import yaml

import doctor
import doctor_orchpad as op
import doctor_rows

CHECK = op.CHECK_NAME


def _home(tmp_path, names):
    home = tmp_path / ".hermes"
    for name in names:
        (home / "profiles" / name).mkdir(parents=True)
    return home


def _plugin(home, name, enabled=True):
    target = home / "profiles" / name / "plugins" / "orchpad"
    target.mkdir(parents=True)
    for filename in op.PLUGIN_FILES:
        (target / filename).write_text("payload\n", encoding="utf-8")
    if enabled:
        (home / "profiles" / name / "config.yaml").write_text(
            yaml.safe_dump({"plugins": {"enabled": ["hscc-cluster", "orchpad"]}},
                           sort_keys=False), encoding="utf-8")
    return target


def test_all_orchestrators_covered_is_green_and_workers_are_not_named(tmp_path):
    home = _home(tmp_path, ["general", "demo-orch", "coder"])
    _plugin(home, "general")
    _plugin(home, "demo-orch")

    res = op.doctor_check(hermes_home=str(home), main_name="general")

    assert res["name"] == CHECK and res["ok"] is True and res["fatal"] is False
    assert "general" in res["detail"] and "demo-orch" in res["detail"]
    assert "coder" not in res["detail"]


def test_a_project_orchestrator_without_the_plugin_warns(tmp_path):
    home = _home(tmp_path, ["general", "demo-orch"])
    _plugin(home, "general")

    res = op.doctor_check(hermes_home=str(home), main_name="general")

    assert res["ok"] is False and res["fatal"] is False
    assert "demo-orch" in res["detail"] and "general" not in res["detail"]
    assert "orchpad-hermes" in res["fix"]


def test_files_present_but_not_enabled_warns_and_names_the_profile(tmp_path):
    home = _home(tmp_path, ["general", "demo-orch"])
    _plugin(home, "general")
    _plugin(home, "demo-orch", enabled=False)
    (home / "profiles" / "demo-orch" / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["hscc-cluster"]}},
                       sort_keys=False), encoding="utf-8")

    res = op.doctor_check(hermes_home=str(home), main_name="general")

    assert res["ok"] is False and res["fatal"] is False
    assert "demo-orch" in res["detail"] and "general" not in res["detail"]
    assert "config.yaml plugins.enabled" in res["detail"]
    assert "bootstrap" in res["fix"]


def test_the_main_orchestrator_without_the_plugin_warns_even_if_projects_have_it(
        tmp_path):
    home = _home(tmp_path, ["ops", "demo-orch"])
    _plugin(home, "demo-orch")

    res = op.doctor_check(hermes_home=str(home), main_name="ops")

    assert res["ok"] is False
    assert "ops" in res["detail"] and "demo-orch" not in res["detail"]


def test_a_missing_main_home_is_not_invented(tmp_path):
    home = _home(tmp_path, ["demo-orch"])
    _plugin(home, "demo-orch")

    res = op.doctor_check(hermes_home=str(home), main_name="general")

    assert res["ok"] is True
    assert "demo-orch" in res["detail"]


def test_no_profiles_yet_is_a_clean_skip(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()

    res = op.doctor_check(hermes_home=str(home), main_name="general")

    assert res["ok"] is True and res["detail"].startswith(
        "no orchestrator profiles found")


def test_a_profile_scoped_hermes_home_sees_its_siblings(tmp_path):
    home = _home(tmp_path, ["general", "demo-orch"])
    _plugin(home, "general")
    _plugin(home, "demo-orch")

    res = op.doctor_check(hermes_home=str(home / "profiles" / "demo-orch"),
                          main_name="general")

    assert res["ok"] is True
    assert "2 orchestrator profile(s)" in res["detail"]


def test_an_unresolvable_identity_is_a_warn_not_a_green_silence(
        tmp_path, monkeypatch):
    home = _home(tmp_path, ["demo-orch"])
    _plugin(home, "demo-orch")
    monkeypatch.setattr(op, "_main_profile", lambda hscc_dir=None: None)

    res = op.doctor_check(hermes_home=str(home))

    assert res["ok"] is False and res["fatal"] is False
    assert "cannot resolve" in res["detail"]


def test_the_fence_refuses_an_ambient_read_inside_pytest(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    res = op.doctor_check()
    assert res["ok"] is False and res["fatal"] is False
    assert "_orchpad_check" in res["detail"]
    assert doctor_rows.under_pytest() is True


def test_the_row_is_registered_with_both_contexts_and_cannot_drift():
    by_key = {spec.key: spec for spec in doctor.FEATURE_CHECK_ROWS}
    spec = by_key["orchpad"]
    assert spec.seam_kwarg == "_orchpad_check"
    assert spec.row_fn == "", "the generic builder; no run_doctor() edit"
    assert spec.row_kwargs == ("hermes_home", "hscc_dir")
    assert spec.loader == "_load_doctor_orchpad"
    assert spec.refusal == "_refused_orchpad_check"
    assert getattr(doctor, spec.fallback_name) == CHECK


def test_the_registry_refusal_names_the_feature_seam(tmp_path, monkeypatch):
    home = _home(tmp_path, ["general", "demo-orch"])
    _plugin(home, "general")
    _plugin(home, "demo-orch")
    monkeypatch.setattr(doctor, "_sparkrun_ok",
                        lambda: doctor.Check("sparkrun", True, detail="stubbed"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HSCC_DIR", str(tmp_path / ".hscc"))

    res = doctor.run_doctor(
        str(home), fleet_checks=False, hscc_dir=str(tmp_path / ".hscc"))
    row = {c["name"]: c for c in res["checks"]}[CHECK]

    assert row["ok"] is False
    assert "_orchpad_check" in row["detail"]


def test_the_fix_names_no_live_path(tmp_path):
    home = _home(tmp_path, ["general"])
    res = op.doctor_check(hermes_home=str(home), main_name="general")
    assert str(tmp_path) not in res["fix"]
    assert os.path.expanduser("~") not in res["fix"]
