"""F3 help-honesty gate: one ``hscc kanban`` group, THREE help surfaces.

Audit finding F3 (card t_46d03105): ``hscc help kanban`` listed 3 of the 7
subcommands the group actually dispatches (``pause``, ``resume``, ``paused``,
``enforce`` were missing) while ``hscc kanban --help`` listed all 7. Two help
sources for one verb group had drifted, and nothing compared them.

The surfaces, and who owns each:

  * ``hscc kanban --help``  -> ``kanban_blocked.HELP_TEXT`` (Rich console print)
  * ``hscc help kanban``    -> ``hscc.COMMAND_HELP["kanban"]`` (plain print)
  * ``hscc --help`` banner  -> ``hscc._get_help_text()``'s ``_entry`` line

The third one is card t_00502a32 (same defect class, different seam): the
primary banner entry named ``blocked`` and ``stale`` — 2 of 7 — and it is the
surface an operator reads BEFORE they type anything, so it is the one a lie on
costs most. ``TestPrimaryBannerHonesty`` below holds it to the same rule, and
``TestRoutingIsEnumerated`` closes the limit this file's own mutation battery
recorded (routing widened, enumeration untouched, gate green).

The authority is neither text: it is the GROUP DISPATCHER. ``cmd_kanban``'s
unknown-verb refusal line enumerates the verbs the group accepts, so this file
derives the verb set from the dispatcher at runtime (never from a literal),
re-checks that line against the registries that own the verbs, and then demands
that EVERY help surface cover exactly that set. A verb added to the dispatcher
without being advertised in all of them is red here; so is a verb advertised
that the dispatcher does not route.

Safety: nothing here executes a mutating verb. The routing probe installs
recorder stubs over ``kanban_blocked.cmd_blocked`` and
``kanban_cli.cmd_kanban`` BEFORE calling the dispatcher, so a real
``blocked``/``pause`` can never run; the real-CLI probes run only help argv
under a throwaway HOME with recording PATH canaries (exit 97), same fixture
shape as test_cluster_group_help.py. Fake hosts are 192.0.2.x placeholders.
"""

import ast
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import hscc_daemon.hscc as unified
import hscc_daemon.kanban_blocked as kbx
import hscc_daemon.kanban_cli as kcli

REPO = Path(__file__).resolve().parents[2]

# A verb no dispatcher can match — the enumeration probe token, same idiom as
# scripts/hscc_cli_help_audit.py::BOGUS_VERB.
BOGUS_VERB = "zz-not-a-kanban-verb"

TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


def _verbs_mentioned(text, verbs):
    """Which of ``verbs`` appear in ``text`` as whole word-shaped tokens.

    Token-shaped rather than substring matching, so ``pause`` cannot be
    satisfied by ``paused``, and a verb buried inside a sentence still counts
    (this is an honesty gate, not a layout parser).
    """
    tokens = set(TOKEN.findall(text))
    return {v for v in verbs if v in tokens}


def _rendered_help_kanban(monkeypatch, capsys):
    """Exact bytes ``hscc help kanban`` prints (the ``_handle_help`` path)."""
    monkeypatch.setattr(sys, "argv", ["hscc", "help", "kanban"])
    assert unified._handle_help() == 0
    return capsys.readouterr().out


def _rendered_group_help(capsys):
    """Exact bytes ``hscc kanban --help`` prints."""
    assert kbx.cmd_kanban(["--help"]) == 0
    return capsys.readouterr().out


# ---------------------------------------------------------------------------
# the authority: what the group dispatcher actually routes
# ---------------------------------------------------------------------------

class TestDispatcherIsTheAuthority:
    """The dispatcher's own inventory, and the registries that back it.

    The refusal line is what an operator reads when they mistype a verb; if it
    lies, every gate that reads it lies too. This pins it against the two
    registries that own the verbs (``kanban_blocked.SUBCOMMAND_HELP`` for the
    locally-owned verb, ``kanban_cli.VALID_SUBCOMMANDS`` for the delegated six)
    and against the routing itself.
    """

    @pytest.fixture()
    def routed(self, monkeypatch):
        """Dispatcher with every verb body replaced by a recorder.

        Installed over the module attributes the dispatcher resolves at CALL
        time (``cmd_blocked`` is a module global; the delegation does
        ``from hscc_daemon import kanban_cli`` per call), so the real verbs
        cannot run no matter what the dispatcher decides.
        """
        calls = []

        def _record(name):
            def _f(*a, **k):
                calls.append(name)
                return 0
            return _f

        monkeypatch.setattr(kbx, "cmd_blocked", _record("blocked"))
        monkeypatch.setattr(kcli, "cmd_kanban", _record("delegated"))
        return calls

    def test_refusal_line_enumerates_the_group(self, capsys):
        assert kbx.cmd_kanban([BOGUS_VERB]) == 1
        err = capsys.readouterr().err
        m = re.search(r"[Vv]alid subcommands?:\s*([^\n]+)", err)
        assert m, f"the unknown-verb refusal names no verbs:\n{err}"
        listed = {t.strip() for t in m.group(1).split(",") if t.strip()}
        assert listed == (set(kbx.SUBCOMMAND_HELP) | set(kcli.VALID_SUBCOMMANDS)), (
            "the refusal line and the verb registries disagree")
        # The four verbs F3 found missing from the other surface are in here:
        assert {"pause", "resume", "paused", "enforce"} <= listed

    @pytest.mark.parametrize("verb", sorted(
        set(kbx.SUBCOMMAND_HELP) | set(kcli.VALID_SUBCOMMANDS)))
    def test_every_enumerated_verb_is_routed(self, verb, routed, capsys):
        """Refusal-line inventory == routing: no verb is advertised by the
        refusal and then rejected as unknown."""
        rc = kbx.cmd_kanban([verb])
        err = capsys.readouterr().err
        assert "unknown kanban subcommand" not in err, f"{verb}: {err}"
        assert routed, f"{verb} was neither dispatched nor delegated"

    def test_a_non_verb_is_rejected(self, routed, capsys):
        assert kbx.cmd_kanban([BOGUS_VERB]) == 1
        assert "unknown kanban subcommand" in capsys.readouterr().err
        assert routed == [], "the bogus verb must not reach a verb body"


@pytest.fixture()
def dispatched(capsys):
    """The verb set the group dispatches, derived at runtime (no literal)."""
    assert kbx.cmd_kanban([BOGUS_VERB]) == 1
    err = capsys.readouterr().err
    m = re.search(r"[Vv]alid subcommands?:\s*([^\n]+)", err)
    assert m, f"cannot derive the verb set: no inventory in {err!r}"
    return {t.strip() for t in m.group(1).split(",") if t.strip()}


# ---------------------------------------------------------------------------
# the gate: both help surfaces cover the dispatched set, and agree
# ---------------------------------------------------------------------------

class TestKanbanHelpHonesty:
    def test_help_kanban_lists_every_dispatched_verb(self, dispatched,
                                                     monkeypatch, capsys):
        out = _rendered_help_kanban(monkeypatch, capsys)
        missing = dispatched - _verbs_mentioned(out, dispatched)
        assert not missing, (
            f"`hscc help kanban` does not advertise {sorted(missing)}; the "
            f"group dispatches them (COMMAND_HELP entry):\n{out}")

    def test_group_help_lists_every_dispatched_verb(self, dispatched, capsys):
        out = _rendered_group_help(capsys)
        missing = dispatched - _verbs_mentioned(out, dispatched)
        assert not missing, (
            f"`hscc kanban --help` does not advertise {sorted(missing)}:\n{out}")

    def test_both_surfaces_advertise_the_same_verbs(self, dispatched,
                                                    monkeypatch, capsys):
        """F3's actual defect: two surfaces, one group, different verb sets."""
        a = _verbs_mentioned(_rendered_help_kanban(monkeypatch, capsys),
                             dispatched)
        b = _verbs_mentioned(_rendered_group_help(capsys), dispatched)
        assert a == b == dispatched, (
            f"the two help surfaces disagree:\n"
            f"  only in `hscc help kanban`: {sorted(a - b)}\n"
            f"  only in `hscc kanban --help`: {sorted(b - a)}")

    def test_help_entry_advertises_no_verb_the_group_rejects(self, dispatched,
                                                             monkeypatch,
                                                             capsys):
        """The F5 rule, applied to BOTH surfaces: help never advertises a
        command the group does not dispatch.

        Each surface has its own shape, so each gets its own extractor:
        ``hscc help kanban`` lists verbs at a 2-space indent, ``hscc kanban
        --help`` writes them as ``hscc kanban <verb> …`` example lines. Place-
        holders (``<subcommand>``) and help flags never match."""
        named = set()
        for ln in _rendered_help_kanban(monkeypatch, capsys).splitlines():
            m = re.match(r"  ([a-z][a-z-]+)(\s|$|[:\[])", ln)
            if m:
                named.add(m.group(1))
        named |= set(re.findall(r"hscc kanban ([a-z][a-z-]+)",
                                _rendered_group_help(capsys)))
        phantom = named - dispatched
        assert not phantom, (
            f"help advertises {sorted(phantom)} but the group dispatches "
            f"{sorted(dispatched)}")

    def test_every_entry_line_within_the_78_gate(self, monkeypatch, capsys):
        """COMMAND_HELP is plain-printed, so authored width IS rendered width
        (the (B) gate renders at 80 and 120; every line stays inside 78)."""
        for cols in ("80", "120"):
            monkeypatch.setenv("COLUMNS", cols)
            for text in (_rendered_help_kanban(monkeypatch, capsys),
                         _rendered_group_help(capsys)):
                for ln in text.splitlines():
                    assert len(ln) <= 78, f"COLUMNS={cols}: >78: {ln!r}"


# ---------------------------------------------------------------------------
# real CLI — the two invocations an operator actually types
# ---------------------------------------------------------------------------

class TestKanbanHelpRealCLI:
    SURFACES = (["help", "kanban"], ["kanban", "--help"], ["kanban", "-h"])

    @pytest.fixture()
    def sandbox(self, tmp_path):
        home = tmp_path / "home"
        (home / ".hscc").mkdir(parents=True)
        # Seed cluster.json so import never shells out (192.0.2.1 is the
        # documented placeholder LAN host, never a real node).
        (home / ".hscc" / "cluster.json").write_text(json.dumps(
            {"gateway": {"host": "192.0.2.1"}, "workers": [],
             "nasDevices": []}))
        canary = tmp_path / "canary-bin"
        canary.mkdir()
        for name in ("sparkrun", "hermes", "gh", "git"):
            p = canary / name
            p.write_text("#!/bin/sh\n"
                         'echo "' + name + ' $*" >> "$AUDIT_CANARY_LOG"\n'
                         "exit 97\n")
            p.chmod(0o755)
        trips = canary / "trips.log"
        trips.write_text("")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("CONDA_") and k not in (
                   "HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE",
                   "HERMES_DELEGATED_CHILD_CONTEXT", "HSCC_SPARKRUN_BIN",
                   "COLUMNS")}
        env.update({
            "HOME": str(home),
            "PYTHONPATH": os.pathsep.join(
                [str(REPO), str(REPO / "hscc-project")]),
            "NO_COLOR": "1", "TERM": "dumb", "HSCC_THEME": "dark",
            "PATH": os.pathsep.join([str(canary), env.get("PATH", "")]),
            "AUDIT_CANARY_LOG": str(trips),
        })
        return {"home": home, "trips": trips, "env": env}

    def _run(self, sandbox, argv, cols):
        return subprocess.run(
            [sys.executable, "-m", "hscc_daemon.hscc", *argv],
            env=dict(sandbox["env"], COLUMNS=str(cols)),
            cwd=str(Path.cwd()), capture_output=True, text=True, timeout=60)

    @pytest.mark.parametrize("cols", (80, 120))
    @pytest.mark.parametrize("argv", SURFACES)
    def test_surface_is_honest_inert_and_in_budget(self, argv, cols, sandbox,
                                                   dispatched):
        home = sandbox["home"]
        before = {str(p.relative_to(home)) for p in home.rglob("*")
                  if p.is_file()}
        proc = self._run(sandbox, argv, cols)
        blob = proc.stdout + proc.stderr
        assert proc.returncode == 0, f"{argv} @COLUMNS={cols} -> {proc.returncode}\n{blob}"
        assert "Traceback" not in proc.stderr
        assert "\x1b[" not in blob, "ANSI leaked into the help surface"
        missing = dispatched - _verbs_mentioned(proc.stdout, dispatched)
        assert not missing, (
            f"{argv} @COLUMNS={cols} hides {sorted(missing)} "
            f"that the group dispatches:\n{proc.stdout}")
        for ln in proc.stdout.splitlines():
            assert len(ln) <= 78, f"{argv} @COLUMNS={cols}: >78: {ln!r}"
        assert sandbox["trips"].read_text().strip() == "", (
            f"{argv} shelled out: {sandbox['trips'].read_text()}")
        after = {str(p.relative_to(home)) for p in home.rglob("*")
                 if p.is_file()}
        assert after == before, (
            f"{argv} wrote state under HOME: {sorted(after - before)}")


# ---------------------------------------------------------------------------
# the third surface: the primary `hscc --help` banner (card t_00502a32)
# ---------------------------------------------------------------------------

# The group this file is about. A command NAME, not a verb list: verbs are only
# ever derived, from the refusal line (`dispatched`) or the routing source
# (`_routing_from_source`).
GROUP = "kanban"

# Deeper than the banner's 2-space usage column, so anything past it belongs to
# an entry as description or hanging-indent continuation.
_TAIL_INDENT = 4


def _rendered_primary_banner(monkeypatch, capsys, cols=80):
    """Exact bytes the PRIMARY surface prints: ``hscc --help`` (F5 layout)."""
    monkeypatch.setenv("COLUMNS", str(cols))
    monkeypatch.setattr(sys, "argv", ["hscc", "--help"])
    with pytest.raises(SystemExit) as exc:
        unified.main()
    assert exc.value.code == 0, "`hscc --help` stopped exiting 0"
    return capsys.readouterr().out


def _banner_entry(banner, group=GROUP):
    """The banner lines that belong to one group's entry.

    ``_get_help_text``'s ``_entry`` keeps a usage/description pair on one line
    while it fits the 78 budget, and otherwise leaves the usage alone with the
    description as 6-space hanging-indented continuations (the layout contract
    F5 wrote). Both shapes are collected here: the usage line, then every
    deeper-indented line under it, stopping at the blank line between sections.
    """
    lines = banner.splitlines()
    head = re.compile(rf"  {group}(?:\s|$)")
    for i, ln in enumerate(lines):
        if head.match(ln):
            block = [ln]
            for nxt in lines[i + 1:]:
                if not nxt.strip() or not nxt.startswith(" " * _TAIL_INDENT):
                    break
                block.append(nxt)
            return block
    raise AssertionError(
        f"the primary `hscc --help` banner has no `{group}` entry at all:\n"
        f"{banner}")


def _banner_verb_claims(entry_lines):
    """The verbs the banner entry CLAIMS for the group.

    Shape rule, taken from the banner's own convention: an entry that carries
    subcommands writes them as a bare space-separated list after the first
    colon — ``Idle autodown/autoup: status enable disable wake cancel``,
    ``Local merge queue: status list add remove pause resume enable disable``.
    The colon-tail is therefore the entry's verb inventory, which makes it the
    one place where a verb the dispatcher refuses can be advertised as if it
    worked. Parentheses are prose by that same convention (a gloss belongs
    before the colon or in brackets), so they are stripped rather than being
    mistaken for verbs.
    """
    text = " ".join(ln.strip() for ln in entry_lines)
    if ":" not in text:
        return set()
    tail = re.sub(r"\([^()]*\)", " ", text.split(":", 1)[1])
    return {t for t in re.split(r"[^A-Za-z0-9_-]+", tail) if t}


class TestPrimaryBannerHonesty:
    """F3's defect class on the surface read BEFORE anything is typed.

    The banner entry said ``Board hygiene: blocked (show/recover), stale
    (list/archive)``: 2 of the 7 verbs ``hscc kanban`` routes, and the four it
    hid (``pause``, ``resume``, ``paused``, ``enforce``) are the four that stop
    workers. Same authority as the rest of this file — the dispatcher's refusal
    line — same rule: advertise what routes, advertise nothing else.
    """

    def test_banner_lists_every_dispatched_verb(self, dispatched, monkeypatch,
                                               capsys):
        entry = _banner_entry(_rendered_primary_banner(monkeypatch, capsys))
        missing = dispatched - _verbs_mentioned(" ".join(entry), dispatched)
        assert not missing, (
            f"`hscc --help` hides {sorted(missing)} that `hscc kanban` "
            f"dispatches. The entry was:\n" + "\n".join(entry))

    def test_banner_advertises_no_verb_the_group_rejects(self, dispatched,
                                                        monkeypatch, capsys):
        entry = _banner_entry(_rendered_primary_banner(monkeypatch, capsys))
        claims = _banner_verb_claims(entry)
        assert claims, (
            "the banner entry advertises no verbs in the inventory position, "
            "so there would be nothing to check — that is a gate going "
            "vacuous, not a gate passing:\n" + "\n".join(entry))
        phantom = claims - dispatched
        assert not phantom, (
            f"`hscc --help` advertises {sorted(phantom)} under `kanban`, but "
            f"the dispatcher refuses them (it routes {sorted(dispatched)})")

    @pytest.mark.parametrize("cols", (80, 120))
    def test_banner_entry_within_the_78_gate(self, cols, monkeypatch, capsys):
        """The (B) budget, measured on the entry this card moved: the banner
        renders at the caller's COLUMNS and a hard line past the budget is what
        made Rich spill continuations at column 0 (audit Class B)."""
        for ln in _banner_entry(_rendered_primary_banner(monkeypatch,
                                                         capsys, cols)):
            assert len(ln) <= 78, f"COLUMNS={cols}: >78: {ln!r}"

    def test_banner_entry_is_width_stable(self, monkeypatch, capsys):
        """Every authored line is <= 78, so COLUMNS 80 and 120 render the SAME
        entry. Help whose verb inventory depends on the terminal width is help
        that lies at one of the two widths."""
        narrow = _banner_entry(_rendered_primary_banner(monkeypatch, capsys, 80))
        wide = _banner_entry(_rendered_primary_banner(monkeypatch, capsys, 120))
        assert narrow == wide, (
            f"the banner entry reflows with width:\n  80: {narrow}\n 120: {wide}")


# ---------------------------------------------------------------------------
# routed -> enumerated: this file's recorded limit, closed (card t_00502a32)
# ---------------------------------------------------------------------------

class TestRoutingIsEnumerated:
    """The one mutant t_46d03105's battery could not kill, and why it was real.

    Every case above takes the verb set FROM the refusal line, so together they
    prove *enumerated -> routed* and never the converse: widening
    ``cmd_kanban``'s routing tuples while leaving ``kanban_cli.VALID_SUBCOMMANDS``
    and the refusal line untouched stayed green (mutant M5). The shipped
    consequence is only loud, not silent — ``kanban_cli.cmd_kanban`` re-validates
    against ``VALID_SUBCOMMANDS`` — but "loud" is a bug report, not a gate.

    So this case derives the routing set from a source the rest of the file
    never reads: the dispatcher's own AST. Nothing here is a verb literal, and
    the assertions refuse to go vacuous if the dispatcher changes shape. The
    parent's gate is untouched above — this adds the converse direction, it does
    not widen anything that already passes.
    """

    @staticmethod
    def _routing_from_source():
        """``(membership tuples, equality names)`` as cmd_kanban writes them.

        ``sub in (…)``` gives the tuples (the dispatch branch and its ``--help``
        twin); ``sub == "blocked"`` gives the verb the dispatcher owns directly.
        Comparisons against anything else — ``argv[0] in ("--help", "-h")``,
        ``a in SUB_HELP`` — are not verb routing and are skipped by requiring a
        bare ``sub`` against a literal of strings.
        """
        tree = ast.parse(inspect.getsource(kbx.cmd_kanban))
        membership, equality = [], set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or len(node.comparators) != 1:
                continue
            left, right = node.left, node.comparators[0]
            if not (isinstance(left, ast.Name) and left.id == "sub"):
                continue
            if isinstance(right, (ast.Tuple, ast.List)):
                words = [e.value for e in right.elts
                         if isinstance(e, ast.Constant)
                         and isinstance(e.value, str)]
                if words and len(words) == len(right.elts):
                    membership.append(frozenset(words))
            elif (isinstance(right, ast.Constant)
                  and isinstance(right.value, str)
                  and isinstance(node.ops[0], ast.Eq)):
                equality.add(right.value)
        return membership, equality

    def test_routed_verbs_are_exactly_the_enumerated_ones(self, dispatched):
        membership, equality = self._routing_from_source()
        assert membership and equality, (
            "the routing parse found no `sub in (…)` tuple and no "
            "`sub == \"<verb>\"` branch: cmd_kanban changed shape, so this "
            "case would pass by reading nothing. Teach it; do not delete it.")
        routed = frozenset(equality) | frozenset().union(*membership)
        assert routed == frozenset(dispatched), (
            f"routing and enumeration disagree both ways — routed-only "
            f"{sorted(routed - dispatched)} (the refusal line and "
            f"VALID_SUBCOMMANDS must name them too), enumerated-only "
            f"{sorted(dispatched - routed)} (the refusal advertises them and "
            f"the dispatcher then refuses them)")

    def test_the_verb_tuples_do_not_drift_apart(self, dispatched):
        """The dispatch tuple and its ``--help`` twin must hold the same verbs.

        A verb missing from the help-scan twin still runs, but `hscc kanban
        <verb> --help` answers with the GROUP help instead of that verb's own —
        the same "help answers a different question" defect this family is
        about, one indirection further in.
        """
        membership, equality = self._routing_from_source()
        assert len(membership) >= 2, (
            f"expected the dispatch tuple AND its --help twin, found "
            f"{len(membership)}: cmd_kanban changed shape, teach this case")
        routed = frozenset(equality) | frozenset().union(*membership)
        for i, verbs in enumerate(sorted(membership, key=sorted)):
            assert verbs == routed - equality, (
                f"verb tuple #{i} ({sorted(verbs)}) is not the group's verb "
                f"set ({sorted(routed - equality)})")
