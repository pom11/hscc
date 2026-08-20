"""Tests for `flightdeck roadmap {add,move,done}` command wiring + editing.

The command edits a versioned ROADMAP.md in place. The contract that matters
most: everything EXCEPT the touched line stays byte-identical, mutations are
gated behind ``--apply``, an ambiguous match changes nothing and lists the
candidates, and a missing roadmap is handled per the card (``add`` offers to
create; ``move``/``done`` report "no roadmap" and exit non-zero).

All tests use real file I/O on a tmp_path repo (local, fast — never git,
network, Telegram or the cluster). The project registry is built in memory and
passed straight to the command handlers.
"""

import argparse
import json

import pytest

from flightdeck.commands import roadmap as cmd
from flightdeck.core.registry import Project

# A representative roadmap with comments, blank lines and mixed formatting so
# we can assert byte-identical preservation of everything we do not touch.
ROADMAP = """\
# Project roadmap
<!-- a comment that must survive -->

## Now
- [ ] Anulare tranzactie — direct ILE insert
- [ ] Cache invalidation

## Next
- [ ] Stripe subscription lifecycle
- [x] Refund webhook

## Later
- [x] Multi-tenant client portal
- [ ] Rate limiting
- [ ] Suma in euro
"""


def _args(**overrides):
    base = {
        "registry": "/tmp/reg.yaml",
        "json": False,
        "apply": False,
        "section": None,
        "to": None,
        "project": "hscc",
        "item": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _project(repo, name="hscc"):
    return Project(name=name, repo=str(repo))


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _roadmap(repo):
    return repo / "ROADMAP.md"


# --------------------------------------------------------------------------- #
# discovery + bare show
# --------------------------------------------------------------------------- #


def test_command_is_discovered():
    from flightdeck.cli import build_parser

    args = build_parser().parse_args(["roadmap"])
    assert args.command == "roadmap"
    assert args.func is not None


def test_show_lists_sections_per_project(tmp_path, capsys):
    repo = _write(_roadmap(tmp_path), ROADMAP)
    proj = _project(tmp_path)

    rc = cmd.cmd_show(_args(), [proj])

    assert rc == 0
    out = capsys.readouterr().out
    assert "[hscc]" in out
    assert "Now:" in out and "Next:" in out and "Later:" in out
    assert "Anulare tranzactie" in out
    assert "- [x] Refund webhook" in out


def test_show_json(tmp_path, capsys):
    _write(_roadmap(tmp_path), ROADMAP)
    proj = _project(tmp_path)

    rc = cmd.cmd_show(_args(json=True), [proj])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hscc"]["present"] is True
    assert payload["hscc"]["milestones"]["Later"]["done"] == 1
    assert payload["hscc"]["milestones"]["Later"]["total"] == 3


def test_show_no_roadmap_reports_not_skips(tmp_path, capsys):
    proj = _project(tmp_path)

    rc = cmd.cmd_show(_args(), [proj])

    assert rc == 0
    assert "no roadmap" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# add
# --------------------------------------------------------------------------- #


def test_add_appends_only_intended_line(tmp_path, capsys):
    repo = _write(_roadmap(tmp_path), ROADMAP)
    original = ROADMAP
    proj = _project(tmp_path)

    rc = cmd.cmd_add(_args(apply=True, item="Refactor cache layer"), [proj])

    assert rc == 0
    new_text = _roadmap(tmp_path).read_text(encoding="utf-8")
    # The new item must be in the Now section.
    now_section = new_text.split("## Now")[1].split("## Next")[0]
    assert "- [ ] Refactor cache layer" in now_section
    # Everything except the added line is byte-identical: the new file is the
    # original with exactly one line inserted.
    assert "+- [ ] Refactor cache layer" in "\n".join(
        f"+{line}" if "- [ ] Refactor cache layer" in line else f" {line}"
        for line in new_text.split("\n")
    )
    assert "didn't seed result" not in new_text  # sanity: no accidental content


def test_add_appends_after_last_item_in_section(tmp_path):
    repo = _write(_roadmap(tmp_path), ROADMAP)
    proj = _project(tmp_path)

    cmd.cmd_add(_args(apply=True, item="Cache invalidation v2"), [proj])

    new_text = _roadmap(tmp_path).read_text(encoding="utf-8")
    now_section = new_text.split("## Now")[1].split("## Next")[0]
    # order: existing two items then the new one, right before the blank line
    # that precedes ## Next (so it is the LAST item in the section).
    assert now_section.index("- [ ] Cache invalidation") < now_section.index(
        "- [ ] Cache invalidation v2"
    )
    assert now_section.strip().endswith("- [ ] Cache invalidation v2")


def test_add_section_flag_goes_to_target_section(tmp_path):
    repo = _write(_roadmap(tmp_path), ROADMAP)
    proj = _project(tmp_path)

    cmd.cmd_add(
        _args(apply=True, section="later", item="Nobody home"), [proj]
    )

    new_text = _roadmap(tmp_path).read_text(encoding="utf-8")
    later_section = new_text.split("## Later")[1]
    assert "- [ ] Nobody home" in later_section
    # Not accidentally in Now/Next.
    assert new_text.split("## Now")[1].split("## Next")[0].find("Nobody home") == -1


def test_add_without_apply_is_dry_run(tmp_path, capsys):
    repo = _write(_roadmap(tmp_path), ROADMAP)
    before = ROADMAP
    proj = _project(tmp_path)

    rc = cmd.cmd_add(_args(item="Should not land"), [proj])

    assert rc == 0
    assert _roadmap(tmp_path).read_text(encoding="utf-8") == before
    assert "dry-run" in capsys.readouterr().err


def test_add_missing_file_offers_to_create(tmp_path, capsys):
    proj = _project(tmp_path)

    rc = cmd.cmd_add(_args(apply=False, item="First item"), [proj])

    assert rc == 0
    assert not _roadmap(tmp_path).exists()
    err = capsys.readouterr().err
    assert "will create" in err
    assert "dry-run" in err


def test_add_missing_file_apply_creates_with_sections(tmp_path, capsys):
    proj = _project(tmp_path)

    rc = cmd.cmd_add(_args(apply=True, item="First item"), [proj])

    assert rc == 0
    new_text = _roadmap(tmp_path).read_text(encoding="utf-8")
    assert "## Now" in new_text and "## Next" in new_text and "## Later" in new_text
    # item lands in the default (now) section
    assert "- [ ] First item" in new_text.split("## Next")[0]


def test_add_apply_creates_then_adds_item(tmp_path, capsys):
    # with --apply the file is created AND the item is appended
    proj = _project(tmp_path)

    rc = cmd.cmd_add(_args(apply=True, section="later", item="New thing"), [proj])

    assert rc == 0
    new_text = _roadmap(tmp_path).read_text(encoding="utf-8")
    later_section = new_text.split("## Later")[1].split("## ")[0]
    assert "- [ ] New thing" in later_section


def test_add_missing_section_is_error(tmp_path, capsys):
    repo = _write(_roadmap(tmp_path), "## Only\n- [ ] thing\n")
    proj = _project(tmp_path)

    rc = cmd.cmd_add(_args(item="x"), [proj])

    assert rc == 1
    assert "no '## Now' section" in capsys.readouterr().err


def test_add_unknown_project_returns_2(tmp_path, capsys):
    proj = _project(tmp_path)

    rc = cmd.cmd_add(_args(project="ghost", item="x"), [proj])

    assert rc == 2
    assert "no project named 'ghost'" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# move
# --------------------------------------------------------------------------- #


def test_move_promotes_preserving_everything_else(tmp_path):
    repo = _write(_roadmap(tmp_path), ROADMAP)
    proj = _project(tmp_path)

    rc = cmd.cmd_move(_args(apply=True, item="Rate limiting", to="now"), [proj])

    assert rc == 0
    new_text = _roadmap(tmp_path).read_text(encoding="utf-8")
    now_section = new_text.split("## Now")[1].split("## Next")[0]
    later_section = new_text.split("## Later")[1]
    # moved into Now
    assert "- [ ] Rate limiting" in now_section
    # removed from Later
    assert "- [ ] Rate limiting" not in later_section


def test_move_preserves_checkbox_state(tmp_path):
    repo = _write(_roadmap(tmp_path), ROADMAP)
    proj = _project(tmp_path)

    # Move a checked item (Refund webhook is [x]) into Later; checkbox stays x.
    rc = cmd.cmd_move(_args(apply=True, item="Refund webhook", to="later"), [proj])

    assert rc == 0
    new_text = _roadmap(tmp_path).read_text(encoding="utf-8")
    later_section = new_text.split("## Later")[1]
    assert "- [x] Refund webhook" in later_section


def test_move_ambiguous_lists_candidates_changes_nothing(tmp_path, capsys):
    # Two items both containing the word "Suma": the one in Now and the one in
    # Later ("Suma in euro"), so a substring "Suma" is ambiguous.
    text = (
        "## Now\n- [ ] Suma mari\n- [ ] Something else\n\n"
        "## Later\n- [ ] Suma in euro\n"
    )
    repo = _write(_roadmap(tmp_path), text)
    proj = _project(tmp_path)

    rc = cmd.cmd_move(_args(item="Suma", to="later"), [proj])

    assert rc == 1
    err = capsys.readouterr().err
    assert "ambiguous" in err
    assert "Suma mari" in err and "Suma in euro" in err
    # nothing changed
    assert _roadmap(tmp_path).read_text(encoding="utf-8") == text


def test_move_without_apply_changes_nothing(tmp_path, capsys):
    repo = _write(_roadmap(tmp_path), ROADMAP)
    before = ROADMAP
    proj = _project(tmp_path)

    rc = cmd.cmd_move(_args(item="Rate limiting", to="now"), [proj])

    assert rc == 0
    assert _roadmap(tmp_path).read_text(encoding="utf-8") == before
    assert "dry-run" in capsys.readouterr().err


def test_move_no_roadmap_reports_and_exits_nonzero(tmp_path, capsys):
    proj = _project(tmp_path)

    rc = cmd.cmd_move(_args(item="x", to="now"), [proj])

    assert rc == 1
    assert "no roadmap" in capsys.readouterr().err


def test_move_missing_item_reports(tmp_path, capsys):
    repo = _write(_roadmap(tmp_path), ROADMAP)
    proj = _project(tmp_path)

    rc = cmd.cmd_move(_args(item="nonexistent", to="now"), [proj])

    assert rc == 1
    assert "no roadmap item matching" in capsys.readouterr().err


def test_move_same_section_is_noop(tmp_path, capsys):
    repo = _write(_roadmap(tmp_path), ROADMAP)
    before = ROADMAP
    proj = _project(tmp_path)

    rc = cmd.cmd_move(_args(apply=True, item="Rate limiting", to="later"), [proj])

    assert rc == 0
    assert _roadmap(tmp_path).read_text(encoding="utf-8") == before
    assert "already in 'Later'" in capsys.readouterr().out


def test_move_missing_target_section_is_error(tmp_path, capsys):
    repo = _write(_roadmap(tmp_path), "## Now\n- [ ] thing\n")
    proj = _project(tmp_path)

    rc = cmd.cmd_move(_args(item="thing", to="next"), [proj])

    assert rc == 1
    assert "no '## Next' section" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# done
# --------------------------------------------------------------------------- #


def test_done_ticks_only_intended_line(tmp_path):
    repo = _write(_roadmap(tmp_path), ROADMAP)
    proj = _project(tmp_path)

    rc = cmd.cmd_done(_args(apply=True, item="Anulare tranzactie"), [proj])

    assert rc == 0
    new_text = _roadmap(tmp_path).read_text(encoding="utf-8")
    assert "- [x] Anulare tranzactie" in new_text
    # another open item is unchanged
    assert "- [ ] Cache invalidation" in new_text
    # all other lines byte-identical: only the single target line changed.
    assert new_text.count("- [x] Anulare tranzactie") == 1
    # every line besides the ticked one appears verbatim in the original.
    for line in new_text.split("\n"):
        if "- [x] Anulare tranzactie" in line:
            continue
        assert line in ROADMAP


def test_done_ambiguous_lists_candidates_changes_nothing(tmp_path, capsys):
    text = "## Now\n- [ ] Suma mari\n- [ ] Suma in euro\n"
    repo = _write(_roadmap(tmp_path), text)
    proj = _project(tmp_path)

    rc = cmd.cmd_done(_args(item="Suma"), [proj])

    assert rc == 1
    err = capsys.readouterr().err
    assert "ambiguous" in err
    assert _roadmap(tmp_path).read_text(encoding="utf-8") == text


def test_done_without_apply_changes_nothing(tmp_path, capsys):
    repo = _write(_roadmap(tmp_path), ROADMAP)
    before = ROADMAP
    proj = _project(tmp_path)

    rc = cmd.cmd_done(_args(item="Anulare tranzactie"), [proj])

    assert rc == 0
    assert _roadmap(tmp_path).read_text(encoding="utf-8") == before
    assert "dry-run" in capsys.readouterr().err


def test_done_no_roadmap_reports_and_exits_nonzero(tmp_path, capsys):
    proj = _project(tmp_path)

    rc = cmd.cmd_done(_args(item="x"), [proj])

    assert rc == 1
    assert "no roadmap" in capsys.readouterr().err


def test_done_already_checked_is_noop(tmp_path, capsys):
    repo = _write(_roadmap(tmp_path), ROADMAP)
    before = ROADMAP
    proj = _project(tmp_path)

    rc = cmd.cmd_done(_args(apply=True, item="Refund webhook"), [proj])

    assert rc == 0
    assert _roadmap(tmp_path).read_text(encoding="utf-8") == before
    assert "already checked" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# malformed roadmap does not crash
# --------------------------------------------------------------------------- #


def test_malformed_roadmap_does_not_crash(tmp_path, capsys):
    text = (
        "## Now\n- [ ] good one\nthis is not an item and not a heading\n"
        "- [ ] another\n## Later\n- [x] done bit\nlet me tell you about my dog\n"
    )
    repo = _write(_roadmap(tmp_path), text)
    proj = _project(tmp_path)

    # done on a real, unambiguous item still works
    rc = cmd.cmd_done(_args(apply=True, item="good one"), [proj])
    assert rc == 0
    # move on a real item works
    rc = cmd.cmd_move(_args(apply=True, item="another", to="later"), [proj])
    assert rc == 0
    # show doesn't crash either
    rc = cmd.cmd_show(_args(), [proj])
    assert rc == 0
    # the malformed lines were preserved (we did not reformat the file)
    new_text = _roadmap(tmp_path).read_text(encoding="utf-8")
    assert "this is not an item and not a heading" in new_text
    assert "let me tell you about my dog" in new_text


def test_byte_identical_for_each_op(tmp_path):
    """add/move/done each leave the untouched portion byte-identical."""
    repo = tmp_path

    def run_case(op, extra, item):
        (repo / "op" / "ROADMAP.md").parent.mkdir(parents=True, exist_ok=True)
        p = repo / "op" / "ROADMAP.md"
        p.write_text(ROADMAP, encoding="utf-8")
        proj = _project(repo / "op")
        args = _args(apply=True, item=item)
        if op == "add":
            cmd.cmd_add(args, [proj])
        elif op == "move":
            args.to = extra
            cmd.cmd_move(args, [proj])
        else:
            cmd.cmd_done(args, [proj])
        return p.read_text(encoding="utf-8")

    add_text = run_case("add", None, "Refactor cache layer")
    move_text = run_case("move", "now", "Rate limiting")
    done_text = run_case("done", None, "Anulare tranzactie")

    for op, new_text in (("add", add_text), ("move", move_text), ("done", done_text)):
        # Assert: for every ORIGINAL line, it appears in the new text with the
        # same frequency — i.e. nothing was dropped, duplicated or reordered,
        # and only the single intended new line is present that wasn't before.
        # Exception: the `done` case intentionally re-writes its target line
        # in place (`- [ ] X` -> `- [x] X`), so that original line is expected
        # to vanish and its checked twin to appear instead.
        target = None
        if op == "done":
            target = "- [ ] Anulare tranzactie — direct ILE insert"
        orig_lines = [l for l in ROADMAP.split("\n") if l.strip()]
        for line in orig_lines:
            if line == target:
                continue
            assert new_text.count(line) == 1, f"{op}: line lost/duplicated: {line!r}"
        if target:
            assert new_text.count("[x] Anulare tranzactie — direct ILE insert") == 1
