"""``hscc check --repo`` — the per-checkout address-guard posture (t_5abdb13d).

WHY: ``core.hooksPath`` lives in the COMMON git config and is RELATIVE, so the
config value alone lies per checkout — measured during the t_ec2c2f95 review,
the primary and every pre-merge worktree reported "armed" while resolving no
hook (docs/audits/commit-time-address-guard-t_ec2c2f95.md §6c). ``posture()``
in ``hscc-bootstrap/install_hooks.py`` is the single per-checkout truth, and
``hscc check --repo`` is the operator surface for it.

These tests pin the wiring contract, NOT the classification (the four states
are posture()'s own behaviour, covered in
hscc-bootstrap/tests/test_install_hooks.py):

  1. the posture is obtained by CALLING the shipped ``posture()`` — the same
     single-implementation rule that governs the detector itself, so these
     tests fail if hscc_daemon ever re-derives the state with its own git
     probes;
  2. ``--json`` passes the dict through verbatim (machine contract: plain
     print, byte-identical to the shipped payload);
  3. exit codes: 0 only for ``armed``; ``unarmed``, ``armed-but-absent``
     (the fail-open line) and ``not-a-repo`` are non-zero for scripts/cron;
  4. the real-shape fixture — tmp git repos built like the parent task's
     ``test_arming_from_one_worktree_does_not_advertise_protection_elsewhere``
     — drives all four states end-to-end through the shipped module.

Stdlib + pytest only. No test here touches the operator's git config: every
repo is a throwaway under tmp_path.
"""

import importlib
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from hscc_daemon import cli
from hscc_daemon import guard_posture

REPO = Path(__file__).resolve().parents[2]

# Fixture repos need a git identity that does not depend on the machine's.
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@e.invalid",
       "-c", "commit.gpgsign=false"]

HOOK_BODY = b"#!/bin/sh\n# committed hook\nexit 0\n"


def _git(repo, *args):
    return subprocess.run(GIT + ["-C", str(repo), *args],
                          capture_output=True, text=True)


def _make_repo(tmp_path, name="checkout", *, with_hook=True, hook_mode=0o755,
               with_guard=True, arm=False):
    """A throwaway checkout in any of the four postures' shapes.

    Same fixture pattern as
    hscc-bootstrap/tests/test_install_hooks.py::_make_repo — both halves
    (.githooks/pre-commit + scripts/address_guard.py) exist so the shipped
    installer will arm it; then we remove halves / set the config directly to
    land in the state under test.
    """
    repo = tmp_path / name
    (repo / ".githooks").mkdir(parents=True)
    (repo / "scripts").mkdir()
    hook = repo / ".githooks" / "pre-commit"
    hook.write_bytes(HOOK_BODY)
    os.chmod(hook, hook_mode)
    if not with_hook:
        hook.unlink()
    guard = repo / "scripts" / "address_guard.py"
    guard.write_text("# stub detector\n", encoding="utf-8")
    if not with_guard:
        guard.unlink()
    (repo / "README.md").write_text("# x\n", encoding="utf-8")
    assert _git(repo, "init", "-q", "-b", "main").returncode == 0
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    if arm:
        assert _git(repo, "config", "core.hooksPath", ".githooks").returncode == 0
    return repo


@pytest.fixture
def shipped_source(tmp_path, monkeypatch):
    """Point the loader at the REAL shipped install_hooks.py, cache cleared.

    The default resolution (parents[1] sibling) already finds this file in a
    checkout, but pinning the candidate list + flushing the module cache makes
    this suite independent of whatever hscc_daemon/guard_posture.py was loaded
    from elsewhere (e.g. ~/.hermes/plugins) and of test order.
    """
    src = REPO / "hscc-bootstrap" / "install_hooks.py"
    assert src.is_file(), f"shipped posture() missing: {src}"
    monkeypatch.setattr(guard_posture, "_candidate_paths", lambda: [src])
    monkeypatch.setattr(guard_posture, "_MODULES", {})
    return src


def _run_check(monkeypatch, *argv):
    """Invoke cmd_check the way hscc.py dispatches it; capture output + exit code."""
    monkeypatch.setattr(sys, "argv", ["hscc", "check", *argv])
    out = io.StringIO()
    code = None
    try:
        with redirect_stdout(out):
            cli.cmd_check(*argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 0
    return (code if code is not None else 0), out.getvalue()


class TestSingleImplementation:
    """check --repo must CALL posture(); it must not re-derive it."""

    def test_shipped_source_resolves_to_the_sibling_bootstrap_dir(self, shipped_source):
        assert guard_posture.install_hooks_source() == shipped_source

    def test_loader_exposes_the_real_posture_function(self, shipped_source):
        mod = guard_posture.load_install_hooks()
        assert mod.__file__ == str(shipped_source)
        assert callable(mod.posture)

    def test_no_guard_source_fails_closed_not_silently_ok(self, monkeypatch):
        missing = [Path("/nonexistent/hscc-bootstrap/install_hooks.py")]
        monkeypatch.setattr(guard_posture, "_candidate_paths", lambda: missing)
        monkeypatch.setattr(guard_posture, "_MODULES", {})
        with pytest.raises(guard_posture.GuardSourceError):
            guard_posture.posture("/tmp")
        code, out = _run_check(monkeypatch, "--repo", "/tmp")
        assert code != 0
        assert "UNVERIFIED" in out

    def test_posture_is_the_shipped_function_verbatim(self, shipped_source, monkeypatch,
                                                       tmp_path):
        """Patch a stub posture into the LOADED module and prove the CLI's dict
        is exactly what that function returned — pass-through, zero edits."""
        stub = {"state": "armed", "toplevel": "/x", "core_hooks_path": ".githooks",
                "hook_present": True, "hook_executable": True,
                "guard_present": True, "detail": "STUB DETAIL", "extra": "kept"}
        monkeypatch.setattr(guard_posture, "_MODULES", {})
        mod = guard_posture.load_install_hooks()
        monkeypatch.setattr(mod, "posture", lambda repo=None: dict(stub))
        code, out = _run_check(monkeypatch, "--repo", str(tmp_path), "--json")
        assert code == 0
        assert json.loads(out) == stub


class TestFourStatesEndToEnd:
    """All four states driven through the shipped posture() in real git repos."""

    def test_armed_exits_zero(self, shipped_source, monkeypatch, tmp_path):
        repo = _make_repo(tmp_path, arm=True)
        code, out = _run_check(monkeypatch, "--repo", str(repo), "--json")
        assert code == 0
        p = json.loads(out)
        assert p["state"] == "armed"
        assert p["core_hooks_path"] == ".githooks"
        assert p["hook_executable"] is True and p["guard_present"] is True

    def test_unarmed_exits_nonzero(self, shipped_source, monkeypatch, tmp_path):
        repo = _make_repo(tmp_path, arm=False)
        code, out = _run_check(monkeypatch, "--repo", str(repo), "--json")
        assert code != 0
        p = json.loads(out)
        assert p["state"] == "unarmed"
        assert p["core_hooks_path"] is None
        # Files being present does NOT mean armed — the config is what arms it.
        assert p["hook_present"] is True

    def test_not_a_repo_exits_nonzero(self, shipped_source, monkeypatch, tmp_path):
        plain = tmp_path / "just-a-dir"
        plain.mkdir()
        code, out = _run_check(monkeypatch, "--repo", str(plain), "--json")
        assert code != 0
        p = json.loads(out)
        assert p["state"] == "not-a-repo"
        assert p["toplevel"] is None

    def test_armed_but_absent_is_the_headline_nonzero_state(
            self, shipped_source, monkeypatch, tmp_path):
        """The exact scenario the operator asked to see: armed from one checkout
        makes a sibling worktree (pre-merge revision) *report itself protected*
        while it resolves no hook. check --repo must name it, exit non-zero, and
        print the recovery hint — the config alone cannot fix it."""
        repo = _make_repo(tmp_path, "main-checkout", arm=True)
        wt = tmp_path / "outside" / "wt-premerge"
        assert _git(repo, "worktree", "add", "-q", "-b", "premerge", str(wt)).returncode == 0
        # Simulate a pre-merge revision in the worktree: strip both halves.
        (wt / ".githooks" / "pre-commit").unlink()
        (wt / "scripts" / "address_guard.py").unlink()

        # The primary itself is genuinely armed.
        code, out = _run_check(monkeypatch, "--repo", str(repo), "--json")
        assert code == 0 and json.loads(out)["state"] == "armed"

        # The sibling shares the COMMON config but has nothing runnable.
        code, out = _run_check(monkeypatch, "--repo", str(wt), "--json")
        assert code != 0
        p = json.loads(out)
        assert p["state"] == "armed-but-absent"
        assert p["core_hooks_path"] == ".githooks"
        assert p["hook_present"] is False and p["guard_present"] is False

        # Human view carries the fail-open truth + the greppable hint.
        code, out = _run_check(monkeypatch, "--repo", str(wt))
        assert code != 0
        assert "armed-but-absent" in out
        assert "next step:" in out
        assert "NOT guarded" in out

    def test_non_executable_hook_is_not_armed(self, shipped_source, monkeypatch, tmp_path):
        """A 0644 hook is silently ignored by git — posture() must not call that
        armed, and neither may check --repo."""
        repo = _make_repo(tmp_path, arm=True, hook_mode=0o644)
        code, out = _run_check(monkeypatch, "--repo", str(repo), "--json")
        assert code != 0
        p = json.loads(out)
        assert p["state"] == "armed-but-absent"
        assert p["hook_present"] is True and p["hook_executable"] is False


class TestDefaultsAndFlags:
    def test_repo_flag_without_value_defaults_to_the_cwd_checkout(self, shipped_source,
                                                                   monkeypatch, tmp_path):
        """Card's default: `--repo` with no path means cwd's top-level."""
        repo = _make_repo(tmp_path, arm=True)
        (repo / "sub").mkdir()
        monkeypatch.chdir(repo / "sub")
        code, out = _run_check(monkeypatch, "--repo", "--json")
        assert code == 0
        p = json.loads(out)
        assert p["state"] == "armed"
        assert p["toplevel"] == str(repo.resolve())

    def test_bare_check_still_runs_the_check_cycle(self, tmp_hfcc_dir, monkeypatch):
        """Regression: `hscc check` with no flags is UNCHANGED — it runs the
        check cycle, it does not become a posture report. Scripts/cron and
        TestCmdCheck depend on that."""
        from hscc_daemon import health, state as state_mod, daemon_ops

        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_hfcc_dir / "pid"))
        monkeypatch.setattr(health, "check_dgx", lambda: True)
        code, out = _run_check(monkeypatch)
        assert "Running DGX check" in out
        assert "address-guard posture" not in out

    def test_repo_equals_form(self, shipped_source, monkeypatch, tmp_path):
        repo = _make_repo(tmp_path, arm=True)
        code, out = _run_check(monkeypatch, f"--repo={repo}", "--json")
        assert code == 0
        assert json.loads(out)["state"] == "armed"

    def test_repo_with_a_stream_is_refused(self, shipped_source, monkeypatch, tmp_path):
        repo = _make_repo(tmp_path, arm=True)
        code, out = _run_check(monkeypatch, "dgx", "--repo", str(repo))
        assert code == 2
        assert "does not take a stream" in out

    def test_json_is_a_single_machine_line_no_rich(self, shipped_source, monkeypatch, tmp_path):
        repo = _make_repo(tmp_path, arm=True)
        code, out = _run_check(monkeypatch, "--repo", str(repo), "--json")
        assert code == 0
        line = out.strip()
        assert "\n" not in line
        assert "\x1b[" not in line
        json.loads(line)  # parseable

    def test_human_view_is_plain_text_when_piped(self, shipped_source, monkeypatch, tmp_path):
        repo = _make_repo(tmp_path, arm=True)
        code, out = _run_check(monkeypatch, "--repo", str(repo))
        assert code == 0
        assert "\x1b[" not in out
        assert "armed" in out

    def test_bare_stream_still_works(self, tmp_hfcc_dir, monkeypatch):
        """The old positional path is untouched: `hscc check dgx` never enters
        the --repo branch."""
        from hscc_daemon import health, state as state_mod, daemon_ops

        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_hfcc_dir / "pid"))
        monkeypatch.setattr(health, "check_dgx", lambda: True)
        code, out = _run_check(monkeypatch, "dgx")
        assert "dgx" in out.lower()
        assert "Result: OK" in out
