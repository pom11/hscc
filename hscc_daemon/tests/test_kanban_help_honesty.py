"""F3 help-honesty gate: one ``hscc kanban`` group, TWO help surfaces.

Audit finding F3 (card t_46d03105): ``hscc help kanban`` listed 3 of the 7
subcommands the group actually dispatches (``pause``, ``resume``, ``paused``,
``enforce`` were missing) while ``hscc kanban --help`` listed all 7. Two help
sources for one verb group had drifted, and nothing compared them.

The surfaces, and who owns each:

  * ``hscc kanban --help``  -> ``kanban_blocked.HELP_TEXT`` (Rich console print)
  * ``hscc help kanban``    -> ``hscc.COMMAND_HELP["kanban"]`` (plain print)

The authority is neither text: it is the GROUP DISPATCHER. ``cmd_kanban``'s
unknown-verb refusal line enumerates the verbs the group accepts, so this file
derives the verb set from the dispatcher at runtime (never from a literal),
re-checks that line against the registries that own the verbs, and then demands
that BOTH help surfaces cover exactly that set. A verb added to the dispatcher
without being advertised in both places is red here; so is a verb advertised
that the dispatcher does not route.

Safety: nothing here executes a mutating verb. The routing probe installs
recorder stubs over ``kanban_blocked.cmd_blocked`` and
``kanban_cli.cmd_kanban`` BEFORE calling the dispatcher, so a real
``blocked``/``pause`` can never run; the real-CLI probes run only help argv
under a throwaway HOME with recording PATH canaries (exit 97), same fixture
shape as test_cluster_group_help.py. Fake hosts are 192.0.2.x placeholders.
"""

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
