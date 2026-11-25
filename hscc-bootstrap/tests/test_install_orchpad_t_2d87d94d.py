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

import yaml

import install_orchpad as io


def _cp(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)


def _profile(home, name):
    path = home / "profiles" / name
    path.mkdir(parents=True)
    return path


def _config(home, name, cfg):
    path = home / "profiles" / name / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return path


def _enabled(home, name):
    doc = yaml.safe_load(
        (home / "profiles" / name / "config.yaml").read_text(encoding="utf-8"))
    return (doc.get("plugins") or {}).get("enabled")


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


def test_apply_enables_orchpad_in_each_orchestrator_profiles_own_config(tmp_path):
    home = tmp_path / ".hermes"
    for name in ("general", "demo-orch", "coder", "reviewer"):
        _profile(home, name)
    _config(home, "general", {
        "model": {"default": "orchestrator-model"},
        "plugins": {"enabled": ["hscc-cluster", "video_gen/openai_compat"],
                    "entries": {"video_gen/openai_compat": {"llm": {}}}},
    })
    _config(home, "demo-orch", {"toolsets": ["hermes-cli"]})  # no plugins key
    _config(home, "coder", {"plugins": {"enabled": ["hscc-cluster"]}})
    _config(home, "reviewer", {"plugins": {"enabled": []}})
    root = home / "config.yaml"
    root.write_text(yaml.safe_dump(
        {"plugins": {"enabled": ["hscc-cluster", "hscc-commands",
                                  "sparkrun-hermes"]}}, sort_keys=False),
        encoding="utf-8")
    root_before = root.read_text(encoding="utf-8")
    worker_before = (home / "profiles" / "coder" / "config.yaml").read_text(
        encoding="utf-8")

    report = io.ensure(home, "/venv/bin/python", apply=True, main="general",
                       run=FakeVenv(), installer_run=_plugin_runner([]))

    assert report["ok"] is True
    assert _enabled(home, "general") == [
        "hscc-cluster", "video_gen/openai_compat", "orchpad"]
    assert _enabled(home, "demo-orch") == ["orchpad"]
    assert _enabled(home, "coder") == ["hscc-cluster"]
    assert _enabled(home, "reviewer") == []
    general = yaml.safe_load(
        (home / "profiles" / "general" / "config.yaml").read_text(
            encoding="utf-8"))
    assert general["plugins"]["entries"] == {
        "video_gen/openai_compat": {"llm": {}}}
    assert root.read_text(encoding="utf-8") == root_before
    assert (home / "profiles" / "coder" / "config.yaml").read_text(
        encoding="utf-8") == worker_before
    by_name = {e["profile"]: e for e in report["profiles"]}
    assert by_name["general"]["enabled"] is True
    assert by_name["general"]["enabled_added"] == ["orchpad"]
    assert by_name["demo-orch"]["enabled_added"] == ["orchpad"]


def test_enabling_is_idempotent_and_keeps_entry_order(tmp_path):
    home = tmp_path / ".hermes"
    _profile(home, "general")
    _profile(home, "demo-orch")
    _config(home, "general", {"plugins": {"enabled": ["b-third", "a-first"]}})
    _config(home, "demo-orch", {"plugins": {"enabled": []}})

    venv = FakeVenv()
    installer = _plugin_runner([])
    io.ensure(home, "/venv/bin/python", apply=True, main="general",
              run=venv, installer_run=installer)

    general = home / "profiles" / "general" / "config.yaml"
    orch = home / "profiles" / "demo-orch" / "config.yaml"
    assert _enabled(home, "general") == ["b-third", "a-first", "orchpad"]
    before = {p: (p.read_text(encoding="utf-8"), p.stat().st_mtime_ns)
              for p in (general, orch)}
    backups = {p: len(list(p.parent.glob("config.yaml.bak-*")))
               for p in (general, orch)}

    second = io.ensure(home, "/venv/bin/python", apply=True, main="general",
                       run=venv, installer_run=installer)

    assert second["action"] == "verified"
    for path, (text, mtime) in before.items():
        assert path.read_text(encoding="utf-8") == text
        assert path.stat().st_mtime_ns == mtime
        assert len(list(path.parent.glob("config.yaml.bak-*"))) == backups[path]
    assert all(e["enabled"] is True for e in second["profiles"])
    assert all("enabled_added" not in e for e in second["profiles"])


def test_report_mode_shows_files_that_are_not_enabled_and_writes_nothing(
        tmp_path):
    home = tmp_path / ".hermes"
    _profile(home, "general")
    _plugin(home, "general")
    config = _config(home, "general",
                     {"plugins": {"enabled": ["hscc-cluster"]}})
    before = config.read_text(encoding="utf-8")

    report = io.ensure(home, "/venv/bin/python", apply=False, main="general",
                       run=FakeVenv(version=io.PIN_VERSION),
                       installer_run=_plugin_runner([]))

    assert report["profiles"][0]["status"] == "present"
    assert report["profiles"][0]["enabled"] is False
    assert config.read_text(encoding="utf-8") == before


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
    # general had no config.yaml in this fixture: the shared enable step seeds
    # the new-home tier + orchpad, so this run counts as a change, not "verified".
    assert by_name["general"]["status"] == "installed"


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


# ── the single-profile repair path every provisioning door shares ───────────

def test_ensure_profile_installs_and_enables_one_orchestrator(tmp_path):
    home = tmp_path / ".hermes"
    _profile(home, "demo-orch")

    venv = FakeVenv()
    calls = []
    status = io.ensure_profile(home, "demo-orch", "/venv/bin/python",
                               apply=True, main="general", run=venv,
                               installer_run=_plugin_runner(calls))

    assert status == "enabled"
    assert io.plugin_present(home / "profiles" / "demo-orch")
    assert "orchpad" in (_enabled(home, "demo-orch") or [])
    assert calls[0][calls[0].index("--profile") + 1] == "demo-orch"
    assert len([c for c in venv.calls if "pip" in c]) == 1


def test_ensure_profile_is_idempotent(tmp_path):
    home = tmp_path / ".hermes"
    _profile(home, "demo-orch")
    venv, calls = FakeVenv(), []
    installer = _plugin_runner(calls)

    first = io.ensure_profile(home, "demo-orch", "/venv/bin/python",
                              apply=True, main="general", run=venv,
                              installer_run=installer)
    config = home / "profiles" / "demo-orch" / "config.yaml"
    before = (config.read_text(encoding="utf-8"), config.stat().st_mtime_ns)

    second = io.ensure_profile(home, "demo-orch", "/venv/bin/python",
                               apply=True, main="general", run=venv,
                               installer_run=installer)

    assert (first, second) == ("enabled", "already")
    assert (config.read_text(encoding="utf-8"),
            config.stat().st_mtime_ns) == before
    assert len([c for c in venv.calls if "pip" in c]) == 1


def test_ensure_profile_refuses_worker_and_reviewer_homes(tmp_path):
    home = tmp_path / ".hermes"
    for name in ("coder", "reviewer"):
        _profile(home, name)

    for name in ("coder", "reviewer"):
        status = io.ensure_profile(home, name, "/venv/bin/python",
                                   apply=True, main="general",
                                   run=FakeVenv(),
                                   installer_run=_plugin_runner([]))
        assert status.startswith("failed:"), status
        assert "not an orchestrator profile" in status
        assert not (home / "profiles" / name / "plugins").exists()
        assert not (home / "profiles" / name / "config.yaml").exists()


def test_ensure_profile_failure_is_a_token_never_an_exception(tmp_path):
    home = tmp_path / ".hermes"
    _profile(home, "demo-orch")

    def install(argv):
        return _cp(argv, 1, "", "disk full")

    status = io.ensure_profile(home, "demo-orch", "/venv/bin/python",
                               apply=True, main="general",
                               run=FakeVenv(version=io.PIN_VERSION),
                               installer_run=install)

    assert status.startswith("failed:")
    assert "disk full" in status


def test_ensure_profile_without_a_hermes_venv_names_the_fix(tmp_path,
                                                           monkeypatch):
    home = tmp_path / ".hermes"
    _profile(home, "demo-orch")
    monkeypatch.delenv("HSCC_HERMES_PYTHON", raising=False)

    status = io.ensure_profile(home, "demo-orch", apply=True, main="general")

    assert status.startswith("failed:")
    assert "no Hermes venv python" in status
    assert "HSCC_HERMES_PYTHON" in status


def test_ensure_profile_does_not_claim_enabled_without_the_config_entry(
        tmp_path):
    home = tmp_path / ".hermes"
    _profile(home, "demo-orch")
    (home / "profiles" / "demo-orch" / "config.yaml").write_text(
        "plugins: [unterminated\n", encoding="utf-8")

    status = io.ensure_profile(home, "demo-orch", "/venv/bin/python",
                               apply=True, main="general",
                               run=FakeVenv(),
                               installer_run=_plugin_runner([]))

    assert status.startswith("failed:")
    assert "does not name it" in status


def test_the_main_name_comes_from_the_one_resolver(tmp_path, monkeypatch):
    hscc = tmp_path / "hscc"
    hscc.mkdir()
    (hscc / "main-orchestrator.json").write_text(
        json.dumps({"name": "ops"}), encoding="utf-8")
    monkeypatch.delenv("MAIN_ORCH_NAME", raising=False)

    assert io.main_name(hscc_dir=str(hscc)) == "ops"
