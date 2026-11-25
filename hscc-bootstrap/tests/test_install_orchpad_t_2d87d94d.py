"""Installer for orchpad on orchestrator profiles (card t_2d87d94d).

The installer has two jobs and one boundary:

* install the pinned package into the Hermes venv (once, not once per run);
* run orchpad's ``orchpad-hermes`` plugin installer on the main orchestrator
  and every ``<project>-orch`` home, and nowhere else.

Everything here runs against a tmp HERMES_HOME with injected runners. No test
imports orchpad itself (it may not be installed in the test interpreter) and no
test reaches the network, the operator's ``~/.hermes`` or the fleet.
"""

import json
import subprocess

import install_orchpad as io


def _cp(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)


def _profile(home, name):
    path = home / "profiles" / name
    path.mkdir(parents=True)
    return path


def _plugin(home, name):
    target = home / "profiles" / name / "plugins" / "orchpad"
    target.mkdir(parents=True)
    for filename in io.PLUGIN_FILES:
        (target / filename).write_text("payload\n", encoding="utf-8")
    return target


class FakeVenv:
    """A venv whose orchpad version changes only when pip "installs" it."""

    def __init__(self, version=None):
        self.version = version
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if "-I" in argv:
            if self.version:
                return _cp(argv, 0, self.version + "\n")
            return _cp(argv, 1, "", "No module named orchpad")
        if "pip" in argv:
            self.version = io.PIN_VERSION
            return _cp(argv)
        raise AssertionError(f"unexpected command: {argv!r}")


def _plugin_runner(calls):
    """Stand-in for ``orchpad-hermes``: writes only missing files, like it."""

    def run(argv):
        calls.append(list(argv))
        home = argv[argv.index("--hermes-home") + 1]
        name = argv[argv.index("--profile") + 1]
        from pathlib import Path
        target = Path(home) / "profiles" / name / "plugins" / "orchpad"
        target.mkdir(parents=True, exist_ok=True)
        written = []
        for filename in io.PLUGIN_FILES:
            dest = target / filename
            if not dest.exists():
                dest.write_text("payload\n", encoding="utf-8")
                written.append(filename)
        return _cp(argv, 0, json.dumps({"written": written}))

    return run


def test_the_pin_is_a_tag_and_comes_from_orchpads_own_repo():
    assert io.PIN.startswith("orchpad @ git+https://github.com/pom11/orchpad@")
    assert io.PIN.endswith("@v" + io.PIN_VERSION)
    assert "/main" not in io.PIN and "/master" not in io.PIN


def test_only_the_main_and_project_orchestrators_get_the_plugin(tmp_path):
    home = tmp_path / ".hermes"
    for name in ("general", "demo-orch", "web-orch", "coder", "reviewer"):
        _profile(home, name)

    venv = FakeVenv()
    calls = []
    report = io.ensure(home, "/venv/bin/python", apply=True, main="general",
                       run=venv, installer_run=_plugin_runner(calls))

    assert report["ok"] is True
    assert [entry["profile"] for entry in report["profiles"]] == [
        "general", "demo-orch", "web-orch"]
    installed = {argv[argv.index("--profile") + 1] for argv in calls}
    assert installed == {"general", "demo-orch", "web-orch"}
    for worker in ("coder", "reviewer"):
        assert not (home / "profiles" / worker / "plugins").exists()


def test_a_second_apply_changes_nothing_and_hits_the_network_once(tmp_path):
    home = tmp_path / ".hermes"
    _profile(home, "general")
    _profile(home, "demo-orch")

    venv = FakeVenv()
    calls = []
    installer = _plugin_runner(calls)

    first = io.ensure(home, "/venv/bin/python", apply=True, main="general",
                      run=venv, installer_run=installer)
    assert first["action"] == "installed"
    assert first["package"]["status"] == "installed"
    assert [e["status"] for e in first["profiles"]] == ["installed", "installed"]
    pip_calls = [c for c in venv.calls if "pip" in c]
    assert len(pip_calls) == 1

    second = io.ensure(home, "/venv/bin/python", apply=True, main="general",
                       run=venv, installer_run=installer)
    assert second["action"] == "verified"
    assert second["package"]["status"] == "present"
    assert [e["status"] for e in second["profiles"]] == ["verified", "verified"]
    assert all(e["written"] == [] for e in second["profiles"])
    assert len([c for c in venv.calls if "pip" in c]) == 1


def test_report_mode_installs_nothing_and_names_the_missing_profiles(tmp_path):
    home = tmp_path / ".hermes"
    _profile(home, "general")
    _profile(home, "demo-orch")
    _plugin(home, "general")

    venv = FakeVenv(version=io.PIN_VERSION)
    calls = []
    report = io.ensure(home, "/venv/bin/python", apply=False, main="general",
                       run=venv, installer_run=_plugin_runner(calls))

    assert report["ok"] is True and report["action"] == "reported"
    assert calls == []
    assert {e["profile"]: e["status"] for e in report["profiles"]} == {
        "general": "present", "demo-orch": "missing"}


def test_report_mode_still_sees_a_plugin_without_the_package(tmp_path):
    home = tmp_path / ".hermes"
    _profile(home, "general")
    _plugin(home, "general")

    report = io.ensure(home, "/venv/bin/python", apply=False, main="general",
                       run=FakeVenv(version=None),
                       installer_run=lambda argv: _cp(argv))

    assert report["package"]["status"] == "missing"
    assert report["profiles"][0]["status"] == "present"


def test_a_failed_package_install_is_reported_not_hidden(tmp_path):
    home = tmp_path / ".hermes"
    _profile(home, "general")

    class Broken(FakeVenv):
        def __call__(self, argv):
            self.calls.append(list(argv))
            if "-I" in argv:
                return _cp(argv, 1, "", "No module named orchpad")
            return _cp(argv, 1, "", "network unreachable")

    report = io.ensure(home, "/venv/bin/python", apply=True, main="general",
                       run=Broken(), installer_run=lambda argv: _cp(argv))

    assert report["ok"] is False and report["action"] == "failed"
    assert report["package"]["status"] == "failed"
    assert "network unreachable" in report["package"]["error"]
    assert report["profiles"][0]["status"] == "unknown"


def test_a_failed_plugin_install_names_the_profile(tmp_path):
    home = tmp_path / ".hermes"
    _profile(home, "general")
    _profile(home, "demo-orch")

    def install(argv):
        if argv[argv.index("--profile") + 1] == "demo-orch":
            return _cp(argv, 1, "", "disk full")
        return _cp(argv, 0, json.dumps({"written": []}))

    report = io.ensure(home, "/venv/bin/python", apply=True, main="general",
                       run=FakeVenv(version=io.PIN_VERSION),
                       installer_run=install)

    assert report["ok"] is False and report["action"] == "failed"
    by_name = {e["profile"]: e for e in report["profiles"]}
    assert by_name["demo-orch"]["status"] == "failed"
    assert "disk full" in by_name["demo-orch"]["error"]
    assert by_name["general"]["status"] == "verified"


def test_the_installer_prefers_the_venv_console_script(tmp_path):
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    python = venv_bin / "python"
    python.write_text("")
    script = venv_bin / "orchpad-hermes"
    script.write_text("")

    cmd = io.installer_cmd(python, tmp_path / "home", "demo-orch")
    assert cmd[0] == str(script)
    assert cmd[-1] == "--json"
    assert cmd[cmd.index("--profile") + 1] == "demo-orch"

    script.unlink()
    fallback = io.installer_cmd(python, tmp_path / "home", "demo-orch")
    assert fallback[:3] == [str(python), "-m", "orchpad.hermes_plugin.cli"]
