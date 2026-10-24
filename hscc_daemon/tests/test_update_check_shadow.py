"""PATH shadow scan (card t_9b98e696) — warn-only, never destructive.

Everything here is FABRICATED: tmp PATH strings, fake ``hscc`` executables
(shell scripts printing canned ``--version`` output), and a fabricated brew
keg tree. No test reads the operator's real ``$PATH``, and the suite-wide
conftest pins ``SHADOW_SCAN_MODES`` to ``()`` so a forgotten fabricator
returns None instead of walking live PATH — the tests that exercise the scan
re-arm it explicitly.

The contract the tests pin, per the card:

  * a shadow exists when an EARLIER PATH ``hscc`` resolves to a different
    realpath AND (different version OR different install family);
  * an unresolvable version still warns ("unknown version");
  * clean PATH is SILENT (no extra noise on normal machines);
  * a LATER entry is never a shadow;
  * the scan never writes, deletes, re-points, or edits PATH/rc — warn only;
  * ``hscc update --check`` never fails because of the warning, and the
    ``--json`` shape only GAINS a key (back-compat pinned below);
  * ``hscc bootstrap`` (and --dry-run) prints the same WARN before exec-ing
    the installer.
"""
import io
import json
import os
import stat
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

import hscc_daemon.hscc as h
import hscc_daemon.update_check as uc

KEG_VERSION = "9.9.9"
SHIM_VERSION = "2.12.0"


@pytest.fixture(autouse=True)
def _armed_scan(monkeypatch):
    """Re-arm the scan for THIS suite only, and pin the running version.

    conftest pins ``SHADOW_SCAN_MODES`` to ``()`` so no verb test anywhere in
    the package can walk the operator's live PATH; this suite fabricates
    every input (path_env, argv0, source_root), so it re-arms the gate
    explicitly — the shape the conftest comment documents. ``installed_version``
    is pinned so a release VERSION bump can never flip a family/version
    comparison here.
    """
    monkeypatch.setattr(uc, "SHADOW_SCAN_MODES", (uc.MODE_BREW,))
    monkeypatch.setattr(uc, "installed_version", lambda: KEG_VERSION)


@dataclass
class _Fabricated:
    """What :func:`_fabricate` built — the keg launcher, its tree, and the
    optional earlier PATH copy (None for a clean fabricated PATH)."""
    launcher: Path
    keg: Path
    path_dirs: list
    shim: Optional[Path]


def _exe(path, version=None, body=None):
    """An executable fake ``hscc``. Prints a canned version, or `body`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if body is None:
        body = f"echo 'hscc {version}'\n"
    path.write_text(f"#!/bin/sh\n{body}")
    path.chmod(0o755)
    return path


def _fabricate(tmp_path, *, shim="conda", shim_version=SHIM_VERSION):
    """A fabricated keg + optional earlier shim. Returns a _Fabricated.

    keg launcher : ``<tmp>/prefix/bin/hscc``            (brew family)
    keg tree     : ``<tmp>/prefix/libexec/hscc`` + the ``.hscc-brew-libexec``
                   marker, so detect_install_mode() says brew for it
    shim         : conda -> ``<tmp>/miniconda3/envs/<env>/bin/hscc``
                   local  -> ``<tmp>/home/.local/bin/hscc``
                   deployed -> ``<tmp>/home/.hermes/plugins/hscc/hscc``
                   none  -> no shim at all (clean PATH)
    """
    prefix = tmp_path / "prefix"
    keg = prefix / "libexec" / "hscc"
    (keg / "hscc_daemon").mkdir(parents=True)
    (keg / ".hscc-brew-libexec").write_text("fabricated\n")
    launcher = _exe(prefix / "bin" / "hscc", KEG_VERSION)
    path_dirs = [str(prefix / "bin")]

    shim_path = None
    if shim == "conda":
        shim_path = _exe(tmp_path / "miniconda3" / "envs" / "p313" / "bin" / "hscc",
                         shim_version)
    elif shim == "local":
        shim_path = _exe(tmp_path / "home" / ".local" / "bin" / "hscc",
                         shim_version)
    elif shim == "deployed":
        shim_path = _exe(tmp_path / "home" / ".hermes" / "plugins" / "hscc" / "hscc",
                         shim_version)
    elif shim != "none":
        raise ValueError(shim)
    return _Fabricated(launcher=launcher, keg=keg, path_dirs=path_dirs,
                       shim=shim_path)


def _scan(ns, *, shim_first=True, **kw):
    """Run the scan over the fabricated PATH, scan armed, probe = real one.

    ``shim_first`` controls ORDER: True puts the shim ahead of the keg (the
    operator's case), False puts it after (which must never warn).
    """
    dirs = list(ns.path_dirs)
    if ns.shim is not None:
        shim_dir = str(Path(ns.shim).parent)
        dirs = ([shim_dir] + dirs) if shim_first else (dirs + [shim_dir])
    kw.setdefault("install_mode", uc.MODE_BREW)
    kw.setdefault("path_env", os.pathsep.join(dirs))
    kw.setdefault("argv0", str(ns.launcher))
    kw.setdefault("source_root", ns.keg)
    return uc.detect_path_shadow(**kw)


# ── the helper: core rule ───────────────────────────────────────────────────

class TestShadowDetection:
    def test_earlier_conda_shim_different_version_is_a_shadow(self, tmp_path):
        ns = _fabricate(tmp_path)
        res = _scan(ns)
        assert res is not None
        assert res["path"] == str(ns.shim)
        assert res["version"] == SHIM_VERSION
        assert res["family"] == uc.FAMILY_CONDA
        assert res["running_version"] == KEG_VERSION
        assert res["running_family"] == uc.FAMILY_BREW
        # The suggestion is DERIVED from the actual keg path, never a
        # hardcoded /opt/homebrew.
        assert res["explicit_path"] == str(ns.launcher)
        assert "/opt/homebrew" not in res["explicit_path"]

    def test_earlier_shim_same_version_different_family_is_still_a_shadow(
            self, tmp_path):
        """The body's AND clause: different realpath AND (version OR family).
        An earlier conda shim even at the keg's version is still the copy a
        plain `hscc` runs, so it still warns."""
        ns = _fabricate(tmp_path, shim_version=KEG_VERSION)
        res = _scan(ns)
        assert res is not None and res["family"] == uc.FAMILY_CONDA
        assert res["version"] == KEG_VERSION

    def test_clean_path_is_silent(self, tmp_path):
        ns = _fabricate(tmp_path, shim="none")
        assert _scan(ns) is None

    def test_later_entry_is_not_a_shadow(self, tmp_path):
        """ORDERING (the reason this is not shutil.which): the keg comes
        FIRST, the shim after — plain `hscc` already runs the keg."""
        ns = _fabricate(tmp_path)
        assert _scan(ns, shim_first=False) is None

    def test_running_install_under_another_name_is_not_a_shadow(self, tmp_path):
        """A symlink in an earlier dir pointing AT the running launcher is
        the same install (same realpath), not a shadow."""
        ns = _fabricate(tmp_path, shim="none")
        link_dir = tmp_path / "first" / "bin"
        link_dir.mkdir(parents=True)
        link = link_dir / "hscc"
        link.symlink_to(ns.launcher)
        res = uc.detect_path_shadow(
            install_mode=uc.MODE_BREW,
            path_env=os.pathsep.join([str(link_dir)] + ns.path_dirs),
            argv0=str(ns.launcher), source_root=ns.keg)
        assert res is None

    def test_path_inside_the_running_tree_is_not_a_shadow(self, tmp_path):
        """A second NAME *inside* the keg (libexec/hscc/bin/hscc) is the same
        install even though argv[0] names only the bin/ launcher."""
        ns = _fabricate(tmp_path, shim="none")
        inner = ns.keg / "bin" / "hscc"
        _exe(inner, KEG_VERSION)
        res = uc.detect_path_shadow(
            install_mode=uc.MODE_BREW,
            path_env=os.pathsep.join([str(inner.parent)] + ns.path_dirs),
            argv0=str(ns.launcher), source_root=ns.keg)
        assert res is None

    def test_non_brew_mode_is_silent(self, tmp_path):
        """The scan is scoped to mode=brew (a dev checkout shadows itself by
        definition; the body asks for the keg case)."""
        ns = _fabricate(tmp_path)
        assert _scan(ns, install_mode=uc.MODE_DEV) is None
        assert _scan(ns, install_mode=uc.MODE_UNKNOWN) is None

    def test_empty_path_entry_does_not_scan_cwd(self, tmp_path, monkeypatch):
        """POSIX treats '' in PATH as cwd — the scan must not, or a stray
        ./hscc in whatever directory a check happened to run from would warn."""
        ns = _fabricate(tmp_path, shim="none")
        here = tmp_path / "cwd"
        _exe(here / "hscc", "0.0.1")
        monkeypatch.chdir(here)
        res = uc.detect_path_shadow(
            install_mode=uc.MODE_BREW,
            path_env=os.pathsep.join([""] + ns.path_dirs),
            argv0=str(ns.launcher), source_root=ns.keg)
        assert res is None

    def test_non_executable_and_directories_are_skipped(self, tmp_path):
        """A non-executable file or a DIRECTORY named hscc earlier on PATH is
        not a candidate at all (the shell would not run it either)."""
        ns = _fabricate(tmp_path, shim="none")
        dead_dir = tmp_path / "dead" / "bin"
        dead_dir.mkdir(parents=True)
        (dead_dir / "hscc").mkdir()                    # a directory named hscc
        quiet = dead_dir / "other" / "hscc"            # exists, not +x
        quiet.parent.mkdir(parents=True)
        quiet.write_text("#!/bin/sh\necho 0.0.1\n")
        quiet.chmod(0o644)
        res = uc.detect_path_shadow(
            install_mode=uc.MODE_BREW,
            path_env=os.pathsep.join([str(dead_dir), str(quiet.parent)]
                                     + ns.path_dirs),
            argv0=str(ns.launcher), source_root=ns.keg)
        assert res is None


# ── fail-quiet: unresolvable version still warns ────────────────────────────

class TestFailQuiet:
    def test_unresolvable_version_still_warns_as_unknown(self, tmp_path):
        ns = _fabricate(tmp_path)
        _exe(ns.shim, body="echo 'Unknown command: version'\nexit 1\n")
        res = _scan(ns)
        assert res is not None
        assert res["version"] is None
        lines = uc.format_shadow_warning(res)
        assert any("unknown version" in ln for ln in lines)

    def test_hanging_candidate_times_out_and_still_warns(self, tmp_path,
                                                         monkeypatch):
        """A wedged shim must not hang the check: the 2 s budget is a
        constant here — shrink it to keep the suite fast, then pin that a
        timeout becomes 'unknown version' and a WARN, not a crash."""
        monkeypatch.setattr(uc, "SHADOW_VERSION_TIMEOUT_SECONDS", 0.2)
        ns = _fabricate(tmp_path)
        _exe(ns.shim, body="sleep 30\n")
        res = _scan(ns)
        assert res is not None and res["version"] is None
        assert any("unknown version" in ln for ln in uc.format_shadow_warning(res))

    def test_probe_that_raises_is_contained(self, tmp_path):
        """An injected probe exploding is still 'unresolvable', not a crash
        (the scan may never break the verb that called it)."""
        ns = _fabricate(tmp_path)

        def explode(path, timeout=None):
            raise RuntimeError("probe exploded")
        res = _scan(ns, probe=explode)
        assert res is not None and res["version"] is None

    def test_broken_candidate_exec_is_contained(self, tmp_path):
        """A candidate whose --version cannot even start (shebang names a
        missing interpreter) is unresolvable, not an error."""
        ns = _fabricate(tmp_path)
        assert ns.shim is not None
        ns.shim.write_text("#!/nonexistent/interpreter\n")
        ns.shim.chmod(0o755)
        res = _scan(ns)
        assert res is not None and res["version"] is None

    def test_scan_never_raises_on_garbage(self, tmp_path, monkeypatch):
        """Even a catastrophic interior failure ends as None, not a raise."""
        monkeypatch.setattr(uc, "_running_install",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("internal")))
        assert uc.detect_path_shadow(install_mode=uc.MODE_BREW,
                                     path_env=str(tmp_path)) is None


# ── install families + shebang reading ─────────────────────────────────────

class TestFamilies:
    @pytest.mark.parametrize("rel,family", [
        (["prefix", "libexec", "hscc", "bin", "hscc"], uc.FAMILY_BREW),
        (["opt", "homebrew", "bin", "hscc"], uc.FAMILY_BREW),
        (["root", "Cellar", "hscc", "9.9.9", "bin", "hscc"], uc.FAMILY_BREW),
        (["home", "op", "miniconda3", "envs", "p313", "bin", "hscc"],
         uc.FAMILY_CONDA),
        (["home", "op", "miniforge3", "bin", "hscc"], uc.FAMILY_CONDA),
        (["home", "op", ".local", "bin", "hscc"], uc.FAMILY_USER_LOCAL),
        (["home", "op", ".hermes", "plugins", "hscc", "hscc"],
         uc.FAMILY_DEPLOYED),
        (["home", "op", ".hermes", "hermes-agent", "venv", "bin", "hscc"],
         uc.FAMILY_DEPLOYED),
        (["home", "op", ".pyenv", "shims", "hscc"], uc.FAMILY_VENV),
        (["usr", "local", "bin", "hscc"], uc.FAMILY_SYSTEM),
        (["Users", "op", "weird", "place", "hscc"], uc.FAMILY_OTHER),
    ])
    def test_family_labels(self, rel, family):
        assert uc._family_for(Path("/").joinpath(*rel)) == family

    def test_shebang_family_when_path_shape_is_unremarkable(self, tmp_path):
        """A copy in an anonymous dir whose shebang is the Hermes venv reads
        as deployed-plugins — the operator's conda shim's real code source."""
        f = tmp_path / "anything" / "hscc"
        f.parent.mkdir()
        f.write_text("#!/home/op/.hermes/hermes-agent/venv/bin/python\n")
        f.chmod(0o755)
        assert uc._family_for(f, uc._read_shebang(f)) == uc.FAMILY_DEPLOYED

    def test_read_shebang_variants(self, tmp_path):
        def sheb(text):
            f = tmp_path / "s"
            f.write_text(text)
            return uc._read_shebang(f)
        assert sheb("#!/bin/sh\necho hi\n") == "/bin/sh"
        assert sheb("#!/usr/bin/env python3\n") == "python3"
        assert sheb("#!/bin/bash -e\n") == "/bin/bash"
        assert sheb("# no shebang\necho hi\n") is None
        assert sheb("") is None
        binary = tmp_path / "b"
        binary.write_bytes(b"\x7fELF\x00\x01garbage")
        assert uc._read_shebang(binary) is None

    def test_earlier_local_bin_shim_warns_with_family(self, tmp_path):
        ns = _fabricate(tmp_path, shim="local")
        res = _scan(ns)
        assert res is not None and res["family"] == uc.FAMILY_USER_LOCAL

    def test_earlier_deployed_plugins_shim_warns_with_family(self, tmp_path):
        ns = _fabricate(tmp_path, shim="deployed")
        res = _scan(ns)
        assert res is not None and res["family"] == uc.FAMILY_DEPLOYED


# ── bounded work ────────────────────────────────────────────────────────────

class TestBoundedProbing:
    def test_probe_cap_respected(self, tmp_path):
        """Four earlier copies ahead of the keg: at most SHADOW_MAX_PROBES
        subprocess probes happen; the scan still concludes (a cap-exceeded
        candidate cannot be resolved -> it warns as unknown, fail-toward-
        warning, because it IS the copy a plain `hscc` runs)."""
        ns = _fabricate(tmp_path, shim="none")
        earlier = []
        for i in range(4):
            d = tmp_path / f"e{i}" / "bin"
            _exe(d / "hscc", "1.0.0")
            earlier.append(str(d))
        calls = []

        def counting_probe(path, timeout=None):
            calls.append(str(path))
            return "1.0.0"
        res = uc.detect_path_shadow(
            install_mode=uc.MODE_BREW,
            path_env=os.pathsep.join(earlier + ns.path_dirs),
            argv0=str(ns.launcher), source_root=ns.keg,
            probe=counting_probe)
        assert len(calls) <= uc.SHADOW_MAX_PROBES
        assert res is not None and res["probes"] <= uc.SHADOW_MAX_PROBES

    def test_same_version_same_family_duplicate_is_not_reported(self, tmp_path):
        """An earlier brew-shaped copy of the SAME version (a second bin dir
        from the same brew world) is a duplicate, not a shadow — the scan
        keeps walking past it and stays silent when nothing else differs."""
        ns = _fabricate(tmp_path, shim="none")
        dup_dir = tmp_path / "opt" / "homebrew" / "bin"
        _exe(dup_dir / "hscc", KEG_VERSION)
        res = uc.detect_path_shadow(
            install_mode=uc.MODE_BREW,
            path_env=os.pathsep.join([str(dup_dir)] + ns.path_dirs),
            argv0=str(ns.launcher), source_root=ns.keg)
        assert res is None


# ── never destructive ───────────────────────────────────────────────────────

class TestNeverDestructive:
    def test_scan_changes_nothing_on_disk(self, tmp_path):
        """Snapshot every path + content hash + mtime + mode under tmp_path
        before and after a WARNING scan: the scan is report-only — it must
        create nothing, delete nothing, touch nothing."""
        import hashlib

        def snapshot():
            out = {}
            for p in sorted(tmp_path.rglob("*")):
                st = p.lstat()
                out[str(p)] = (
                    p.is_symlink(),
                    st.st_mode & 0o7777,
                    st.st_mtime_ns,
                    hashlib.sha256(p.read_bytes()).hexdigest()
                    if p.is_file() else None)
            return out

        ns = _fabricate(tmp_path)
        before = snapshot()
        res = _scan(ns)
        after = snapshot()
        assert res is not None                      # warned AND
        assert before == after                      # changed nothing

    def test_no_writes_outside_tmp_path(self, tmp_path, monkeypatch):
        """The scan receives only injected paths; even a hostile argv0 or
        source_root cannot make it write (there is no write call to find —
        this pins the observable: the tmp tree is byte-identical after)."""
        ns = _fabricate(tmp_path)
        marker = tmp_path / "canary.txt"
        marker.write_text("do not touch\n")
        before = marker.read_bytes()
        assert _scan(ns, argv0=str(tmp_path / "nowhere" / "hscc")) is not None
        assert marker.read_bytes() == before


# ── the WARN text itself ────────────────────────────────────────────────────

class TestWarnText:
    def test_warn_names_path_version_consequence_and_explicit_path(self, tmp_path):
        ns = _fabricate(tmp_path)
        res = _scan(ns)
        text = "\n".join(uc.format_shadow_warning(res))
        assert "WARN:" in text
        assert str(ns.shim) in text                 # the shadowing path
        assert f"v{SHIM_VERSION}" in text            # its version
        assert "a plain 'hscc' runs" in text         # the consequence
        assert str(ns.launcher) in text              # derived keg path
        assert "nothing was changed" in text         # non-destructive promise

    def test_missing_explicit_path_falls_back_to_brew_hint(self):
        lines = uc.format_shadow_warning({
            "path": "/somewhere/hscc", "realpath": "/somewhere/hscc",
            "family": uc.FAMILY_OTHER, "version": None,
            "running_version": "2.13.0", "running_family": uc.FAMILY_BREW,
            "explicit_path": None, "probes": 1})
        text = "\n".join(lines)
        assert "unknown version" in text
        assert "brew --prefix" in text               # never invent a path

    def test_clean_scan_renders_nothing(self):
        assert uc.format_shadow_warning(None) == []


# ── the verb surface: hscc update --check ──────────────────────────────────

class TestUpdateVerbShadow:
    """Wiring tests for requirement 2. The verb calls detect_path_shadow()
    with only install_mode, so these tests re-arm the conftest kill-switch
    and fabricate PATH/argv[0] — the verb's REAL default-argument path."""

    def _run_verb(self, monkeypatch, tmp_path, ns, argv=("--check",),
                  shim_first=True, tag="v2.13.0", install=KEG_VERSION):
        monkeypatch.setattr(uc, "SHADOW_SCAN_MODES", (uc.MODE_BREW,))
        monkeypatch.setattr(uc, "installed_version", lambda: install)
        monkeypatch.setattr(uc, "detect_install_mode", lambda r=None: uc.MODE_BREW)
        monkeypatch.setattr(uc, "CACHE_FILE",
                            str(tmp_path / "hscc" / "update-check.json"))
        dirs = list(ns.path_dirs)
        if ns.shim is not None:
            d = str(Path(ns.shim).parent)
            dirs = ([d] + dirs) if shim_first else (dirs + [d])
        monkeypatch.setenv("PATH", os.pathsep.join(dirs))
        monkeypatch.setattr(sys, "argv",
                            [str(ns.launcher), "update", *argv])
        monkeypatch.setattr(uc, "fetch_latest_release", lambda: tag)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = h._handle_update()
        return rc, out.getvalue(), err.getvalue()

    def test_warn_present_on_human_output_and_rc_stays_0(self, monkeypatch,
                                                         tmp_path):
        ns = _fabricate(tmp_path)
        rc, out, err = self._run_verb(monkeypatch, tmp_path, ns)
        assert rc == 0 and err == ""            # never fail the check
        assert "WARN:" in out
        assert str(ns.shim) in out
        assert f"v{SHIM_VERSION}" in out
        assert str(ns.launcher) in out
        # the normal comparison block is still there — the WARN extends,
        # it does not replace
        assert "latest:    v2.13.0" in out

    def test_up_to_date_line_plus_warn(self, monkeypatch, tmp_path):
        """A shadow does not swallow the up-to-date one-liner."""
        ns = _fabricate(tmp_path)
        rc, out, err = self._run_verb(monkeypatch, tmp_path, ns,
                                      tag=f"v{KEG_VERSION}",
                                      install=KEG_VERSION)
        assert rc == 0
        assert out.strip().startswith("up to date")
        assert "WARN:" in out

    def test_clean_path_verb_output_has_no_warn(self, monkeypatch, tmp_path):
        ns = _fabricate(tmp_path, shim="none")
        rc, out, err = self._run_verb(monkeypatch, tmp_path, ns)
        assert rc == 0
        assert "WARN" not in out

    def test_later_shim_verb_output_has_no_warn(self, monkeypatch, tmp_path):
        ns = _fabricate(tmp_path)
        rc, out, err = self._run_verb(monkeypatch, tmp_path, ns,
                                      shim_first=False)
        assert rc == 0
        assert "WARN" not in out

    def test_offline_contract_unchanged_single_stderr_line(self, monkeypatch,
                                                           tmp_path):
        """offline-quiet contract (t_a4055572) survives the shadow feature:
        a failed check still prints its ONE stderr line, exits 1, says
        nothing extra about shadows."""
        ns = _fabricate(tmp_path)

        def boom():
            raise uc.UpdateCheckError("offline (test)")
        monkeypatch.setattr(uc, "SHADOW_SCAN_MODES", (uc.MODE_BREW,))
        monkeypatch.setattr(uc, "installed_version", lambda: KEG_VERSION)
        monkeypatch.setattr(uc, "detect_install_mode", lambda r=None: uc.MODE_BREW)
        monkeypatch.setattr(uc, "CACHE_FILE",
                            str(tmp_path / "hscc" / "update-check.json"))
        dirs = [str(Path(str(ns.shim)).parent)] + ns.path_dirs
        monkeypatch.setenv("PATH", os.pathsep.join(dirs))
        monkeypatch.setattr(sys, "argv", [str(ns.launcher), "update", "--check"])
        monkeypatch.setattr(uc, "fetch_latest_release", boom)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = h._handle_update()
        assert rc == 1
        assert out.getvalue() == ""
        assert err.getvalue().count("\n") <= 2      # the one line + salvage
        assert "WARN" not in err.getvalue()

    def test_json_shape_back_compat_pin(self, monkeypatch, tmp_path):
        """`shadow` only GAINS: every pre-existing key keeps its name and
        meaning; a failure payload carries no shadow key at all."""
        ns = _fabricate(tmp_path)
        rc, out, err = self._run_verb(monkeypatch, tmp_path, ns,
                                      argv=("--check", "--json"),
                                      tag="v9.9.10")
        assert rc == 0
        payload = json.loads(out)
        prior_keys = {"ok", "available", "latest_tag", "installed", "cmp",
                      "mode", "upgrade_cmd", "cache_written"}
        assert prior_keys <= set(payload)
        assert payload["ok"] is True
        assert payload["available"] is True
        assert payload["latest_tag"] == "v9.9.10"
        assert payload["mode"] == uc.MODE_BREW
        assert payload["shadow"] is not None
        assert payload["shadow"]["path"] == str(ns.shim)
        assert payload["shadow"]["version"] == SHIM_VERSION

    def test_json_shadow_null_when_clean(self, monkeypatch, tmp_path):
        ns = _fabricate(tmp_path, shim="none")
        rc, out, err = self._run_verb(monkeypatch, tmp_path, ns,
                                      argv=("--check", "--json"))
        payload = json.loads(out)
        assert payload["shadow"] is None

    def test_json_failure_shape_has_no_shadow_key(self, monkeypatch, tmp_path):
        ns = _fabricate(tmp_path)
        rc, out, err = self._run_verb(monkeypatch, tmp_path, ns,
                                      argv=("--check", "--json"))
        # First make the fetch fail (after _run_verb wired everything):
        monkeypatch.setattr(uc, "fetch_latest_release",
                            lambda: (_ for _ in ()).throw(
                                uc.UpdateCheckError("offline (test)")))
        monkeypatch.setattr(sys, "argv",
                            [str(ns.launcher), "update", "--check", "--json"])
        out2, err2 = io.StringIO(), io.StringIO()
        with redirect_stdout(out2), redirect_stderr(err2):
            rc2 = h._handle_update()
        assert rc2 == 1
        payload = json.loads(out2.getvalue())
        assert payload["ok"] is False
        assert "shadow" not in payload
        # pre-existing failure keys unchanged
        assert "error" in payload and "cached" in payload


# ── the bootstrap surface ───────────────────────────────────────────────────

class _Recorder:
    def __init__(self, watch=None):
        self.calls = []
        self.watch = watch

    def __call__(self, cmd, **kw):
        # snapshot what stdout holds AT the call — proves the WARN printed
        # before the installer stage output, not after.
        if self.watch is not None:
            self.calls.append((list(cmd), kw, self.watch.getvalue()))
        else:
            self.calls.append((list(cmd), kw, ""))
        return type("R", (), {"returncode": 0})()


class TestBootstrapVerbShadow:
    """Requirement 3: the same WARN lands in `hscc bootstrap` (and
    --dry-run) stage output, before the installer runs, warn-only."""

    def _run_bootstrap(self, monkeypatch, tmp_path, ns, shim_first=True,
                       args=("--dry-run",)):
        script = tmp_path / "bootstrap.sh"
        script.write_text("#!/bin/bash\n")
        script.chmod(0o755)
        buf = io.StringIO()
        rec = _Recorder(watch=buf)
        monkeypatch.setattr(h.subprocess, "run", rec)
        # The scan's --version probe would ALSO be caught by the global
        # subprocess patch above (update_check shares the subprocess module),
        # turning the recorder's call #1 into the probe. Stub the probe so
        # the recorder sees exactly one call — the installer exec — and the
        # shim's canned version is what the WARN reports. The PATH walk,
        # realpath comparison and family detection still run for real.
        monkeypatch.setattr(
            uc, "_probe_version",
            lambda path, timeout=None: SHIM_VERSION)
        monkeypatch.setattr(h, "_resolve_bootstrap_script", lambda *a: script)
        monkeypatch.setattr(uc, "SHADOW_SCAN_MODES", (uc.MODE_BREW,))
        monkeypatch.setattr(uc, "detect_install_mode", lambda r=None: uc.MODE_BREW)
        monkeypatch.setattr(uc, "installed_version", lambda: KEG_VERSION)
        dirs = list(ns.path_dirs)
        if ns.shim is not None:
            d = str(Path(ns.shim).parent)
            dirs = ([d] + dirs) if shim_first else (dirs + [d])
        monkeypatch.setenv("PATH", os.pathsep.join(dirs))
        monkeypatch.setattr(sys, "argv", [str(ns.launcher), "bootstrap", *args])
        with redirect_stdout(buf):
            rc = h._handle_bootstrap()
        return rc, buf.getvalue(), rec

    def test_bootstrap_warns_before_running_installer(self, monkeypatch,
                                                      tmp_path):
        ns = _fabricate(tmp_path)
        rc, out, rec = self._run_bootstrap(monkeypatch, tmp_path, ns)
        assert rc == 0
        assert len(rec.calls) == 1
        cmd, kw, stdout_at_exec = rec.calls[0]
        assert cmd == [str(tmp_path / "bootstrap.sh"), "--dry-run"]
        # the WARN is already on stdout when the installer is exec'd —
        # call order pinned (a unit test of the formatter would miss a
        # removed call site here)
        assert "WARN:" in stdout_at_exec
        assert str(ns.shim) in stdout_at_exec
        assert "WARN:" in out

    def test_bootstrap_dry_run_warns_identically(self, monkeypatch, tmp_path):
        ns = _fabricate(tmp_path)
        rc, out, rec = self._run_bootstrap(monkeypatch, tmp_path, ns,
                                           args=("--dry-run", "--yes"))
        assert "WARN:" in out and str(ns.shim) in out

    def test_bootstrap_clean_path_silent(self, monkeypatch, tmp_path):
        ns = _fabricate(tmp_path, shim="none")
        rc, out, rec = self._run_bootstrap(monkeypatch, tmp_path, ns)
        assert "WARN" not in out and len(rec.calls) == 1

    def test_bootstrap_later_entry_silent(self, monkeypatch, tmp_path):
        ns = _fabricate(tmp_path)
        rc, out, rec = self._run_bootstrap(monkeypatch, tmp_path, ns,
                                           shim_first=False)
        assert "WARN" not in out

    def test_bootstrap_never_touches_the_shadow(self, monkeypatch, tmp_path):
        """Warn-only: after a warning run, the shim exists, is executable,
        and its bytes are untouched; no PATH/rc file anywhere nearby."""
        ns = _fabricate(tmp_path)
        assert ns.shim is not None
        before_bytes = ns.shim.read_bytes()
        before_mode = stat.S_IMODE(os.stat(ns.shim).st_mode)
        rc, out, rec = self._run_bootstrap(monkeypatch, tmp_path, ns)
        assert "WARN:" in out
        assert ns.shim.read_bytes() == before_bytes
        assert stat.S_IMODE(os.stat(ns.shim).st_mode) == before_mode
