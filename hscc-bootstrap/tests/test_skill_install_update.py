"""The installer must UPDATE hscc-OWNED skills (backup, then overwrite).

Card t_45fa38e2 (2.17.4 workstream, card 4-group 4/5). The audit
(t_4c12ff7f §8) measured three distinct outcomes for one bundled skill and
only one of them was a real skip; this file reproduces all of them in a
sandboxed ``HERMES_HOME`` so the seam is pinned by shipped text, not by
reading the walker. Measured at the base sha (39c887cf) with
``hscc-skills/hscc.py install-skills`` against a throwaway home:

  * stale owned copy, no twin        -> refreshed, but the old bytes were
                                        GONE: no backup anywhere.
  * stale FLAT copy + category twin  -> ``GUARDED  collision: ...`` — the
                                        stale copy was frozen forever, which
                                        is how July content survived.
  * name resolves only elsewhere     -> correctly refused (writing here would
                                        CREATE the collision).
  * foreign/user skill               -> untouched.

The fix therefore has two halves and both are pinned here: (1) every write of
a bundled skill into an existing installed copy is preceded by a recoverable
snapshot kept OUTSIDE the scanned skills tree (hermes' scanner walks
``skills/`` at any depth, so a ``.bak-*`` copy left in there registers its
frontmatter ``name:`` and CREATES the very collision ``skill_guard`` exists to
prevent — that is pinned too); (2) a copy that already exists at the intended
target is refreshed even when the guard would veto the write, while the
refusal that protects a *non-existent* target stays.

Everything here runs the installer as a subprocess with ``HERMES_HOME`` and
``HSCC_SKILLS_SRC`` pointed at temp dirs, i.e. the same command
``bootstrap.sh`` runs, so the pin covers the CLI path and the module in the
same breath. No live home is touched.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest

import backup_util
import skill_guard

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_PLUGIN_DIR)
_INSTALLER = os.path.join(_REPO, "hscc-skills", "hscc.py")
_TEMPLATE = os.path.join(_REPO, "install", "hscc-skills")

STALE = "---\nname: {name}\n---\n\n# JULY-STALE CONTENT — do not ship\n"


def md5(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except OSError:
        return None


def write_skill(home, *rel, text):
    path = os.path.join(home, "skills", *rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def hscc_state_dir(home):
    """The HSCC_DIR sandbox beside the Hermes root that owns *home*."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(home)))
    return os.path.join(os.path.dirname(root), "hscc")


def run_installer(home, cmd="install-skills"):
    """The exact invocation bootstrap.sh:417 uses, in a sandboxed home."""
    env = dict(os.environ)
    env["HERMES_HOME"] = home
    env["HSCC_DIR"] = hscc_state_dir(home)
    env["HSCC_SKILLS_SRC"] = _TEMPLATE
    r = subprocess.run([sys.executable, _INSTALLER, cmd], env=env,
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, f"{cmd} rc={r.returncode}\n{r.stdout}\n{r.stderr}"
    return r.stdout


@pytest.fixture
def home(tmp_path):
    """A sandbox whose HERMES_HOME is the MAIN profile home (t_b52e70ba).

    The installer writes main-only skills to ``<root>/profiles/<main_name>``,
    so these update/backup pins run with that profile as the caller's home.
    HSCC state is a sibling dir carrying the same resolved name.
    """
    root = tmp_path / "hermes"
    hscc = tmp_path / "hscc"
    hscc.mkdir()
    (hscc / "main-orchestrator.json").write_text(
        json.dumps({"name": "general"}), encoding="utf-8")
    h = root / "profiles" / "general"
    h.mkdir(parents=True)
    return str(h)


def backups_of(home, skill):
    d = os.path.join(home, "skills-backups")
    if not os.path.isdir(d):
        return []
    return sorted(n for n in os.listdir(d)
                  if n.startswith(skill + backup_util.BACKUP_INFIX))


# ── the card's pin ──────────────────────────────────────────────────────────

def test_stale_owned_skill_comes_away_matching_the_repo(home):
    """Shipped-text hash comparison: installed SKILL.md == repo template."""
    installed = write_skill(home, "hscc", "SKILL.md", text=STALE.format(name="hscc"))
    assert md5(installed) != md5(os.path.join(_TEMPLATE, "hscc", "SKILL.md"))
    run_installer(home)
    assert md5(installed) == md5(os.path.join(_TEMPLATE, "hscc", "SKILL.md"))


def test_stale_copy_is_recoverable_from_the_backup(home):
    """The replaced bytes must survive under a suffix, outside skills/."""
    installed = write_skill(home, "hscc", "SKILL.md", text=STALE.format(name="hscc"))
    stale = md5(installed)
    run_installer(home)
    backups = backups_of(home, "hscc")
    assert len(backups) == 1, f"expected one hscc backup, got {backups}"
    recovered = os.path.join(home, "skills-backups", backups[0], "SKILL.md")
    assert md5(recovered) == stale, "the backup is not the copy we replaced"


def test_backup_is_never_written_inside_the_scanned_skills_tree(home):
    """A `.bak-*` copy left under skills/ registers as a skill of its own.

    hermes collects candidates by directory name AND frontmatter ``name:`` at
    any depth (skill_guard's module docstring), so a copy of ``hscc/SKILL.md``
    kept in place would name-collide ``hscc`` and kill every spawn asking for
    it — the exact June failure this repo already paid for.
    """
    write_skill(home, "hscc", "SKILL.md", text=STALE.format(name="hscc"))
    run_installer(home)
    skills = os.path.join(home, "skills")
    inside = [os.path.join(root, n)
              for root, _d, files in os.walk(skills) for n in files
              if backup_util.BACKUP_INFIX in n]
    assert inside == [], f"backup copies inside the scan root: {inside}"
    dirs = [n for n in os.listdir(skills) if backup_util.BACKUP_INFIX in n]
    assert dirs == [], f"backup dirs inside the scan root: {dirs}"
    assert skill_guard.duplicate_names(skills) == {}, "the run created a name collision"


def test_every_shipped_file_of_an_owned_skill_matches_after_a_run(home):
    """Convergence is whole-tree, not just the top-level SKILL.md."""
    write_skill(home, "hscc", "SKILL.md", text=STALE.format(name="hscc"))
    run_installer(home)
    src = os.path.join(_TEMPLATE, "hscc")
    checked = 0
    for root, _d, files in os.walk(src):
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), src)
            assert md5(os.path.join(src, rel)) == md5(os.path.join(home, "skills",
                                                                   "hscc", rel)), rel
            checked += 1
    assert checked > 1, "the template has only one file — the pin would be vacuous"


# ── idempotence + hygiene ───────────────────────────────────────────────────

def test_second_run_writes_nothing_and_backs_up_nothing(home):
    write_skill(home, "hscc", "SKILL.md", text=STALE.format(name="hscc"))
    run_installer(home)
    first = backups_of(home, "hscc")
    installed = os.path.join(home, "skills", "hscc", "SKILL.md")
    converged = md5(installed)
    out = run_installer(home)
    assert md5(installed) == converged
    assert backups_of(home, "hscc") == first, "a clean run minted another backup"
    assert "hscc" not in out or "UP-TO-DATE" in out


def test_skill_backups_are_pruned_to_the_retention_window(home, monkeypatch):
    """Retention is the 27k-file pile t_9462260b removed for every other
    writer; the skills seam must inherit the same cap."""
    mod = _load_installer_module(monkeypatch, home)
    stamps = iter(f"2026{m:02d}03-1200{m:02d}" for m in range(1, 8))
    monkeypatch.setattr(mod, "_backup_stamp", lambda: next(stamps))
    src = os.path.join(_TEMPLATE, "hscc")
    for _ in range(6):
        write_skill(home, "hscc", "SKILL.md", text=STALE.format(name="hscc"))
        results, bak = mod.install_owned_skill(src, os.path.join(home, "skills"), "hscc")
        assert bak is not None
    kept = backups_of(home, "hscc")
    assert len(kept) == backup_util.BACKUP_KEEP, kept
    assert all(backup_util._STAMP_RE.search(n) for n in kept), \
        "backup names must carry the stamp the pruner orders by"


# ── the guard: refresh what is here, keep refusing what is not ──────────────

def test_stale_flat_copy_under_a_category_twin_is_refreshed_not_frozen(home):
    """The measured July survivor: a flat copy the guard refused to touch."""
    flat = write_skill(home, "sdlc-review", "SKILL.md",
                       text=STALE.format(name="sdlc-review"))
    twin = write_skill(home, "devops", "sdlc-review", "SKILL.md",
                       text="---\nname: sdlc-review\n---\n\n# HERMES CATEGORY COPY\n")
    twin_before = md5(twin)
    out = run_installer(home)
    assert md5(flat) == md5(os.path.join(_TEMPLATE, "sdlc-review", "SKILL.md")), \
        f"stale flat copy survived; installer said:\n{out}"
    assert md5(twin) == twin_before, "hscc must not rewrite another writer's copy"
    recovered = os.path.join(home, "skills-backups",
                             backups_of(home, "sdlc-review")[0], "SKILL.md")
    assert "JULY-STALE" in open(recovered, encoding="utf-8").read()


def test_a_copy_that_does_not_exist_yet_is_still_refused(home):
    """Writing a NEW flat copy beside a category twin CREATES the collision —
    the June bug. Narrowing the guard must not reopen that door."""
    twin = write_skill(home, "devops", "systematic-debugging", "SKILL.md",
                       text="---\nname: systematic-debugging\n---\n\n# CATEGORY ONLY\n")
    twin_before = md5(twin)
    out = run_installer(home)
    assert not os.path.exists(os.path.join(home, "skills", "systematic-debugging")), \
        f"guard regressed: hscc created a colliding copy\n{out}"
    assert md5(twin) == twin_before
    assert "systematic-debugging" in out and "GUARDED" in out


# ── foreign content ─────────────────────────────────────────────────────────

def test_foreign_user_skill_is_never_touched_or_backed_up(home):
    mine = write_skill(home, "my-own-skill", "SKILL.md",
                       text="---\nname: my-own-skill\n---\n\n# MINE\n")
    before = md5(mine)
    write_skill(home, "hscc", "SKILL.md", text=STALE.format(name="hscc"))
    run_installer(home)
    assert md5(mine) == before
    assert backups_of(home, "my-own-skill") == []
    d = os.path.join(home, "skills-backups")
    assert all(not n.startswith("my-own-skill") for n in
               (os.listdir(d) if os.path.isdir(d) else []))


def test_unowned_extra_file_inside_an_owned_skill_survives(home):
    """The installer converges the shipped files; it removes nothing."""
    write_skill(home, "hscc", "SKILL.md", text=STALE.format(name="hscc"))
    extra = write_skill(home, "hscc", "references", "operator-note.md",
                        text="# kept by the operator\n")
    before = md5(extra)
    run_installer(home)
    assert md5(extra) == before, "hscc deleted operator content it never shipped"


def test_status_reports_nested_drift_the_writer_can_see(home):
    """`status` must not call a skill OK while a shipped file differs.

    The old compare walked only the top level of each skill AND only files
    present on both sides, so a stale — or outright missing — nested
    ``references/*.md`` printed ✓ OK. The report and the writer now share
    ``owned_skill_drift``, which is the only definition worth having.
    """
    run_installer(home)  # converge everything first
    nested = sorted(os.listdir(os.path.join(_TEMPLATE, "hscc", "references")))
    assert nested, "the template ships no nested files — the pin would be vacuous"
    target = os.path.join(home, "skills", "hscc", "references", nested[0])
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("# drifted by hand\n")
    out = run_installer(home, cmd="status")
    row = [ln for ln in out.splitlines() if ln.strip().startswith("hscc ")]
    assert row, f"the status table lost the hscc row:\n{out}"
    assert "OUT-OF-DATE" in row[0], f"status lied about a nested drift:\n{row[0]}"


# ── fail-closed on a missing backup ─────────────────────────────────────────

def _load_installer_module(monkeypatch, home):
    """The installer as a module (its paths are read from env at import)."""
    monkeypatch.setenv("HERMES_HOME", home)
    monkeypatch.setenv("HSCC_DIR", hscc_state_dir(home))
    monkeypatch.setenv("HSCC_SKILLS_SRC", _TEMPLATE)
    spec = importlib.util.spec_from_file_location("hscc_skills_installer", _INSTALLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_write_is_refused_when_the_backup_cannot_be_taken(home, monkeypatch):
    """install_review_skill's contract, applied to this seam: a replacing
    write without a backup is a deletion."""
    mod = _load_installer_module(monkeypatch, home)
    installed = write_skill(home, "hscc", "SKILL.md", text=STALE.format(name="hscc"))
    before = md5(installed)
    monkeypatch.setattr(mod, "_load_backup_util", lambda: None)
    with pytest.raises(mod.BackupUnavailable):
        mod.install_owned_skill(os.path.join(_TEMPLATE, "hscc"),
                                os.path.join(home, "skills"), "hscc")
    assert md5(installed) == before, "the stale copy was replaced with no backup"
    assert backups_of(home, "hscc") == []


def test_report_names_the_backup_it_just_made(home):
    """An operator must be able to find the rollback point from the output
    alone — a silent backup is a backup nobody recovers from."""
    write_skill(home, "hscc", "SKILL.md", text=STALE.format(name="hscc"))
    out = run_installer(home)
    row = [ln for ln in out.splitlines() if ln.strip().startswith("hscc ")]
    assert row, f"the status table lost the hscc row:\n{out}"
    bak = backups_of(home, "hscc")
    assert len(bak) == 1
    assert bak[0] in row[0], f"backup not named in the report:\n{row[0]}"
