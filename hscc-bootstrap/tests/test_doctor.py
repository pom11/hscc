import doctor


class TestIndividualChecks:
    def test_python_ok(self):
        c = doctor._python_ok()
        assert c.name == "python" and c.ok is True

    def test_pyyaml_ok(self):
        # pyyaml is installed in the venv running the tests
        assert doctor._pyyaml_ok().ok is True

    def test_sparkrun_cluster_uses_runner(self):
        good = doctor._sparkrun_cluster_ok(_runner=lambda: '[{"name":"x"}]')
        assert good.ok is True
        bad = doctor._sparkrun_cluster_ok(_runner=lambda: "")
        assert bad.ok is False and bad.fatal and "add" in bad.fix

    def test_hermes_missing(self, tmp_path):
        c = doctor._hermes_ok(str(tmp_path))   # no hermes-agent dir
        assert c.ok is False and c.fatal

    def test_hermes_present(self, tmp_path):
        (tmp_path / "hermes-agent").mkdir()
        assert doctor._hermes_ok(str(tmp_path)).ok is True

    def test_disk_is_nonfatal(self):
        c = doctor._disk_ok("/", min_gb=0)   # always plenty over 0
        assert c.ok is True and c.fatal is False

    def test_gateway_is_nonfatal(self):
        assert doctor._gateway_running().fatal is False

    def test_nas_nonfatal_and_optional(self):
        none_nas = doctor._nas_ok(_runner=lambda: None)
        assert none_nas.ok is True and none_nas.fatal is False
        with_nas = doctor._nas_ok(_runner=lambda: "/mnt/nas")
        assert with_nas.ok is True and "/mnt/nas" in with_nas.detail


class TestRunDoctor:
    def test_all_good(self, tmp_path):
        (tmp_path / "hermes-agent").mkdir()
        res = doctor.run_doctor(str(tmp_path),
                                _cluster_runner=lambda: '[{"name":"x"}]')
        # python+pyyaml+sparkrun(maybe)+cluster+hermes; sparkrun may be absent in
        # CI, so assert no FATAL failure comes from cluster/hermes specifically.
        names_failed = res["fatal_failures"]
        assert "sparkrun cluster" not in names_failed
        assert "hermes" not in names_failed

    def test_fatal_when_cluster_missing(self, tmp_path):
        (tmp_path / "hermes-agent").mkdir()
        res = doctor.run_doctor(str(tmp_path), _cluster_runner=lambda: "")
        assert res["ok"] is False
        assert "sparkrun cluster" in res["fatal_failures"]

    def test_fatal_when_hermes_missing(self, tmp_path):
        res = doctor.run_doctor(str(tmp_path),
                                _cluster_runner=lambda: '[{"name":"x"}]')
        assert res["ok"] is False
        assert "hermes" in res["fatal_failures"]

    def test_structure(self, tmp_path):
        (tmp_path / "hermes-agent").mkdir()
        res = doctor.run_doctor(str(tmp_path), _cluster_runner=lambda: '[{"x":1}]')
        assert "ok" in res and "checks" in res and "fatal_failures" in res
        for c in res["checks"]:
            assert "name" in c and "ok" in c and "fatal" in c
