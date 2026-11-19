"""End-to-end bootstrap for the ONE main orchestrator (M6).

The unit suites pin each piece (name interview, merged SOUL + migration, board
pointer, pane start, daemon wake-ups). This file drives the REAL installer in a
throwaway HERMES_HOME / HSCC_DIR with a fake sparkrun fleet, so the pieces are
proven to meet in the shipped order:

  * fresh ``--yes`` install on a host where Hermes has run once -> profile
    ``general``, board ``general`` current, the four daemon wake-ups visible
    through ``hscc main wakeups list``;
  * a first install where Hermes has NEVER run -> the board stage defers, the
    home stays config-less and the wiring stage reports NOT WIRED (t_6f4895aa);
  * fresh install with a custom name -> the same, named by the answer;
  * upgrade from an install carrying ``general-orch`` + ``orchestrator`` +
    the legacy ``default`` board -> nothing deleted, both SOULs retired, the
    resolver returns the main name, and a second bootstrap is a no-op.

No fleet is contacted: the cluster is a ``sparkrun`` stub answering the
documented placeholder hosts (192.0.2.x). Node-recovery stages are skipped, and
the payload copy, CLI install, daemon and patch stages are skipped exactly as
the existing isolated-install suite does — this test is about the main
orchestrator's install path, not about touching the operator's machine.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
REAL_VENV_PY = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
REAL_HERMES_AGENT = Path.home() / ".hermes" / "hermes-agent"

needs_runtime = pytest.mark.skipif(
    not REAL_VENV_PY.is_file(),
    reason="needs the real Hermes venv interpreter to run the installer")

FAKE_CLUSTER = json.dumps([
    {"name": "fabricated", "hosts": ["192.0.2.1"], "user": "op",
     "cache_dir": "", "default": True}])

BASE_ARGS = ("--skip-cli", "--skip-daemon", "--skip-patches",
             "--skip-node-recovery")


def _payload_tree(tmp_path):
    """A private copy of the checkout, so install hooks/backups never touch the
    real repo during the run."""
    root = tmp_path / "checkout"
    shutil.copytree(
        REPO, root,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache",
                                      "*.pyc"))
    return root


def _sandbox(tmp_path, *, hermes_initialised=True):
    """Throwaway home + fake sparkrun + a real Hermes venv (symlinked dir).

    ``hermes_initialised`` writes the ``config.yaml`` Hermes creates on its
    first run. A real install on a healthy host has one; ``False`` models a
    machine where Hermes has never been started (t_6f4895aa).
    """
    home = tmp_path / "home"
    hermes = home / ".hermes"
    (hermes / "hermes-agent" / "venv").mkdir(parents=True)
    (hermes / "hermes-agent" / "venv").rmdir()
    (hermes / "hermes-agent" / "venv").symlink_to(REAL_HERMES_AGENT / "venv")
    if hermes_initialised:
        (hermes / "config.yaml").write_text("_config_version: 52\n",
                                            encoding="utf-8")
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
    registry = tmp_path / "registry.yaml"
    registry.write_text("projects: []\n", encoding="utf-8")
    return {"home": home, "hermes": hermes, "hscc": hscc, "bin": binp,
            "registry": registry}


def _env(sandbox):
    env = {k: v for k, v in os.environ.items()
           if k not in ("HERMES_HOME", "HSCC_DIR", "HERMES_SCRIPTS",
                        "RECIPES_DIR", "HSCC_REGISTRY", "HERMES_BIN",
                        "HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE",
                        "HERMES_DELEGATED_CHILD_CONTEXT", "HSCC_BOOTSTRAP_SH",
                        "PYTHONPATH", "TMUX")}
    env.update({
        "HERMES_HOME": str(sandbox["hermes"]),
        "HSCC_DIR": str(sandbox["hscc"]),
        "HERMES_SCRIPTS": str(sandbox["home"] / "scripts"),
        "RECIPES_DIR": str(sandbox["home"] / "recipes"),
        "HSCC_REGISTRY": str(sandbox["registry"]),
        # Sandbox bin (fake sparkrun) + system tools only: no tmux server, no
        # operator's sparkrun/uv, no second hscc on PATH.
        "PATH": f'{sandbox["bin"]}:/usr/bin:/bin',
        "NO_COLOR": "1", "TERM": "dumb",
    })
    return env


def _bootstrap(tree, sandbox, *, yes=True, stdin=None, extra=()):
    args = ["bash", str(tree / "hscc-bootstrap" / "bootstrap.sh")]
    if yes:
        args.append("--yes")
    args.extend(BASE_ARGS)
    args.extend(extra)
    return subprocess.run(args, input=stdin, capture_output=True, text=True,
                          env=_env(sandbox), timeout=900,
                          cwd=str(sandbox["home"]))


def _hermes(sandbox, *args, check=True):
    binary = sandbox["hermes"] / "hermes-agent" / "venv" / "bin" / "hermes"
    proc = subprocess.run([str(binary), *args], capture_output=True, text=True,
                          env=_env(sandbox), timeout=180,
                          cwd=str(sandbox["home"]))
    if check and proc.returncode != 0:
        raise AssertionError(f"hermes {' '.join(args)} failed:\n"
                             f"{proc.stdout}\n{proc.stderr}")
    return proc


def _current_board(sandbox):
    path = sandbox["hermes"] / "kanban" / "current"
    return path.read_text(encoding="utf-8").strip() if path.is_file() else "default"


def _wakeups(sandbox):
    """The real installed CLI: ``hscc main wakeups list --json``."""
    env = _env(sandbox)
    env["PYTHONPATH"] = str(sandbox["hermes"] / "plugins")
    proc = subprocess.run(
        [str(REAL_VENV_PY), "-m", "hscc_daemon.hscc",
         "main", "wakeups", "list", "--json"],
        capture_output=True, text=True, env=env, timeout=120,
        cwd=str(sandbox["home"]))
    assert proc.returncode == 0, f"wakeups list failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


def _resolver_name(sandbox):
    helper = sandbox["hermes"] / "plugins" / "hscc-roles" / "main_orch.py"
    proc = subprocess.run([str(REAL_VENV_PY), str(helper), "name"],
                          capture_output=True, text=True, env=_env(sandbox),
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _identity_files(root):
    """SOUL/config bytes only — the re-run may back up skill payloads, which is
    not an identity rewrite."""
    return {str(p.relative_to(root)): p.read_bytes()
            for p in root.rglob("*")
            if p.is_file() and p.name in ("SOUL.md", "config.yaml")
            and "plugins-backups" not in p.parts}


def _assert_wakeups(sandbox, profile):
    receipt = _wakeups(sandbox)
    assert receipt["profile"] == profile
    rows = {row["name"]: row for row in receipt["wakeups"]}
    assert set(rows) == {"health", "sweep", "memory", "settings"}
    assert all(row["enabled"] for row in rows.values())
    assert rows["health"]["schedule"] == "every 2h"
    assert rows["sweep"]["schedule"] == "every 3h"


def _assert_installed(sandbox, name):
    state = json.loads(
        (sandbox["hscc"] / "main-orchestrator.json").read_text(encoding="utf-8"))
    assert state["name"] == name
    profile = sandbox["hermes"] / "profiles" / name
    assert (profile / "SOUL.md").is_file(), f"profile {name} was not provisioned"
    assert "main orchestrator" in (profile / "SOUL.md").read_text().lower()
    assert _current_board(sandbox) == name
    boards = json.loads(_hermes(sandbox, "kanban", "boards", "list",
                                "--json").stdout)
    slugs = {row.get("slug") or row.get("name") for row in boards}
    assert name in slugs, f"board {name} missing from {sorted(slugs)}"
    _assert_wakeups(sandbox, name)


@needs_runtime
def test_fresh_yes_install_creates_general_profile_board_and_wakeups(tmp_path):
    tree = _payload_tree(tmp_path)
    sandbox = _sandbox(tmp_path)
    proc = _bootstrap(tree, sandbox)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"bootstrap failed:\n{out[-4000:]}"
    assert "HSCC installed" in out
    assert "config wired (" in out, \
        "a home Hermes already initialised must report the wiring it did"
    _assert_installed(sandbox, "general")


@needs_runtime
def test_first_install_before_hermes_ever_ran_defers_the_board_and_stays_unwired(
        tmp_path):
    """t_6f4895aa: on a genuinely fresh home the board stage must not run the
    Hermes CLI. Doing so creates config.yaml, and the wiring stage six stages
    later then reports "config wired" on a machine nothing ever wired."""
    tree = _payload_tree(tmp_path)
    sandbox = _sandbox(tmp_path, hermes_initialised=False)
    proc = _bootstrap(tree, sandbox)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"bootstrap failed:\n{out[-4000:]}"
    assert "NOT WIRED" in out, out[-2000:]
    assert "config wired (" not in out, \
        "claimed success on a home where Hermes has never run"
    assert not (sandbox["hermes"] / "config.yaml").exists(), \
        "a bootstrap stage created Hermes' config.yaml before Hermes did"
    assert not (sandbox["hermes"] / "kanban").exists(), \
        "a bootstrap stage drove the Hermes CLI into a home it never initialised"
    assert _current_board(sandbox) == "default", \
        "the main board must not be created before Hermes initialised the home"
    # The identity/profile work still happened; only the board is deferred.
    assert (sandbox["hermes"] / "profiles" / "general" / "SOUL.md").is_file()


@needs_runtime
def test_fresh_install_with_a_custom_name_uses_it_everywhere(tmp_path):
    tree = _payload_tree(tmp_path)
    sandbox = _sandbox(tmp_path)
    proc = _bootstrap(tree, sandbox, yes=False, stdin="ops\n")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"bootstrap failed:\n{out[-4000:]}"
    assert "main orchestrator: ops" in out
    _assert_installed(sandbox, "ops")
    assert not (sandbox["hermes"] / "profiles" / "general" / "SOUL.md").exists()


@needs_runtime
def test_upgrade_keeps_legacy_profiles_and_retires_them(tmp_path):
    tree = _payload_tree(tmp_path)
    sandbox = _sandbox(tmp_path)
    profiles = sandbox["hermes"] / "profiles"

    # The pre-migration install: the legacy catch-all with history, the legacy
    # cluster role with config, and the legacy `default` board holding a card.
    legacy = profiles / "general-orch"
    (legacy / "memories").mkdir(parents=True)
    (legacy / "sessions").mkdir()
    (legacy / "memories" / "MEMORY.md").write_text(
        "# legacy memory kept\n", encoding="utf-8")
    (legacy / "sessions" / "s-legacy.json").write_text(
        '{"legacy": true}\n', encoding="utf-8")
    (legacy / "SOUL.md").write_text(
        "# legacy general orchestration history\n\nkeep me\n", encoding="utf-8")
    role = profiles / "orchestrator"
    role.mkdir()
    (role / "SOUL.md").write_text(
        "# old cluster role\n\n## Role: orchestrator\n", encoding="utf-8")
    (role / "config.yaml").write_text("operator_key: 7\n", encoding="utf-8")

    _hermes(sandbox, "kanban", "boards", "switch", "default")
    _hermes(sandbox, "kanban", "--board", "default", "create",
            "legacy card kept", "--json")
    assert _current_board(sandbox) == "default"

    proc = _bootstrap(tree, sandbox)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"upgrade failed:\n{out[-4000:]}"
    _assert_installed(sandbox, "general")

    # Nothing was deleted: both legacy profiles, their files, and the card on
    # the old default board are still there.
    assert legacy.is_dir() and role.is_dir()
    assert (legacy / "memories" / "MEMORY.md").read_text() == "# legacy memory kept\n"
    assert (legacy / "sessions" / "s-legacy.json").is_file()
    # `generate` may add its own keys to the legacy role's config, but the
    # operator's value must survive.
    assert "operator_key: 7" in (role / "config.yaml").read_text()
    cards = json.loads(
        _hermes(sandbox, "kanban", "--board", "default", "list", "--json").stdout)
    assert any(card.get("title") == "legacy card kept" for card in cards)

    # Both SOULs say retired in favour of the resolved main name; the
    # catch-all's original content survived the retirement block.
    for soul in ((legacy / "SOUL.md").read_text(),
                 (role / "SOUL.md").read_text()):
        assert "retired — use general" in soul
    assert "# legacy general orchestration history" in \
        (legacy / "SOUL.md").read_text()

    # The catch-all's missing memory/session files were COPIED to the main
    # profile, never moved.
    main = profiles / "general"
    assert (main / "memories" / "MEMORY.md").read_text() == "# legacy memory kept\n"
    assert (main / "sessions" / "s-legacy.json").is_file()
    assert _resolver_name(sandbox) == "general"

    # A re-run is idempotent: success, same name/board, no duplicate retirement
    # block and no profile rewrite.
    before = _identity_files(profiles)
    second = _bootstrap(tree, sandbox)
    assert second.returncode == 0, (second.stdout + second.stderr)[-4000:]
    assert _identity_files(profiles) == before
    assert (legacy / "SOUL.md").read_text().count("retired — use") == 1
    assert _resolver_name(sandbox) == "general"
    assert _current_board(sandbox) == "general"
    _assert_wakeups(sandbox, "general")
