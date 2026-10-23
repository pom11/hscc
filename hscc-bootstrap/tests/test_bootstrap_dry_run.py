"""`bootstrap.sh --dry-run` — fabricated-tree proof (card t_bc5a3fee).

The card's contract: build a FAKE libexec layout in tmp_path, run bootstrap.sh
with `--dry-run`, and assert ZERO filesystem writes outside the (empty)
expected set. Every writable destination is redirected into tmp_path via the
env the installer already honours — HERMES_HOME, HSCC_DIR, HERMES_SCRIPTS,
RECIPES_DIR — and PATH is narrowed to the sandbox + /usr/bin:/bin so no stage
can reach the operator's real tools.

A fake `sparkrun` stub answers preflight + detection with the documented
placeholder cluster (10.0.0.x — never a real host); the Hermes venv is
symlinked as a DIRECTORY (same trick as test_bootstrap_isolated) so the real
interpreter runs the helpers.

The brew-libexec detection is pinned BOTH ways: marker file, and the
`libexec/hscc-*` path shape without it.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO / "hscc-bootstrap" / "bootstrap.sh"
REAL_VENV_PY = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
REAL_HERMES_AGENT = Path.home() / ".hermes" / "hermes-agent"

needs_runtime = pytest.mark.skipif(
    not REAL_VENV_PY.is_file(),
    reason="needs the real Hermes venv interpreter to run the installer")

# What a real brew tap extracts (minus docs/assets — enough for every stage to
# resolve; the dry-run must never READ payload contents anyway).
TREE_DIRS = ("hscc-bootstrap", "hscc_daemon", "hscc-cli", "hscc-cluster",
             "hscc-roles", "hscc-skills")

FAKE_CLUSTER = json.dumps([
    {"name": "fabricated", "hosts": ["10.0.0.1"], "user": "op",
     "cache_dir": "", "default": True}])


def _make_tree(tmp_path, *, brew: bool, with_cli_pkg: bool = True) -> Path:
    """Fabricated source root: copy the repo dirs into libexec/hscc-<ver>/.

    brew=True also drops the marker file the tap writes at the tree root.
    Non-brew trees sit OUTSIDE any libexec/Cellar path shape so the fallback
    heuristic cannot fire on them.

    with_cli_pkg=False drops hscc-cli from the copy: the real-mode shim-skip
    test needs the tree to carry NO installable package, so even a regression
    that failed to skip the stage would fail harmlessly (uv against a missing
    source dir) instead of writing a console script into the sandboxed —
    symlinked — real Hermes venv.
    """
    if brew:
        root = tmp_path / "opt" / "libexec" / "hscc-9.9.9"
    else:
        root = tmp_path / "checkout"
    for d in TREE_DIRS:
        if d == "hscc-cli" and not with_cli_pkg:
            continue
        shutil.copytree(REPO / d, root / d,
                        ignore=shutil.ignore_patterns("__pycache__",
                                                      ".pytest_cache"))
    if brew:
        (root / ".hscc-brew-libexec").write_text("fabricated\n")
    (root / "VERSION").write_text("9.9.9\n")
    return root


def _make_home(tmp_path):
    """Sandbox home: HERMES_HOME/HSCC_DIR/HERMES_SCRIPTS + fake sparkrun."""
    home = tmp_path / "home"
    hermes = home / ".hermes"
    (hermes / "hermes-agent").mkdir(parents=True)
    (hermes / "hermes-agent" / "venv").symlink_to(REAL_HERMES_AGENT / "venv")
    hscc = home / ".hscc"
    hscc.mkdir()
    binp = home / "bin"
    binp.mkdir()
    stub = binp / "sparkrun"
    stub.write_text("#!/bin/sh\n"
                    'if [ "$1" = cluster ] && [ "$2" = list ]; then\n'
                    f"  printf '%s' '{FAKE_CLUSTER}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "exit 0\n")
    stub.chmod(0o755)
    return {"home": home, "hermes": hermes, "hscc": hscc, "bin": binp}


def _run_dry(tree, sandbox, extra=()):
    env = {k: v for k, v in os.environ.items()
           if k not in ("HERMES_HOME", "HSCC_DIR", "HERMES_SCRIPTS",
                        "RECIPES_DIR", "HERMES_KANBAN_TASK",
                        "HERMES_KANBAN_WORKSPACE",
                        "HERMES_DELEGATED_CHILD_CONTEXT", "HSCC_BOOTSTRAP_SH")}
    env.update({
        "HERMES_HOME": str(sandbox["hermes"]),
        "HSCC_DIR": str(sandbox["hscc"]),
        "HERMES_SCRIPTS": str(sandbox["home"] / "scripts"),
        "RECIPES_DIR": str(sandbox["home"] / "recipes"),
        # Narrow PATH: sandbox bin (fake sparkrun) + system tools only — no
        # /opt/homebrew (tmux absent => that stage takes its report-only path),
        # no user dirs that could resolve the operator's real sparkrun/uv.
        "PATH": f'{sandbox["bin"]}:/usr/bin:/bin',
    })
    # Run the COPY inside the fabricated tree — that is the whole point:
    # REPO_ROOT must resolve to the libexec copy for the brew heuristic to be
    # exercised (running the live repo's script would test the wrong root).
    script = tree / "hscc-bootstrap" / "bootstrap.sh"
    assert script.is_file(), f"fabricated tree has no bootstrap.sh: {script}"
    return subprocess.run(
        ["bash", str(script), "--yes", "--dry-run", *extra],
        capture_output=True, text=True, env=env, timeout=300,
        cwd=str(sandbox["home"]))


def _snapshot(root: Path):
    """Every path under root WITHOUT following symlinks (os.walk default):
    the sandbox contains a symlink to the real Hermes venv and must never
    walk it (slow, and a real-venv write would be hidden by the walk)."""
    out = set()
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            out.add(str(Path(dirpath, name).relative_to(root)))
    return out


@needs_runtime
def test_dry_run_from_brew_libexec_tree_writes_nothing(tmp_path):
    tree = _make_tree(tmp_path, brew=True)
    sandbox = _make_home(tmp_path)
    before_h = _snapshot(sandbox["hermes"])
    before_s = _snapshot(sandbox["hscc"])

    r = _run_dry(tree, sandbox)
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"dry run failed:\n{out[-3000:]}"
    assert "DRY RUN complete" in out, f"no dry-run summary:\n{out[-2000:]}"
    assert "NOTHING was installed" in out

    # Every planned write is printed: the card names skills, role profiles,
    # ~/.hscc state + serving.json, plugins payload, daemon, CLI shim.
    plans = [ln for ln in out.splitlines() if "→ plan:" in ln]
    blob = "\n".join(plans)
    for needle in ("plugin payload", "CLI shim", "watchdog scripts",
                   "skills", "role profiles", "orchestrators", "serving.json",
                   "daemon", "trigger rules"):
        assert needle in blob, f"no planned-write line for {needle!r}:\n{blob}"

    # And ZERO writes: the sandbox home trees are byte-identical to before.
    assert _snapshot(sandbox["hermes"]) == before_h, \
        "dry run wrote under HERMES_HOME"
    assert _snapshot(sandbox["hscc"]) == before_s, \
        "dry run wrote under HSCC_DIR"
    # No console script anywhere under the sandbox or the fabricated tree.
    def _names(root, target):
        hits = []
        for dirpath, dirnames, filenames in os.walk(root):
            if target in dirnames or target in filenames:
                hits.append(str(Path(dirpath, target).relative_to(root)))
        return hits
    assert _names(tmp_path / "home", "hscc") == [], "shim appeared in sandbox"
    # The dry-run summary must not claim a real install.
    assert "HSCC installed." not in out

    # Brew libexec => the shim stage plans a SKIP, naming brew.
    assert "brew libexec" in blob and "SKIPPED" in blob


@needs_runtime
def test_dry_run_from_plain_tree_plans_the_cli_shim(tmp_path):
    """Non-brew path keeps the shim behaviour: the plan says install, not
    skip — the brew skip must not leak into git-checkout installs."""
    tree = _make_tree(tmp_path, brew=False)
    sandbox = _make_home(tmp_path)
    before_h = _snapshot(sandbox["hermes"])
    before_s = _snapshot(sandbox["hscc"])

    r = _run_dry(tree, sandbox)
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"dry run failed:\n{out[-3000:]}"
    plans = "\n".join(ln for ln in out.splitlines() if "→ plan:" in ln)
    assert "CLI shim: install hscc-cli" in plans, plans
    assert _snapshot(sandbox["hermes"]) == before_h
    assert _snapshot(sandbox["hscc"]) == before_s


@needs_runtime
def test_dry_run_path_heuristic_fires_without_the_marker(tmp_path):
    """Detection must also work on the bare `libexec/hscc-*` path shape (a tap
    that ships without the marker file)."""
    tree = _make_tree(tmp_path, brew=True)
    (tree / ".hscc-brew-libexec").unlink()
    sandbox = _make_home(tmp_path)
    r = _run_dry(tree, sandbox)
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"dry run failed:\n{out[-3000:]}"
    assert "CLI shim: SKIPPED — brew libexec install" in out, out[-2000:]


@needs_runtime
def test_dry_run_respects_skip_flags_and_stays_idempotent(tmp_path):
    """--dry-run composes with the existing flags and is safe to re-run."""
    tree = _make_tree(tmp_path, brew=True)
    sandbox = _make_home(tmp_path)
    before_h = _snapshot(sandbox["hermes"])
    before_s = _snapshot(sandbox["hscc"])
    first = _run_dry(tree, sandbox,
                     extra=("--skip-cli", "--skip-daemon", "--skip-skills"))
    assert first.returncode == 0
    assert "CLI shim: skipped (--skip-cli)" in first.stdout
    assert "daemon:" not in "\n".join(
        ln for ln in first.stdout.splitlines()
        if "→ plan:" in ln and "launchd" in ln)
    second = _run_dry(tree, sandbox,
                      extra=("--skip-cli", "--skip-daemon", "--skip-skills"))
    assert second.returncode == 0
    assert _snapshot(sandbox["hermes"]) == before_h
    assert _snapshot(sandbox["hscc"]) == before_s


@needs_runtime
def test_dry_run_is_orthogonal_to_existing_sandbox_isolation(tmp_path):
    """Even a dry run that dies in a later stage must never have written:
    corrupt the cluster stub so detect.py fails the preflight gate."""
    tree = _make_tree(tmp_path, brew=True)
    sandbox = _make_home(tmp_path)
    stub = sandbox["bin"] / "sparkrun"
    stub.write_text("#!/bin/sh\nexit 1\n")
    stub.chmod(0o755)
    before_h = _snapshot(sandbox["hermes"])
    before_s = _snapshot(sandbox["hscc"])
    r = _run_dry(tree, sandbox)
    assert r.returncode != 0  # the hard gate still gates in dry-run mode
    assert _snapshot(sandbox["hermes"]) == before_h
    assert _snapshot(sandbox["hscc"]) == before_s


# ── real (non-dry) mode: the libexec-aware shim skip ─────────────────────────

def _run_real(tree, sandbox):
    """Real-mode run with every stage that would do heavy or fleet-touching
    work skipped, so ONLY the payload copy + the CLI-shim decision execute.

    The tree deliberately has no hscc-cli package (with_cli_pkg=False): the
    non-brew control must fail the shim stage harmlessly — `uv` is not even on
    the narrowed PATH, so install_cli returns 'command not found' rather than
    touching any interpreter. Nothing here installs anything for real.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("HERMES_HOME", "HSCC_DIR", "HERMES_SCRIPTS",
                        "RECIPES_DIR", "HERMES_KANBAN_TASK",
                        "HERMES_KANBAN_WORKSPACE",
                        "HERMES_DELEGATED_CHILD_CONTEXT", "HSCC_BOOTSTRAP_SH")}
    env.update({
        "HERMES_HOME": str(sandbox["hermes"]),
        "HSCC_DIR": str(sandbox["hscc"]),
        "HERMES_SCRIPTS": str(sandbox["home"] / "scripts"),
        "RECIPES_DIR": str(sandbox["home"] / "recipes"),
        "PATH": f'{sandbox["bin"]}:/usr/bin:/bin',
    })
    script = tree / "hscc-bootstrap" / "bootstrap.sh"
    return subprocess.run(
        ["bash", str(script), "--yes", "--skip-skills", "--skip-roles",
         "--skip-daemon", "--skip-patches"],
        capture_output=True, text=True, env=env, timeout=300,
        cwd=str(sandbox["home"]))


@needs_runtime
def test_brew_tree_skips_the_shim_and_completes(tmp_path):
    """Non-dry run from a brew libexec tree: the shim stage prints WHY it
    skips and the installer still completes (rc 0) — the stage is skipped,
    not fatal."""
    tree = _make_tree(tmp_path, brew=True, with_cli_pkg=False)
    sandbox = _make_home(tmp_path)
    r = _run_real(tree, sandbox)
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"brew install run failed:\n{out[-3000:]}"
    assert "CLI shim skipped — brew libexec install" in out, out[-2000:]
    assert "hscc CLI install FAILED" not in out
    # The payload copy still ran (the skip is scoped to the shim only).
    assert (sandbox["hermes"] / "plugins" / "hscc_daemon").is_dir()


@needs_runtime
def test_non_brew_tree_still_runs_the_shim_stage(tmp_path):
    """Control: a non-brew tree takes the CURRENT behavior — the shim stage
    attempts the install (here: fails, because uv is not on PATH — which
    HARD-STOPS the installer exactly as before). The skip did not leak."""
    tree = _make_tree(tmp_path, brew=False, with_cli_pkg=False)
    sandbox = _make_home(tmp_path)
    r = _run_real(tree, sandbox)
    out = r.stdout + r.stderr
    assert r.returncode != 0, "non-brew run must still attempt (and fail) the shim"
    assert "hscc CLI install FAILED" in out, out[-2000:]
    assert "CLI shim skipped — brew libexec install" not in out


# ── e2e: `hscc bootstrap --dry-run` where hscc runs from libexec/venv ───────

@needs_runtime
def test_hscc_bootstrap_verb_from_libexec_venv(tmp_path):
    """The card's headline scenario, end to end:

      * a brew-style prefix whose payload lives under ``libexec/hscc-<ver>/``;
      * the RUNNING `hscc` comes from a venv INSIDE libexec — simulated by
        copying hscc_daemon into ``libexec/venv/lib/python3.11/site-packages``
        and running the venv's python with that dir on PYTHONPATH, so the
        dispatcher's own ``__file__`` resolves inside the venv tree;
      * ``hscc bootstrap --dry-run`` must resolve the bootstrap.sh INSIDE the
        versioned libexec tree, pass the flag through, complete, and write
        nothing (brew shim stage skipped, dry-run summary present).
    """
    tree = _make_tree(tmp_path, brew=True)
    sandbox = _make_home(tmp_path)
    site = (tmp_path / "opt" / "libexec" / "venv" / "lib"
            / "python3.11" / "site-packages")
    site.mkdir(parents=True)
    shutil.copytree(REPO / "hscc_daemon", site / "hscc_daemon",
                    ignore=shutil.ignore_patterns("__pycache__",
                                                  ".pytest_cache", "tests"))
    before_h = _snapshot(sandbox["hermes"])
    before_s = _snapshot(sandbox["hscc"])

    env = {k: v for k, v in os.environ.items()
           if k not in ("HERMES_HOME", "HSCC_DIR", "HERMES_SCRIPTS",
                        "RECIPES_DIR", "HERMES_KANBAN_TASK",
                        "HERMES_KANBAN_WORKSPACE",
                        "HERMES_DELEGATED_CHILD_CONTEXT", "HSCC_BOOTSTRAP_SH",
                        "PYTHONPATH")}
    env.update({
        "HERMES_HOME": str(sandbox["hermes"]),
        "HSCC_DIR": str(sandbox["hscc"]),
        "HERMES_SCRIPTS": str(sandbox["home"] / "scripts"),
        "RECIPES_DIR": str(sandbox["home"] / "recipes"),
        "PATH": f'{sandbox["bin"]}:/usr/bin:/bin',
        "PYTHONPATH": str(site),
        "NO_COLOR": "1", "TERM": "dumb",
    })
    r = subprocess.run(
        [str(REAL_VENV_PY), "-m", "hscc_daemon.hscc", "bootstrap", "--dry-run"],
        capture_output=True, text=True, env=env, timeout=300,
        cwd=str(sandbox["home"]))
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"hscc bootstrap --dry-run failed:\n{out[-3000:]}"
    # Proved the verb ran the copy INSIDE the libexec tree (not the repo's):
    assert str(tree) in out, "verb resolved a bootstrap.sh outside the libexec tree"
    assert "DRY RUN complete" in out
    assert "CLI shim: SKIPPED — brew libexec install" in out
    assert _snapshot(sandbox["hermes"]) == before_h
    assert _snapshot(sandbox["hscc"]) == before_s
