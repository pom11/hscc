import pathlib
import re

import install_payload

# ── repo-root test-dir census (shared by the two DIRS drift guards) ──────────
# Both guards below compare the tree's test dirs against the DIRS array in
# scripts/run_tests.sh, and both must use the runner's own definition of what is
# collectable or they disagree with it in both directions (false pass on an
# unregistered dir, false failure on one the runner can never run).
# Anchored on __file__, never the cwd: under run_tests.sh pytest runs with the
# repo root as cwd but the plugin dir on sys.path, and pytest's rootdir can move
# to the plugin when a plugin dir carries its own ini (t_95d864fc recorded the
# same anchoring requirement for hscc-project tests).
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Mirrors pytest.ini `norecursedirs` (read back by _norecurse_names so the two
# cannot drift silently): a dir the runner does not even descend into cannot be
# collected by it, so requiring it in DIRS would be a false failure.
_NORECURSE_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules"}
_NORECURSE_SKIP_SUFFIXES = (".egg-info",)


def _norecurse_names():
    """The `norecursedirs` patterns from pytest.ini at the repo root.

    Returns (exact_names, suffixes). A missing/blank pytest.ini fails closed to
    the hardcoded defaults instead of silently widening the census.
    """
    exact, suffixes = set(), []
    ini = _REPO_ROOT / "pytest.ini"
    if ini.is_file():
        for line in ini.read_text().splitlines():
            if not line.strip().startswith("norecursedirs"):
                continue
            rhs = line.split("=", 1)[1] if "=" in line else ""
            for pat in rhs.split():
                pat = pat.strip()
                if not pat:
                    continue
                if pat.startswith("*"):
                    suffixes.append(pat[1:])
                elif "*" not in pat and "?" not in pat:
                    exact.add(pat)
    return (exact or {".worktrees", "_archive", ".venv"}), tuple(suffixes) or (".egg-info",)


def _test_dirs_on_tree():
    """Every dir in the tree that holds `test_*.py` directly, as repo-relative
    posix paths — the census run_tests.sh would collect if it were pointed at the
    whole repo.

    Scoped to `git ls-files`: an untracked dir is not the repo's promise to test
    anything, and a scratch or generated dir must not turn the guard red.
    """
    import subprocess

    exact, suffixes = _norecurse_names()
    skip_dirs = _NORECURSE_SKIP_DIRS | exact

    out = subprocess.run(
        ["git", "ls-files", "*test_*.py"],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    dirs = set()
    for rel in out.splitlines():
        parts = rel.split("/")
        if any(p in skip_dirs or p.endswith(suffixes) for p in parts[:-1]):
            continue
        if parts[-1].startswith("test_") and len(parts) > 1:
            dirs.add("/".join(parts[:-1]))
    return dirs


def _dirs_listed_in_runner():
    """The DIRS=() array parsed out of the actual scripts/run_tests.sh."""
    script = (_REPO_ROOT / "scripts" / "run_tests.sh").read_text()
    m = re.search(r"DIRS=\(([^)]*)\)", script)
    assert m, "could not find DIRS=(...) in scripts/run_tests.sh"
    return set(m.group(1).split())


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


def test_reinstalls_are_retain_limited_in_plugins_backups(tmp_path):
    """t_9462260b: plugins-backups/ grew to 3,385 dirs because nothing ever
    pruned them. Five machine-stamped reinstalls must leave keep-N (3), and
    the survivors must be the NEWEST stamps (name-stamp order, not mtime)."""
    repo = _make_repo(tmp_path)
    plugins = tmp_path / "plugins"
    stamps = [f"2026010{i}-000000" for i in range(1, 6)]
    for ts in stamps:
        install_payload.install_payload(repo, plugins, ["hscc-cluster"], ts=ts)

    backups = tmp_path / "plugins-backups"
    left = sorted(p.name for p in backups.glob("hscc-cluster.bak-*"))
    assert left == ["hscc-cluster.bak-20260103-000000",
                    "hscc-cluster.bak-20260104-000000",
                    "hscc-cluster.bak-20260105-000000"]
    assert (plugins / "hscc-cluster" / "__init__.py").read_text() == "x=1\n"


def test_retain_limit_spares_operator_labelled_plugin_backups(tmp_path):
    """A human bookmark in plugins-backups (no machine stamp) must survive the
    retain sweep even when it outnumbers the keep window."""
    repo = _make_repo(tmp_path)
    plugins = tmp_path / "plugins"
    backups = tmp_path / "plugins-backups"
    backups.mkdir()
    bookmark = backups / "hscc-cluster.bak-pre-v2-migration"
    bookmark.mkdir()
    (bookmark / "__init__.py").write_text("historic\n")

    for ts in (f"2026010{i}-000000" for i in range(1, 6)):
        install_payload.install_payload(repo, plugins, ["hscc-cluster"], ts=ts)

    assert (bookmark / "__init__.py").read_text() == "historic\n"
    assert len(list(backups.glob("hscc-cluster.bak-2026*"))) == 3


def test_non_machine_stamps_are_never_pruned(tmp_path):
    """Existing suites pass ts='A'/'B'/'C'. Those are not machine stamps, so
    the sweep must leave them alone — otherwise this change would silently
    break every caller that uses short test stamps."""
    repo = _make_repo(tmp_path)
    plugins = tmp_path / "plugins"
    for ts in ("A", "B", "C", "D", "E", "F"):
        install_payload.install_payload(repo, plugins, ["hscc-cluster"], ts=ts)
    backups = tmp_path / "plugins-backups"
    # 6 installs -> 5 backups (the first has nothing to back up); all 5 kept.
    assert sorted(p.name for p in backups.glob("hscc-cluster.bak-*")) == [
        f"hscc-cluster.bak-{t}" for t in "BCDEF"]


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


def test_install_propagates_to_named_profile_plugin_dirs(tmp_path):
    """Regression (t_9e8732b8): memori_byodb fix deployed only to the root
    ~/.hermes/plugins never reached the profiles that actually load it
    ($HERMES_HOME/plugins per profile). install_payload_profiles must copy into
    every named profile's plugins dir too."""
    repo = _make_repo(tmp_path)
    root = tmp_path / "hermes"
    plugins = root / "plugins"
    # Two profiles with a plugins dir; one named profile with no plugins dir
    # (must be skipped, not error).
    for name in ("hscc-orch", "backend-engineer"):
        (root / "profiles" / name / "plugins").mkdir(parents=True)
    (root / "profiles" / "no-plugins").mkdir(parents=True)

    res = install_payload.install_payload_profiles(
        repo, plugins, ["hscc-cluster", "README.md"], ts="A")

    # Root home installed.
    assert res["skipped"] is False
    assert (plugins / "hscc-cluster" / "__init__.py").is_file()
    # Both profile plugin dirs got a copy.
    assert (root / "profiles" / "hscc-orch" / "plugins" / "hscc-cluster" / "__init__.py").is_file()
    assert (root / "profiles" / "backend-engineer" / "plugins" / "hscc-cluster" / "__init__.py").is_file()
    # Two profile summaries, both installed.
    assert len(res["profiles"]) == 2
    assert all(p["skipped"] is False for p in res["profiles"])
    # The profile with no plugins dir was skipped (not an error).
    assert not (root / "profiles" / "no-plugins" / "plugins").exists()


def test_named_profile_plugins_dirs_noop_when_plugins_already_profile_scoped(tmp_path):
    """When the target plugins_dir is itself a profile-scoped plugins dir
    (~/.hermes/profiles/<name>/plugins), no profile fan-out is derived — you
    cannot install into your own siblings from inside a profile."""
    repo = _make_repo(tmp_path)
    root = tmp_path / "hermes"
    profile_plugins = root / "profiles" / "hscc-orch" / "plugins"
    profile_plugins.mkdir(parents=True)

    dirs = install_payload._named_profile_plugins_dirs(profile_plugins)
    assert dirs == []


def test_run_tests_sh_covers_every_tested_payload_package():
    """Every deployable python package in DEFAULT_PAYLOAD that ships a tests/
    dir must appear in scripts/run_tests.sh DIRS. This is the drift guard for
    the 2026-09-08 regression where hscc-project shipped in DEFAULT_PAYLOAD but
    was silently absent from DIRS — the runner printed ALL GREEN while the only
    changed package went completely untested. DEFAULT_PAYLOAD is the single
    source of truth (same precedent as check_plugin_payload in
    hscc_daemon/verify.py:959, which loads it by file path for this reason), so
    the two lists can never drift again.

    This guard sees PACKAGES only. It is the narrower of two guards: the
    companion below covers every test dir in the tree, package or not, and is
    the one that would have caught `scripts/tests` and `.github/scripts/tests`
    (neither is in DEFAULT_PAYLOAD, so neither was ever visible from here).
    """
    root = _REPO_ROOT

    # DEFAULT_PAYLOAD is the single source of truth (same reason verify.py loads
    # it by file path rather than re-globbing `hscc-*`).
    payload = install_payload.DEFAULT_PAYLOAD

    # Parse the DIRS=() array out of the actual runner script.
    dirs = _dirs_listed_in_runner()

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


def test_run_tests_sh_covers_every_tested_dir_not_just_packages():
    """The wide version of the guard above: EVERY dir in the tracked tree that
    holds test_*.py must be reachable from scripts/run_tests.sh DIRS — not just
    dirs belonging to a deployable package.

    Why the wide version had to exist: the payload guard above enumerates
    DEFAULT_PAYLOAD, so any test dir that is not a deployed plugin is invisible
    to it, and dirs stayed uncollected behind a green ALL GREEN stamp twice —
    `.github/scripts/tests` (t_9a4b7687) and `scripts/tests` (t_95d864fc, the
    address guard's own 56-case suite among them). Registering each instance
    fixes the sighting; only this assertion prevents the next one.

    The runner resolves each DIRS entry `d` as `<root>/d/tests`, so a test dir is
    reachable iff `rel` ends in `/tests` and everything before that leaf is a
    DIRS entry (entries may be multi-component — `.github/scripts` is one). Any
    other shape (`foo/test/`, `a/b/tests`, a dir not named `tests`) cannot be
    reached by the runner at all, so it is reported too — it needs a new runner
    leg, not a token. Dirs under pytest.ini's norecursedirs are exempt: the
    runner cannot collect them either, so demanding registration is a false
    failure.
    """
    dirs = _dirs_listed_in_runner()
    uncollected = []
    for rel in sorted(_test_dirs_on_tree()):
        if rel.endswith("/tests"):
            entry = rel[: -len("/tests")]
            if entry in dirs:
                continue
        uncollected.append(rel)
    assert not uncollected, (
        "test dir(s) hold test_*.py but are unreachable from "
        "scripts/run_tests.sh DIRS — the runner would print ALL GREEN while "
        f"never running them: {', '.join(uncollected)}. Fix: if the dir is "
        "<top>/tests, add <top> to DIRS in scripts/run_tests.sh; otherwise it "
        "needs its own leg in the runner, since the loop only resolves "
        "$ROOT/<entry>/tests."
    )


def test_test_dir_census_helper_sees_every_registered_leg():
    """Meta-check: the wide guard above passes VACUOUSLY if the census helper
    returns an empty or short set (a bad git glob, a skip-list bug, a moved
    pytest.ini). Assert the helper actually reproduces the runner's own DIRS
    list — every registered entry whose <entry>/tests exists must come back from
    the census, which makes a silently-empty census impossible.
    """
    dirs = _dirs_listed_in_runner()
    census = _test_dirs_on_tree()

    expected = {
        d + "/tests" for d in dirs
        if (_REPO_ROOT / d / "tests").is_dir()
        and next((_REPO_ROOT / d / "tests").glob("test_*.py"), None) is not None
    }
    missing = sorted(expected - census)
    assert not missing, (
        f"_test_dirs_on_tree() failed to report registered test dirs {missing} "
        "— the census is broken, so the wide drift guard would pass vacuously"
    )
    # Sanity: the census is a superset of the registered legs, and the two are
    # equal on a clean tree (every tracked test dir is registered).
    assert census >= expected
    assert census == expected, (
        f"census has unregistered dirs {sorted(census - expected)} — that is "
        "exactly what test_run_tests_sh_covers_every_tested_dir_not_just_packages "
        "fails on; this meta-check must not be where it first surfaces"
    )

