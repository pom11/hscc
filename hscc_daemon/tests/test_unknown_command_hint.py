"""The unknown-command hint must name EXACTLY the verbs the dispatcher accepts.

Audit A GAP-A-4 (cosmetic): ``main()``'s else-branch printed a hand-maintained
220-char "Available:" line that listed ``orch`` but omitted ``cards`` and
``autodown``, even though both dispatch a few dozen lines above it (``cards``
at the ``if cmd == "cards":`` group branch, ``autodown`` at its own). A human
who mistyped a verb — the one moment they are actually reading the list —
was told two live commands do not exist.

The line is now GENERATED from ``unified.TOP_LEVEL_COMMANDS``, and this file
pins that constant against the two surfaces it has to agree with, so the drift
is structurally impossible rather than merely fixed once:

  * ``main()``'s own dispatch table — parsed from ``inspect.getsource(main)``,
    the same source-guard shape the GAP-C cards use. Every verb the dispatcher
    resolves must be listed, and nothing listed may fail to resolve (a hint
    that advertises a dead verb is the SAME class of surface dishonesty the
    F5 card deleted the ``ed-*`` trio for, just pointed the other way).
    ``help`` and ``unit-emit`` are the two documented exclusions: the first is
    a help verb, the second an internal machine verb deliberately kept off
    every help surface (see the comment on its branch).
  * ``COMMAND_HELP`` — the ``hscc help <cmd>`` source. Every listed verb must
    have an entry there, and the only entries allowed to sit OUTSIDE the list
    are ``help`` (a help verb) and ``advanced`` (a help TOPIC: it prints its
    own listing and is NOT dispatchable, so advertising it on the hint would
    send a reader to a command that exits 1).

Plus the behavioural pin: a bogus verb still exits 1 and the printed line
really does contain ``cards`` and ``autodown`` — the acceptance the audit
asked for, asserted on the printed bytes rather than on the constant.

Address doctrine: fictional verbs only; nothing here dispatches a real verb
(only ``bogus-verb`` reaches ``main()``), so no side effects and no network.
"""

import inspect
import io
import re
import sys
from contextlib import redirect_stdout

import pytest

import hscc_daemon.hscc as unified


# ── helpers ────────────────────────────────────────────────────────────────

def _main(monkeypatch, argv):
    """Drive unified.main() with argv; returns (rc_or_None, stdout)."""
    monkeypatch.setattr(sys, "argv", ["hscc", *argv])
    out = io.StringIO()
    rc = None
    try:
        with redirect_stdout(out):
            unified.main()
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 0
    return rc, out.getvalue()


def _hint_line(monkeypatch):
    """The exact 'Available:' bytes main() prints for a bogus verb."""
    rc, out = _main(monkeypatch, ["bogus-verb"])
    assert rc == 1, out
    lines = [ln for ln in out.splitlines() if ln.startswith("Available:")]
    assert len(lines) == 1, f"expected one hint line, got {lines!r}"
    return lines[0]


def _dispatched_verbs():
    """Names main() actually resolves, parsed from main()'s own source.

    Regexes mirror the dispatcher's real shapes: the group branches compare
    the raw ``cmd``, the daemon chain compares ``cmd_lower``. Help intercepts
    (``in DAEMON_COMMANDS``, ``advanced_cmds``, the heal tuple) are membership
    tests against tables, not dispatch branches, and every verb they mention
    also has a dispatch branch — so none of them leak in here.
    """
    src = inspect.getsource(unified.main)
    names = set(re.findall(r'\bif cmd == "([a-z0-9-]+)":', src))
    names |= set(re.findall(r'\belif cmd_lower == "([a-z0-9-]+)":', src))
    names |= set(re.findall(r'\bif cmd_lower == "([a-z0-9-]+)":', src))
    return names


# Documented exclusions from "every dispatchable verb is listed".
NOT_ADVERTISED = {"help", "unit-emit"}
# Documented COMMAND_HELP entries allowed to sit outside the hint list.
HELP_ONLY_TOPICS = {"help", "advanced"}


# ── hint list ⊆ dispatchable (the audit's explicit ask) ────────────────────

class TestHintIsHonest:
    def test_every_listed_verb_dispatches(self):
        """No listed verb may fall through to 'Unknown command'."""
        listed = set(unified.TOP_LEVEL_COMMANDS)
        missing = sorted(listed - _dispatched_verbs())
        assert not missing, (
            f"unknown-command hint advertises verbs the dispatcher does not "
            f"resolve: {missing} — add the dispatch branch or drop them from "
            f"TOP_LEVEL_COMMANDS")

    def test_hint_line_matches_the_constant(self, monkeypatch):
        """The printed line is GENERATED from TOP_LEVEL_COMMANDS — a re-added
        literal would let the constant and the surface drift silently again."""
        line = _hint_line(monkeypatch)
        assert line == "Available: " + ", ".join(unified.TOP_LEVEL_COMMANDS)

    def test_dispatcher_uses_the_constant_not_a_literal(self):
        src = inspect.getsource(unified.main)
        assert '", ".join(TOP_LEVEL_COMMANDS)' in src, (
            "the hint must be derived from TOP_LEVEL_COMMANDS; the hand-"
            "maintained literal is the bug (audit A GAP-A-4)")


# ── dispatchable ⊆ hint list (the half that caught GAP-A-4) ────────────────

class TestHintIsComplete:
    def test_every_dispatched_verb_is_listed(self):
        unlisted = sorted(_dispatched_verbs() - NOT_ADVERTISED
                          - set(unified.TOP_LEVEL_COMMANDS))
        assert not unlisted, (
            f"dispatched verbs missing from the unknown-command hint: "
            f"{unlisted} — a reader is told a live command does not exist")

    def test_gap_a_4_verbs_are_present(self, monkeypatch):
        """The regression itself, named: `cards` (:group branch) and `autodown`
        were both dispatched and both absent from the printed list."""
        line = _hint_line(monkeypatch)
        items = [t.strip() for t in line.split("Available:", 1)[1].split(",")]
        for verb in ("cards", "autodown", "orch", "kanban"):
            assert verb in items, f"`{verb}` missing from the printed hint"


# ── the three surfaces agree ───────────────────────────────────────────────

class TestSurfacesAgree:
    def test_every_listed_verb_has_help(self):
        nohelp = sorted(set(unified.TOP_LEVEL_COMMANDS)
                        - set(unified.COMMAND_HELP))
        assert not nohelp, (
            f"hint-listed verbs with no COMMAND_HELP entry: {nohelp} — "
            f"`hscc help <verb>` would exit 1 on a verb we advertise")

    def test_only_documented_help_entries_sit_outside_the_hint(self):
        outside = sorted(set(unified.COMMAND_HELP)
                         - set(unified.TOP_LEVEL_COMMANDS))
        assert outside == sorted(HELP_ONLY_TOPICS), (
            f"COMMAND_HELP entries neither on the hint nor documented as "
            f"help-only topics: {outside}. `advanced` is a help TOPIC, not a "
            f"dispatched verb — adding it to TOP_LEVEL_COMMANDS would "
            f"advertise a command that exits 1.")

    def test_advanced_is_not_advertised_as_dispatchable(self):
        """`hscc advanced` (no help token) falls through to 'Unknown command'
        rc 1 — only `hscc advanced --help` and `hscc help advanced` resolve.
        Listing it would be the F5 dead-command finding in reverse."""
        assert "advanced" not in unified.TOP_LEVEL_COMMANDS
        assert "advanced" not in _dispatched_verbs()

    def test_internal_unit_verb_stays_off_the_hint(self):
        """`unit-emit` dispatches but is deliberately absent from every help
        surface (t_d8f13693) — pin the exception, not just the rule."""
        assert "unit-emit" in _dispatched_verbs()
        assert "unit-emit" not in unified.TOP_LEVEL_COMMANDS
        assert "unit-emit" not in unified.COMMAND_HELP


# ── behavioural: the surface itself ────────────────────────────────────────

class TestBehavioural:
    def test_bogusVerb_exits_1_and_names_the_groups(self, monkeypatch):
        rc, out = _main(monkeypatch, ["bogus-verb"])
        assert rc == 1
        blob = [ln for ln in out.splitlines() if ln.startswith("Available:")]
        assert blob
        items = [t.strip() for t in blob[0].split("Available:", 1)[1].split(",")]
        assert "cards" in items and "autodown" in items
        assert "Traceback" not in out

    def test_hint_lists_no_duplicates_and_no_empty_items(self):
        items = list(unified.TOP_LEVEL_COMMANDS)
        assert len(items) == len(set(items)), f"duplicate in hint: {items}"
        assert all(i and i == i.strip() for i in items), items

    @pytest.mark.parametrize("verb", sorted(unified.TOP_LEVEL_COMMANDS))
    def test_listed_verb_is_a_real_argv_shape(self, verb):
        """Every listed verb must at least survive argv shaping: it has to be
        a token the dispatcher recognises BEFORE the help-token scan, so it
        can never come back as 'Unknown command'."""
        assert verb in _dispatched_verbs() or verb in NOT_ADVERTISED
