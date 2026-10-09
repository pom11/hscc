"""Rich-markup-injection regression for the flightdeck human views (t_12f3c8a8).

Same failure class as ``hscc_daemon/tests/test_markup_escape.py`` (t_716cf37c),
which this file deliberately mirrors in idiom: Rich parses every ``str``
renderable as MARKUP, so a project name, repo path, roadmap path, card id,
daemon PID, verify duration or state-file message containing ``[/bold]`` — any
unmatched closing tag — raised ``rich.errors.MarkupError`` in the middle of a
render and took the whole command view down with a traceback.

The contract these tests pin:

  * a value containing ``[/bold]`` / ``[bold]`` reaching ANY touched flightdeck
    render surface (``Console.print``, ``Table.add_row``, ``Panel`` body AND
    ``Panel``/``Table`` title) renders LITERALLY and never raises;
  * the ``[ok]``/``[error]``/``[warn]``/``[dim]``/``[label]`` styles the
    renderer itself builds still work — we escape the DATA, not the markup;
  * the escape happens ONCE. A title is escaped inside ``_theme`` (mirroring
    ``cli_theme._view_title``), so a call site that pre-escapes a title would
    show the operator literal backslashes — pinned below too.

Why the two tag forms are both asserted: ``[/bold]`` alone raises (a closing
tag with no open tag), while ``[bold]`` alone does not raise but SILENTLY
swallows the rest of the line into bold. A sweep that only survived the first
form would have "passed" while still corrupting the view.

Both render paths are exercised: the themed one (``hscc_daemon.cli_theme``
importable, as in a deployed ``hscc`` process) and the plain-Console fallback
(standalone flightdeck install), because the fallback has its own title-escape
code.

``--json`` stays plain ``print`` by design and is covered by the per-command
no-ANSI pins, not here.
"""

import argparse
import io
import os
from contextlib import redirect_stdout

import pytest
from rich.errors import MarkupError

from flightdeck.commands import _theme
from flightdeck.commands import archive as archive_cmd
from flightdeck.commands import daemon as daemon_cmd
from flightdeck.commands import map_sessions as maps_cmd
from flightdeck.commands import project as project_cmd
from flightdeck.commands import release as release_cmd
from flightdeck.commands import report as report_cmd
from flightdeck.commands import review as review_cmd
from flightdeck.commands import roadmap as roadmap_cmd
from flightdeck.commands import start as start_cmd
from flightdeck.commands import verify as verify_cmd
from flightdeck.core import archive as archive_core
from flightdeck.core import map_sessions as maps_core
from flightdeck.core import registry
from flightdeck.core import verify as verify_core

MARK_CLOSE = "[/bold]"
MARK_BOTH = "a[bold]b[/bold]c"


def _run(fn):
    """Run ``fn`` capturing stdout; SystemExit is part of the CLI contract.

    ANY other exception (MarkupError in particular) fails the test — that IS
    the bug being pinned.
    """
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            fn()
    except SystemExit:
        pass
    return buf.getvalue()


def _assert_literal(out, value=MARK_CLOSE):
    assert value in out, f"markup not rendered literally: {out!r}"


def _assert_no_swallow(out, marker=MARK_BOTH):
    """A title must neither raise nor swallow the text around the tags.

    A Panel/Table TITLE is escaped centrally (_view_title / _theme.panel), so
    the rendered bytes are the escaper's spelling rather than the input string —
    comparing them byte-for-byte would test the escaper's implementation. What
    matters is the contract: no MarkupError (the caller asserts that by not
    raising) and every piece of the value still visible, i.e. `[bold]` was not
    consumed as a style that ate the rest of the line.
    """
    for piece in ("a", "b", "c"):
        assert piece in out, f"title text swallowed: {out!r} (from {marker!r})"


@pytest.fixture
def plain_fallback(monkeypatch):
    """Force the plain-Console render path (a standalone flightdeck install).

    ``theme()`` is the single switch every constructor goes through, so
    neutralising it once is enough — no need to also poke the module globals.
    """
    monkeypatch.setattr(_theme, "theme", lambda: None)


@pytest.fixture
def themed():
    """Require the themed path (hscc_daemon.cli_theme importable), else skip."""
    pytest.importorskip("hscc_daemon.cli_theme",
                        reason="peer plugin not on sys.path")


# ── the escaper contract itself ────────────────────────────────────────────

def test_esc_coerces_non_strings():
    """esc() must accept what call sites hand it (ints, Path-ish, None).

    The flightdeck sweep esc()es integers too (PIDs, counters) so a site stays
    safe if the value later becomes a string; that only works if esc() coerces.
    Same contract as ``hscc_daemon.cli_theme.esc``.
    """
    assert _theme.esc(7) == "7"
    assert _theme.esc(None) == "None"
    assert _theme.esc(f"x{MARK_CLOSE}") == r"x\[/bold]"


def test_esc_is_not_escape_alias():
    """``escape`` requires a real str; ``esc`` coerces. They are not the same."""
    from typing import cast

    assert _theme.esc(1) == "1"
    with pytest.raises(TypeError):
        _theme.escape(cast(str, 1))


# ── the render constructors: body AND title, on both render paths ──────────

@pytest.mark.parametrize("path", ["themed", "plain_fallback"],
                         ids=["themed-cli_theme", "plain-fallback"])
def test_panel_title_and_body_survive_markup(request, path):
    """Titles are as much a markup surface as bodies.

    The themed path escapes a title inside ``cli_theme._view_title``; the
    fallback path escapes it in ``_theme.panel``. Both must hold.
    """
    request.getfixturevalue(path)
    buf = io.StringIO()
    console = _theme.make_console(file=buf)
    # The BODY is caller-escaped data (that is the call-site contract), so it
    # must come back byte-identical; the TITLE is escaped centrally, so it is
    # checked for the swallow failure instead.
    console.print(_theme.panel(f"title {MARK_BOTH}", _theme.esc(f"body {MARK_CLOSE}")))
    out = buf.getvalue()
    _assert_literal(out)
    _assert_no_swallow(out)


@pytest.mark.parametrize("path", ["themed", "plain_fallback"],
                         ids=["themed-cli_theme", "plain-fallback"])
def test_status_panel_title_survives_markup(request, path):
    request.getfixturevalue(path)
    buf = io.StringIO()
    console = _theme.make_console(file=buf)
    console.print(_theme.status_panel(f"did the thing {_theme.esc(MARK_CLOSE)}",
                                      status="ok",
                                      title=f"daemon {MARK_BOTH}"))
    out = buf.getvalue()
    _assert_literal(out)
    _assert_no_swallow(out)


@pytest.mark.parametrize("path", ["themed", "plain_fallback"],
                         ids=["themed-cli_theme", "plain-fallback"])
def test_table_title_and_rows_survive_markup(request, path):
    request.getfixturevalue(path)
    buf = io.StringIO()
    console = _theme.make_console(file=buf)
    t = _theme.table(f"projects {MARK_CLOSE}")
    t.add_column("NAME")
    t.add_row(_theme.esc(f"repo{MARK_BOTH}"))
    console.print(t)
    out = buf.getvalue()
    _assert_literal(out, f"repo{MARK_BOTH}")
    _assert_no_swallow(out)


def test_renderer_markup_stays_live_not_escaped():
    """The other half of the contract: DATA escaped, markup NOT.

    If a sweep escaped the whole renderable instead of the value, styling would
    silently disappear and the operator would see raw [ok] tags.
    """
    buf = io.StringIO()
    console = _theme.make_console(file=buf)
    console.print(_theme.panel("t", f"[ok]{_theme.esc('fine')}[/ok]"))
    out = buf.getvalue()
    assert "fine" in out
    assert "[ok]" not in out, f"renderer markup was escaped away: {out!r}"


# ── command-level drives (the actual per-command pin the card asks for) ────
#
# Each drives a REAL command function with markup-bearing data injected at the
# seam the command already exposes, so the assertion covers the whole path from
# operator/file-derived data to the rendered view — not just the helper.

def _registry(tmp_path, projects):
    reg = tmp_path / "registry.yaml"
    registry.save_registry(projects, path=str(reg))
    return str(reg)


def test_daemon_status_state_file_message_survives(monkeypatch):
    """The daemon status table renders a MESSAGE read from a state file.

    A check message (or timestamp) containing a closing tag used to raise while
    the table was being built — the operator could not read `daemon status` at
    all until the state file was hand-edited.
    """
    monkeypatch.setattr(daemon_cmd.d, "get_pid", lambda: 4242)
    monkeypatch.setattr(
        daemon_cmd.d, "read_all_states",
        # cmd_status iterates daemon_cmd.STREAM_NAMES — a fake keyed on an
        # invented stream name would render NOTHING and make the assertions
        # vacuously true. "fleet" is a real stream.
        lambda: {"fleet": {"ok": False,
                           "timestamp": f"2026-10-09T00:00:00{MARK_CLOSE}",
                           "message": f"gpu temp tripped {MARK_BOTH}"}})
    monkeypatch.setattr(daemon_cmd.d, "write_stopped", lambda: None)
    args = argparse.Namespace(json=False)
    out = _run(lambda: daemon_cmd.cmd_status(args, "unused"))
    _assert_literal(out)
    _assert_literal(out, MARK_BOTH)


def test_daemon_status_pid_line_survives(monkeypatch):
    monkeypatch.setattr(daemon_cmd.d, "get_pid", lambda: 1)
    monkeypatch.setattr(daemon_cmd.d, "read_all_states", lambda: {})
    monkeypatch.setattr(daemon_cmd.d, "write_stopped", lambda: None)
    args = argparse.Namespace(json=False)
    out = _run(lambda: daemon_cmd.cmd_status(args, "unused"))
    assert "RUNNING" in out


def test_verify_single_project_name_and_duration_survive(monkeypatch, tmp_path):
    """A project NAME is operator-authored (registry.yaml) and lands in the
    verify panel; so does the formatted duration."""
    name = f"proj{MARK_CLOSE}"
    reg = _registry(tmp_path, [registry.Project(
        name=name, repo=str(tmp_path / "repo"), verify="pytest")])

    monkeypatch.setattr(
        verify_core, "run_verify",
        lambda proj, _run=None, _clock=None: verify_core.VerifyResult(
            status=verify_core.PASS, duration_s=1.5, error=None))
    monkeypatch.setattr(verify_core, "record_result", lambda *a, **k: None)

    args = argparse.Namespace(project=name, all=False, json=False,
                              cwd=str(tmp_path))
    out = _run(lambda: verify_cmd.run(args, reg))
    _assert_literal(out)


def test_verify_all_rows_survive(monkeypatch, tmp_path):
    name = f"alpha{MARK_BOTH}"
    reg = _registry(tmp_path, [registry.Project(
        name=name, repo=str(tmp_path / "repo"), verify="pytest")])

    monkeypatch.setattr(
        verify_core, "run_verify",
        lambda proj, _run=None, _clock=None: verify_core.VerifyResult(
            status=verify_core.FAIL, duration_s=0.25,
            error=f"assertion failed in {MARK_CLOSE}"))
    monkeypatch.setattr(verify_core, "record_result", lambda *a, **k: None)

    args = argparse.Namespace(project=None, all=True, json=False,
                              cwd=str(tmp_path))
    out = _run(lambda: verify_cmd.run(args, reg))
    _assert_literal(out)
    _assert_literal(out, MARK_BOTH)


def test_roadmap_add_path_and_heading_survive(monkeypatch, tmp_path):
    """The roadmap path and section heading reach a status_panel body.

    Both are file/registry derived: the path comes from the project's repo, the
    heading from the section map — and a repo directory literally named
    ``docs[/bold]`` is exactly the kind of thing a closing tag comes from.
    """
    # os.makedirs, not Path.mkdir(): in this sandbox a bare mkdir on a
    # bracket-bearing directory name raises FileNotFoundError while makedirs
    # succeeds. A fixture that cannot build its own directory is not testing
    # the command.
    repo = tmp_path / f"repo{MARK_CLOSE}"
    os.makedirs(str(repo))
    (repo / "ROADMAP.md").write_text(
        "# Roadmap\n\n## Now\n- [ ] something\n", encoding="utf-8")
    reg = _registry(tmp_path, [registry.Project(
        name="alpha", repo=str(repo), roadmap="ROADMAP.md")])

    args = argparse.Namespace(project="alpha", item="a new item",
                              section="now", apply=True, func=roadmap_cmd.cmd_add)
    out = _run(lambda: roadmap_cmd.run(args, reg))
    _assert_literal(out)


def test_roadmap_adopt_backup_path_survives(tmp_path):
    """`roadmap adopt` prints the .bak path — a path, so markup-bearing."""
    repo = tmp_path / f"repo{MARK_BOTH}"
    os.makedirs(str(repo))
    (repo / "ROADMAP.md").write_text(
        "# old\n\n## Now\n- [ ] old item\n", encoding="utf-8")
    # adopt resolves the draft itself as <repo>/docs/ROADMAP.draft.md — the
    # command takes no --draft argument, so the fixture has to plant it there.
    os.makedirs(str(repo / "docs"))
    draft = repo / "docs" / "ROADMAP.draft.md"
    draft.write_text("# new\n\n## Now\n- [ ] x\n", encoding="utf-8")
    reg = _registry(tmp_path, [registry.Project(
        name="alpha", repo=str(repo), roadmap="ROADMAP.md")])

    args = argparse.Namespace(project="alpha", apply=True,
                              func=roadmap_cmd.cmd_adopt)
    out = _run(lambda: roadmap_cmd.run(args, reg))
    _assert_literal(out, MARK_BOTH)
    # the .bak line is a second path render and must survive too
    assert ".bak" in out, f"backup line missing: {out!r}"


def test_archive_sessions_counts_and_index_path_survive(monkeypatch, tmp_path):
    """The archive panel renders a result object whose index_path is a path."""
    marker = f"out{MARK_CLOSE}"
    monkeypatch.setattr(
        archive_core, "archive_sessions",
        lambda **kw: archive_core.ArchiveResult(
            sessions=3, messages=99, bytes_written=1234, files=4,
            by_project={f"proj{MARK_BOTH}": 3},
            unmapped_threads=[f"thread{MARK_CLOSE}"],
            index_path=str(tmp_path / marker / "index.json")))

    args = argparse.Namespace(json=False, out=str(tmp_path / marker),
                              db="x.db", registry=None, project=None,
                              func=archive_cmd.cmd_archive_sessions)
    out = _run(lambda: archive_cmd.run(args, "unused"))
    _assert_literal(out)
    _assert_literal(out, MARK_BOTH)


def test_map_sessions_counts_survive(monkeypatch, tmp_path):
    monkeypatch.setattr(
        maps_core, "propose_owners",
        lambda **kw: maps_core.MapResult(
            total=7, resolved_repo_path=3, resolved_model=2, unknown=2,
            deterministic_by_project={f"proj{MARK_BOTH}": 3},
            model_by_project={f"other{MARK_CLOSE}": 2},
            proposals=[]))
    monkeypatch.setattr(maps_core, "write_proposal",
                        lambda result, out_dir, ts: (str(tmp_path / "p.md"),
                                                     str(tmp_path / "p.json")))

    args = argparse.Namespace(json=False, out=str(tmp_path), db="x.db",
                              registry=None, ask=None, apply=False)
    out = _run(lambda: maps_cmd.cmd_map_sessions(args))
    _assert_literal(out)
    _assert_literal(out, MARK_BOTH)


def test_report_window_string_survives(monkeypatch, tmp_path):
    """report's `_window_str` is interpolated into a panel body."""
    proj = registry.Project(name=f"alpha{MARK_CLOSE}",
                            repo=str(tmp_path / "repo"), board="b", topic=1)
    monkeypatch.setattr(report_cmd, "gather_data",
                        lambda *a, **k: {"anything": False})
    args = argparse.Namespace(since=None, report_state=None, now=lambda: 0.0,
                              run=None, state=None, cards=None, apply=False)
    out = _run(lambda: report_cmd._report_one(args, proj,
                                              lambda data: "summary"))
    _assert_literal(out)


def test_report_all_summary_line_survives(monkeypatch, tmp_path):
    """The --all footer interpolates an action string + counts."""
    monkeypatch.setattr(
        report_cmd, "_report_one",
        lambda args, project, renderer, **kw: ("dry", 0))
    projects = [registry.Project(name=f"p{MARK_BOTH}", repo="/r", board="b")]
    args = argparse.Namespace(apply=False)
    out = _run(lambda: report_cmd.cmd_report_all(args, projects))
    # The --all footer interpolates the ACTION string and the counts, not the
    # project names (those reach a panel inside _report_one, pinned by
    # test_report_window_string_survives). So what this site can prove is that a
    # markup-bearing project name did not blow the loop or the footer, and that
    # the counts still rendered.
    assert "1 project(s)" in out, f"footer lost its counts: {out!r}"
    assert "a[bold]b[/bold]c" not in out, "project name leaked unescaped"


def test_review_watermark_stamp_survives(monkeypatch):
    """The `data as of <stamp>` line formats a strftime string into a panel."""
    monkeypatch.setattr(review_cmd.kanban, "list_board_watermarks",
                        lambda **kw: {"default": 1791500000})
    monkeypatch.setattr(review_cmd.kanban, "freshness_watermark",
                        lambda boards, watermarks=None: 1791500000)
    # Force the stamp itself to carry markup: strftime output is locale-derived,
    # and this is the value that reaches the panel body.
    monkeypatch.setattr(review_cmd.time, "strftime",
                        lambda fmt, *a: f"2026-10-09 07:00{MARK_CLOSE}")
    enriched = [{"board": "default"}]
    out = _run(lambda: review_cmd._print_watermark(enriched))
    _assert_literal(out)


def test_release_plan_version_file_survives():
    """release prints the version-file name and the files it bumped."""
    proj = registry.Project(name="alpha", repo="/r", version_file=f"VERSION{MARK_CLOSE}")
    out = _run(lambda: release_cmd._print_apply(proj, ["bump"], "v1.2.3",
                                                (f"VERSION{MARK_BOTH}",)))
    _assert_literal(out)
    _assert_literal(out, MARK_BOTH)


def test_start_plan_caps_and_labels_survive():
    """start's plan panel interpolates caps, a milestone and card labels."""
    cards = [{"id": f"t_1{MARK_CLOSE}", "title": f"do it{MARK_BOTH}",
              "_assignee": "backend-engineer"}]
    out = _run(lambda: start_cmd._print_plan(
        milestone=f"m{MARK_CLOSE}", assigned=cards, held=[],
        total_cap=3, per_profile_cap=2, remaining=1))
    _assert_literal(out)
    _assert_literal(out, MARK_BOTH)


def test_project_list_rows_survive_markup(tmp_path):
    """`project list` is a 5-column themed Table of operator-derived strings.

    The largest single surface in the sweep (63 of the 154 flagged sites), and
    three of its five cells are registry DATA: project name, repo path, board
    slug. HEALTH is the renderer's own [ok]/[error] wrapper around a fixed
    token, so this also proves the sweep did not escape the style away.
    """
    repo = tmp_path / f"repo{MARK_CLOSE}"
    os.makedirs(str(repo))
    reg = _registry(tmp_path, [registry.Project(
        name=f"alpha{MARK_BOTH}", repo=str(repo), board=f"board{MARK_CLOSE}")])

    # Health resolves the board through the injected provider; reporting the
    # board as present keeps the cell on its [ok] branch rather than degrading
    # the whole row to the error path.
    kb = type("KB", (), {"board_exists": staticmethod(lambda slug: True)})()
    args = argparse.Namespace(project=None, json=False, registry=reg,
                              run=None, client=None, kanban=kb,
                              func=project_cmd.cmd_list)
    out = _run(lambda: project_cmd.run(args, reg))
    _assert_literal(out, MARK_BOTH)
    assert "ok" in out, f"[ok] health styling lost: {out!r}"


# ── the {escape(x)!r} hole ─────────────────────────────────────────────────
#
# escape() inserts a backslash before a bracket; repr() then escapes THAT
# backslash, so the bracket is live again and the crash returns. Probed against
# real Rich before this was written. sync.py/start.py shipped this idiom on
# what looked like an escaped value.

def test_escape_then_repr_conversion_still_crashes_in_rich():
    """Documents WHY the safe form is esc(repr(x)), not {escape(x)!r}.

    This test asserts the FAILURE stays true (it is a Rich-semantics pin, not a
    regression we are fixing): if a future Rich version made this safe, this
    test tells us the workaround could be relaxed.
    """
    from rich.markup import escape as rich_escape

    buf = io.StringIO()
    console = _theme.make_console(file=buf)
    with pytest.raises(MarkupError):
        console.print(f"value: {rich_escape(f'x{MARK_CLOSE}')!r}")


def test_esc_repr_conversion_is_the_safe_form():
    buf = io.StringIO()
    console = _theme.make_console(file=buf)
    console.print(f"value: {_theme.esc(repr(f'x{MARK_CLOSE}'))}")
    _assert_literal(buf.getvalue())


def test_no_broken_escape_then_repr_idiom_remains_in_flightdeck():
    """{escape(x)!r} is a crash waiting to happen — none may survive.

    Source-level pin: the sweep replaced every occurrence with esc(repr(x)),
    and this stops the idiom creeping back in through a copy-paste.
    """
    import ast
    from pathlib import Path

    cmds = Path(_theme.__file__).parent
    offenders = []
    for path in sorted(cmds.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FormattedValue):
                continue
            if node.conversion in (-1, None):
                continue
            inner = node.value
            # escape(x)!r  ->  conversion applied to an escape() call
            if isinstance(inner, ast.Call) and \
                    getattr(inner.func, "id", None) in ("escape", "esc"):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "escape()/esc() followed by !r re-opens the markup (repr escapes the "
        f"backslash escape() inserted); use esc(repr(x)): {offenders}")
