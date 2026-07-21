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
