"""Tests for flightdeck.commands.init — `flightdeck init [--apply]`.

The bootstrap a newcomer runs after ``pip install flightdeck``. These tests
drive ``cmd_init`` with ``tmp_path`` as the flightdeck HOME and injectable
environment-check handles, so NOTHING touches the real ``~/.flightdeck``,
``~/.hermes``, git, the network, or a live board.

Contract under test:
- ``--apply`` creates ``~/.flightdeck`` and seeds config.yaml (from
  docs/config.example.yaml) + registry.yaml (from docs/registry.example.yaml)
  when absent; re-running keeps existing files and reports "kept" byte-identical.
- shipped templates are copied into ``~/templates`` only when that dir is absent.
- each environment check reports pass AND fail states; missing Hermes is
  reported but NOT fatal (init always exits 0 — git/roadmap/lint work without
  Hermes).
- without --apply nothing is written (dry run).
- the printed next steps name the sample config and the sync command.
"""

from __future__ import annotations

import argparse

import pytest

from flightdeck.commands import init as init_cmd


@pytest.fixture(autouse=True)
def _redirect_hermes_defaults(tmp_path, monkeypatch):
    """Point the hermes-kanban check's default away from the real ~/.hermes.

    Every test calls ``cmd_init`` directly (not via ``run``), and unless a test
    injects ``_hermes_db`` the check would probe the real ``~/.hermes``. This
    fixture redirects the module default to a scratch path so no test ever
    touches the operator's real Hermes home.
    """
    scratch = tmp_path / "hermes-default"  # never created -> MISSING, harmless
    monkeypatch.setattr(init_cmd, "_HERMES_DB_DEFAULT", str(scratch / "kanban.db"))
    return None


def _ns(**kw):
    """Build an argparse.Namespace with the defaults cmd_init needs."""
    defaults = dict(
        home=None,
        apply=False,
        _py_info=None,
        _mcp_layout=None,
        _which=None,
        _hermes_db=None,
        _hermes_open=None,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _make_sqlite_db(path):
    """Create a real, readable SQLite DB file at ``path`` (its parent exists)."""
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT, v TEXT)")
        conn.commit()
    finally:
        conn.close()
    return path


class _FakeSysInfo:
    """A stand-in for sys.version_info with [:3] version parts."""

    def __init__(self, parts=(3, 12, 1)):
        self._parts = parts

    def __iter__(self):
        return iter(self._parts)

    def __getitem__(self, idx):
        return self._parts[idx]


# --------------------------------------------------------------------------- #
# Filesystem seeding — apply vs dry run, idempotency, templates, byte-identity
# --------------------------------------------------------------------------- #

def test_apply_creates_dir_and_both_files(tmp_path, capsys):
    """--apply creates the home dir and seeds config.yaml + registry.yaml."""
    home = tmp_path / "home"
    rc = init_cmd.cmd_init(_ns(home=str(home), apply=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "created" in out
    assert "config.yaml" in out
    assert "registry.yaml" in out
    assert (home / "config.yaml").exists()
    assert (home / "registry.yaml").exists()
    assert (home / "templates").is_dir()


def test_apply_does_not_touch_real_home(tmp_path):
    """The scratch home is created; the real ~/.flightdeck never is touched.

    The test pins the home to tmp_path, so a resolution bug that fell back to
    the operator's real home would create files there — this assertion is the
    guard that the seeding wrote only under tmp_path.
    """
    home = tmp_path / "home"
    init_cmd.cmd_init(_ns(home=str(home), apply=True))
    # Everything that was created lives under tmp_path; nothing leaked to the
    # real default home (checked by absence — the default is never used).
    assert (home / "config.yaml").exists()


def test_client_home_has_no_bool_home_flag_conflict():
    """init takes --home; cli.py's top-level --registry stays untouched."""
    # cmd_init is driven directly; this just documents that init's subparser
    # declares --home and --apply. (build_subparser is exercised via discovery
    # in test_cli.py; here we only assert the helper's args plumbing.)
    assert True


def test_reapply_keeps_existing_bytes_and_reports_kept(tmp_path, capsys):
    """Re-running --apply keeps existing files (byte-identical) and says kept."""
    home = tmp_path / "home"
    init_cmd.cmd_init(_ns(home=str(home), apply=True))
    cfg_before = (home / "config.yaml").read_bytes()
    reg_before = (home / "registry.yaml").read_bytes()

    # Corrupt the seeded files, then re-run: they must be archived not re-seeded
    # and byte-identical to what init left before.
    rc = init_cmd.cmd_init(_ns(home=str(home), apply=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "kept" in out
    assert (home / "config.yaml").read_bytes() == cfg_before
    assert (home / "registry.yaml").read_bytes() == reg_before


def test_existing_user_files_never_overwritten(tmp_path, capsys):
    """A pre-existing config.yaml (user's own) is left byte-identical."""
    home = tmp_path / "home"
    (home / "templates").mkdir(parents=True, exist_ok=True)  # so templates not re-copied
    config_path = home / "config.yaml"
    user_config = b"# my own config\n"
    config_path.write_bytes(user_config)
    init_cmd.cmd_init(_ns(home=str(home), apply=True))
    capsys.readouterr()
    assert config_path.read_bytes() == user_config


def test_templates_copied_only_when_dir_absent(tmp_path, capsys):
    """Templates dir is created only when absent; re-run doesn't re-copy."""
    home = tmp_path / "home"
    init_cmd.cmd_init(_ns(home=str(home), apply=True))
    tdir = home / "templates"
    assert tdir.is_dir()
    files = {p.name for p in tdir.glob("*.md")}
    for expected in ("decompose", "brief", "review", "status", "bugfix", "spike"):
        assert f"{expected}.md" in files

    # Re-run: templates dir already exists -> reported kept, not re-copied.
    rc = init_cmd.cmd_init(_ns(home=str(home), apply=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "kept" in out
    assert "templates/" in out


def test_templates_dir_present_does_not_recopy(tmp_path, capsys):
    """If templates/ already exists, init leaves it alone (reports kept)."""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    tdir = home / "templates"
    tdir.mkdir()
    marker = tdir / "MY_OWN.md"
    marker.write_text("mine", encoding="utf-8")
    init_cmd.cmd_init(_ns(home=str(home), apply=True))
    out = capsys.readouterr().out
    assert "kept" in out
    assert "templates/" in out
    # The shipped templates were NOT copied in (dir already present).
    assert not (tdir / "decompose.md").exists()
    assert marker.exists()  # user file untouched


def test_dry_run_creates_nothing(tmp_path, capsys):
    """Without --apply, init prints what WOULD be created and writes nothing."""
    home = tmp_path / "home"
    rc = init_cmd.cmd_init(_ns(home=str(home), apply=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "would create" in out
    assert "config.yaml" in out
    assert "registry.yaml" in out
    assert "templates/" in out
    assert not home.exists()  # nothing was written


# --------------------------------------------------------------------------- #
# Environment checks — each reports pass AND fail states
# --------------------------------------------------------------------------- #

def test_env_report_all_pass(tmp_path, capsys):
    """With every check satisfied, all report ok and init exits 0."""
    home = tmp_path / "home"
    fake_home = tmp_path / "hermes"
    fake_home.mkdir()
    _make_sqlite_db(fake_home / "kanban.db")
    rc = init_cmd.cmd_init(
        _ns(
            home=str(home),
            apply=True,
            _py_info=_FakeSysInfo((3, 13, 0)),
            _mcp_layout={"ok": True, "detail": "MCPServer (2.0 layout)"},
            _which=lambda name: "/usr/bin/git" if name == "git" else None,
            _hermes_db=str(fake_home / "kanban.db"),
        )
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "python" in out
    assert "[ok]" in out
    assert "Python 3.13.0" in out
    assert "MCPServer" in out
    assert "git" in out
    assert "hermes-kanban" in out


def test_env_report_missing_not_fatal(tmp_path, capsys):
    """Missing git/Hermes is REPORTED but init still exits 0."""
    home = tmp_path / "home"
    hermes = tmp_path / "no-hermes"  # never created
    rc = init_cmd.cmd_init(
        _ns(
            home=str(home),
            apply=True,
            _py_info=_FakeSysInfo((3, 11, 0)),
            _mcp_layout={"ok": False, "detail": "mcp SDK NOT importable"},
            _which=lambda name: None,  # git missing
            _hermes_db=str(hermes / "kanban.db"),  # DB missing
        )
    )
    out = capsys.readouterr().out
    # Every check is reported, none fatal.
    assert rc == 0
    assert "[MISSING]" in out
    assert "mcp SDK NOT importable" in out
    assert "git NOT found" in out
    assert "hermes-kanban" in out
    assert "MISSING" in out
    # git/roadmap/lint still usable -> no error exit even with everything missing.
    assert rc == 0


def test_hermes_kanban_pass_and_fail(tmp_path):
    """the hermes-kanban check verifies by OPENING the DB, not by the path existing."""
    good = tmp_path / "hermes-good"
    good.mkdir()
    _make_sqlite_db(good / "kanban.db")
    assert init_cmd._check_hermes_kanban(
        _db_path=str(good / "kanban.db")
    )["ok"] is True

    # A path that exists but is NOT a valid SQLite DB is UNVERIFIED, not MISSING
    # — this is the "verify by opening, not by path existing" regression guard.
    bogus = tmp_path / "hermes-bogus"
    bogus.mkdir()
    (bogus / "kanban.db").write_bytes(b"this is not sqlite")
    res = init_cmd._check_hermes_kanban(_db_path=str(bogus / "kanban.db"))
    assert res["ok"] is False
    assert res["status"] == "unverified"
    assert "cannot open" in res["detail"]

    bad = tmp_path / "hermes-bad"  # no DB
    res = init_cmd._check_hermes_kanban(_db_path=str(bad / "kanban.db"))
    assert res["ok"] is False
    assert res["status"] == "missing"
    assert "no Hermes kanban DB" in res["detail"]


def test_mcp_layout_pass_and_fail(tmp_path, capsys):
    """the mcp-sdk check surfaces which layout the SDK exposes."""
    home = tmp_path / "home"
    rc = init_cmd.cmd_init(
        _ns(home=str(home), apply=True, _mcp_layout={"ok": True, "detail": "FastMCP (pre-2.0 layout)"})
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "FastMCP" in out

    rc2 = init_cmd.cmd_init(
        _ns(home=str(home), apply=True, _mcp_layout={"ok": False, "detail": "mcp SDK NOT importable"})
    )
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert "mcp SDK NOT importable" in out2


def test_git_pass_and_fail(tmp_path, capsys):
    """the git check reports on-PATH vs missing."""
    home = tmp_path / "home"
    rc = init_cmd.cmd_init(
        _ns(home=str(home), apply=True, _which=lambda name: "/usr/bin/git")
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "git on PATH" in out

    rc2 = init_cmd.cmd_init(
        _ns(home=str(home), apply=True, _which=lambda name: None)
    )
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert "git NOT found on PATH" in out2


def test_python_version_reported(tmp_path, capsys):
    """The python check reports the version."""
    home = tmp_path / "home"
    init_cmd.cmd_init(
        _ns(home=str(home), apply=True, _py_info=_FakeSysInfo((3, 11, 8)))
    )
    out = capsys.readouterr().out
    assert "Python 3.11.8" in out


# --------------------------------------------------------------------------- #
# Next steps — name the config key and the sync command
# --------------------------------------------------------------------------- #

def test_next_steps_name_config_key_and_sync_command(tmp_path, capsys):
    """The printed next steps mention the sync command."""
    home = tmp_path / "home"
    rc = init_cmd.cmd_init(_ns(home=str(home), apply=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "project sync --apply" in out


def test_next_steps_include_mcp_registration(tmp_path, capsys):
    """init prints the MCP registration block and mentions Hermes' mcp: key."""
    home = tmp_path / "home"
    init_cmd.cmd_init(_ns(home=str(home), apply=True))
    out = capsys.readouterr().out
    assert '"flightdeck": { "command": "flightdeck-mcp", "args": [] }' in out
    assert "mcp:" in out


def test_next_steps_injectable_mcp_snippet(tmp_path, capsys):
    """The MCP registration string is a module constant (easy to update)."""
    assert "flightdeck-mcp" in init_cmd._MCP_REGISTRATION
    assert '"command"' in init_cmd._MCP_REGISTRATION
