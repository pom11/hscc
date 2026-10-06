"""Tests for scripts/changelog_fragments.py (t_95f1d6e1).

Run standalone (the file needs no plugin sys.path):

    python3 -m pytest -q scripts/tests/test_changelog_fragments.py

The important test is `test_two_concurrent_cards_merge_without_conflict`: it
reproduces the exact failure this scheme removes (two cards adding a Fixed
entry in the same window) in a throwaway git repo and asserts git reports zero
conflicts on the merge — and it asserts the OLD scheme conflicts, so the test
proves the fix rather than asserting a happy path.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import changelog_fragments as cf  # noqa: E402
import migrate_changelog_fragments as mg  # noqa: E402


# ---------------------------------------------------------------- helpers

def write_fragment(dirpath: Path, task: str, kind: str, body: str, order=None):
    header = f"kind: {kind}\ntask: {task}\n"
    if order is not None:
        header += f"order: {order}\n"
    (dirpath / f"{task}.md").write_text(header + "\n" + body, encoding="utf-8")


def make_changelog(path: Path, unreleased_body: str = "", versions: str = ""):
    text = (
        "# Changelog\n\nAll notable changes.\n\n"
        "## [Unreleased]\n\n" + unreleased_body + "\n" + versions
    )
    path.write_text(text, encoding="utf-8")


def run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def git(args, cwd):
    r = run("git", *args, cwd=str(cwd))
    assert r.returncode == 0, f"git {' '.join(args)} failed: {r.stderr}"
    return r.stdout


@pytest.fixture()
def frags(tmp_path):
    d = tmp_path / "changelog.d"
    d.mkdir()
    return d


@pytest.fixture()
def changelog(tmp_path):
    p = tmp_path / "CHANGELOG.md"
    make_changelog(p, versions="## [1.0.0] - 2026-01-01\n\n### Fixed\n- old\n")
    return p


# ---------------------------------------------------------------- parsing

def test_entry_body_is_verbatim(frags):
    write_fragment(frags, "t_aaaaaaaa", "Fixed",
                   "- **boom.** first line\n  continuation line\n")
    entries = cf.load_entries(frags)
    assert len(entries) == 1
    assert entries[0].body == ["- **boom.** first line", "  continuation line"]


def test_multiple_entries_one_file(frags):
    (frags / "t_bbbbbbbb.md").write_text(
        "kind: Fixed\ntask: t_bbbbbbbb\n\n- one\n"
        "---\n"
        "kind: Verified\ntask: t_bbbbbbbb\n\n- two\n",
        encoding="utf-8",
    )
    kinds = [e.kind for e in cf.load_entries(frags)]
    assert kinds == ["Fixed", "Verified"]


def test_deterministic_ordering_by_task_id(frags):
    for t in ("t_zzzzzzzz", "t_aaaaaaaa", "t_mmmmmmmm"):
        write_fragment(frags, t, "Fixed", f"- entry {t}\n")
    order = [e.task for e in cf.load_entries(frags)]
    assert order == sorted(order)


def test_kind_order_keepachangelog_then_verified(frags):
    for kind in ("Verified", "Fixed", "Added", "Removed"):
        (frags / f"k-{kind}.md").write_text(
            f"kind: {kind}\ntask: t_11111111\n\n- {kind}\n", encoding="utf-8")
    out = cf.render_block(fragments_dir=frags)
    positions = {k: out.index(f"### {k}") for k in ("Added", "Removed", "Fixed", "Verified")}
    assert positions["Added"] < positions["Removed"] < positions["Fixed"] < positions["Verified"]


def test_malformed_fragment_raises(frags):
    (frags / "t_22222222.md").write_text("- no header at all\n", encoding="utf-8")
    with pytest.raises(cf.FragmentError):
        cf.load_entries(frags)


def test_empty_body_raises(frags):
    (frags / "t_33333333.md").write_text("kind: Fixed\n", encoding="utf-8")
    with pytest.raises(cf.FragmentError):
        cf.load_entries(frags)


def test_bad_order_value_raises(frags):
    (frags / "t_44444444.md").write_text(
        "kind: Fixed\norder: soon\n\n- x\n", encoding="utf-8")
    with pytest.raises(cf.FragmentError):
        cf.load_entries(frags)


def test_readme_fragment_is_ignored(frags):
    (frags / "README.md").write_text("# prose, kind: not an entry\n", encoding="utf-8")
    assert cf.load_entries(frags) == []


# ---------------------------------------------------------------- render/sync

def test_render_block_shape(frags):
    write_fragment(frags, "t_55555555", "Fixed", "- broken thing\n")
    block = cf.render_block(fragments_dir=frags, marker="")
    assert block.split("\n")[:4] == ["## [Unreleased]", "", "### Fixed", "- broken thing"]


def test_sync_is_idempotent(frags, changelog):
    write_fragment(frags, "t_66666666", "Fixed", "- thing\n")
    before = changelog.read_text()
    assert cf.main(["sync", "--fragments-dir", str(frags),
                    "--changelog", str(changelog)]) == 0
    once = changelog.read_text()
    assert cf.main(["sync", "--fragments-dir", str(frags),
                    "--changelog", str(changelog)]) == 0
    assert changelog.read_text() == once, "sync must be a no-op the second time"
    assert "### Fixed" in once and "- thing" in once
    assert before != once  # first sync actually wrote


def test_check_accepts_pointer_and_synced(frags, changelog):
    write_fragment(frags, "t_77777777", "Fixed", "- thing\n")
    # pointer form (the committed state between releases)
    text = changelog.read_text()
    pre, _, post = cf.split_changelog(text)
    changelog.write_text(cf.assemble(pre, cf.pointer_block(), post), encoding="utf-8")
    assert cf.main(["check", "--fragments-dir", str(frags),
                    "--changelog", str(changelog)]) == 0
    # materialised form
    assert cf.main(["sync", "--fragments-dir", str(frags),
                    "--changelog", str(changelog)]) == 0
    assert cf.main(["check", "--fragments-dir", str(frags),
                    "--changelog", str(changelog)]) == 0


def test_check_flags_hand_edit(frags, changelog, capsys):
    write_fragment(frags, "t_88888888", "Fixed", "- from fragment\n")
    text = changelog.read_text()
    pre, _, post = cf.split_changelog(text)
    hand = pre + "\n\n## [Unreleased]\n\n### Fixed\n- hand-written entry\n" + post
    changelog.write_text(cf.assemble(pre, "## [Unreleased]\n\n### Fixed\n- hand-written entry", post),
                         encoding="utf-8")
    assert hand  # sanity on the fixture
    rc = cf.main(["check", "--fragments-dir", str(frags),
                  "--changelog", str(changelog)])
    assert rc == 1
    assert "DRIFT" in capsys.readouterr().err


def test_sync_never_leaves_partial_file(frags, changelog, monkeypatch):
    """The write is tmp + os.replace, so a crash cannot truncate the changelog."""
    write_fragment(frags, "t_99999999", "Fixed", "- thing\n")
    calls = []
    real_replace = os.replace

    def boom(src, dst):
        calls.append((Path(src).name, Path(dst).name))
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        cf.main(["sync", "--fragments-dir", str(frags),
                 "--changelog", str(changelog)])
    monkeypatch.setattr(os, "replace", real_replace)
    assert calls and calls[0][0].endswith(".tmp-frag")
    # original is intact and no stray tmp file remains to be mistaken for it
    assert "All notable changes." in changelog.read_text()
    assert not list(changelog.parent.glob("*.tmp-frag"))


# ---------------------------------------------------------------- add

def test_add_creates_then_appends(frags):
    p = cf.add_entry("t_aaaaaaaa", "Fixed", "- first entry\n", fragments_dir=frags)
    assert p.name == "t_aaaaaaaa.md"
    cf.add_entry("t_aaaaaaaa", "Verified", "already a bullet\n", fragments_dir=frags)
    entries = cf.load_entries(frags)
    assert [e.kind for e in entries] == ["Fixed", "Verified"]
    assert entries[1].body[0].startswith("- ")  # bare text got a bullet


def test_add_rejects_non_task_id(frags):
    with pytest.raises(cf.FragmentError):
        cf.add_entry("chore", "Fixed", "- x\n", fragments_dir=frags)


def test_add_rejects_empty_body(frags):
    with pytest.raises(cf.FragmentError):
        cf.add_entry("t_bbbbbbbb", "Fixed", "   \n", fragments_dir=frags)


# ------------------------------------------------ rescue (legacy branch landing)

LEGACY_CHANGELOG = (
    "# Changelog\n\n## [Unreleased]\n\n### Fixed\n"
    "- **old entry.** already on main\n"
    "- **old entry two.** also already on main\n\n"
    "### Verified\n- old evidence\n\n"
    "## [1.0.0] - 2026-01-01\n\n### Fixed\n- previous release\n"
)


def test_rescue_extracts_legacy_branch_entries(frags):
    """A pre-scheme branch hand-edited CHANGELOG.md; rescue moves its added
    entries into a task-named fragment, kinds inferred from the section."""
    branch = LEGACY_CHANGELOG.replace(
        "- **old entry two.** also already on main\n",
        "- **old entry two.** also already on main\n"
        "- **new bug from the branch.**\n  continuation prose\n")
    branch = branch.replace(
        "### Verified\n- old evidence\n",
        "### Verified\n- old evidence\n- suite green on both interpreters\n")
    path, n = cf.rescue_entries(LEGACY_CHANGELOG, branch, "t_abcd1234",
                                fragments_dir=frags)
    assert n == 2 and path.name == "t_abcd1234.md"
    entries = cf.load_entries(frags)
    assert [(e.kind, e.task) for e in entries] == [("Fixed", "t_abcd1234"),
                                                   ("Verified", "t_abcd1234")]
    assert entries[0].body == ["- **new bug from the branch.**",
                               "  continuation prose"]


def test_rescue_noop_when_branch_adds_nothing(frags):
    with pytest.raises(cf.FragmentError):
        cf.rescue_entries(LEGACY_CHANGELOG, LEGACY_CHANGELOG, "t_abcd1234",
                          fragments_dir=frags)
    assert list(frags.glob("t_*.md")) == []


def test_rescue_rejects_non_task_id(frags):
    branch = LEGACY_CHANGELOG.replace("### Fixed\n", "### Fixed\n- new\n")
    with pytest.raises(cf.FragmentError):
        cf.rescue_entries(LEGACY_CHANGELOG, branch, "whatever",
                          fragments_dir=frags)


# ---------------------------------------------------------------- release

def test_release_moves_fragments_into_version_section(frags, changelog):
    write_fragment(frags, "t_cccccccc", "Fixed", "- fixed thing\n")
    write_fragment(frags, "t_dddddddd", "Verified", "- verified thing\n")
    assert cf.main(["release", "--version", "1.1.0", "--date", "2026-01-02",
                    "--fragments-dir", str(frags),
                    "--changelog", str(changelog)]) == 0
    text = changelog.read_text()
    assert "## [1.1.0] - 2026-01-02" in text
    assert "- fixed thing" in text and "- verified thing" in text
    # the released fragments are archived, so [Unreleased] starts empty again
    assert (frags / "archive" / "1.1.0" / "t_cccccccc.md").is_file()
    assert not list(frags.glob("t_*.md"))
    assert cf.main(["check", "--fragments-dir", str(frags),
                    "--changelog", str(changelog)]) == 0


def test_release_refuses_duplicate_version(frags, changelog):
    write_fragment(frags, "t_eeeeeeee", "Fixed", "- x\n")
    cf.main(["release", "--version", "1.0.0", "--fragments-dir", str(frags),
             "--changelog", str(changelog)])
    write_fragment(frags, "t_ffffffff", "Fixed", "- y\n")
    rc = cf.main(["release", "--version", "1.0.0", "--fragments-dir", str(frags),
                  "--changelog", str(changelog)])
    assert rc == 2


# ---------------------------------------------------------------- migration

def test_migration_is_lossless_on_the_live_changelog():
    """Every non-blank Unreleased entry line of the real CHANGELOG.md must land
    in exactly one fragment. Guards the one-shot migrator against the class of
    bug it already hit (the last entry of each section was dropped).

    Runs against the pre-migration state (``HEAD~1``'s file) so the check keeps
    its teeth after the migration has landed: the entries that had to survive
    are pinned by the blob at the commit before the migration, not by whatever
    [Unreleased] happens to hold today.
    """
    import subprocess

    def blob(rev):
        r = subprocess.run(["git", "show", f"{rev}:CHANGELOG.md"],
                           cwd=str(REPO_ROOT), capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None

    candidates = [blob("HEAD")]
    # find the last commit whose CHANGELOG.md still had hand-written bullets
    log = subprocess.run(["git", "log", "--format=%H", "--", "CHANGELOG.md"],
                         cwd=str(REPO_ROOT), capture_output=True, text=True).stdout.split()
    for rev in log[:12]:
        text = blob(rev)
        if not text:
            continue
        _, block, _ = cf.split_changelog(text)
        if block and any(l.startswith("- ") for l in block.split("\n")):
            candidates.insert(0, text)
            break
    text = next((t for t in candidates if t), None)
    if text is None:
        pytest.skip("no pre-migration CHANGELOG.md found")
    _, block, _ = cf.split_changelog(text)
    block_lines = block.split("\n")
    entries = mg.split_entries(block_lines[1:])
    original = [l.rstrip() for l in block_lines
                if l.strip() and not l.startswith("## [")
                and not l.startswith("### ") and not l.startswith("<!--")]
    moved = [l.rstrip() for _, lines, _ in entries for l in lines]
    assert original, "expected the pinned CHANGELOG.md to have entries"
    assert sorted(moved) == sorted(original)


def test_committed_repo_state_is_consistent():
    """changelog.d/ and the committed CHANGELOG.md must agree at HEAD."""
    assert cf.main(["check", "--fragments-dir", str(REPO_ROOT / "changelog.d"),
                    "--changelog", str(REPO_ROOT / "CHANGELOG.md")]) == 0


def test_repo_has_no_legacy_handwritten_unreleased_entries():
    """After the migration, [Unreleased] carries no hand-written bullets."""
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    _, block, _ = cf.split_changelog(text)
    assert block is not None
    bullets = [l for l in block.split("\n") if l.startswith("- ")]
    assert bullets == [], f"[Unreleased] still hand-written: {bullets[:2]}"


# ------------------------------------------------- THE POINT: no merge conflicts

def _init_repo(path: Path):
    path.mkdir(parents=True)
    git(["init", "-q"], cwd=path)
    git(["config", "user.email", "t@example.invalid"], cwd=path)
    git(["config", "user.name", "test"], cwd=path)
    git(["config", "commit.gpgsign", "false"], cwd=path)
    (path / "changelog.d").mkdir()
    (path / "changelog.d" / "README.md").write_text("# fragments\n", encoding="utf-8")
    (path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n"
        "<!-- GENERATED -->\n\n_pointer text_\n\n"
        "## [1.0.0] - 2026-01-01\n\n### Fixed\n- previous release\n",
        encoding="utf-8")
    git(["add", "-A"], cwd=path)
    git(["commit", "-q", "-m", "base"], cwd=path)
    git(["branch", "-M", "main"], cwd=path)
    return path


def _card_branch(repo: Path, name: str, task: str, kind: str, body: str):
    git(["checkout", "-q", "-b", f"wt/{task}"], cwd=repo)
    (repo / "changelog.d" / f"{task}.md").write_text(
        f"kind: {kind}\ntask: {task}\n\n{body}", encoding="utf-8")
    (repo / f"code-{task}.py").write_text(f"# {task}\n", encoding="utf-8")
    git(["add", "-A"], cwd=repo)
    git(["commit", "-q", "-m", f"feat({task}): add {kind} entry"], cwd=repo)
    git(["checkout", "-q", "main"], cwd=repo)


def test_two_concurrent_cards_merge_without_conflict(tmp_path):
    """The scheme's whole reason to exist, proven by execution.

    Two cards, created from the same base, each add an entry to a file named
    after its own task id and each land a code change. Merging both into a
    scratch branch must produce ZERO conflicts and both entries must be
    present when the block is assembled.
    """
    repo = _init_repo(tmp_path / "new-scheme")
    _card_branch(repo, "wt/t_1111aaaa", "t_1111aaaa", "Fixed",
                 "- **first card bug.** fixed in hscc-bootstrap.\n")
    _card_branch(repo, "wt/t_2222bbbb", "t_2222bbbb", "Fixed",
                 "- **second card bug.** fixed in hscc-api.\n")
    git(["checkout", "-q", "-b", "scratch"], cwd=repo)
    r = run("git", "merge", "--no-edit", "wt/t_1111aaaa", cwd=str(repo))
    assert r.returncode == 0, r.stdout + r.stderr
    r = run("git", "merge", "--no-edit", "wt/t_2222bbbb", cwd=str(repo))
    assert r.returncode == 0, (r.stdout + r.stderr)
    assert "CONFLICT" not in r.stdout + r.stderr
    # both code files exist
    assert (repo / "code-t_1111aaaa.py").is_file()
    assert (repo / "code-t_2222bbbb.py").is_file()
    # both entries assemble, ordered by task id
    entries = cf.load_entries(repo / "changelog.d")
    assert [e.task for e in entries] == ["t_1111aaaa", "t_2222bbbb"]
    block = cf.render_block(fragments_dir=repo / "changelog.d", marker="")
    assert "first card bug" in block and "second card bug" in block


def test_old_single_file_scheme_conflicts(tmp_path):
    """Same experiment with the OLD rule (both cards prepend into the same
    Unreleased/### Fixed hunk) — git must conflict. Without this assertion the
    test above would only prove that git merges disjoint files."""
    repo = _init_repo(tmp_path / "old-scheme")
    for task, title in (("t_1111aaaa", "first"), ("t_2222bbbb", "second")):
        git(["checkout", "-q", "-b", f"wt/{task}"], cwd=repo)
        text = (repo / "CHANGELOG.md").read_text()
        (repo / "CHANGELOG.md").write_text(
            text.replace("## [Unreleased]\n\n",
                         f"## [Unreleased]\n\n### Fixed\n- **{title} card.** "
                         f"added by {task}\n\n", 1),
            encoding="utf-8")
        git(["add", "-A"], cwd=repo)
        git(["commit", "-q", "-m", f"docs({task}): changelog entry"], cwd=repo)
        git(["checkout", "-q", "main"], cwd=repo)
    git(["checkout", "-q", "-b", "scratch"], cwd=repo)
    assert run("git", "merge", "--no-edit", "wt/t_1111aaaa",
               cwd=str(repo)).returncode == 0
    r = run("git", "merge", "--no-edit", "wt/t_2222bbbb", cwd=str(repo))
    assert r.returncode != 0 or "CONFLICT" in (r.stdout + r.stderr), \
        "expected the old scheme to conflict — it did not, so the test is stale"
