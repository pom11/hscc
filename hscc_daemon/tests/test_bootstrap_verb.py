"""`hscc bootstrap` — the installer verb (card t_bc5a3fee).

The verb does exactly two things: locate ``bootstrap.sh`` relative to the
RESOLVED SOURCE ROOT of the running CLI (repo checkout, deployed plugins dir,
or a brew libexec tree — the tarball extracted under ``libexec/hscc-<ver>/``),
and exec it with every remaining argv token passed through verbatim.

Locator tests are pure (no subprocess): they point the resolver at fabricated
roots. Dispatch tests stub ``subprocess.run`` so no installer ever executes.
``HSCC_BOOTSTRAP_SH`` / ``HERMES_HOME`` are pinned per-test so the resolver
never falls through to the operator's live ``~/.hermes`` copy.
"""
import os
import sys
from pathlib import Path

import pytest

import hscc_daemon.hscc as h

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolated_resolver(tmp_path, monkeypatch):
    """No live-state reads: env override unset, plugins fallback sandboxed."""
    monkeypatch.delenv("HSCC_BOOTSTRAP_SH", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty-hermes"))


# ── locator: the three roots ─────────────────────────────────────────────────

def test_finds_bootstrap_sh_sibling_in_repo_checkout():
    """Repo/dev layout: hscc_daemon/ and hscc-bootstrap/ are siblings."""
    cands = h._bootstrap_script_candidates(REPO / "hscc_daemon")
    assert cands[0] == REPO / "hscc-bootstrap" / "bootstrap.sh"
    resolved = h._resolve_bootstrap_script(REPO / "hscc_daemon")
    assert resolved is not None and resolved.is_file()


def test_finds_bootstrap_sh_in_deployed_plugins_layout(tmp_path):
    """Deployed layout: ~/.hermes/plugins/hscc_daemon + .../hscc-bootstrap."""
    plugins = tmp_path / "plugins"
    (plugins / "hscc_daemon").mkdir(parents=True)
    boot = plugins / "hscc-bootstrap" / "bootstrap.sh"
    boot.parent.mkdir()
    boot.write_text("#!/bin/bash\necho hi\n")
    assert h._resolve_bootstrap_script(plugins / "hscc_daemon") == boot


def test_finds_bootstrap_sh_in_brew_libexec_versioned_tree(tmp_path):
    """Brew libexec layout: the tarball lands under libexec/hscc-<ver>/, so a
    CLI whose hscc_daemon sits inside that versioned dir must resolve the
    installer INSIDE the same versioned dir (not the libexec root)."""
    ver = tmp_path / "prefix" / "libexec" / "hscc-2.12.0"
    (ver / "hscc_daemon").mkdir(parents=True)
    boot = ver / "hscc-bootstrap" / "bootstrap.sh"
    boot.parent.mkdir()
    boot.write_text("#!/bin/bash\necho hi\n")
    assert h._resolve_bootstrap_script(ver / "hscc_daemon") == boot


def test_finds_bootstrap_through_the_venv_glob_fallback(tmp_path):
    """The venv-shim case the card names: `hscc` comes from a venv inside
    libexec/venv, so hscc_daemon resolves deep inside site-packages. The
    resolver walks ancestors to the libexec prefix and globs hscc-<ver>/."""
    prefix = tmp_path / "prefix"
    ver = prefix / "libexec" / "hscc-2.12.0"
    (ver / "hscc-bootstrap").mkdir(parents=True)
    boot = ver / "hscc-bootstrap" / "bootstrap.sh"
    boot.write_text("#!/bin/bash\n")
    site = prefix / "libexec" / "venv" / "lib" / "python3.11" / "site-packages"
    (site / "hscc_daemon").mkdir(parents=True)
    assert h._resolve_bootstrap_script(site / "hscc_daemon") == boot


def test_libexec_glob_picks_newest_version(tmp_path):
    ver1 = tmp_path / "libexec" / "hscc-2.11.0"
    ver2 = tmp_path / "libexec" / "hscc-2.12.0"
    for v in (ver1, ver2):
        (v / "hscc-bootstrap").mkdir(parents=True)
        (v / "hscc-bootstrap" / "bootstrap.sh").write_text("#!/bin/bash\n")
    site = tmp_path / "libexec" / "venv" / "lib" / "python3.11" / "site-packages"
    (site / "hscc_daemon").mkdir(parents=True)
    got = h._resolve_bootstrap_script(site / "hscc_daemon")
    assert got == ver2 / "hscc-bootstrap" / "bootstrap.sh"


def test_libexec_glob_uses_numeric_version_order(tmp_path):
    """Lexicographic order would rank 2.9.0 ABOVE 2.10.0 ('9' > '1'). The
    resolver must compare version digits numerically."""
    low = tmp_path / "libexec" / "hscc-2.9.0"
    high = tmp_path / "libexec" / "hscc-2.10.0"
    for v in (low, high):
        (v / "hscc-bootstrap").mkdir(parents=True)
        (v / "hscc-bootstrap" / "bootstrap.sh").write_text("#!/bin/bash\n")
    site = tmp_path / "libexec" / "venv" / "lib" / "python3.11" / "site-packages"
    (site / "hscc_daemon").mkdir(parents=True)
    got = h._resolve_bootstrap_script(site / "hscc_daemon")
    assert got == high / "hscc-bootstrap" / "bootstrap.sh"


# ── the layout the SHIPPED tap actually produces (card t_0f5616fd) ───────────
#
# The merged tap (pom11/homebrew-hscc@main, Formula/hscc.rb) installs the full
# tree at the version-STABLE path ``<prefix>/Cellar/hscc/<ver>/libexec/hscc/``
# (``tree = libexec/"hscc"``), drops NO ``.hscc-brew-libexec`` marker, and links
# only ``site-packages/plugins -> ../../../../hscc``. So the ``libexec/hscc-*``
# glob above matches NOTHING on a real keg. These cases pin the real shape; the
# glob and its ordering tests stay as future-proofing.

def _fabricate_shipped_keg(prefix: Path):
    """Fabricate what ``brew install hscc`` actually leaves on disk.

    Returns ``(payload, site_packages)`` where ``payload`` is the version-stable
    ``libexec/hscc`` carrying a ``hscc-bootstrap/bootstrap.sh``. No ``hscc-*``
    dir and no marker file anywhere — asserted by
    ``test_shipped_keg_fixture_has_no_versioned_dir_and_no_marker``.
    """
    keg = prefix / "Cellar" / "hscc" / "2.12.0" / "libexec"
    payload = keg / "hscc"
    boot = payload / "hscc-bootstrap" / "bootstrap.sh"
    boot.parent.mkdir(parents=True)
    boot.write_text("#!/bin/bash\necho shipped-keg\n")
    (payload / "hscc_daemon").mkdir()
    site = keg / "venv" / "lib" / "python3.11" / "site-packages"
    site.mkdir(parents=True)
    (site / "plugins").symlink_to("../../../../hscc")   # the formula's own link
    return payload, site


def test_shipped_keg_fixture_has_no_versioned_dir_and_no_marker(tmp_path):
    """Guard on the fixture: if it ever grows an ``hscc-*`` dir or the marker,
    the cases below stop pinning the shipped layout (they would silently be
    served by the glob/marker legs instead)."""
    payload, site = _fabricate_shipped_keg(tmp_path / "opt")
    libexec = payload.parent
    assert payload.name == "hscc"
    assert list(libexec.glob("hscc-*")) == []
    assert not (libexec / ".hscc-brew-libexec").exists()
    assert not (payload / ".hscc-brew-libexec").exists()
    assert (site / "plugins").readlink() == Path("../../../../hscc")


def test_finds_bootstrap_sh_in_the_shipped_keg_through_the_plugins_symlink(
        tmp_path):
    """The real keg's import path: ``hscc_daemon`` is imported through
    ``site-packages/plugins``, and ``hscc.py``'s own ``Path(__file__).resolve()``
    follows that symlink to ``libexec/hscc/hscc_daemon``. The resolver must land
    on the installer INSIDE the stable tree — not report 'not found'."""
    payload, site = _fabricate_shipped_keg(tmp_path / "opt")
    imported_via = (site / "plugins" / "hscc_daemon").resolve()
    assert imported_via == payload / "hscc_daemon"
    got = h._resolve_bootstrap_script(imported_via)
    assert got == payload / "hscc-bootstrap" / "bootstrap.sh"


def test_finds_bootstrap_sh_from_a_venv_site_packages_root_in_the_shipped_keg(
        tmp_path):
    """Same keg, dispatcher NOT reached through the symlink (a copied /
    wheel-installed ``hscc_daemon`` directly in ``libexec/venv/.../site-packages``
    — the layout the existing venv-glob tests use). The libexec leg must know
    the version-STABLE ``libexec/hscc`` name too, not only ``hscc-*``."""
    payload, site = _fabricate_shipped_keg(tmp_path / "opt")
    (site / "hscc_daemon").mkdir()
    got = h._resolve_bootstrap_script(site / "hscc_daemon")
    assert got == payload / "hscc-bootstrap" / "bootstrap.sh", (
        "shipped tap layout unsupported: the resolver only globs libexec/hscc-*")


def test_shipped_keg_stable_tree_beats_the_plugins_home_fallback(tmp_path,
                                                                 monkeypatch):
    """Ordering pin: on an operator machine ``$HERMES_HOME/plugins`` also holds
    a bootstrap.sh (bootstrapped earlier). The keg's own copy must win, or the
    verb would run a stale installer against a freshly upgraded keg."""
    payload, site = _fabricate_shipped_keg(tmp_path / "opt")
    (site / "hscc_daemon").mkdir()
    home = tmp_path / "hh"
    stale = home / "plugins" / "hscc-bootstrap" / "bootstrap.sh"
    stale.parent.mkdir(parents=True)
    stale.write_text("#!/bin/bash\necho stale\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert h._resolve_bootstrap_script(site / "hscc_daemon") != stale
    assert h._resolve_bootstrap_script(site / "hscc_daemon") == \
        payload / "hscc-bootstrap" / "bootstrap.sh"


def test_env_override_wins_over_everything(tmp_path, monkeypatch):
    real = tmp_path / "somewhere-else" / "bootstrap.sh"
    real.parent.mkdir()
    real.write_text("#!/bin/bash\n")
    monkeypatch.setenv("HSCC_BOOTSTRAP_SH", str(real))
    assert h._resolve_bootstrap_script(REPO / "hscc_daemon") == real
    # ...but a bogus override is NOT silently used (no fallback to a wrong tree)
    monkeypatch.setenv("HSCC_BOOTSTRAP_SH", str(tmp_path / "nope.sh"))
    got = h._resolve_bootstrap_script(REPO / "hscc_daemon")
    assert got != tmp_path / "nope.sh"
    assert got is not None and got.is_file()


def test_plugins_home_fallback_when_source_has_no_copy(tmp_path, monkeypatch):
    """A wheel-installed hscc_daemon (no sibling hscc-bootstrap) still finds
    the deployed copy under $HERMES_HOME/plugins."""
    home = tmp_path / "hh"
    (home / "plugins" / "hscc-bootstrap").mkdir(parents=True)
    boot = home / "plugins" / "hscc-bootstrap" / "bootstrap.sh"
    boot.write_text("#!/bin/bash\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    site = tmp_path / "sp" / "hscc_daemon"
    site.mkdir(parents=True)
    assert h._resolve_bootstrap_script(site) == boot


def test_returns_none_when_no_root_carries_a_copy(tmp_path):
    site = tmp_path / "sp" / "hscc_daemon"
    site.mkdir(parents=True)
    assert h._resolve_bootstrap_script(site) is None


# ── dispatch: pass-through + failure paths ───────────────────────────────────

class _Recorder:
    def __init__(self, rc=0):
        self.rc = rc
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((list(cmd), kw))

        return type("R", (), {"returncode": self.rc})()


def _dispatch(monkeypatch, argv, script=None, rc=0):
    rec = _Recorder(rc)
    monkeypatch.setattr(h.subprocess, "run", rec)
    if script is not None:
        monkeypatch.setattr(h, "_resolve_bootstrap_script", lambda *a: Path(script))
    monkeypatch.setattr(sys, "argv", argv)
    return rec, h._handle_bootstrap()


def test_handle_bootstrap_execs_script_with_passthrough_args(monkeypatch, tmp_path):
    script = tmp_path / "bootstrap.sh"
    script.write_text("#!/bin/bash\n")
    script.chmod(0o755)
    rec, rc = _dispatch(monkeypatch, ["hscc", "bootstrap", "--dry-run", "--yes"],
                        script=script)
    assert rc == 0
    (cmd, kw) = rec.calls[0]
    assert cmd == [str(script), "--dry-run", "--yes"]
    # env is inherited (HERMES_HOME/HSCC_DIR sandboxing must survive the hop)
    assert kw.get("env") == os.environ
    # stdout/stderr are NOT captured: the installer talks to the operator live
    assert "capture_output" not in kw and "stdout" not in kw


def test_handle_bootstrap_falls_back_to_bash_without_exec_bit(monkeypatch, tmp_path):
    script = tmp_path / "bootstrap.sh"
    script.write_text("#!/bin/bash\n")  # mode 644: copy that lost its +x
    rec, rc = _dispatch(monkeypatch, ["hscc", "bootstrap", "--yes"], script=script)
    assert rc == 0
    assert rec.calls[0][0] == ["bash", str(script), "--yes"]


def test_handle_bootstrap_propagates_installer_exit_code(monkeypatch, tmp_path):
    script = tmp_path / "bootstrap.sh"
    script.write_text("#!/bin/bash\n")
    script.chmod(0o755)
    _, rc = _dispatch(monkeypatch, ["hscc", "bootstrap", "--yes"],
                      script=script, rc=3)
    assert rc == 3


def test_handle_bootstrap_passes_no_extra_args_when_none_given(monkeypatch, tmp_path):
    script = tmp_path / "bootstrap.sh"
    script.write_text("#!/bin/bash\n")
    script.chmod(0o755)
    rec, rc = _dispatch(monkeypatch, ["hscc", "bootstrap"], script=script)
    assert rc == 0
    assert rec.calls[0][0] == [str(script)]


def test_handle_bootstrap_reports_missing_script(monkeypatch, capsys):
    monkeypatch.setattr(h, "_resolve_bootstrap_script", lambda *a: None)
    monkeypatch.setattr(sys, "argv", ["hscc", "bootstrap", "--dry-run"])
    rc = h._handle_bootstrap()
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert rc == 1
    assert "bootstrap.sh" in out and "not found" in out


def test_main_dispatches_bootstrap_verb(monkeypatch):
    seen = {}

    def fake():
        seen["called"] = True
        return 0

    monkeypatch.setattr(h, "_handle_bootstrap", fake)
    monkeypatch.setattr(sys, "argv", ["hscc", "bootstrap", "--dry-run"])
    with pytest.raises(SystemExit) as ex:
        h.main()
    assert ex.value.code == 0
    assert seen.get("called") is True


def test_bootstrap_help_intercepts_before_dispatch(monkeypatch, capsys):
    """`hscc bootstrap --help`/`-h` print the COMMAND_HELP text and NEVER exec
    the installer — the raw-argv help intercept every verb has (t_5132deb3)."""
    def boom(*a, **k):
        raise AssertionError("help must not exec bootstrap.sh")

    monkeypatch.setattr(h.subprocess, "run", boom)
    monkeypatch.setattr(h, "_resolve_bootstrap_script", boom)
    for flag in ("--help", "-h"):
        monkeypatch.setattr(sys, "argv", ["hscc", "bootstrap", "--yes", flag])
        with pytest.raises(SystemExit) as ex:
            h.main()
        assert ex.value.code == 0
        out = capsys.readouterr().out
        assert "hscc bootstrap" in out


# ── help surfaces: budget + declaration (F5/B-gate convention) ───────────────

def test_bootstrap_is_a_daemon_command_for_help_intercept():
    assert "bootstrap" in h.DAEMON_COMMANDS


def test_command_help_entry_exists_and_fits_budget():
    entry = h.COMMAND_HELP.get("bootstrap")
    assert entry, "COMMAND_HELP is missing 'bootstrap'"
    for line in entry.splitlines():
        # `hscc help bootstrap` prints `  {value}` — only the FIRST line gets
        # the 2-space prefix; every line is authored <=76 (the (B) 78 budget).
        assert len(line) <= 76, f"COMMAND_HELP bootstrap line over budget: {line!r}"
    for needle in ("--dry-run", "--yes", "bootstrap.sh"):
        assert needle in entry


def test_help_topic_bootstrap_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["hscc", "help", "bootstrap"])
    assert h._handle_help() == 0
    assert "hscc bootstrap" in capsys.readouterr().out


def test_main_help_lists_bootstrap():
    from rich.console import Group
    group = h._get_help_text()
    assert isinstance(group, Group)
    blob = "\n".join(getattr(r, "plain", str(r)) for r in group.renderables)
    assert "bootstrap" in blob


def test_unknown_command_blob_names_bootstrap(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["hscc", "bogus-verb"])
    with pytest.raises(SystemExit) as ex:
        h.main()
    assert ex.value.code == 1
    assert "bootstrap" in capsys.readouterr().out
