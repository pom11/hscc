"""F8: the ``hscc kanban monitor`` <-> ``GET /v1/kanban/running`` parity is NAMED.

Audit ``docs/audits/2026-11-23-agent-surface.md:158`` (F8, Low) measured that the
monitor verb's only non-CLI door is a route answering under a DIFFERENT name, and
that nothing on the monitor surface said so -- so a reader standing at
``hscc kanban monitor --help`` could not learn the route exists, and a reader at
the route could not learn which verb it mirrors. The disposition is to name the
parity honestly on both ends; no tool and no mutation path was invented
(``kanban pause/resume/paused/enforce`` stay CLI-only per decision D6, and the
``--live`` loop is a TTY panel no surface can carry).

What this pins, and why each end is worth a test:

  * the verb's own help names the path -- the pointer is on the surface an
    operator or agent actually reads, not only in a doc nobody opens;
  * BOTH help entry points render it (``kanban_blocked`` delegates
    ``monitor --help`` to ``kanban_cli``; a second copy of the wording there
    would drift and go silent again);
  * the route's docstring names the verb back -- a one-directional pointer is
    how F8 happened in the first place;
  * the pointer is not a lie: the route really is GET-only (no mutation door
    was smuggled in beside it) and both doors reach the same
    ``kanban_live.collect(registered_only=True)`` pass.

Row-for-row equality of the two answers is NOT re-proved here -- that is
``test_widget_route_parity_cards.py``'s job over a fixture board set. This
module owns only the NAMING claim F8 asked for, plus the two facts that make
the naming true rather than aspirational.

Nolive: nothing here opens a board, spawns a process or contacts a host. The
``collect`` call is a spy returning a canned snapshot, and the kill-detail
backing is stubbed (``_backing_list_running`` would otherwise enumerate
processes). The leg's autouse ``_isolate_hscc`` fixture keeps ``~/.hscc`` out
of it.
"""

import pytest

import api_server
import routes_kanban

from hscc_daemon import kanban_blocked, kanban_cli, kanban_live

PATH = "/v1/kanban/running"

# The audit row this card closes, quoted so a rewrite of the doc that drops the
# parity claim is visible next to the test that depends on it.
AUDIT_ROW = "docs/audits/2026-11-23-agent-surface.md:158"


def _matching_routes(path=PATH):
    """Every (method, pattern, handler) in the live table that matches ``path``."""
    return [(m, p, h) for (m, p, h) in api_server.ROUTES if p.match(path)]


# --------------------------------------------------------------------------- #
# 1. the CLI end names the route
# --------------------------------------------------------------------------- #

class TestCliEndNamesTheRoute:
    def test_verb_help_names_the_route_and_stays_readable(self):
        """The monitor verb's help states the parity and keeps the 78-col budget.

        The width rule is not cosmetic: every help surface is walked by
        ``scripts/hscc_cli_help_audit.py`` and an over-wide line is an (A)
        finding that gate fails on.
        """
        help_text = kanban_cli.SUBCOMMAND_HELP["monitor"]
        assert PATH in help_text, (
            f"{AUDIT_ROW}: `hscc kanban monitor --help` must name its route "
            f"parity ({PATH!r}) -- that naming IS the F8 fix")
        assert "GET" in help_text, "the pointer must name the METHOD, not a bare path"
        # The same surface must not read as a mutation door.
        assert "Read-only" in help_text

    def test_pointer_survives_both_help_entry_points(self, capsys):
        """``hscc kanban monitor --help`` renders it however the verb is reached.

        ``kanban_blocked.cmd_kanban`` hands ``monitor --help`` to
        ``kanban_cli`` (single source of truth, kanban_cli's own comment at the
        delegation site); if either end grows its own copy the pointer can go
        stale on one side while the other still claims parity.
        """
        assert kanban_cli.cmd_kanban(["monitor", "--help"]) == 0
        via_owner = capsys.readouterr().out
        assert kanban_blocked.cmd_kanban(["monitor", "--help"]) == 0
        via_group = capsys.readouterr().out

        assert via_owner == via_group, "the two entry points print different help"
        assert PATH in via_owner, "the pointer never reaches the rendered surface"
        # Rich must not have wrapped the pointer into an over-wide line.
        for ln in via_owner.splitlines():
            assert len(ln) <= 78, f"help line over 78 cols: {ln!r}"

    def test_group_help_verb_line_still_describes_the_read(self):
        """Guard, not a feature: the pointer went on the VERB help only.

        If someone later moves it onto the group block, this is the assertion
        that has to be edited -- i.e. the place the decision gets re-made
        deliberately instead of by accident.
        """
        assert "hscc kanban monitor [--live]" in kanban_cli.HELP_TEXT


# --------------------------------------------------------------------------- #
# 2. the route end names the verb back
# --------------------------------------------------------------------------- #

class TestRouteEndNamesTheVerb:
    def test_handler_summary_line_names_the_cli_verb(self):
        """The docstring's FIRST line names the verb, not merely its body.

        The body already mentioned ``hscc kanban monitor --board`` before this
        card, and F8 still happened -- which is exactly why the pointer is
        pinned on the summary line: a pairing buried three lines down in prose
        a caller never opens is not a pointer.
        """
        doc = routes_kanban.handle_kanban_running.__doc__ or ""
        summary = doc.strip().splitlines()[0]
        assert "hscc kanban monitor" in summary, (
            f"{AUDIT_ROW}: the route's own summary line must name the verb it "
            f"mirrors, or the parity is discoverable from only one side "
            f"(summary reads: {summary!r})")

    def test_backing_docstring_names_both_monitor_surfaces(self):
        """``_backing_collect`` is where the two names meet in one sentence."""
        doc = routes_kanban._backing_collect.__doc__ or ""
        assert "collect" in doc
        assert "cards/kanban monitor" in doc, (
            "the shared backing must say WHICH two verbs it serves, or the "
            "parity is inferable only from the handler above it")


# --------------------------------------------------------------------------- #
# 3. the pointer is true: GET-only, and the same backing pass
# --------------------------------------------------------------------------- #

class TestPointerIsHonest:
    def test_route_is_registered_get_only(self):
        """Exactly one registration answers the path, and it is a GET.

        F8's disposition says explicitly: do NOT invent a mutation path. A POST
        appearing on this path would be exactly that mistake, and this
        assertion is what stops it landing quietly.
        """
        hits = _matching_routes()
        methods = {m for (m, _p, _h) in hits}
        assert methods == {"GET"}, (
            f"{PATH} answered by methods {sorted(methods)} -- the monitor parity "
            "is a read; a mutating registration here needs its own decision")
        assert {h.__name__ for (_m, _p, h) in hits} == {"handle_kanban_running"}

    def test_the_mutation_door_is_a_different_path(self):
        """The kill switch stays a separate, confirm-gated POST.

        The pointer tells a caller where to READ; it must not be readable as
        "this route can also stop a card".
        """
        hits = _matching_routes("/v1/kanban/task/t_1/kill")
        assert {m for (m, _p, _h) in hits} == {"POST"}
        assert {h.__name__ for (_m, _p, h) in hits} == {"handle_kanban_kill"}

    @pytest.fixture()
    def spy_collect(self, monkeypatch):
        """Record every ``kanban_live.collect`` call; return a canned snapshot.

        The three ``attach_*`` enrichers are stubbed to identity: they are the
        CLI's token/orchestrator/merge-queue top-ups, they read other state
        files, and none of them is the parity claim.
        """
        calls = []
        snapshot = {"running": [], "ready": [], "boards": [{"name": "general"}],
                    "errors": []}

        def fake_collect(**kwargs):
            calls.append(dict(kwargs))
            return {k: (list(v) if isinstance(v, list) else v)
                    for k, v in snapshot.items()}

        monkeypatch.setattr(kanban_live, "collect", fake_collect)
        for name in ("attach_tokens", "attach_orchestrators",
                     "attach_merge_queue"):
            monkeypatch.setattr(kanban_live, name, lambda data, **kw: data)
        # The route merges kill detail from kill_switch, which enumerates
        # processes -- that is the kill panel's business, not this claim.
        monkeypatch.setattr(routes_kanban, "_backing_list_running", lambda: {})
        return calls

    def test_both_doors_reach_the_same_collect_pass(self, spy_collect, capsys):
        """The named parity is a real shared read, not a wish.

        Both doors must call ``kanban_live.collect`` with ``registered_only =
        True`` (the project-scoped scope the CLI's own monitor uses) and must
        honour ``--board`` / ``?board=`` the same way.
        """
        # CLI door: the --json snapshot path (no TTY, no --live loop).
        assert kanban_cli._cmd_monitor(["--board", "general"],
                                       json_mode=True) == 0
        capsys.readouterr()
        # Route door: the handler directly, exactly as api_server dispatches it.
        status, payload = routes_kanban.handle_kanban_running(
            None, None, {"board": "general"}, None)

        assert status == 200
        assert payload["count"] == 0 and payload["speak"] == "No running tasks."
        assert spy_collect == [{"board": "general", "registered_only": True},
                               {"board": "general", "registered_only": True}], (
            "the pointer claims both doors answer from ONE collect pass; a "
            f"diverging call set makes it a lie: {spy_collect}")

    def test_pointer_does_not_add_a_backing_call(self, spy_collect, monkeypatch):
        """The route reaches collect ONLY through ``_backing_collect``.

        A second, hand-rolled board read beside the shared one is how a
        "parity" pointer becomes a second source of truth (audit F2's shape).
        """
        seen = []
        real = routes_kanban._backing_collect

        def spy(**kwargs):
            seen.append(dict(kwargs))
            return real(**kwargs)

        monkeypatch.setattr(routes_kanban, "_backing_collect", spy)
        routes_kanban.handle_kanban_running(None, None, {}, None)
        assert seen == [{"board": None}], (
            f"the handler made {seen} -- exactly one shared-backing call is the "
            "contract; a second read is a second source of truth")
