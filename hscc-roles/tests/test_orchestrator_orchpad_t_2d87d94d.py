"""`ensure_orchestrator` gives every orchestrator profile its pad (t_2d87d94d).

This is the `hscc roles` / API / migrate path: unlike bootstrap it runs after a
project exists, so it is the door a profile created outside bootstrap comes
through. Every case uses a tmp profile root and stub pip/plugin runners; no
test installs a package, reaches the network, or reads the live profile tree.
"""

import json
import os
import subprocess

import pytest
import yaml

import generator
import orchestrators

io = orchestrators._load_install_orchpad()


@pytest.fixture
def registry_file(tmp_path):
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump({"projects": [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
    ]}), encoding="utf-8")
    return str(path)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    (home / "profiles").mkdir(parents=True)
    (home / "hermes-agent" / "venv" / "bin").mkdir(parents=True)
    (home / "hermes-agent" / "venv" / "bin" / "python3").write_text("")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR",
                        str(home / "profiles"))
    monkeypatch.delenv("HSCC_HERMES_PYTHON", raising=False)
    return home


class FakeVenv:
    def __init__(self):
        self.version = None
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if "-I" in argv:
            out = self.version + "\n" if self.version else ""
            rc = 0 if self.version else 1
            return subprocess.CompletedProcess(argv, rc, stdout=out,
                                               stderr="" if rc == 0 else "absent")
        if "pip" in argv:
            self.version = io.PIN_VERSION
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command: {argv!r}")


def plugin_runner(calls):
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
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"written": written}), "")
    return run


def _configure(home, monkeypatch, installer=None):
    venv = FakeVenv()
    calls = []
    monkeypatch.setattr(orchestrators, "_ORCHPAD_RUN", venv)
    monkeypatch.setattr(orchestrators, "_ORCHPAD_INSTALLER_RUN",
                        installer or plugin_runner(calls))
    return venv, calls


def _enabled(profile_home):
    doc = yaml.safe_load(
        (profile_home / "config.yaml").read_text(encoding="utf-8"))
    return (doc.get("plugins") or {}).get("enabled") or []


def test_main_orchestrator_gets_orchpad(registry_file, isolated_home,
                                         monkeypatch):
    venv, calls = _configure(isolated_home, monkeypatch)

    got = orchestrators.ensure_orchestrator(
        None, base_identity="BASE", path=registry_file)

    assert got["profile"] == "general"
    assert got["orchpad"] == "enabled"
    profile_home = isolated_home / "profiles" / "general"
    assert io.plugin_present(profile_home)
    assert "orchpad" in _enabled(profile_home)
    assert calls[0][calls[0].index("--profile") + 1] == "general"
    assert len([c for c in venv.calls if "pip" in c]) == 1


def test_project_orchestrator_gets_orchpad(registry_file, isolated_home,
                                            monkeypatch):
    _, calls = _configure(isolated_home, monkeypatch)

    got = orchestrators.ensure_orchestrator(
        "hscc", base_identity="BASE", path=registry_file)

    assert got["profile"] == "hscc-orch"
    assert got["orchpad"] == "enabled"
    profile_home = isolated_home / "profiles" / "hscc-orch"
    assert io.plugin_present(profile_home)
    assert "orchpad" in _enabled(profile_home)
    assert calls[0][calls[0].index("--profile") + 1] == "hscc-orch"


def test_worker_and_reviewer_profiles_are_never_touched(registry_file,
                                                         isolated_home,
                                                         monkeypatch):
    _, calls = _configure(isolated_home, monkeypatch)
    workers = {}
    for name in ("coder", "reviewer"):
        home = isolated_home / "profiles" / name
        home.mkdir()
        config = home / "config.yaml"
        config.write_text(yaml.safe_dump(
            {"plugins": {"enabled": ["hscc-cluster"]}}, sort_keys=False),
            encoding="utf-8")
        workers[name] = config.read_text(encoding="utf-8")

    orchestrators.ensure_orchestrator("hscc", base_identity="BASE",
                                      path=registry_file)

    installed = {c[c.index("--profile") + 1] for c in calls}
    assert installed == {"hscc-orch"}
    for name, before in workers.items():
        home = isolated_home / "profiles" / name
        assert (home / "config.yaml").read_text(encoding="utf-8") == before
        assert not (home / "plugins").exists()
    assert io.ensure_profile(
        isolated_home, "coder", str(isolated_home / "hermes-agent" / "venv"
                                    / "bin" / "python3"),
        main="general", run=FakeVenv(),
        installer_run=plugin_runner([])).startswith("failed:")


def test_a_re_run_reports_already_and_changes_nothing(registry_file,
                                                       isolated_home,
                                                       monkeypatch):
    venv, calls = _configure(isolated_home, monkeypatch)

    first = orchestrators.ensure_orchestrator(
        "hscc", base_identity="BASE", path=registry_file)
    config = isolated_home / "profiles" / "hscc-orch" / "config.yaml"
    before = (config.read_text(encoding="utf-8"), config.stat().st_mtime_ns)

    second = orchestrators.ensure_orchestrator(
        "hscc", base_identity="BASE", path=registry_file)

    assert (first["orchpad"], second["orchpad"]) == ("enabled", "already")
    assert (config.read_text(encoding="utf-8"),
            config.stat().st_mtime_ns) == before
    assert len([c for c in venv.calls if "pip" in c]) == 1
    second_call = calls[-1]
    assert second_call[second_call.index("--profile") + 1] == "hscc-orch"


def test_a_failed_orchpad_install_is_reported_not_raised(registry_file,
                                                          isolated_home,
                                                          monkeypatch):
    def failing(argv):
        return subprocess.CompletedProcess(argv, 1, "", "disk full")

    _configure(isolated_home, monkeypatch, installer=failing)

    got = orchestrators.ensure_orchestrator(
        "hscc", base_identity="BASE", path=registry_file)

    assert got["orchpad"].startswith("failed:")
    assert "disk full" in got["orchpad"]
    assert (isolated_home / "profiles" / "hscc-orch" / "SOUL.md").is_file()
