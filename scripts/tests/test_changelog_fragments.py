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
    # task ids are spread far apart and written out of order; the names are all
    # legal task ids because the filename gate (t_9c79b937) rejects the old
    # `t_zzzzzzzz`-style placeholders
    for t in ("t_ffffffff", "t_aaaaaaaa", "t_dddddddd"):
        write_fragment(frags, t, "Fixed", f"- entry {t}\n")
    order = [e.task for e in cf.load_entries(frags)]
    assert order == sorted(order)
    # and the assembled block agrees with it, not with file write order
    block = cf.render_block(fragments_dir=frags, marker="")
    positions = [block.index(f"entry {t}") for t in
                 ("t_aaaaaaaa", "t_dddddddd", "t_ffffffff")]
    assert positions == sorted(positions), block


def test_kind_order_keepachangelog_then_verified(frags):
    # one legal task id per kind: the live-directory filename gate means a
    # `k-Added.md`-style helper name is now a parse error (t_9c79b937)
    names = {"Added": "t_a1000000", "Removed": "t_b2000000",
             "Fixed": "t_c3000000", "Verified": "t_d4000000"}
    for kind in ("Verified", "Fixed", "Added", "Removed"):
        (frags / f"{names[kind]}.md").write_text(
            f"kind: {kind}\ntask: {names[kind]}\n\n- {kind}\n", encoding="utf-8")
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


# ------------------------------------------- filename ownership (t_9c79b937)
#
# `changelog.d/README.md` has claimed since t_95f1d6e1 that "a fragment whose
# file name is not a kanban task id also fails `check`". It never did:
# TASK_ID_RE was consulted only by `add`/`rescue` — the commands validating the
# id they were *given* — never by the path reading the files that are *present*.
# Measured on the pointer-form CHANGELOG before this fix:
#
#   cp changelog.d/*.md $D && printf 'kind: Fixed\n\n- …\n' > $D/misc.md \
#      && printf 'kind: Fixed\n\n- …\n' > $D/chore.md
#   python3 scripts/changelog_fragments.py check --fragments-dir $D ...
#   => "OK — [Unreleased] is pointer, … consistent"   rc=0
#
# Both foreign entries were counted and would have been materialised into the
# next release. The name IS the ownership rule that keeps worker diffs
# disjoint, so the gate now lives in `load_entries` (every command funnels
# through it) and fail-closes as a malformed fragment: rc=2, same convention as
# the missing-separator gate from t_f57b9d82. `archive/` stays exempt — those
# files must keep parsing for audit under their pre-scheme names.

MISC_FRAGMENT = "kind: Fixed\n\n- entry authored in a shared misc file\n"
CHORE_FRAGMENT = "kind: Fixed\n\n- entry authored by a card named wrong\n"


def test_misc_named_fragment_fails_check(frags, changelog, capsys):
    """The card's headline defect: `check` stayed rc=0 on `misc.md`."""
    (frags / "misc.md").write_text(MISC_FRAGMENT, encoding="utf-8")
    rc = cf.main(["check", "--fragments-dir", str(frags),
                  "--changelog", str(changelog)])
    err = capsys.readouterr().err
    assert rc == 2, f"check must not stay green on a shared name (rc={rc})"
    assert "malformed fragment" in err and "misc.md" in err, err
    assert "task id" in err, err


def test_non_hex_stem_raises(frags):
    """A name that merely *looks* task-shaped is still not a task id: the hex
    constraint is what ties the file to a real kanban card."""
    (frags / "t_zzzzzzzz.md").write_text(MISC_FRAGMENT, encoding="utf-8")
    with pytest.raises(cf.FragmentError) as exc:
        cf.load_entries(frags)
    assert "t_zzzzzzzz" in str(exc.value), str(exc.value)


def test_valid_task_id_fragment_passes_check(frags, changelog, capsys):
    """ANTI-VACUITY positive leg: identical content under a legal name passes.
    Without this leg the two failing tests above would also pass on a loader
    that rejected every file."""
    (frags / "t_9c79b937.md").write_text(MISC_FRAGMENT, encoding="utf-8")
    # pointer form = the committed state between releases (see
    # test_check_accepts_pointer_and_synced for why this is set up first)
    pre, _, post = cf.split_changelog(changelog.read_text())
    changelog.write_text(cf.assemble(pre, cf.pointer_block(), post), encoding="utf-8")
    rc = cf.main(["check", "--fragments-dir", str(frags),
                  "--changelog", str(changelog)])
    out = capsys.readouterr()
    assert rc == 0, out.err + out.out
    assert "1 fragment entries consistent" in out.out, out.out


def test_uppercase_md_suffix_is_caught_not_skipped(frags):
    """`Path.glob('*.md')` is case-sensitive even on macOS's case-insensitive
    APFS (measured): `misc.MD` was invisible to the old selector — it bypassed
    every gate AND silently vanished at release. The selector is now
    case-insensitive, so such a file must fail, not hide."""
    (frags / "misc.MD").write_text(MISC_FRAGMENT, encoding="utf-8")
    with pytest.raises(cf.FragmentError) as exc:
        cf.load_entries(frags)
    assert "misc.MD" in str(exc.value), str(exc.value)
    # and the README exemption is likewise case-insensitive
    (frags / "README.MD").write_text("# prose, kind: not an entry\n",
                                     encoding="utf-8")
    (frags / "misc.MD").unlink()
    assert cf.load_entries(frags) == []


def test_release_refuses_a_foreign_name_without_side_effects(frags, changelog):
    """The step that would materialise the shared entry into a release must
    refuse rc=2 and change nothing on disk (same shape as the glue gate)."""
    (frags / "misc.md").write_text(MISC_FRAGMENT, encoding="utf-8")
    (frags / "t_aaaaaaa1.md").write_text("kind: Fixed\n\n- legit\n",
                                         encoding="utf-8")
    before = changelog.read_text()
    rc = cf.main(["release", "--version", "1.2.3", "--fragments-dir", str(frags),
                  "--changelog", str(changelog)])
    assert rc == 2
    assert changelog.read_text() == before, "a refused release touched CHANGELOG.md"
    assert (frags / "t_aaaaaaa1.md").is_file(), "a refused release archived fragments"
    assert not (frags / "archive").exists()


def test_archive_is_not_scanned(frags):
    """`archive/<version>/` keeps pre-scheme names that must stay parseable for
    audit — the gate is a property of the live directory, not of the parser.
    The real corpus pins it: archive/2.7.0/t_tmux_chat_20261013.md fails
    TASK_ID_RE and still parses when loaded directly."""
    legacy = "kind: Fixed\n\n- archived entry under a legacy name\n"
    archived = frags / "archive" / "2.7.0"
    archived.mkdir(parents=True)
    (archived / "t_tmux_chat_20261013.md").write_text(legacy, encoding="utf-8")
    # live dir holds only the README: the archived legacy name must not leak
    # into the live load at all...
    assert cf.load_entries(frags) == []
    # ...while audit access to the legacy file itself still works
    assert [e.body for e in cf.parse_fragment(
        archived / "t_tmux_chat_20261013.md")] == [["- archived entry under a legacy name"]]
    # and a release that *created* an archive keeps check green afterwards
    (frags / "t_aaaaaaa2.md").write_text("kind: Fixed\n\n- x\n", encoding="utf-8")
    cl = frags.parent / "CHANGELOG.md"
    make_changelog(cl)
    assert cf.main(["release", "--version", "9.9.9", "--fragments-dir", str(frags),
                    "--changelog", str(cl)]) == 0
    assert (archived.parent / "9.9.9" / "t_aaaaaaa2.md").is_file()
    assert cf.main(["check", "--fragments-dir", str(frags),
                    "--changelog", str(cl)]) == 0


def test_release_consumes_an_uppercase_suffix_fragment(frags):
    """The count and the consumption must use ONE selector. A legal stem with a
    `.MD` suffix is loaded (so `release` counts it); if the archive move kept
    the old case-sensitive glob, the file would stay behind and be counted
    again at the next release — the entry shipping twice with every gate
    green, because `check` accepts the pointer form whatever it counts."""
    (frags / "t_aaaaaaa3.MD").write_text("kind: Fixed\n\n- shouty suffix\n",
                                         encoding="utf-8")
    assert [e.source for e in cf.load_entries(frags)] == ["t_aaaaaaa3.MD"]
    cl = frags.parent / "CHANGELOG.md"
    make_changelog(cl)
    assert cf.main(["release", "--version", "9.9.8", "--fragments-dir", str(frags),
                    "--changelog", str(cl)]) == 0
    assert (frags / "archive" / "9.9.8" / "t_aaaaaaa3.MD").is_file()
    assert cf.load_entries(frags) == [], "release left a counted fragment behind"


def test_check_fragment_name_unit_contract():
    """`check_fragment_name` is also a unit-level contract (it is the function
    a landing step or a reviewer script calls on one path), so its own
    exemptions must not depend on `fragment_files` having pre-filtered them:
    a readme is allowed in any case, a legal stem is allowed, anything else
    raises. Without this leg the readme exemption here is dead code — the
    selector hides it — and a later edit that case-tightens it would go
    unnoticed. Note the division of labour: this function judges the STEM only;
    which files count as fragments at all is `fragment_files`' job (see
    test_task_named_non_md_file_is_refused)."""
    cf.check_fragment_name(Path("t_9c79b937.md"))          # legal: no raise
    cf.check_fragment_name(Path("t_95f1d6e1.mdx"))         # legal stem, odd suffix
    cf.check_fragment_name(Path("README.md"))              # exempt
    cf.check_fragment_name(Path("readme.md"))              # exempt, lowercase
    cf.check_fragment_name(Path("README.MD"))              # exempt, either case
    for bad in ("misc.md", "t_zzzzzzzz.md", "misc.MD", "chore.md",
                "t_9c79b93z.md", "t_tmux_chat_20261013.md"):
        with pytest.raises(cf.FragmentError):
            cf.check_fragment_name(Path(bad))


def test_task_named_non_md_file_is_refused(frags):
    """The mirror of the `.MD` case: a file *named* like a card but with a
    non-`.md` suffix is an explicit ownership claim that the reader will never
    open, so it must fail loudly instead of being skipped on suffix alone —
    that is the same silent-vanish shape, approached from the other side.
    Non-claiming files (`notes.txt`, dotfiles) stay ignored."""
    (frags / "t_9c79b937.txt").write_text("kind: Fixed\n\n- wrong suffix\n",
                                          encoding="utf-8")
    with pytest.raises(cf.FragmentError) as exc:
        cf.load_entries(frags)
    assert "t_9c79b937.txt" in str(exc.value), str(exc.value)
    assert "silently dropped" in str(exc.value), str(exc.value)
    (frags / "t_9c79b937.txt").unlink()
    (frags / "notes.txt").write_text("scratch, not a fragment\n", encoding="utf-8")
    (frags / ".t_9c79b937.md.swp").write_text("vim junk\n", encoding="utf-8")
    assert cf.load_entries(frags) == []


def test_filename_gate_is_what_fires(monkeypatch, frags):
    """ANTI-VACUITY (mutation): neuter the gate — i.e. delete the check — and
    the shared-name files parse into exactly the two foreign entries that the
    pre-fix tool counted and would have released. So every failing test above
    is red iff the gate is removed, and red for the right reason."""
    (frags / "misc.md").write_text(MISC_FRAGMENT, encoding="utf-8")
    (frags / "chore.md").write_text(CHORE_FRAGMENT, encoding="utf-8")
    monkeypatch.setattr(cf, "check_fragment_name", lambda path: None)
    entries = cf.load_entries(frags)
    assert [e.source for e in entries] == ["chore.md", "misc.md"], entries
    assert "shared misc file" in entries[1].body[0], entries[1].body


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
    # Whole history, not a recent window: every release adds a CHANGELOG.md
    # commit, so a fixed window eventually loses the pre-migration blob and the
    # test silently falls back to HEAD's pointer-only [Unreleased].
    for rev in log:
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
