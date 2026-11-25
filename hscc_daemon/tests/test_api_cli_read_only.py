"""Read-only guard for `hscc api status` / `hscc verify --json` (card t_cc7b6023).

THE INVARIANT
    Neither verb may create ``$HSCC_HOME/api-token``. That file is the bearer
    credential for the whole API (`api_server.token_valid`), so it comes into
    existence only behind a verb that means it: `hscc api start`
    (api_cli.py `_handle_start`) and the serve boot path
    (`api_server.create_servers`). Both call ``load_token``, which documents
    "generating it on first run".

    `hscc api status` used to call ``load_token`` too, from inside its QR block.
    Its own help advertised "Read-only report" and "QR **when a token exists**",
    and its docstring promised "if the token is missing ... says so and exits
    cleanly" — so the read was violating a contract it wrote itself. Worse, the
    token's mere existence flips a branch elsewhere: `hscc notify list` refuses
    without it (notify_cli.py:334) and proceeds with it, so a status line
    silently changed what a different verb does.

WHY A SUBPROCESS, NOT A CALL
    `hscc verify --json` reaches the mint through a *child process*
    (verify.py `check_api_routes` → api_route_sweep.py → ``subprocess hscc api
    status`` → the QR block). An in-process monkeypatch cannot see a hop that
    crosses a process boundary, so the seal is done with the only thing that
    reaches a child — its environment (the recipe is
    hscc-cluster/tests/test_doctor_tool.py::_seal_door_env, and the snapshot /
    canary halves are `_snapshot` + `test_the_read_writes_nothing_anywhere`).

    The child resolves ``hscc`` off PATH for that inner hop, and on a developer
    host PATH holds the INSTALLED CLI, not this tree. `_worktree_path_first`
    shims it, so these tests measure the code under test rather than whatever
    payload happens to be deployed (a green test against a stale installed copy
    would prove nothing).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

#: Repo root (this file is hscc_daemon/tests/test_api_cli_read_only.py).
REPO = Path(__file__).resolve().parents[2]
HSCC_PY = REPO / "hscc_daemon" / "hscc.py"

#: Captured before any fixture runs: after the seal, the process's own
#: ``expanduser`` answers the sandbox, so comparing it against itself would pass
#: anywhere (test_doctor_tool.py:_the_seal_actually_took, same reasoning).
_REAL_HOME = os.path.expanduser("~")
_REAL_HSCC = os.path.expanduser("~/.hscc")

#: The credential's file name, and the two verbs whose contract is "read".
TOKEN_NAME = "api-token"

#: Fleet stubs: a read must not need the cluster, and a stub that logs proves
#: which binary answered rather than leaving a test to hope nothing called out.
FAKE_BIN_FILES = {
    "sparkrun": "#!/bin/sh\necho sparkrun-stub: no fleet in tests\n",
    "curl": "#!/bin/sh\necho curl-stub: no fleet in tests\nexit 22\n",
    "ssh": "#!/bin/sh\necho ssh-stub: no fleet in tests\nexit 255\n",
}


def _snapshot(directory):
    """name -> bytes for every file under ``directory`` ({} when absent)."""
    root = Path(directory)
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def sealed_home(tmp_path, monkeypatch):
    """A `$HOME` whose `~/.hscc` has NO api-token, and no way to reach the fleet.

    Deliberately pre-seeded the way a fresh install looks: the hscc root exists
    (so "the verb created ~/.hscc" and "the verb created the token" stay
    separate observations) but the credential does not — which is exactly the
    state the mint used to fire in.
    """
    home = tmp_path / "fakehome"
    hscc = home / ".hscc"
    hscc.mkdir(parents=True)
    (home / ".hermes" / "plugins").mkdir(parents=True)
    # TEST-NET-1 only: nothing here can be a real endpoint even if a probe
    # decides to try one.
    (hscc / "cluster.json").write_text(json.dumps(
        {"clusters": [{"name": "sandbox", "hosts": ["192.0.2.1"]}]}),
        encoding="utf-8")
    # NO api.json, on purpose. `api_route_sweep.api_base()` reads api.json first
    # and only falls back to shelling `hscc api status` when it has no host
    # (api_route_sweep.py:206) — and that fallback IS the path that used to mint.
    # Planting an api.json would make the verify guard below vacuous: it would
    # assert about a verb that never reached the code under audit.

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in FAKE_BIN_FILES.items():
        p = bin_dir / name
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)

    # uv keeps a cache under $HOME and `verify` shells out to things that touch
    # it; sealed outside the snapshot so "wrote nothing" is a claim about HSCC
    # state, not about uv (same reasoning as the doctor suite).
    cache = tmp_path / "xdg-cache"
    cache.mkdir()

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    monkeypatch.setenv("HSCC_HOME", str(hscc))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setenv("UV_CACHE_DIR", str(cache / "uv"))
    # Never let a leftover override name a real host.
    for var in ("HSCC_DIR", "HERMES_REAL_HOME"):
        monkeypatch.delenv(var, raising=False)
    return type("Sealed", (), {"home": home, "hscc": hscc, "bin": bin_dir,
                               "cache": cache,
                               "token": hscc / TOKEN_NAME})()


def _the_seal_actually_took():
    """Fail loudly if the sandbox is not what the child will read."""
    assert os.path.expanduser("~") != _REAL_HOME, (
        "HOME was not redirected — the verb would read the operator's live "
        f"{_REAL_HOME} and, if it mints, write a real credential there")
    assert os.path.expanduser("~/.hscc") != _REAL_HSCC, (
        f"~/.hscc still resolves to the operator's live state dir ({_REAL_HSCC})")


def _worktree_path_first(sealed, monkeypatch):
    """Make `hscc` on PATH mean THIS tree, for the inner `hscc api status` hop.

    `api_route_sweep.py:206` runs a bare ``hscc``. Left alone it resolves to the
    installed CLI, so a `verify` guard would be testing the deployed payload —
    which is exactly the shape of bug this card is about, so it must not be able
    to hide behind it.
    """
    shim = sealed.bin / "hscc"
    shim.write_text(f"#!/bin/sh\nexec {sys.executable} {HSCC_PY} \"$@\"\n",
                    encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{sealed.bin}{os.pathsep}{os.environ.get('PATH', '')}")
    return shim


def _run_cli(argv, timeout=180):
    """The real spawn of the real door, in the sealed environment."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    # HSCC_TEST_PY: verify picks its own child interpreter from this (verify.py
    # check_api_routes), so pin it to the one that can import the package.
    env["HSCC_TEST_PY"] = sys.executable
    for k in ("HSCC_NO_FLEET", "HSCC_OFFLINE", "HSCC_STUB_FLEET",
              "HSCC_SKIP_PROBE"):
        env[k] = "1"
    proc = subprocess.run([sys.executable, str(HSCC_PY), *argv],
                          capture_output=True, text=True, timeout=timeout,
                          cwd=str(REPO), env=env)
    return proc.returncode, proc.stdout, proc.stderr


# ── the invariant ───────────────────────────────────────────────────────────
class TestReadNeverMints:
    def test_api_status_creates_no_credential(self, sealed_home, monkeypatch):
        """`hscc api status` is advertised read-only; the snapshot is the proof.

        rc is deliberately NOT asserted first: what is being pinned is the
        absence of a write, and a verb that fails for an unrelated reason must
        not be allowed to pass by never reaching its own QR block. The run is
        checked to have reached the block instead (see the presence control).
        """
        _the_seal_actually_took()
        assert not sealed_home.token.exists(), (
            "the fixture planted a token — the assertion below would be vacuous")
        before = _snapshot(sealed_home.home)
        _rc, out, _err = _run_cli(["api", "status"])
        after = _snapshot(sealed_home.home)
        assert after == before, (
            "a status read wrote into the sealed home: "
            f"{sorted(set(after) ^ set(before))}")
        assert not sealed_home.token.exists(), (
            "`hscc api status` created the API bearer credential — a read must "
            "not mint (t_cc7b6023)")

    def test_verify_json_creates_no_credential(self, sealed_home, monkeypatch):
        """`hscc verify --json` inherits the mint through a CHILD process.

        This is the one an in-process guard structurally cannot see: verify's
        api_routes row spawns api_route_sweep.py, which spawns `hscc api status`.
        """
        _the_seal_actually_took()
        _worktree_path_first(sealed_home, monkeypatch)
        before = _snapshot(sealed_home.home)
        rc, out, _err = _run_cli(["verify", "--json"], timeout=300)
        after = _snapshot(sealed_home.home)
        assert not sealed_home.token.exists(), (
            "`hscc verify --json` created the API bearer credential through its "
            "`hscc api status` hop (t_cc7b6023)")
        assert after == before, (
            f"verify wrote into the sealed home: {sorted(set(after) ^ set(before))}")
        # Presence control: verify must have really run the api_routes row AND
        # really have had no host to work with, because the no-host branch is
        # what shells `hscc api status`. A payload whose api_routes row skipped
        # the sweep (or found a host) would satisfy both assertions above
        # without ever touching the audited code.
        row = [c for c in json.loads(out)["checks"] if c["name"] == "api_routes"]
        assert row, f"verify produced no api_routes row at all (rc={rc})"
        assert "hscc api status" in json.dumps(row), (
            "the api_routes row never took the no-host fallback that shells "
            f"`hscc api status`, so it never reached the audited path: {row}")

    def test_the_snapshot_bites(self, sealed_home):
        """Control: an assertion that nothing changed is worthless if nothing
        could change. Plant a file and require the snapshot to notice."""
        _the_seal_actually_took()
        before = _snapshot(sealed_home.home)
        sealed_home.token.write_text("canary\n", encoding="utf-8")
        assert _snapshot(sealed_home.home) != before, (
            "the snapshot cannot see a write — every assertion above proves "
            "nothing")

    def test_the_mint_is_reachable_in_this_sandbox(self, sealed_home):
        """Presence control: the seal does not simply PREVENT the write.

        Without this, "no token appeared" would be ambiguous between "the verb
        is innocent" and "the sandbox made creation impossible". ``load_token``
        — the very call that used to sit in the read path — must still be able
        to create the file here. If this ever stops being true, the guard above
        has silently stopped guarding anything.
        """
        _the_seal_actually_took()
        sys.path.insert(0, str(REPO / "hscc-api"))
        try:
            import api_server
            minted = api_server.load_token(str(sealed_home.hscc))
        finally:
            sys.path.pop(0)
        assert sealed_home.token.exists(), (
            "load_token could not create the token in this sandbox — the seal "
            "is blocking writes, so 'the read wrote nothing' proves nothing")
        assert minted and len(minted) > 20, "load_token returned no credential"

    def test_peek_token_reads_without_creating(self, sealed_home):
        """The read-side twin: None for an absent token, and still no file.

        The two verbs' behaviour is pinned by the snapshot tests above; this
        pins the seam itself, so a future `load_token` inside the read path
        fails here too even if the CLI shape moves.
        """
        _the_seal_actually_took()
        before = _snapshot(sealed_home.home)
        sys.path.insert(0, str(REPO / "hscc-api"))
        try:
            import api_server
            assert api_server.peek_token(str(sealed_home.hscc)) is None, (
                "peek_token invented a credential for an absent token file")
            assert api_server.load_token is not api_server.peek_token, (
                "peek_token must not be an alias of the minting loader")
        finally:
            sys.path.pop(0)
        assert _snapshot(sealed_home.home) == before, (
            "peek_token wrote into the sealed home — it must never create, "
            f"{sorted(set(_snapshot(sealed_home.home)) ^ set(before))}")

    def test_start_still_mints(self, sealed_home):
        """The other half of the verdict: moving the mint OUT of the read must
        not have moved it OUT of existence. `hscc api start` is the verb with
        operator intent, and the rest of the repo tells the operator to use it
        (notify_cli.py:334, verify.py's api_routes next_step).

        Runs the verb with a stubbed fork so no server is actually launched.
        """
        _the_seal_actually_took()
        assert not sealed_home.token.exists()
        code = (
            "import sys;"
            f"sys.path.insert(0, {str(REPO)!r});"
            "from hscc_daemon import api_cli as a;"
            "import os;"
            "a.os.fork = lambda: 0;"              # child branch: no real fork
            "a.daemon_ops.save_pid = lambda *x, **k: None;"
            "a._warn_payload_drift = lambda **k: None;"
            "a._serve = lambda *x, **k: None;"
            "raise SystemExit(a._handle_start(['--no-qr']))"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
        subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, timeout=120, cwd=str(REPO), env=env)
        assert sealed_home.token.exists(), (
            "`hscc api start` no longer creates the token — the fix moved the "
            "mint instead of gating it, and 'hscc notify list' can never be "
            "satisfied")


# ── the read that is supposed to work still works ────────────────────────────
class TestReadStillReads:
    def test_existing_token_still_yields_a_qr(self, sealed_home):
        """No-regression pin on the fix: when a token EXISTS, `status` still
        prints the QR. A fix that suppressed the QR to stop the mint would pass
        every test above and break the verb's actual feature."""
        _the_seal_actually_took()
        sealed_home.token.write_text("a-real-looking-token-value-abc123\n",
                                     encoding="utf-8")
        before = _snapshot(sealed_home.home)
        _rc, out, _err = _run_cli(["api", "status"])
        assert "Scan to connect" in out, (
            "the QR block stopped working for a host that HAS a token: "
            f"stdout={out[-400:]!r}")
        assert "No connection QR" not in out, (
            "status claims it could not read a token that is plainly present")
        assert _snapshot(sealed_home.home) == before, (
            "the token-present path wrote into the sealed home")

    def test_no_token_says_so_and_names_the_verb(self, sealed_home):
        """The message the docstring always promised, and it must point at the
        verb that creates the token rather than leaving the operator guessing."""
        _the_seal_actually_took()
        _rc, out, _err = _run_cli(["api", "status"])
        assert "No connection QR" in out, (
            f"silent about the missing token: {out[-400:]!r}")
        assert "api start" in out, (
            "the notice does not name the verb that creates the token: "
            f"{out[-400:]!r}")
        assert not sealed_home.token.exists()
