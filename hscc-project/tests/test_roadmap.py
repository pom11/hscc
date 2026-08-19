"""Tests for flightdeck.core.roadmap — pure file parsing, no I/O side effects.

ROADMAP.md parsing reads a file at a caller-supplied path (tests use
tmp_path). A missing file must yield a clear "no roadmap" marker — never an
exception, never silence. A malformed file must not crash: parse what you can
and report the rest in ``unparsed``.
"""

from flightdeck.core import roadmap
from flightdeck.core.roadmap import Roadmap, parse_roadmap


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _milestone(r, name):
    """Index milestones by name (tests control the input, so it is present)."""
    for m in r.milestones:
        if m.name == name:
            return m
    raise AssertionError(f"no milestone named {name!r}")


ROADMAP = """\
# Project roadmap

## Now
- [ ] Anulare tranzactie — direct ILE insert
- [x] Cache invalidation

## Next
- [ ] Stripe subscription lifecycle
- [ ] Refund webhook

## Later
- [x] Multi-tenant client portal
- [ ] Rate limiting
"""


def test_parses_all_three_sections(tmp_path):
    p = _write(tmp_path / "ROADMAP.md", ROADMAP)

    r = parse_roadmap(str(p))

    assert r.present is True
    assert [m.name for m in r.milestones] == ["Now", "Next", "Later"]


def test_checked_and_unchecked_items(tmp_path):
    p = _write(tmp_path / "ROADMAP.md", ROADMAP)

    r = parse_roadmap(str(p))

    now = _milestone(r, "Now")
    assert [i.text for i in now.items] == [
        "Anulare tranzactie — direct ILE insert",
        "Cache invalidation",
    ]
    assert [i.checked for i in now.items] == [False, True]


def test_counts_open_and_done(tmp_path):
    p = _write(tmp_path / "ROADMAP.md", ROADMAP)

    r = parse_roadmap(str(p))

    later = _milestone(r, "Later")
    assert later.total == 2
    assert later.open_count == 1
    assert later.done_count == 1

    nxt = _milestone(r, "Next")
    assert nxt.open_count == 2
    assert nxt.done_count == 0


def test_missing_file_is_no_roadmap_marker(tmp_path):
    # Reading a path that does not exist must NOT raise and must NOT be silent:
    # present=False is the explicit "no roadmap" marker.
    missing = str(tmp_path / "ROADMAP.md")

    r = parse_roadmap(missing)

    assert r.present is False
    assert r.milestones == []
    assert r.unparsed == []


def test_missing_file_returns_roadmap_not_exception(tmp_path):
    # A missing file returns a Roadmap object (never an exception) with
    # present False.
    missing = str(tmp_path / "nope.md")
    r = parse_roadmap(missing)
    assert isinstance(r, Roadmap)
    assert r.present is False


def test_malformed_file_does_not_crash_and_reports_rest(tmp_path):
    text = """\
## Now
- [ ] good item
this is not a checklist item and not a heading
- [ ] another good item
## Later
- [x] done item
stray text with no section
"""
    p = _write(tmp_path / "ROADMAP.md", text)

    r = parse_roadmap(str(p))

    # still present, still parses the recognizable bits
    assert r.present is True
    assert [m.name for m in r.milestones] == ["Now", "Later"]
    assert len(_milestone(r, "Now").items) == 2
    assert len(_milestone(r, "Later").items) == 1
    # the malformed lines are reported, not silently swallowed
    assert "this is not a checklist item and not a heading" in r.unparsed
    assert "stray text with no section" in r.unparsed


def test_malformed_item_before_any_heading_reported(tmp_path):
    text = """\
- [ ] orphan item with no section
## Now
- [ ] real item
"""
    p = _write(tmp_path / "ROADMAP.md", text)

    r = parse_roadmap(str(p))

    # the orphan item is not silently attached to Now
    assert len(_milestone(r, "Now").items) == 1
    assert "- [ ] orphan item with no section" in r.unparsed


def test_empty_file_is_present_but_empty(tmp_path):
    p = _write(tmp_path / "ROADMAP.md", "")

    r = parse_roadmap(str(p))

    assert r.present is True
    assert r.milestones == []
    assert r.unparsed == []


def test_accepts_x_uppercase_and_star_bullets(tmp_path):
    text = """\
## Now
- [X] uppercase done
* [ ] star bullet unchecked
"""
    p = _write(tmp_path / "ROADMAP.md", text)

    r = parse_roadmap(str(p))

    now = _milestone(r, "Now")
    assert [(i.text, i.checked) for i in now.items] == [
        ("uppercase done", True),
        ("star bullet unchecked", False),
    ]


def test_project_roadmap_resolves_default_path(tmp_path):
    from flightdeck.core.registry import Project

    _write(tmp_path / "ROADMAP.md", "## Now\n- [ ] thing\n")
    project = Project(name="svc", repo=str(tmp_path))

    r = roadmap.project_roadmap(project)

    assert r.present is True
    assert _milestone(r, "Now").open_count == 1


def test_project_roadmap_resolves_custom_path(tmp_path):
    from flightdeck.core.registry import Project

    _write(tmp_path / "docs" / "ROADMAP.md", "## Next\n- [x] released\n")
    project = Project(name="svc", repo=str(tmp_path), roadmap="docs/ROADMAP.md")

    r = roadmap.project_roadmap(project)

    assert r.present is True
    assert _milestone(r, "Next").done_count == 1


def test_roadmap_show_narrows_to_one_project(tmp_path, capsys):
    """`roadmap show <project>` renders only that project."""
    from flightdeck.cli import main

    reg = tmp_path / "r.yaml"
    a = tmp_path / "a"; (a / "docs").mkdir(parents=True)
    (a / "docs" / "ROADMAP.md").write_text("## Now\n- [ ] alpha item\n")
    b = tmp_path / "b"; (b / "docs").mkdir(parents=True)
    (b / "docs" / "ROADMAP.md").write_text("## Now\n- [ ] beta item\n")
    reg.write_text(
        f"projects:\n"
        f"  - name: alpha\n    repo: {a}\n    roadmap: docs/ROADMAP.md\n"
        f"  - name: beta\n    repo: {b}\n    roadmap: docs/ROADMAP.md\n"
    )
    assert main(["--registry", str(reg), "roadmap", "show", "alpha"]) == 0
    out = capsys.readouterr().out
    assert "alpha item" in out
    assert "beta item" not in out


def test_roadmap_show_unknown_project_errors(tmp_path, capsys):
    """An unknown name is an error, never a silently empty roadmap."""
    from flightdeck.cli import main

    reg = tmp_path / "r.yaml"
    a = tmp_path / "a"; a.mkdir()
    reg.write_text(f"projects:\n  - name: alpha\n    repo: {a}\n")
    assert main(["--registry", str(reg), "roadmap", "show", "nope"]) == 2
    assert "nope" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# subprojects + milestones with stable ids
# --------------------------------------------------------------------------- #

from flightdeck.core.roadmap import DuplicateMilestoneIdError  # noqa: E402


def _files_milestones(tmp_path, text):
    """Write text to a ROADMAP.md and return (milestones, roadmap)."""
    from flightdeck.core.registry import Project

    p = _write(tmp_path / "ROADMAP.md", text)
    project = Project(name="svc", repo=str(tmp_path))
    r = roadmap.project_roadmap(project)
    return r.milestones, r


NEW_FORMAT = """\
# Subproject: client-portal

## Milestone: auth-hardening         <!-- id: auth-hardening -->
status: now
- [ ] server-side key enforcement
- [x] password reset restricted to self/admin

## Milestone: Billing Cleanup
status: later
- [ ] reconcile invoices
"""


def test_flat_regression_against_a_flat_fixture(tmp_path):
    """The legacy flat Now/Next/Later format still parses unchanged.

    Pinned to a fixture, not to the repo's own docs/ROADMAP.md: that file is a
    live document and converting it to the subproject/milestone format broke
    this test, which is a property of the fixture choice rather than of the
    parser.
    """
    doc = tmp_path / "ROADMAP.md"
    doc.write_text(
        "# Roadmap\n\n"
        "## Now\n- [ ] first now item\n- [x] second now item\n\n"
        "## Next\n- [ ] a next item\n\n"
        "## Later\n- [ ] a later item\n"
    )
    r = parse_roadmap(str(doc))

    assert r.present is True
    assert [m.name for m in r.milestones] == ["Now", "Next", "Later"]
    now = r.milestone("Now")
    assert [i.text for i in now.items] == ["first now item", "second now item"]
    assert [i.checked for i in now.items] == [False, True]
    later = r.milestone("Later")
    assert later.open_count == later.total


def test_repo_own_roadmap_still_parses(tmp_path):
    """Whatever format docs/ROADMAP.md currently uses, it must parse."""
    import os

    docs = os.path.join(os.path.dirname(__file__), "..", "docs", "ROADMAP.md")
    r = parse_roadmap(docs)
    assert r.present is True
    assert r.milestones, "the repo's own roadmap must yield milestones"


def test_milestones_project_wrapper_returns_milestone_list(tmp_path):
    from flightdeck.core.registry import Project

    p = _write(tmp_path / "ROADMAP.md", NEW_FORMAT)
    project = Project(name="svc", repo=str(tmp_path))
    r = roadmap.project_roadmap(project)

    out = roadmap.milestones(project)

    assert out == r.milestones
    assert [m.name for m in out] == ["auth-hardening", "Billing Cleanup"]
    assert all(m.id for m in out)
    assert out[0].subproject == "client-portal"


def test_new_format_explicit_id_and_status(tmp_path):
    ms, r = _files_milestones(tmp_path, NEW_FORMAT)

    auth = ms[0]
    assert auth.name == "auth-hardening"
    assert auth.title == "auth-hardening"
    # explicit <!-- id: ... --> wins over the slug (which would also be this)
    assert auth.id == "auth-hardening"
    assert auth.subproject == "client-portal"
    assert auth.status == "now"
    assert [i.text for i in auth.items] == [
        "server-side key enforcement",
        "password reset restricted to self/admin",
    ]
    assert [i.checked for i in auth.items] == [False, True]
    assert auth.total == 2 and auth.open_count == 1 and auth.done_count == 1

    billing = ms[1]
    assert billing.title == "Billing Cleanup"
    # no explicit comment -> slugified title
    assert billing.id == "billing-cleanup"
    assert billing.status == "later"
    assert billing.done_count == 0


def test_milestone_with_no_subproject_lands_in_implicit(tmp_path):
    text = """\
## Milestone: payment
status: done
- [x] charge card
"""
    ms, r = _files_milestones(tmp_path, text)

    assert len(ms) == 1
    assert ms[0].subproject == ""   # implicit subproject
    assert ms[0].id == "payment"
    assert ms[0].status == "done"


def test_subproject_scope_rolls_over_between_sections(tmp_path):
    """A new milestone after a subproject header keeps that subproject."""
    text = """\
# Subproject: portal

## Milestone: auth
- [ ] enforce

# Subproject: billing

## Milestone: invoicing
- [ ] reconcile
"""
    ms, r = _files_milestones(tmp_path, text)

    assert [(m.title, m.subproject, m.id) for m in ms] == [
        ("auth", "portal", "auth"),
        ("invoicing", "billing", "invoicing"),
    ]


def test_explicit_id_wins_over_slug(tmp_path):
    """When the prose and the comment disagree, the explicit id wins."""
    text = """\
## Milestone: Brand New Name          <!-- id: keep-this-id -->
- [ ] card
"""
    ms, r = _files_milestones(tmp_path, text)

    assert ms[0].id == "keep-this-id"
    assert ms[0].title == "Brand New Name"


def test_slug_fallback_stable_across_prose_edits(tmp_path):
    """Rewording a milestone's prose must not change its slug id.

    The slug collapses case, punctuation and whitespace, so style edits (which
    keep the meaningful words) leave the id stable — renaming prose must not
    orphan the milestone's cards.
    """
    a, _ = _files_milestones(tmp_path, "## Milestone: Payment Gateway Rails\n- [ ] x\n")
    b, _ = _files_milestones(
        tmp_path, "## Milestone: Payment-Gateway & Rails!!!\n- [ ] x\n"
    )
    # different prose styling, same stable slug id
    assert a[0].id == b[0].id == "payment-gateway-rails"
    assert a[0].title != b[0].title


def test_slug_lowercases_and_hyphenates(tmp_path):
    ms, r = _files_milestones(
        tmp_path, "## Milestone: Auth Hardening!\n- [ ] x\n"
    )
    assert ms[0].id == "auth-hardening"
    assert ms[0].title == "Auth Hardening!"


def test_duplicate_id_errors_naming_both_lines(tmp_path):
    """A duplicate id in one file is an ERROR naming both lines — never last-wins."""
    text = """\
## Milestone: First Section          <!-- id: clash -->
- [ ] a

## Milestone: Second Section         <!-- id: clash -->
- [ ] b
"""
    p = _write(tmp_path / "ROADMAP.md", text)

    import pytest

    with pytest.raises(DuplicateMilestoneIdError) as ei:
        parse_roadmap(str(p))
    err = ei.value
    assert err.milestone_id == "clash"
    assert "First Section" in str(err) and "Second Section" in str(err)
    # both source lines are named, so the operator can find each duplicate
    assert "First Section" in err.first and "Second Section" in err.second


def test_duplicate_slug_from_sections_is_also_an_error(tmp_path):
    """Two legacy sections that slug to the same id are a duplicate too."""
    text = """\
## Now
- [ ] a

## now
- [ ] b
"""
    p = _write(tmp_path / "ROADMAP.md", text)

    import pytest

    with pytest.raises(DuplicateMilestoneIdError):
        parse_roadmap(str(p))


def test_checked_x_counts_as_done_in_new_format(tmp_path):
    text = """\
## Milestone: gate
- [x] shipped
- [ ] pending
"""
    ms, r = _files_milestones(tmp_path, text)

    m = ms[0]
    assert m.done_count == 1
    assert m.open_count == 1
    assert [i.checked for i in m.items] == [True, False]


# --------------------------------------------------------------------------- #
# roadmap adopt — promote a reviewed docs/ROADMAP.draft.md
# --------------------------------------------------------------------------- #

from flightdeck.cli import main as _main  # noqa: E402

DRAFT = """\
# Subproject: alpha

## Milestone: core <!-- id: core -->
status: now
- [x] shipped item
- [ ] finished-by-hand
- [ ] new item
"""

EXISTING = """\
# Subproject: alpha

## Milestone: core <!-- id: core -->
status: now
- [x] shipped item
- [x] finished-by-hand
- [x] legacy item
"""


def _setup_project(tmp_path, name, roadmap_text=None, draft_text=DRAFT):
    """Create a project repo with a docs/ dir, optional ROADMAP.md + draft.

    Returns (registry_path, repo_path). No test touches the real board, git,
    Telegram or the network — everything lives under tmp_path.
    """
    repo = tmp_path / name
    docs = repo / "docs"
    docs.mkdir(parents=True)
    if roadmap_text is not None:
        (docs / "ROADMAP.md").write_text(roadmap_text, encoding="utf-8")
    (docs / "ROADMAP.draft.md").write_text(draft_text, encoding="utf-8")
    reg = tmp_path / f"{name}.yaml"
    reg.write_text(
        f"projects:\n  - name: {name}\n    repo: {repo}\n    roadmap: docs/ROADMAP.md\n",
        encoding="utf-8",
    )
    return str(reg), repo


def test_adopt_refuses_missing_draft(tmp_path, capsys):
    reg, repo = _setup_project(
        tmp_path, "p1", roadmap_text=EXISTING
    )
    (repo / "docs" / "ROADMAP.draft.md").unlink()

    rc = _main(["--registry", reg, "roadmap", "adopt", "p1", "--apply"])

    assert rc == 1
    assert "no draft" in capsys.readouterr().err
    # nothing was touched
    assert (repo / "docs" / "ROADMAP.md").read_text() == EXISTING
    assert not (repo / "docs" / "ROADMAP.md.bak").exists()


def test_adopt_refuses_unparseable_draft_and_writes_nothing(tmp_path, capsys):
    # A draft with a milestone but no items does not count as a valid roadmap.
    reg, repo = _setup_project(
        tmp_path, "p1", roadmap_text=EXISTING,
        draft_text="## Milestone: core <!-- id: core -->\nstatus: now\n",
    )

    rc = _main(["--registry", reg, "roadmap", "adopt", "p1", "--apply"])

    assert rc == 1
    assert "refusing" in capsys.readouterr().err
    # existing roadmap untouched, no backup, draft still present
    assert (repo / "docs" / "ROADMAP.md").read_text() == EXISTING
    assert not (repo / "docs" / "ROADMAP.md.bak").exists()
    assert (repo / "docs" / "ROADMAP.draft.md").exists()


def test_adopt_diff_lists_added_removed_and_checked_state_changes(tmp_path, capsys):
    reg, _ = _setup_project(
        tmp_path, "p1", roadmap_text=EXISTING, draft_text=DRAFT
    )

    rc = _main(["--registry", reg, "roadmap", "adopt", "p1"])

    assert rc == 0
    out = capsys.readouterr().out
    # added item from the draft
    assert "added item" in out and "new item" in out
    # removed legacy item (present in existing, dropped by draft)
    assert "removed item" in out and "legacy item" in out
    # the load-bearing flag: finished-by-hand un-ticked by the model
    assert "CHECK-STATE CHANGED" in out
    assert "finished-by-hand" in out and "was done, now open" in out


def test_adopt_reports_added_and_removed_milestones(tmp_path, capsys):
    reg, _ = _setup_project(
        tmp_path, "p1",
        roadmap_text="## Milestone: old <!-- id: old -->\nstatus: now\n- [x] stale\n",
        draft_text="## Milestone: brand-new <!-- id: brand-new -->\nstatus: now\n- [ ] fresh\n",
    )

    rc = _main(["--registry", reg, "roadmap", "adopt", "p1"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "added milestone: brand-new" in out
    assert "removed milestone: old" in out


def test_adopt_apply_backs_up_existing_and_reports_path(tmp_path, capsys):
    reg, repo = _setup_project(
        tmp_path, "p1", roadmap_text=EXISTING, draft_text=DRAFT
    )

    rc = _main(["--registry", reg, "roadmap", "adopt", "p1", "--apply"])

    assert rc == 0
    out = capsys.readouterr().out
    bak = repo / "docs" / "ROADMAP.md.bak"
    assert bak.exists(), "existing roadmap must be backed up before overwriting"
    # the backup holds the PRE-adoption roadmap
    assert bak.read_text() == EXISTING
    # and the command says where the backup went
    assert "backed up existing roadmap to" in out
    assert str(bak) in out


def test_adopt_dry_run_writes_and_removes_nothing(tmp_path, capsys):
    reg, repo = _setup_project(
        tmp_path, "p1", roadmap_text=EXISTING, draft_text=DRAFT
    )

    rc = _main(["--registry", reg, "roadmap", "adopt", "p1"])

    assert rc == 0
    assert "dry-run" in capsys.readouterr().err
    # roadmap unchanged, no backup, draft still present
    assert (repo / "docs" / "ROADMAP.md").read_text() == EXISTING
    assert not (repo / "docs" / "ROADMAP.md.bak").exists()
    assert (repo / "docs" / "ROADMAP.draft.md").exists()


def test_adopt_apply_promotes_and_removes_draft(tmp_path, capsys):
    reg, repo = _setup_project(
        tmp_path, "p1", roadmap_text=EXISTING, draft_text=DRAFT
    )

    rc = _main(["--registry", reg, "roadmap", "adopt", "p1", "--apply"])

    assert rc == 0
    # the draft is gone
    assert not (repo / "docs" / "ROADMAP.draft.md").exists()
    # and the promoted ROADMAP.md parses with the draft's content
    from flightdeck.core.roadmap import parse_roadmap
    r = parse_roadmap(str(repo / "docs" / "ROADMAP.md"))
    assert r.present is True
    assert r.milestones and _milestone(r, "core")
    assert [i.text for i in _milestone(r, "core").items] == [
        "shipped item",
        "finished-by-hand",
        "new item",
    ]


def test_adopt_fresh_project_clean_without_backup(tmp_path, capsys):
    reg, repo = _setup_project(tmp_path, "p1", roadmap_text=None, draft_text=DRAFT)

    rc = _main(["--registry", reg, "roadmap", "adopt", "p1", "--apply"])

    assert rc == 0
    # no backup was made (there was nothing to back up)
    assert not (repo / "docs" / "ROADMAP.md.bak").exists()
    # draft promoted and removed
    assert not (repo / "docs" / "ROADMAP.draft.md").exists()
    r = parse_roadmap(str(repo / "docs" / "ROADMAP.md"))
    assert r.present is True and _milestone(r, "core")
    assert not any("backed up" in line for line in capsys.readouterr().err.splitlines())
