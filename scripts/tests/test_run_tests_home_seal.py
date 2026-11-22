"""``scripts/run_tests.sh`` must seal ``HOME`` for EVERY leg (t_2b2a6c3b).

WHAT THIS PINS, and why the runner rather than a fixture. A leg that resolves a
default path through ``~`` reads the operator's machine: it passes locally and is
red — or worse, silently SKIPPED — on a runner whose HOME is empty. That class hid
#593, and measured at ``47e7f0d0``/``96a9f38a`` it produced two roster deltas:
``hscc-project`` red (``UnknownProjectError: unknown project 'hscc'``, reached from
a test) and ``hscc-cluster``'s two sparkrun source cross-checks turning into skips
reasoned ``no sparkrun checkout at <sealed-home>/sparkrun/src/sparkrun``. The
``hscc-roles`` leg has its own conftest seal (PR #595, ``ca2aa8d2``); the eleven
others have none, and only the runner reaches every leg CHILD — the same argument
that puts the kanban-pin scrub and ``HSCC_SKIP_COMPACT_PROBE`` here (a
``monkeypatch`` does not cross a ``subprocess``, and half of these cases spawn one).

Six claims, each pinned separately so a regression names itself:

  1. the shipped text carries the block, and it sits AFTER the two lines that
     derive values from ``HOME`` (``HSCC_TEST_PY``, ``FASTEMBED_CACHE_DIR``) and
     BEFORE the leg loop — a seal placed too early moves the runner's own
     interpreter path, and one placed too late is not seen by any leg;
  2. executed under bash, the block seals ``HOME``+``USERPROFILE`` to a FRESH EMPTY
     dir under ``/tmp`` and says so on stdout (the log has to prove its posture);
  3. ``HSCC_TEST_NO_SEAL=1`` restores the operator's HOME — the escape hatch has to
     be one env var, or a developer edits the runner instead;
  4. a sealed home INSIDE the login home is REFUSED (rc 3), and so is an inability
     to create one — the runner fails CLOSED rather than running the roster
     unsealed while claiming the opposite;
  5. ``TMPDIR`` pointing inside the login home does not move the seal (macOS puts
     TMPDIR under ``/var/folders/…``; honouring it would be a second answer, and a
     HOME containing ``/Users`` keeps a ``'/Users' in argv`` escape hatch green);
  6. ``SPARKRUN_SRC`` is DECLARED from the machine's own checkout when the operator
     did not set one, and an operator's value always wins — the runner exports that
     one path and never the home, so the coverage of hscc-cluster's source
     cross-checks survives the seal without the seal becoming a suggestion.

Hermetic: tmp dirs, a ``mktemp`` shim on ``PATH``, and ``bash`` only. Nothing here
imports a plugin or reads the operator's real home.
"""
import os
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
RUNNER = SCRIPTS / "run_tests.sh"

BLOCK_START = "# ━━━ roster-wide HOME seal for every leg (t_2b2a6c3b)"
PROBE = ("printf 'HOME=%s\\nUSERPROFILE=%s\\nSPARKRUN_SRC=%s\\nNOSEAL=%s\\n' "
         '"${HOME:-<unset>}" "${USERPROFILE:-<unset>}" '
         '"${SPARKRUN_SRC:-<unset>}" "${HSCC_TEST_NO_SEAL:-<unset>}"')


def _block() -> str:
    """The shipped block: banner through its UNINDENTED closing ``fi``."""
    text = RUNNER.read_text(encoding="utf-8")
    start = text.index(BLOCK_START)
    end = text.index("\nfi\n", start) + len("\nfi\n")
    return text[start:end]


def _run(block: str, env: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    """Run the block with ``HOME`` = a fake login home under *tmp_path*."""
    return subprocess.run(["bash", "-c", block + "\n" + PROBE],
                          capture_output=True, text=True, env=env, timeout=60)


def _home(tmp_path) -> Path:
    home = tmp_path / "login-home"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _env(tmp_path, **extra) -> dict:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": str(_home(tmp_path)),
           "TMPDIR": str(tmp_path / "tmpdir")}
    env.update({k: v for k, v in extra.items() if v is not None})
    return env


# ── 1. the shipped text, and WHERE it sits ────────────────────────────────────

def test_runner_seals_home_and_userprofile_for_every_leg():
    block = _block()
    assert 'mktemp -d /tmp/hscc-suite-home-XXXXXX' in block, (
        "the empty home must be created under /tmp explicitly — `$TMPDIR` is a "
        "second answer to 'where is a CI-shaped home', and a HOME containing "
        "`/Users` keeps a '/Users' in argv escape hatch green")
    assert 'export HOME="$SUITE_HOME"' in block
    assert 'export USERPROFILE="$SUITE_HOME"' in block, (
        "USERPROFILE seals too: pathlib/expanduser readers on a Windows host, and "
        "the posture the CI comparison used both variables")


def test_the_seal_sits_after_every_home_derived_line_and_before_the_leg_loop():
    """Order is the property, not decoration.

    ``PY=${HSCC_TEST_PY:-$HOME/.hermes/…}`` and the FASTEMBED block read HOME; seal
    earlier and the runner loses its own interpreter. The leg loop is where children
    are spawned; seal after it and no leg ever sees the seal.
    """
    text = RUNNER.read_text(encoding="utf-8")
    seal = text.index(BLOCK_START)
    for marker, why in (
        ('HSCC_TEST_PY:-$HOME', "the interpreter path derives from HOME"),
        ('FASTEMBED_CACHE_DIR="${FASTEMBED_CACHE_DIR:-$HOME', "the model cache derives from HOME"),
    ):
        assert text.index(marker) < seal, f"the seal moved before a line where {why}"
    assert seal < text.index('for d in "${LEGS[@]}"'), (
        "the seal must precede the leg loop — a leg child spawned before it runs "
        "with the operator's HOME and the roster log still says it was sealed")


# ── 2. executed: what a sealed leg actually gets ──────────────────────────────

def test_executing_the_block_seals_home_to_an_empty_dir_under_tmp(tmp_path):
    r = _run(_block(), _env(tmp_path), tmp_path)
    assert r.returncode == 0, r.stderr
    values = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    sealed = Path(values["HOME"])
    assert str(sealed).startswith("/tmp/hscc-suite-home-"), values
    assert sealed.is_dir() and not any(sealed.iterdir()), \
        "the sealed home must be EMPTY — a seeded home is the posture the class hides"
    assert values["USERPROFILE"] == str(sealed)
    assert str(_home(tmp_path)) != str(sealed)
    assert str(_home(tmp_path)) not in str(sealed)
    assert "HOME seal:" in r.stdout, "the log has to prove its own posture"


# ── 3. the escape hatch ───────────────────────────────────────────────────────

def test_no_seal_env_var_restores_the_operator_home(tmp_path):
    r = _run(_block(), _env(tmp_path, HSCC_TEST_NO_SEAL="1"), tmp_path)
    assert r.returncode == 0, r.stderr
    assert f"HOME={_home(tmp_path)}" in r.stdout, (
        "HSCC_TEST_NO_SEAL=1 must restore HOME — otherwise a developer edits the "
        "runner to debug one HOME-dependent case")
    assert "DISABLED" in r.stdout


# ── 4. fail CLOSED, both ways ─────────────────────────────────────────────────

def test_a_sealed_home_inside_the_login_home_is_refused(tmp_path):
    """The ``t_d39c720e`` rule, executed: a home under the login home is not sealed.

    A ``mktemp`` shim on PATH hands back a path inside the fake login home, which is
    what a mis-set TMPDIR/`-p` argument would do on a real host.
    """
    home = _home(tmp_path)
    shim = tmp_path / "shim"
    shim.mkdir()
    # A `mktemp -d` shim that CREATES and echoes a path inside the login home — that
    # is what a mis-set TMPDIR/`-p` argument produces on a real host. (A shim that
    # only echoed it fell into the "cannot create" arm first, which is the fail-closed
    # check below, not this one — measured on the first draft of this file.)
    (shim / "mktemp").write_text(
        '#!/bin/sh\n'
        f'd="{home}/hscc-suite-home-FAKE"\n'
        'mkdir -p "$d"\n'
        'echo "$d"\n')
    (shim / "mktemp").chmod(0o755)
    env = _env(tmp_path)
    env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"
    r = _run(_block(), env, tmp_path)
    assert r.returncode == 3, f"expected the refusal rc 3, got {r.returncode}: {r.stdout}{r.stderr}"
    assert "refusing a sealed HOME inside the login home" in r.stderr
    assert "HOME=" not in r.stdout, (
        "the probe must never run — a refusal that still exported leaves the "
        "leg children with the path this check just refused")


def test_a_home_that_cannot_be_created_stops_the_run_instead_of_running_unsealed(tmp_path):
    """No /tmp, no roster: rc 3 before leg 1, not a green run with the wrong posture."""
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "mktemp").write_text('#!/bin/sh\nexit 1\n')
    (shim / "mktemp").chmod(0o755)
    env = _env(tmp_path)
    env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"
    r = _run(_block(), env, tmp_path)
    assert r.returncode == 3, f"expected rc 3 (fail closed), got {r.returncode}: {r.stdout}"
    assert "refusing to run the roster unsealed" in r.stderr


# ── 5. TMPDIR cannot move the seal ────────────────────────────────────────────

def test_tmpdir_inside_the_login_home_does_not_move_the_seal(tmp_path):
    """Honouring ``TMPDIR`` would put a `/Users`-shaped token back in the posture.

    The runner uses ``/tmp`` literally, so a host whose TMPDIR sits under the login
    home still seals to a path that no ``'/Users' in argv`` escape hatch can match.
    """
    home = _home(tmp_path)
    inside = home / "tmpdir"
    inside.mkdir(parents=True, exist_ok=True)
    env = _env(tmp_path)
    env["TMPDIR"] = str(inside)
    r = _run(_block(), env, tmp_path)
    assert r.returncode == 0, r.stderr
    values = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    assert values["HOME"].startswith("/tmp/hscc-suite-home-"), values
    assert str(home) not in values["HOME"]
    assert "/Users" not in values["HOME"], \
        "the sealed home must not contain the /Users token (t_d24a05ea's rule)"


# ── 6. SPARKRUN_SRC: declared, never discovered from under the seal ───────────

def test_the_machine_own_sparkrun_checkout_is_declared(tmp_path):
    """Without this, the seal would retire hscc-cluster's two source cross-checks.

    The runner exports the ONE path the cross-checks need — not the home — so the
    seam (PR #604) keeps running them and a skip still means "no checkout".
    """
    home = _home(tmp_path)
    (home / "sparkrun" / "src" / "sparkrun" / "runtimes").mkdir(parents=True)
    r = _run(_block(), _env(tmp_path), tmp_path)
    assert r.returncode == 0, r.stderr
    values = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    assert values["SPARKRUN_SRC"] == str(home / "sparkrun" / "src" / "sparkrun"), values
    assert "declared SPARKRUN_SRC=" in r.stdout, "the declaration belongs in the log"


def test_an_operators_sparkrun_src_always_wins(tmp_path):
    home = _home(tmp_path)
    (home / "sparkrun" / "src" / "sparkrun").mkdir(parents=True)
    chosen = tmp_path / "my-checkout" / "src" / "sparkrun"
    chosen.mkdir(parents=True)
    r = _run(_block(), _env(tmp_path, SPARKRUN_SRC=str(chosen)), tmp_path)
    assert r.returncode == 0, r.stderr
    assert f"SPARKRUN_SRC={chosen}" in r.stdout, (
        "the operator's pin wins — the runner declares only when nobody has spoken")


def test_no_checkout_means_no_declaration(tmp_path):
    """On a machine without one (CI) nothing is exported, so the leg skips honestly."""
    r = _run(_block(), _env(tmp_path), tmp_path)
    assert r.returncode == 0, r.stderr
    assert "SPARKRUN_SRC=<unset>" in r.stdout, (
        "inventing a path that does not exist would turn an honest 'no checkout' "
        "skip into a FileNotFoundError at the first read")


def test_agent_source_declared_only_when_the_tree_is_there(tmp_path):
    """`HERMES_AGENT_PATH` is the seam the skip reason itself names.

    Cases that read the live `hermes_cli` / kanban board (measured:
    `hscc-project/tests/test_mcp_server.py:176`, three `hscc_daemon` cases) resolve
    `~/.hermes/hermes-agent` by default; the seal removes the DEFAULT, not the
    capability, and the reason literally says "Set HERMES_AGENT_PATH if it lives
    elsewhere" — so the runner declares the machine's tree. Path, never home.
    """
    home = _home(tmp_path)
    (home / ".hermes" / "hermes-agent" / "hermes_cli").mkdir(parents=True)
    r = _run(_block(), _env(tmp_path), tmp_path)
    assert r.returncode == 0, r.stderr
    assert f"HERMES_AGENT_PATH={home}/.hermes/hermes-agent" in r.stdout

    # An operator's own value wins, exactly as for SPARKRUN_SRC.
    mine = tmp_path / "someone-elses-agent"
    mine.mkdir()
    r = _run(_block(), _env(tmp_path, HERMES_AGENT_PATH=str(mine)), tmp_path)
    assert r.returncode == 0, r.stderr
    assert "HERMES_AGENT_PATH" not in r.stdout, (
        "declaring over an operator's pin would point a deliberate run at a tree "
        "they did not choose")

    # And a host with no agent tree gets no declaration — never a phantom path.
    r = _run(_block(), _env(tmp_path / "bare"), tmp_path)
    assert r.returncode == 0, r.stderr
    assert "HERMES_AGENT_PATH" not in r.stdout, (
        "a declared path that does not exist turns an honest skip into a crash")


def test_tap_checkout_declared_from_the_documented_seam(tmp_path):
    """Third instance of the rule: `HSCC_TAP_DIR` (scripts/bump_formula.py:529).

    The `scripts` leg's formula-drift guard resolves its tap through
    `--tap | $HSCC_TAP_DIR | ~/dev/homebrew-hscc`, so a seal retires FOUR cases unless
    the runner declares the machine's tap. Measured at 96a9f38a: 601 passed/5 skipped ->
    598 passed/8 skipped, reason `Set HSCC_TAP_DIR to a pom11/homebrew-hscc checkout`.
    """
    home = _home(tmp_path)
    (home / "dev" / "homebrew-hscc" / "Formula").mkdir(parents=True)
    (home / "dev" / "homebrew-hscc" / "Formula" / "hscc.rb").write_text("class Hscc < Formula\nend\n")
    r = _run(_block(), _env(tmp_path), tmp_path)
    assert r.returncode == 0, r.stderr
    assert f"HSCC_TAP_DIR={home}/dev/homebrew-hscc" in r.stdout

    # Operator's value wins; and a bare home must not get a phantom tap dir.
    mine = tmp_path / "my-tap"
    mine.mkdir()
    r = _run(_block(), _env(tmp_path, HSCC_TAP_DIR=str(mine)), tmp_path)
    assert r.returncode == 0, r.stderr
    assert "HSCC_TAP_DIR" not in r.stdout
    r = _run(_block(), _env(tmp_path / "bare"), tmp_path)
    assert r.returncode == 0, r.stderr
    assert "HSCC_TAP_DIR" not in r.stdout, (
        "declaring a tap that is not there would turn the guard's honest 'unreachable' "
        "skip into a FileNotFoundError in a test that is supposed to guard a formula")
