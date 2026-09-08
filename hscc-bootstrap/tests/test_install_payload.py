import install_payload


def _make_repo(tmp_path):
    """A fake repo with a plugin dir (incl. caches/tests) and a root file."""
    repo = tmp_path / "repo"
    plug = repo / "hscc-cluster"
    (plug / "__pycache__").mkdir(parents=True)
    (plug / "tests").mkdir()
    (plug / "__init__.py").write_text("x=1\n")
    (plug / "__pycache__" / "junk.pyc").write_text("bytecode")
    (plug / "tests" / "test_x.py").write_text("def test(): pass\n")
    (repo / "README.md").write_text("# hscc\n")
    return repo


def test_fresh_install_copies_and_excludes(tmp_path):
    repo = _make_repo(tmp_path)
    plugins = tmp_path / "plugins"
    res = install_payload.install_payload(
        repo, plugins, ["hscc-cluster", "README.md"])

    assert res["skipped"] is False
    assert set(res["installed"]) == {"hscc-cluster", "README.md"}
    assert (plugins / "hscc-cluster" / "__init__.py").is_file()
    assert (plugins / "README.md").is_file()
    # caches/tests/pyc must NOT ship to runtime
    assert not (plugins / "hscc-cluster" / "__pycache__").exists()
    assert not (plugins / "hscc-cluster" / "tests").exists()


def test_reinstall_backs_up_then_overwrites(tmp_path):
    repo = _make_repo(tmp_path)
    plugins = tmp_path / "plugins"
    install_payload.install_payload(repo, plugins, ["hscc-cluster"], ts="A")
    # mutate runtime copy, then reinstall — old version should be backed up
    (plugins / "hscc-cluster" / "__init__.py").write_text("STALE\n")
    res = install_payload.install_payload(repo, plugins, ["hscc-cluster"], ts="B")

    backups = tmp_path / "plugins-backups"
    assert "hscc-cluster.bak-B" in res["backed_up"]
    assert (backups / "hscc-cluster.bak-B" / "__init__.py").read_text() == "STALE\n"
    assert (plugins / "hscc-cluster" / "__init__.py").read_text() == "x=1\n"
    # No .bak-* entries remain inside plugins_dir
    assert not list(plugins.glob("hscc-cluster.bak-*"))


def test_no_backup_overwrites_in_place(tmp_path):
    repo = _make_repo(tmp_path)
    plugins = tmp_path / "plugins"
    install_payload.install_payload(repo, plugins, ["hscc-cluster"], ts="A")
    (plugins / "hscc-cluster" / "__init__.py").write_text("STALE\n")
    res = install_payload.install_payload(
        repo, plugins, ["hscc-cluster"], backup=False, ts="B")

    assert res["backed_up"] == []
    assert not list(plugins.glob("hscc-cluster.bak-*"))  # nothing kept
    assert (plugins / "hscc-cluster" / "__init__.py").read_text() == "x=1\n"


def test_self_install_guard_skips(tmp_path):
    repo = _make_repo(tmp_path)
    # repo IS the plugins dir → nothing to copy
    res = install_payload.install_payload(repo, repo, ["hscc-cluster"])
    assert res["skipped"] is True
    assert "in-place" in res["reason"]


def test_missing_payload_reported_not_fatal(tmp_path):
    repo = _make_repo(tmp_path)
    plugins = tmp_path / "plugins"
    res = install_payload.install_payload(
        repo, plugins, ["hscc-cluster", "does-not-exist"])
    assert res["missing"] == ["does-not-exist"]
    assert "hscc-cluster" in res["installed"]


def test_idempotent_second_run_backs_up_again(tmp_path):
    repo = _make_repo(tmp_path)
    plugins = tmp_path / "plugins"
    install_payload.install_payload(repo, plugins, ["hscc-cluster"], ts="1")
    res = install_payload.install_payload(repo, plugins, ["hscc-cluster"], ts="2")
    # second run finds the existing live copy and backs it up — not nested
    backups = tmp_path / "plugins-backups"
    assert "hscc-cluster.bak-2" in res["backed_up"]
    assert (backups / "hscc-cluster.bak-2").exists()
    assert (plugins / "hscc-cluster" / "__init__.py").read_text() == "x=1\n"
    assert not (plugins / "hscc-cluster" / "hscc-cluster").exists()
    # No .bak-* entries remain inside plugins_dir
    assert not list(plugins.glob("hscc-cluster.bak-*"))


def test_backup_goes_to_plugins_backups_dir(tmp_path):
    """(a) plugins_dir has no .bak-* after install, (b) backup in sibling dir."""
    repo = _make_repo(tmp_path)
    plugins = tmp_path / "plugins"
    install_payload.install_payload(repo, plugins, ["hscc-cluster"], ts="A")
    (plugins / "hscc-cluster" / "__init__.py").write_text("MUTATED\n")
    res = install_payload.install_payload(repo, plugins, ["hscc-cluster"], ts="B")

    backups = tmp_path / "plugins-backups"
    # (a) no .bak-* entries inside plugins_dir
    assert not list(plugins.glob("*.bak-*"))
    # (b) backup exists under plugins-backups dir
    assert (backups / "hscc-cluster.bak-B").is_dir()
    assert (backups / "hscc-cluster.bak-B" / "__init__.py").read_text() == "MUTATED\n"
    # (c) fresh content in plugins_dir
    assert (plugins / "hscc-cluster" / "__init__.py").read_text() == "x=1\n"


def test_no_backup_leaves_no_backup_anywhere(tmp_path):
    """(d) --no-backup leaves no backup dir and no backup inside plugins_dir."""
    repo = _make_repo(tmp_path)
    plugins = tmp_path / "plugins"
    install_payload.install_payload(repo, plugins, ["hscc-cluster"], ts="A")
    (plugins / "hscc-cluster" / "__init__.py").write_text("STALE\n")
    res = install_payload.install_payload(
        repo, plugins, ["hscc-cluster"], backup=False, ts="B")

    assert res["backed_up"] == []
    assert not list(plugins.glob("*.bak-*"))
    backups = tmp_path / "plugins-backups"
    assert not backups.exists()  # no backup dir created
    assert (plugins / "hscc-cluster" / "__init__.py").read_text() == "x=1\n"


def test_sweep_relocates_planted_bak_out_of_plugins_dir(tmp_path):
    """(e) pre-existing .bak-* entries in plugins_dir are swept to plugins-backups."""
    repo = _make_repo(tmp_path)
    plugins = tmp_path / "plugins"
    install_payload.install_payload(repo, plugins, ["hscc-cluster"], ts="A")

    # Plant a fake old backup inside plugins_dir
    fake_bak = plugins / "foo.bak-123"
    fake_bak.mkdir()
    (fake_bak / "old.py").write_text("junk\n")

    # Reinstall — sweep runs before copy
    res = install_payload.install_payload(repo, plugins, ["hscc-cluster"], ts="C")

    backups = tmp_path / "plugins-backups"
    # Planted backup moved out of plugins_dir
    assert not (plugins / "foo.bak-123").exists()
    assert (backups / "foo.bak-123" / "old.py").read_text() == "junk\n"
    # Current install still worked
    assert "hscc-cluster" in res["installed"]


def test_default_payload_ships_version_marker():
    """The runtime version marker must be shipped so ~/.hermes/plugins/VERSION
    tracks releases instead of going stale."""
    assert "VERSION" in install_payload.DEFAULT_PAYLOAD


def test_default_payload_ships_hscc_project():
    """hscc-project (the relocated flightdeck) must deploy alongside hscc_daemon,
    which imports it as a sibling for the `hscc project ...` verb — otherwise the
    installed CLI's project commands break with 'package not found'."""
    assert "hscc-project" in install_payload.DEFAULT_PAYLOAD
    assert "hscc_daemon" in install_payload.DEFAULT_PAYLOAD


def test_default_payload_ships_hscc_api():
    """hscc-api must deploy alongside hscc_daemon — hscc_daemon/api_cli.py's
    `hscc api` verb locates the server as a SIBLING of hscc_daemon (a fresh
    checkout has hscc-api/ next to hscc_daemon/), so it remains installed and
    the verb does NOT break with 'package not found' on a deployed host."""
    assert "hscc-api" in install_payload.DEFAULT_PAYLOAD
    assert "hscc_daemon" in install_payload.DEFAULT_PAYLOAD


def test_run_tests_sh_covers_every_tested_payload_package():
    """Every deployable python package in DEFAULT_PAYLOAD that ships a tests/
    dir must appear in scripts/run_tests.sh DIRS. This is the drift guard for
    the 2026-09-08 regression where hscc-project shipped in DEFAULT_PAYLOAD but
    was silently absent from DIRS — the runner printed ALL GREEN while the only
    changed package went completely untested. DEFAULT_PAYLOAD is the single
    source of truth (same precedent as check_plugin_payload in
    hscc_daemon/verify.py:959, which loads it by file path for this reason), so
    the two lists can never drift again."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2]

    # DEFAULT_PAYLOAD is the single source of truth (same reason verify.py loads
    # it by file path rather than re-globbing `hscc-*`).
    payload = install_payload.DEFAULT_PAYLOAD

    # Parse the DIRS=() array out of the actual runner script.
    script = (root / "scripts" / "run_tests.sh").read_text()
    m = re.search(r"DIRS=\(([^)]*)\)", script)
    assert m, "could not find DIRS=(...) in scripts/run_tests.sh"
    dirs = set(m.group(1).split())

    for pkg in payload:
        pkg_dir = root / pkg
        # Only deployable python packages that actually ship tests are relevant:
        # non-dirs (docs, assets, VERSION) and testless dirs have nothing to run.
        if pkg_dir.is_dir() and (pkg_dir / "tests").is_dir():
            assert pkg in dirs, (
                f"{pkg} is in install_payload.DEFAULT_PAYLOAD and has "
                f"{pkg}/tests/ but is missing from scripts/run_tests.sh DIRS — "
                f"the runner would silently skip it"
            )

