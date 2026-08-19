"""Tests for the `flightdeck incident` command layer.

`incident` appends a dated, five-field lesson to a project's
``docs/INCIDENTS.md``. The contract that matters most: it is a plain
newest-first log — a new entry appears at the TOP, existing entries are never
rewritten or reformatted (byte-for-byte preserved), and nothing is written at
all without ``--apply``.

No test touches a live repo: each project's ``repo`` points at a ``tmp_path``,
so all file I/O happens under the test's own directory.
"""

import argparse
import re

import pytest

from flightdeck.commands import incident as cmd
from flightdeck.core.registry import Project


def _project(repo, name="flightdeck"):
    return Project(name=name, repo=str(repo))


def _args(repo, **overrides):
    base = {
        "registry": "/tmp/reg.yaml",
        "symptom": "probe reported the service down",
        "project": None,
        "fix": "the shared probe now sends a POST",
        "cause": "bare GET against a POST-only endpoint",
        "lesson": "never probe with a method the endpoint rejects",
        "apply": True,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _run(tmp_path, **overrides):
    repo = tmp_path / "repo"
    repo.mkdir()
    args = _args(repo, **overrides)
    return cmd.cmd_incident(args, [_project(repo)])


def _incidents_path(repo):
    return repo / "docs" / "INCIDENTS.md"


# --------------------------------------------------------------------------- #
# appending: dated heading + all five fields
# --------------------------------------------------------------------------- #


def test_appends_entry_with_dated_heading_and_all_five_fields(tmp_path):
    rc = _run(
        tmp_path,
        symptom="queue timeout read as a wedged span",
        fix="sample generation_tokens_total 60s apart",
        cause="45s client timeout measures queue depth, not liveness",
        lesson="the honest probe is queue depth, not liveness",
    )
    assert rc == 0
    text = _incidents_path(tmp_path / "repo").read_text()
    assert re.search(r"## \d{4}-\d{2}-\d{2} — queue timeout read as a wedged span",
                     text)
    for field in ("Project", "Symptom", "Cause", "Fix", "Lesson"):
        assert f"**{field}:**" in text, f"missing **{field}:** field"
    assert "**Lesson:** the honest probe is queue depth, not liveness" in text


# --------------------------------------------------------------------------- #
# file created with a header when absent
# --------------------------------------------------------------------------- #


def test_creates_file_with_header_when_absent(tmp_path):
    rc = _run(tmp_path, symptom="reconcile tried to close running cards")
    assert rc == 0
    text = _incidents_path(tmp_path / "repo").read_text()
    assert text.startswith("# Incidents\n")
    assert "## " in text  # the entry's heading follows the header


# --------------------------------------------------------------------------- #
# existing entries untouched byte-for-byte
# --------------------------------------------------------------------------- #


def test_existing_entries_preserved_byte_for_byte(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    incidents = _incidents_path(repo)
    incidents.parent.mkdir()
    prior = (
        "# Incidents\n\n"
        "## 2020-01-01 — an old one\n"
        "**Project:** flightdeck\n"
        "**Symptom:** old symptom\n"
        "**Cause:** old cause\n"
        "**Fix:** old fix\n"
        "**Lesson:** old lesson\n"
    )
    incidents.write_text(prior, encoding="utf-8")
    old_entry = (
        "## 2020-01-01 — an old one\n"
        "**Project:** flightdeck\n"
        "**Symptom:** old symptom\n"
        "**Cause:** old cause\n"
        "**Fix:** old fix\n"
        "**Lesson:** old lesson\n"
    )

    rc = cmd.cmd_incident(_args(repo, apply=True), [_project(repo)])
    assert rc == 0

    text = incidents.read_text()
    # The exact prior entry lines appear verbatim (byte-for-byte) inside the new file.
    assert old_entry in text
    # Sanity: the new entry was actually added, not just echoed.
    assert "**Symptom:** probe reported" in text and "probe" in text


# --------------------------------------------------------------------------- #
# newest-first ordering
# --------------------------------------------------------------------------- #


def test_newest_first_ordering(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    incidents = _incidents_path(repo)
    incidents.parent.mkdir()
    incidents.write_text(
        "# Incidents\n\n"
        "## 2021-06-01 — older\n"
        "**Project:** flightdeck\n"
        "**Symptom:** s\n**Cause:** c\n**Fix:** f\n**Lesson:** l\n",
        encoding="utf-8",
    )

    cmd.cmd_incident(_args(repo, apply=True, symptom="the new one"), [_project(repo)])

    text = incidents.read_text()
    assert text.index("the new one") < text.index("2021-06-01 — older")
    assert text.startswith("# Incidents\n")


# --------------------------------------------------------------------------- #
# --apply required to write
# --------------------------------------------------------------------------- #


def test_apply_required_to_write(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = cmd.cmd_incident(_args(repo, apply=False), [_project(repo)])
    assert rc == 0
    assert not _incidents_path(repo).exists(), "dry-run must not write the file"


# --------------------------------------------------------------------------- #
# unknown project errors listing known ones
# --------------------------------------------------------------------------- #


def test_unknown_project_errors_listing_known_ones(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    args = _args(repo, project="nope")
    rc = cmd.cmd_incident(args, [_project(repo, name="alpha"), _project(repo, name="beta")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no project named 'nope'" in err
    assert "alpha" in err and "beta" in err
    assert not _incidents_path(repo).exists(), "unknown project must not write"


def test_default_project_is_flightdeck(tmp_path):
    # --project omitted resolves to the `flightdeck` project in the registry.
    rc = _run(tmp_path)
    assert rc == 0
    assert _incidents_path(tmp_path / "repo").exists()


# --------------------------------------------------------------------------- #
# command discovery
# --------------------------------------------------------------------------- #


def test_command_is_discovered():
    from flightdeck.cli import build_parser

    args = build_parser().parse_args(["incident", "something broke"])
    assert args.command == "incident"
    assert args.func is not None


# --------------------------------------------------------------------------- #
# seeded INCIDENTS.md parses as markdown and contains the five entries
# --------------------------------------------------------------------------- #


def test_seeded_incidents_parses_as_markdown_and_has_five_entries():
    """The five seeded incidents in this repo's docs/INCIDENTS.md are valid.

    Each entry must carry a dated ``##`` heading and all five bold fields, and
    the file must contain exactly the five seeded incidents in newest-first
    order. Asserting on the repo's own docs file keeps the seed honest: if the
    seed drifts this test catches it.
    """
    import os

    from flightdeck.commands import incident as _inc

    path = _inc._INCIDENTS_RELPATH
    text = _read_seeded(os.path.join(_repo_root(), path))
    assert text is not None, f"seeded {path} is missing"

    headings = re.findall(r"^## \d{4}-\d{2}-\d{2} — .+", text, re.MULTILINE)
    # One entry block = a ## heading followed by its five **Field:** lines.
    blocks = re.split(r"(?m)^## ", text)[1:]
    assert len(headings) == 5, f"expected 5 entries, found {len(headings)}"
    for block in blocks:
        for field in ("Project", "Symptom", "Cause", "Fix", "Lesson"):
            assert f"**{field}:**" in block, f"entry missing **{field}:** field"

    # Newest-first: each heading's date must be non-increasing down the file.
    dates = [h[3:13] for h in headings]
    assert dates == sorted(dates, reverse=True), "entries must be newest-first"


def _repo_root() -> str:
    """The repo root (this worktree), for locating docs/INCIDENTS.md."""
    import os

    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_seeded(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, IOError, UnicodeDecodeError):
        return None
