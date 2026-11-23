"""install-skills: cluster-control skills live on the MAIN profile only (R4).

Card t_b52e70ba. The binding operator ruling in
``~/fleet-check/verdict-main-only.txt`` overrides the audit's original
"keep concept + read-path" split:

  * ``hscc``, ``hscc-cluster``, ``hscc-model-onboard`` install/update into
    ``<root>/profiles/<main_orch.main_name()>/skills`` ONLY (never a
    hard-coded ``general``);
  * every copy in every OTHER home - the root home and every other profile -
    is MOVED to that home's ``skills-backups/removed-<stamp>/``; nothing is
    deleted, and profile-authored skills are untouched;
  * shared/work skills (the generic set, ``sdlc-review``, ``devops``) keep
    installing at the caller's ``HERMES_HOME/skills``.

Everything runs the real CLI as a subprocess against throwaway ``HERMES_HOME``
and ``HSCC_DIR`` dirs, the same shape ``bootstrap.sh`` uses.
"""
import json
import os
import re
import subprocess
import sys

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_PLUGIN_DIR)
_INSTALLER = os.path.join(_REPO, "hscc-skills", "hscc.py")
_TEMPLATE = os.path.join(_REPO, "install", "hscc-skills")
_MAIN_ONLY = ("hscc", "hscc-cluster", "hscc-model-onboard")
_STAMPED = re.compile(r"^removed-\d{8}-\d{6}$")


def make_sandbox(tmp_path, main="ops"):
    root = tmp_path / "hermes"
    hscc = tmp_path / "hscc"
    hscc.mkdir()
    (hscc / "main-orchestrator.json").write_text(
        json.dumps({"name": main}), encoding="utf-8")
    main_home = root / "profiles" / main
    main_home.mkdir(parents=True)
    return root, hscc, main_home


def run_installer(root, hscc, cmd="install-skills", home=None):
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home or root)
    env["HSCC_DIR"] = str(hscc)
    env["HSCC_SKILLS_SRC"] = _TEMPLATE
    return subprocess.run([sys.executable, _INSTALLER, cmd], env=env,
                          capture_output=True, text=True, timeout=180)


def seed_skill(home, skill, text, extra=None):
    d = home / "skills" / skill
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")
    if extra:
        (d / extra).parent.mkdir(parents=True, exist_ok=True)
        (d / extra).write_text("operator bytes\n", encoding="utf-8")
    return d


def backup_dirs(home):
    parent = home / "skills-backups"
    if not parent.is_dir():
        return []
    return sorted(p for p in parent.iterdir() if _STAMPED.match(p.name))


def template_text(skill):
    with open(os.path.join(_TEMPLATE, skill, "SKILL.md"), encoding="utf-8") as fh:
        return fh.read()


def test_main_only_skills_land_on_the_resolved_main_and_nowhere_else(tmp_path):
    root, hscc, main_home = make_sandbox(tmp_path, main="ops")

    # Root home and two non-main profiles carry stray copies (the measured
    # shape: 74 hscc* dirs), each with recoverable operator content.
    seed_skill(root, "hscc", "# ROOT JULY COPY\n", extra="references/note.md")
    for profile in ("backend", "frontend"):
        for skill in _MAIN_ONLY:
            seed_skill(root / "profiles" / profile, skill, f"# {profile} {skill}\n")
    mine = seed_skill(root / "profiles" / "backend", "my-own-skill", "# MINE\n")

    r = run_installer(root, hscc)
    assert r.returncode == 0, f"rc={r.returncode}\n{r.stdout}\n{r.stderr}"

    for skill in _MAIN_ONLY:
        installed = main_home / "skills" / skill / "SKILL.md"
        assert installed.read_text(encoding="utf-8") == template_text(skill), skill

    # The no-hard-coded-name pin: "general" was never created anywhere.
    assert not (root / "profiles" / "general").exists()

    # Nothing remains outside main; every copy is recoverable from a dated dir.
    assert not (root / "skills" / "hscc").exists()
    for profile in ("backend", "frontend"):
        home = root / "profiles" / profile
        for skill in _MAIN_ONLY:
            assert not (home / "skills" / skill).exists(), (profile, skill)
            kept = backup_dirs(home)
            assert len(kept) == 1, (profile, kept)
            assert (kept[0] / skill / "SKILL.md").read_text(
                encoding="utf-8").startswith(f"# {profile}")

    root_kept = backup_dirs(root)
    assert len(root_kept) == 1, root_kept
    assert (root_kept[0] / "hscc" / "references" / "note.md").read_text(
        encoding="utf-8") == "operator bytes\n", "move lost nested bytes"

    # A profile-authored skill is not hscc's to move.
    assert (mine / "SKILL.md").read_text(encoding="utf-8") == "# MINE\n"

    # Shared/work skills keep the caller's home.
    assert (root / "skills" / "sdlc-review" / "SKILL.md").is_file()

    assert "MOVED" in r.stdout and "removed-" in r.stdout


def test_category_copies_are_moved_but_a_mere_reference_dir_is_not(tmp_path):
    """hermes scans SKILL.md at any depth, so a category copy is still live."""
    root, hscc, _main_home = make_sandbox(tmp_path)
    nested = root / "profiles" / "backend" / "skills" / "legacy" / "hscc"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("# nested stale copy\n", encoding="utf-8")
    reference = root / "profiles" / "backend" / "skills" / "mine" / "references" / "hscc"
    reference.mkdir(parents=True)
    (reference / "note.md").write_text("not a skill\n", encoding="utf-8")

    r = run_installer(root, hscc)
    assert r.returncode == 0, r.stderr
    assert not nested.exists()
    kept = backup_dirs(root / "profiles" / "backend")
    assert len(kept) == 1
    assert (kept[0] / "hscc" / "SKILL.md").read_text(
        encoding="utf-8") == "# nested stale copy\n"
    assert (reference / "note.md").read_text(encoding="utf-8") == "not a skill\n"


def test_the_sweep_is_idempotent_and_mints_no_second_backup(tmp_path):
    root, hscc, _main_home = make_sandbox(tmp_path)
    seed_skill(root / "profiles" / "backend", "hscc-cluster", "# stale\n")

    first = run_installer(root, hscc)
    assert first.returncode == 0, first.stderr
    kept = backup_dirs(root / "profiles" / "backend")
    assert len(kept) == 1

    second = run_installer(root, hscc)
    assert second.returncode == 0, second.stderr
    assert backup_dirs(root / "profiles" / "backend") == kept
    assert "MOVED" not in second.stdout


def test_a_non_main_hermes_home_is_cleaned_and_main_still_receives(tmp_path):
    root, hscc, main_home = make_sandbox(tmp_path, main="ops")
    backend = root / "profiles" / "backend"
    backend.mkdir(parents=True)
    seed_skill(backend, "hscc-model-onboard", "# worker stale copy\n")

    r = run_installer(root, hscc, home=backend)
    assert r.returncode == 0, f"rc={r.returncode}\n{r.stdout}\n{r.stderr}"

    assert (main_home / "skills" / "hscc-model-onboard" / "SKILL.md").is_file()
    assert not (backend / "skills" / "hscc-model-onboard").exists()
    kept = backup_dirs(backend)
    assert len(kept) == 1
    assert (kept[0] / "hscc-model-onboard" / "SKILL.md").read_text(
        encoding="utf-8") == "# worker stale copy\n"


def test_status_reads_the_main_profile_not_the_caller_home(tmp_path):
    root, hscc, _main_home = make_sandbox(tmp_path, main="ops")
    assert run_installer(root, hscc).returncode == 0

    r = run_installer(root, hscc, cmd="status")
    assert r.returncode == 0, f"rc={r.returncode}\n{r.stdout}\n{r.stderr}"
    rows = [ln for ln in r.stdout.splitlines() if ln.strip().startswith("hscc ")]
    assert rows, r.stdout
    assert "OK" in rows[0], rows[0]


def test_a_tampered_main_name_fails_closed_without_writing(tmp_path):
    root, hscc, _main_home = make_sandbox(tmp_path, main="ops")
    (hscc / "main-orchestrator.json").write_text(
        json.dumps({"name": "../escape"}), encoding="utf-8")
    r = run_installer(root, hscc)
    assert r.returncode != 0
    assert "not a single path component" in (r.stdout + r.stderr)
    assert not (root / "escape").exists()
    assert not (root / "skills").exists()


def test_install_full_verb_uses_the_same_main_only_route(tmp_path):
    """`install` (full bootstrap) must not drift from `install-skills`."""
    root, hscc, main_home = make_sandbox(tmp_path, main="ops")
    seed_skill(root, "hscc", "# root copy\n")

    r = run_installer(root, hscc, cmd="install")
    assert r.returncode == 0, f"rc={r.returncode}\n{r.stdout}\n{r.stderr}"
    assert (main_home / "skills" / "hscc" / "SKILL.md").read_text(
        encoding="utf-8") == template_text("hscc")
    assert not (root / "skills" / "hscc").exists()
    assert "Moved 1" in r.stdout, r.stdout
    assert len(backup_dirs(root)) == 1, (
        sorted(str(p) for p in root.rglob("removed-*")), r.stdout)
