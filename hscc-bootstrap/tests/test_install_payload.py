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

    assert "hscc-cluster.bak-B" in res["backed_up"]
    assert (plugins / "hscc-cluster.bak-B" / "__init__.py").read_text() == "STALE\n"
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
    assert "hscc-cluster.bak-2" in res["backed_up"]
    assert (plugins / "hscc-cluster" / "__init__.py").read_text() == "x=1\n"
    assert not (plugins / "hscc-cluster" / "hscc-cluster").exists()
