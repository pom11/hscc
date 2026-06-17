import os
import stat

import install_scripts


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "hscc_proxy_watchdog.sh").write_text("#!/bin/bash\necho proxy\n")
    (scripts / "hscc_worker_health.sh").write_text("#!/bin/bash\necho workers\n")
    (scripts / "README.md").write_text("# scripts\n")
    (scripts / "not_hscc.sh").write_text("#!/bin/bash\necho other\n")
    return repo


def test_skip_when_no_scripts_dir(tmp_path):
    repo = tmp_path / "repo-no-scripts"
    repo.mkdir()
    res = install_scripts.install_scripts(repo, tmp_path / "runtime")
    assert res["skipped"] is True
    assert res["installed"] == []


def test_fresh_install_copies_hscc_scripts_only(tmp_path):
    repo = _make_repo(tmp_path)
    runtime = tmp_path / "runtime"
    res = install_scripts.install_scripts(repo, runtime)

    assert res["skipped"] is False
    assert set(res["installed"]) == {
        "hscc_proxy_watchdog.sh",
        "hscc_worker_health.sh",
    }
    # README and non-hscc shell files are NOT shipped
    assert not (runtime / "README.md").exists()
    assert not (runtime / "not_hscc.sh").exists()
    # installed scripts are executable
    mode = (runtime / "hscc_proxy_watchdog.sh").stat().st_mode
    assert mode & stat.S_IXUSR


def test_reinstall_backs_up_then_overwrites(tmp_path):
    repo = _make_repo(tmp_path)
    runtime = tmp_path / "runtime"
    install_scripts.install_scripts(repo, runtime, ts="A")

    target = runtime / "hscc_proxy_watchdog.sh"
    target.write_text("STALE\n")
    res = install_scripts.install_scripts(repo, runtime, ts="B")

    assert "hscc_proxy_watchdog.sh.bak-B" in res["backed_up"]
    assert (runtime / "hscc_proxy_watchdog.sh.bak-B").read_text() == "STALE\n"
    assert target.read_text().startswith("#!/bin/bash")


def test_user_scripts_preserved(tmp_path):
    repo = _make_repo(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "user_custom.sh").write_text("custom\n")

    install_scripts.install_scripts(repo, runtime)

    # user script untouched, hscc scripts installed alongside it
    assert (runtime / "user_custom.sh").read_text() == "custom\n"
    assert (runtime / "hscc_proxy_watchdog.sh").is_file()


def test_no_backup_skips_bak_files(tmp_path):
    repo = _make_repo(tmp_path)
    runtime = tmp_path / "runtime"
    install_scripts.install_scripts(repo, runtime, ts="A")
    (runtime / "hscc_proxy_watchdog.sh").write_text("STALE\n")

    res = install_scripts.install_scripts(repo, runtime, backup=False, ts="B")

    assert res["backed_up"] == []
    assert not (runtime / "hscc_proxy_watchdog.sh.bak-B").exists()
    assert (runtime / "hscc_proxy_watchdog.sh").read_text().startswith("#!/bin/bash")
