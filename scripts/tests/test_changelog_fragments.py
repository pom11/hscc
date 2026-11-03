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


# ------------------------------------------- body-level separator gate (t_c365fe82)
#
# The class that shipped: two entries in one file WITHOUT the documented
# standalone `---` separator. parse_fragment then folds the FOLLOWING entry's
# `kind:`/`task:`/`order:` headers into the previous entry's body, `check`
# stays green (it only validates the [Unreleased] block shape), and `release`
# materialises the bookkeeping lines as prose into CHANGELOG.md — 18 lines
# leaked into [2.15.0] this way (5 files; repaired at 9ea52bc4 on PR #178).
# The gate lives in the parser pass (`validate_entry_bodies`, reached through
# every command via `load_entries`), so check/sync/render/release all refuse
# the leak; a malformed fragment is exit 2 per the module contract.

# Two entries in one file; identical content in both shapes — the ONLY
# difference is the separator line. This is what makes the fixture
# mutation-proof: a gate that fails the first must pass the second.
NO_SEPARATOR = (
    "kind: Fixed\ntask: t_deadbeef\n\n- **first entry.** prose.\n"
    "  continuation of the first entry.\n"
    "kind: Verified\ntask: t_deadbeef\norder: 1\n\n"
    "- **second entry.** evidence.\n"
)
WITH_SEPARATOR = (
    "kind: Fixed\ntask: t_deadbeef\n\n- **first entry.** prose.\n"
    "  continuation of the first entry.\n"
    "---\n"
    "kind: Verified\ntask: t_deadbeef\norder: 1\n\n"
    "- **second entry.** evidence.\n"
)


def _pointer_state(changelog):
    """Put CHANGELOG.md in the normal committed shape (pointer block)."""
    pre, _, post = cf.split_changelog(changelog.read_text(encoding="utf-8"))
    changelog.write_text(cf.assemble(pre, cf.pointer_block(), post),
                         encoding="utf-8")


def test_missing_separator_leaks_headers_into_body(frags):
    """Parser pass must refuse the swallowed-header body, naming the file and
    using the README's vocabulary ('missing `---` separator between entries')."""
    (frags / "t_deadbeef.md").write_text(NO_SEPARATOR, encoding="utf-8")
    with pytest.raises(cf.FragmentError) as exc:
        cf.load_entries(frags)
    msg = str(exc.value)
    assert "t_deadbeef.md" in msg
    assert "missing `---` separator between entries" in msg


def test_same_fixture_clean_when_separator_present(frags):
    """Mutation-proof half: byte-identical entries + a `---` -> two clean
    entries. Without this the leak test could go green on any parse failure."""
    (frags / "t_deadbeef.md").write_text(WITH_SEPARATOR, encoding="utf-8")
    entries = cf.load_entries(frags)
    assert [e.kind for e in entries] == ["Fixed", "Verified"]
    assert entries[0].body == ["- **first entry.** prose.",
                               "  continuation of the first entry."]
    assert entries[1].body == ["- **second entry.** evidence."]


def test_check_fails_then_passes_on_the_no_separator_fixture(frags, changelog,
                                                             capsys):
    """The shipped scenario end-to-end: committed CHANGELOG.md is the pointer
    (the shape that hid the defect on the 2.15.0 tree — with zero parsed
    entries `check` validated nothing), the fragment carries the leak.
    check must exit 2 (malformed fragment, per the module contract) naming the
    file; repairing ONLY the separator makes the same command exit 0."""
    (frags / "t_deadbeef.md").write_text(NO_SEPARATOR, encoding="utf-8")
    _pointer_state(changelog)
    rc = cf.main(["check", "--fragments-dir", str(frags),
                  "--changelog", str(changelog)])
    assert rc == 2, "missing separator is a malformed fragment -> exit 2"
    err = capsys.readouterr().err
    assert "t_deadbeef.md" in err
    assert "missing `---` separator between entries" in err
    # repair = separator insertion only, zero prose -> the same check is green
    (frags / "t_deadbeef.md").write_text(WITH_SEPARATOR, encoding="utf-8")
    assert cf.main(["check", "--fragments-dir", str(frags),
                    "--changelog", str(changelog)]) == 0


def test_release_refuses_to_materialise_the_leak(frags, changelog):
    """The leak must be impossible at the release step too — that is where it
    became CHANGELOG prose on the real cut."""
    (frags / "t_deadbeef.md").write_text(NO_SEPARATOR, encoding="utf-8")
    rc = cf.main(["release", "--version", "9.9.9", "--fragments-dir", str(frags),
                  "--changelog", str(changelog)])
    assert rc == 2
    assert not (frags / "archive" / "9.9.9").exists()
    assert "kind: Verified" not in changelog.read_text(encoding="utf-8")


def test_body_prose_may_still_mention_headers(frags):
    """The gate keys on _HEADER_RE/_ALLOWED_HEADERS at column 0 (the same
    tokens the parser consumes), so prose that merely MENTIONS vocabulary —
    an unknown header word, or an indented mention — stays legal."""
    (frags / "t_cafebabe.md").write_text(
        "kind: Fixed\ntask: t_cafebabe\n\n"
        "- **prose.** Notes: the docs use `kind: Fixed` loosely; an entry\n"
        "  kind: Fixed header at column 0 would mean a second entry.\n",
        encoding="utf-8")
    entries = cf.load_entries(frags)
    assert len(entries) == 1
    assert any("kind: Fixed" in l for l in entries[0].body)


def test_repo_pending_fragments_pass_the_body_gate():
    """Guards both directions against regression: the fragments committed on
    this tree must be body-clean, or `release` would leak again.

    Zero pending fragments is a legitimate committed state: a release fold
    consumes the whole set (`release` moves every fragment into
    changelog.d/archive/<version>/), so every post-fold tree — up to the
    next card adding a fragment — has none. Demanding a non-empty set here
    made the FIRST fold since this test landed (2.15.1, at which point no
    fold had exercised it — the 2.15.0 cut predates it) fail the scripts
    leg on a correctly folded tree. The dir itself must exist (a missing
    or renamed dir would make `load_entries` return [] silently), and the
    body gate itself stays pinned both-directional by the
    NO_SEPARATOR / WITH_SEPARATOR fixture pair above."""
    pending = REPO_ROOT / "changelog.d"
    assert pending.is_dir(), "changelog.d/ (the fragments dir) must exist"
    entries = cf.load_entries(pending)  # raises FragmentError -> test fails
    if entries:
        # pending fragments exist: prove the gate actually bites on this
        # tree (t_c365fe82's original intent), not just that parsing ran
        assert any(e.body for e in entries), "parsed entries carry no bodies"


# ------------------------------------------- kind-allowlist gate (t_f48f84c4)
#
# The class this gate stops: `parse_fragment` accepted ANY kind and
# `kind_sort_key` parked unknown kinds in a catch-all bucket, so an author's
# invented word rendered as a permanent `### <Kind>` heading — [2.15.0]
# shipped `### schema` (x8 entries), `### Docs` (x6) and `### Test` (x1) as
# live CHANGELOG vocabulary. Operator decision (t_f48f84c4): an unknown kind
# is a HARD failure — exit 2 like a malformed fragment — and the vocabulary is
# closed at KIND_ORDER, with `Docs` admitted deliberately for doc-only changes.
# The gate lives in the parse pass (`validate_kind`, reached through
# `load_entries`), so check/sync/render/list/release all refuse it.

# Same entry in both shapes — the ONLY difference is the kind word. This is
# what makes the fixture mutation-proof: a gate that fails `schema` must pass
# the byte-identical `Docs` entry (Docs is legal vocabulary; schema is not).
KIND_SCHEMA = "kind: schema\ntask: t_deadbeef\n\n- **schema drift.** prose.\n"
KIND_DOCS = "kind: Docs\ntask: t_deadbeef\n\n- **schema drift.** prose.\n"


def test_unknown_kind_raises_naming_the_file(frags):
    (frags / "t_deadbeef.md").write_text(KIND_SCHEMA, encoding="utf-8")
    with pytest.raises(cf.FragmentError) as exc:
        cf.load_entries(frags)
    msg = str(exc.value)
    assert "t_deadbeef.md" in msg
    assert "kind 'schema' is not an allowed section name" in msg
    # the message must teach the fix, not just refuse
    assert "Docs" in msg and "Verified" in msg


def test_docs_kind_is_legal(frags):
    """Mutation-proof half: the identical entry with `kind: Docs` — the kind
    the operator admitted for doc-only changes — passes the same parse."""
    (frags / "t_deadbeef.md").write_text(KIND_DOCS, encoding="utf-8")
    entries = cf.load_entries(frags)
    assert [e.kind for e in entries] == ["Docs"]
    assert entries[0].body == ["- **schema drift.** prose."]
    assert "### Docs" in cf.render_block(fragments_dir=frags, marker="")


def test_check_fails_then_passes_on_the_unknown_kind_fixture(frags, changelog,
                                                             capsys):
    """End-to-end on the shipped scenario: pointer-committed CHANGELOG.md +
    fragment with `kind: schema` -> check exits 2 (malformed fragment per the
    module contract) naming the file; swapping ONLY the kind word to `Docs`
    (not the body) makes the same command exit 0."""
    (frags / "t_deadbeef.md").write_text(KIND_SCHEMA, encoding="utf-8")
    _pointer_state(changelog)
    rc = cf.main(["check", "--fragments-dir", str(frags),
                  "--changelog", str(changelog)])
    assert rc == 2, "unknown kind is a malformed fragment -> exit 2"
    err = capsys.readouterr().err
    assert "t_deadbeef.md" in err
    assert "not an allowed section name" in err
    (frags / "t_deadbeef.md").write_text(KIND_DOCS, encoding="utf-8")
    assert cf.main(["check", "--fragments-dir", str(frags),
                    "--changelog", str(changelog)]) == 0


def test_release_refuses_to_materialise_an_unknown_kind(frags, changelog):
    """`### schema` must become structurally impossible at the release step —
    that is where the 2.15.0 headings were minted."""
    (frags / "t_deadbeef.md").write_text(KIND_SCHEMA, encoding="utf-8")
    rc = cf.main(["release", "--version", "9.9.9", "--fragments-dir", str(frags),
                  "--changelog", str(changelog)])
    assert rc == 2
    assert not (frags / "archive" / "9.9.9").exists()
    assert "### schema" not in changelog.read_text(encoding="utf-8")


def test_add_command_refuses_unknown_kind(frags):
    """`add --kind schema` must fail at the command that writes the fragment
    (exit 2 via main), and must not leave a fragment behind to fail later."""
    rc = cf.main(["add", "--task", "t_deadbeef", "--kind", "schema",
                  "--body", "- prose", "--fragments-dir", str(frags)])
    assert rc == 2
    assert list(frags.glob("*.md")) == []
    with pytest.raises(cf.FragmentError):
        cf.add_entry("t_deadbeef", "Test", "- prose\n", fragments_dir=frags)


def test_add_accepts_docs_kind(frags):
    """`Docs` is vocabulary, so the writer accepts it end to end."""
    p = cf.add_entry("t_deadbeef", "Docs", "- docs only change\n",
                     fragments_dir=frags)
    assert p.name == "t_deadbeef.md"
    assert [e.kind for e in cf.load_entries(frags)] == ["Docs"]


@pytest.mark.parametrize("kind", cf.KIND_ORDER)
def test_every_allowed_kind_parses(frags, kind):
    """The closed list is the contract in BOTH directions: every name in
    KIND_ORDER must parse, or the allowlist and the gate disagree."""
    write_fragment(frags, "t_1a2b3c4d", kind, f"- entry for {kind}\n")
    assert [e.kind for e in cf.load_entries(frags)] == [kind]


def test_kind_is_case_sensitive(frags):
    """The vocabulary is exact: `fixed` is not `Fixed`. Loosening this would
    re-open the door `schema` walked through."""
    write_fragment(frags, "t_1a2b3c4d", "fixed", "- entry\n")
    with pytest.raises(cf.FragmentError):
        cf.load_entries(frags)


def test_docs_sorts_last_after_verified(frags):
    """Operator chose Docs as the 8th kind, last after Verified — the heading
    order is part of the decision, pin it."""
    for kind in ("Docs", "Verified", "Fixed", "Added"):
        (frags / f"k-{kind}.md").write_text(
            f"kind: {kind}\ntask: t_11111111\n\n- {kind}\n", encoding="utf-8")
    out = cf.render_block(fragments_dir=frags)
    positions = {k: out.index(f"### {k}") for k in ("Added", "Fixed", "Verified", "Docs")}
    assert positions["Added"] < positions["Fixed"] < positions["Verified"] < positions["Docs"]


def test_repo_pending_fragments_pass_the_kind_gate():
    """Every pending fragment on this tree must use the closed vocabulary, or
    `check`/`release` would refuse the tree the moment this gate lands (the
    blast radius was measured zero; this test keeps it zero)."""
    pending = REPO_ROOT / "changelog.d"
    kinds = {e.kind for p in sorted(pending.glob("*.md"))
             if not p.name.lower().startswith("readme")
             for e in cf.parse_fragment(p)}
    assert kinds <= set(cf.KIND_ORDER), f"off-vocabulary kinds pending: {kinds - set(cf.KIND_ORDER)}"


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
