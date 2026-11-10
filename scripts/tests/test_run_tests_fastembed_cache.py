"""``scripts/run_tests.sh`` must pin fastembed's model cache out of the repo (t_317a0f30).

What is under test is a property of a SHELL SCRIPT: the runner exports
``FASTEMBED_CACHE_DIR`` for every leg. The provider pins the same variable at
import (``memori_byodb/_cache_env.py`` — the durable fix, since that covers
gateway, workers, cron and the CLI too), but a leg must not have to import the
provider to keep an 87 MB model out of the tree it is testing, and legs do reach
the memori SDK directly.

Why the runner specifically: ``run_tests.sh`` runs pytest with the repo root as
cwd, and ``~/dev/hscc/.fastembed_cache`` (88,984 KB) is sitting there as the
evidence that a suite can mint one.

Three claims, each pinned separately so a regression names itself:

  1. the shipped file carries the block, in the operator-wins ``${VAR:-default}``
     form (a text check, so editing the invocation cannot quietly drop it — the
     same technique as ``test_run_tests_harness_env.py``);
  2. the block, EXECUTED under bash against a tmp ``$HOME``, exports the shared
     dir and creates it — a block nobody can run is documentation;
  3. an operator's pre-set value survives the block untouched (the ``:-`` is not
     decorative), and the whole thing disappears harmlessly when ``$HOME`` is
     unset rather than exporting a path like ``/.cache/fastembed``.

Hermetic: tmp dirs and ``bash`` only. Nothing here imports memori, touches the
network, or reads the operator's real cache dir.
"""
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
RUNNER = SCRIPTS / "run_tests.sh"

ENV_VAR = "FASTEMBED_CACHE_DIR"
BLOCK_START = "# ━━━ one fastembed model cache per host (t_317a0f30)"


def _block() -> str:
    """The shipped block, from its banner comment through its closing ``fi``."""
    text = RUNNER.read_text(encoding="utf-8")
    start = text.index(BLOCK_START)
    end = text.index("\nfi\n", start) + len("\nfi\n")
    return text[start:end]


def _run(block: str, env: dict) -> subprocess.CompletedProcess:
    # Built by concatenation, not an f-string: `${VAR:-<unset>}` is shell syntax
    # and Python reads `:-<unset>` as a format specifier.
    probe = "printf '%s' \"${" + ENV_VAR + ":-<unset>}\""
    return subprocess.run(
        ["bash", "-c", f"{block}\n{probe}"],
        capture_output=True, text=True, env=env, timeout=60,
    )


# ── 1. the shipped text ───────────────────────────────────────────────────────


def test_runner_exports_the_cache_dir_in_the_operator_wins_form():
    block = _block()

    assert f'{ENV_VAR}:-$HOME/.cache/fastembed' in block, (
        "the export must use ${VAR:-default} so an operator's own value wins")
    assert f'mkdir -p "${ENV_VAR}"' in block, (
        "the dir must be created by the runner: fastembed creates the leaf, not "
        "the intermediate parents, and a leg must not race on it")


def test_block_is_guarded_on_home():
    """With no HOME, `:-$HOME/.cache/...` would export `/.cache/...` — refused."""
    assert 'if [ -n "${HOME:-}" ]' in _block()


# ── 2. the block, executed ────────────────────────────────────────────────────


def test_block_pins_and_creates_the_shared_dir(tmp_path):
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}

    proc = _run(_block(), env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == str(tmp_path / ".cache" / "fastembed")
    assert (tmp_path / ".cache" / "fastembed").is_dir()


def test_block_creates_the_dir_even_when_the_parent_is_missing(tmp_path):
    """The default's parent does not exist on a clean host — mkdir -p is load-bearing."""
    home = tmp_path / "fresh-home"
    home.mkdir()

    proc = _run(_block(), {"HOME": str(home), "PATH": "/usr/bin:/bin"})
    assert proc.returncode == 0, proc.stderr
    assert (home / ".cache" / "fastembed").is_dir()


# ── 3. operator value and no-HOME behaviour ──────────────────────────────────


def test_operator_value_survives_the_block(tmp_path):
    operator_dir = tmp_path / "volumes" / "nas" / "fastembed"
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", ENV_VAR: str(operator_dir)}

    proc = _run(_block(), env)
    assert proc.returncode == 0, proc.stderr

    # Non-vacuity: the default it did NOT take is a different path.
    assert proc.stdout == str(operator_dir)
    assert proc.stdout != str(tmp_path / ".cache" / "fastembed")


def test_block_exports_nothing_without_a_home(tmp_path):
    proc = _run(_block(), {"PATH": "/usr/bin:/bin", "PWD": str(tmp_path)})

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "<unset>", "no HOME must not produce a root-level cache path"
