"""Per-subcommand ``--help`` inertness for the heal verbs — BROKEN-HEAL B4+B5
(card t_aeb982bc, docs/audits/CLI_HELP_2026-10-15.md Class D + D-rc).

Measured on the base tree, in a sandbox HOME with recording PATH canaries:

  * ``heal-clear --help``  -> EXECUTED the mutating verb: the help token was
    consumed as the NODE NAME and ``cmd_heal`` ran the clear path
    (``No strikes recorded for --help`` — the audit's EXEC_ECHO sensor);
    ``heal-clear <node> --help`` CLEARED <node>'s real strikes while the
    operator believed they were reading help;
  * ``help heal-clear`` / ``help heal-status`` -> exit 1
    (``Error: no help for …``): no COMMAND_HELP entries exist for the verbs
    the breaker alert tells every operator to run.

Three layers of proof, same shape as test_cluster_cli_help.py / test_api_cli_help.py:

  * unit — a landmine fixture that makes ``cli.cmd_heal`` and the breaker's
    state reader/writer RAISE, so a ``--help`` that reaches dispatch fails
    here even where the side effect would be invisible in a sandbox; plus
    the argv-shape case (help AFTER the node name) and a positive-dispatch
    pin proving the verbs still work when no help token is present;
  * second-line-of-defence — ``cmd_heal(node="--help")`` called DIRECTLY
    (bypassing the CLI intercept) must print usage and leave a tmp-redirected
    ledger byte-identical (the B6 engine-side ``cmd_stop`` guard's shape);
  * COMMAND_HELP content pins — both entries exist, every line of the
    rendered block stays inside the (B) gate's 78-column budget at width 80
    AND 120 (the block is printed with plain ``print``, so authored width IS
    rendered width), and no line ever matches the audit's EXEC_ECHO sensor
    (``Cleared heal strikes`` / ``healing resumes`` / ``No strikes recorded``)
    — help text that echoes the executing wording would make the gate
    flag help itself as execution;
  * real CLI — the exact audit probe invocations under a throwaway HOME with
    recording ``sparkrun``/``hermes``/``gh``/``git`` canaries on PATH and a
    PRE-SEEDED LIVE heal-breaker.json: state bytes + mtime must not move, no
    canary may trip, no new file may appear under HOME, rc must be 0, and the
    EXEC_ECHO wording must be absent. A positive control (``heal-clear
    <node>`` WITHOUT a help token, same sandbox) proves the tripwire has
    teeth: that same file DOES move when the verb really runs.

Safety: the real-CLI class runs mutating argv ONLY inside the sandbox HOME
(never the operator's real ~/.hscc), and only against a node name that exists
solely in the seeded sandbox file.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hscc_daemon.cli as hscc_cli
import hscc_daemon.heal_breaker as hb
import hscc_daemon.hscc as unified

REPO = Path(__file__).resolve().parents[2]

# The audit's (D) execution-echo sensor (scripts/hscc_cli_help_audit.py
# EXEC_ECHO). Help output must never match it.
EXEC_ECHO_NEEDLES = ("Cleared heal strikes", "healing resumes",
                     "No strikes recorded for")

# Seeded sandbox node — exists ONLY inside the throwaway HOME.
SEEDED_NODE = "spark-01"


# ---------------------------------------------------------------------------
# unit layer — in-process dispatch against landmines
# ---------------------------------------------------------------------------

@pytest.fixture()
def heal_landmine(monkeypatch):
    """Every path that could mutate or read breaker state RAISES.

    ``cmd_heal`` is the verb itself (dispatch landmine); ``_load``/``_save``
    cover any future path that reaches heal_breaker without going through it.
    A ``--help`` that reaches dispatch therefore fails the test on the spot,
    even where the side effect would be invisible in a sandbox.
    """
    def _boom(*a, **k):
        raise AssertionError(
            "heal state reached while printing help — the verb EXECUTED")

    monkeypatch.setattr(hscc_cli, "cmd_heal", _boom)
    monkeypatch.setattr(hb, "_load", _boom)
    monkeypatch.setattr(hb, "_save", _boom)
    return _boom


def _main_capture(monkeypatch, capsys, *argv):
    """Run unified.main() with sys.argv patched; return (rc, stdout+stderr).

    rc is the SystemExit code, or None when main() returns normally.
    """
    monkeypatch.setattr(sys, "argv", ["hscc", *argv])
    code = None
    try:
        unified.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 0
    out = capsys.readouterr()
    return code, out.out + out.err


class TestDispatchHelpInert:
    """B4: --help/-h must be help for the heal verbs, at ANY position."""

    @pytest.mark.parametrize("flag", ["--help", "-h"])
    def test_heal_clear_help_never_reaches_verb(self, flag, heal_landmine,
                                                monkeypatch, capsys):
        rc, out = _main_capture(monkeypatch, capsys, "heal-clear", flag)
        assert rc == 0, f"heal-clear {flag} exited {rc}: {out!r}"
        assert "hscc heal-clear" in out
        # The breaker panel of the EXECUTING verb must not be printed.
        for needle in EXEC_ECHO_NEEDLES:
            assert needle not in out, (
                f"help output echoes execution wording {needle!r} — the "
                "audit's EXEC_ECHO sensor would flag --help as executing")

    def test_heal_clear_help_after_node_never_clears(self, heal_landmine,
                                                     monkeypatch, capsys):
        # The dangerous shape: on the base tree this CLEARED spark-01's real
        # strikes (and printed 'Cleared heal strikes ... healing resumes').
        rc, out = _main_capture(monkeypatch, capsys,
                                "heal-clear", SEEDED_NODE, "--help")
        assert rc == 0, f"--help after the node exited {rc}: {out!r}"
        assert "hscc heal-clear" in out
        for needle in EXEC_ECHO_NEEDLES:
            assert needle not in out

    def test_heal_status_help_inert(self, heal_landmine, monkeypatch, capsys):
        rc, out = _main_capture(monkeypatch, capsys, "heal-status", "--help")
        assert rc == 0, f"heal-status --help exited {rc}: {out!r}"
        assert "hscc heal-status" in out
        for needle in EXEC_ECHO_NEEDLES:
            assert needle not in out

    def test_positive_dispatch_still_clears(self, monkeypatch, capsys):
        """No help token → the verb still dispatches with the same kwargs.

        Without this pin an over-eager 'always print help' fix would also
        pass every inertness test above.
        """
        hits = []
        monkeypatch.setattr(hscc_cli, "cmd_heal",
                            lambda **k: hits.append(k))
        _main_capture(monkeypatch, capsys, "heal-clear", SEEDED_NODE,
                      "operator", "reviewed")
        assert hits == [{"node": SEEDED_NODE, "reason": "operator reviewed"}]

    def test_positive_dispatch_status_only(self, monkeypatch, capsys):
        hits = []
        monkeypatch.setattr(hscc_cli, "cmd_heal",
                            lambda **k: hits.append(k))
        _main_capture(monkeypatch, capsys, "heal-status")
        assert hits == [{"status_only": True}]


class TestCmdHealDirectGuard:
    """Defence-in-depth: the guard inside cmd_heal itself (B6 cmd_stop shape).

    The unified CLI intercept covers the CLI path; these tests pin the
    library-direct path — a caller that lands in cmd_heal with a help token
    as the node name must not touch the ledger. State file redirected to
    tmp: the real ~/.hscc/heal-breaker.json is never opened.
    """

    @pytest.fixture()
    def breaker_file(self, tmp_path, monkeypatch):
        now = time.time() - 30
        path = tmp_path / "heal-breaker.json"
        # The ledger deliberately carries strike records keyed "--help" and
        # "-h" — exactly what the base tree produces after the bug runs once
        # (the clear path stores a "_cleared" stamp for whatever "node" it
        # was handed). Without those keys the base tree's clear("--help")
        # pops nothing and does NOT save, so the byte tripwire below would
        # have no teeth; with them the base bug rewrites the file and the
        # guard test fails for the right reason.
        path.write_text(json.dumps(
            {SEEDED_NODE: [now, now + 10],
             "-h": [now], "--help": [now],
             "_cleared": {"old-node": {"at": now, "reason": "x"}}}))
        monkeypatch.setattr(hb, "HEAL_BREAKER_FILE", str(path))
        return path

    def test_positive_direct_clear_moves_file(self, breaker_file, capsys):
        """Tripwire teeth: the real clear path DOES rewrite this file."""
        blob = breaker_file.read_bytes()
        hscc_cli.cmd_heal(node=SEEDED_NODE)
        assert breaker_file.read_bytes() != blob, (
            "positive control failed — nothing can rewrite the fixture, so "
            "the inertness assertions below prove nothing")

    @pytest.mark.parametrize("flag", ["--help", "-h"])
    def test_direct_call_with_help_token_touches_nothing(self, flag,
                                                         breaker_file,
                                                         capsys):
        blob = breaker_file.read_bytes()
        stamp = breaker_file.stat().st_mtime_ns
        hscc_cli.cmd_heal(node=flag)
        out = capsys.readouterr().out
        assert "Usage: hscc heal-clear" in out
        assert breaker_file.read_bytes() == blob, (
            "cmd_heal mutated the ledger on a help token — the second line "
            "of defence is down")
        assert breaker_file.stat().st_mtime_ns == stamp

    def test_direct_call_missing_node_still_usage(self, breaker_file,
                                                  capsys):
        blob = breaker_file.read_bytes()
        hscc_cli.cmd_heal()
        assert "Usage: hscc heal-clear" in capsys.readouterr().out
        assert breaker_file.read_bytes() == blob


class TestHelpTopics:
    """B5: 'hscc help heal-clear' / 'hscc help heal-status' exit 0."""

    @pytest.mark.parametrize("verb", ["heal-clear", "heal-status"])
    def test_help_topic_exits_zero(self, verb, heal_landmine, monkeypatch,
                                   capsys):
        rc, out = _main_capture(monkeypatch, capsys, "help", verb)
        assert rc == 0, f"help {verb} exited {rc}: {out!r}"
        assert f"hscc {verb}" in out
        assert "heal-breaker" in out

    def test_unknown_topic_still_exits_one(self, heal_landmine, monkeypatch,
                                           capsys):
        rc, out = _main_capture(monkeypatch, capsys, "help", "no-such-verb")
        assert rc == 1
        assert "no help for" in out


class TestCommandHelpEntries:
    """Content + budget pins for the two new COMMAND_HELP entries."""

    @pytest.mark.parametrize("verb", ["heal-clear", "heal-status"])
    def test_entry_exists_with_usage_line(self, verb):
        entry = unified.COMMAND_HELP.get(verb)
        assert entry, f"COMMAND_HELP is missing {verb!r} (audit B5)"
        assert "Usage: hscc " + verb in entry

    @pytest.mark.parametrize("verb", ["heal-clear", "heal-status"])
    def test_every_line_within_gate_budget(self, verb):
        # _handle_help prints '  ' + value; multi-line values are printed
        # per-line with the 2-space prefix only on the FIRST line, so every
        # authored line must stay within 76 to keep the render within the
        # (B) gate's 78-column budget (same budget COMMAND_HELP 'cluster'
        # follows — see t_90082309).
        for line in unified.COMMAND_HELP[verb].splitlines():
            assert len(line) <= 76, f"line over 76 authored cols: {line!r}"

    @pytest.mark.parametrize("verb", ["heal-clear", "heal-status"])
    def test_entry_never_matches_execution_echo_sensor(self, verb):
        entry = unified.COMMAND_HELP[verb]
        for needle in EXEC_ECHO_NEEDLES:
            assert needle not in entry, (
                f"COMMAND_HELP[{verb!r}] contains {needle!r} — help that "
                "reads like the execution echo trips the (D) sensor")

    def test_help_text_says_help_clears_nothing(self):
        # The whole point of B4: the help itself must state the contract.
        assert "clears nothing" in unified.COMMAND_HELP["heal-clear"]


# ---------------------------------------------------------------------------
# real-CLI sandbox — the exact audit probe invocations (B4/B5 repros)
# ---------------------------------------------------------------------------

class TestHealHelpRealCLI:
    """End-to-end inertness of the exact audit probes.

    Pre-seeds a LIVE heal-breaker.json (recent in-window strikes + alert +
    cleared stamps) in the sandbox HOME, so a --help that still acts is
    measurable: bytes and mtimes must not move, no canary may trip, no new
    file may appear, rc must be 0.
    """

    @pytest.fixture()
    def sandbox(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        home = tmp_path / "home"
        (home / ".hscc").mkdir(parents=True)
        # Seed cluster.json so module import never shells out (address guard:
        # 10.0.0.1 is the documented LAN placeholder, never a real host).
        (home / ".hscc" / "cluster.json").write_text(json.dumps(
            {"gateway": {"host": "10.0.0.1"}, "workers": [],
             "nasDevices": []}))
        # LIVE-looking breaker state: strikes inside the 900s window, plus
        # the alert + cleared stamps a tripped node carries. A clear of the
        # seeded node (or of a '--help'/'-h' node) rewrites this file — the
        # tripwire below pins its bytes AND mtime.
        now = time.time() - 60
        (home / ".hscc" / "heal-breaker.json").write_text(json.dumps(
            {SEEDED_NODE: [now - 3, now - 2, now - 1],
             "_alerted": {SEEDED_NODE: now - 5},
             "_cleared": {"old-node": {"at": now - 40,
                                       "reason": "earlier incident"}}}))
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
            "COLUMNS": "120", "NO_COLOR": "1", "TERM": "dumb",
            "HSCC_THEME": "dark",
            "PATH": os.pathsep.join([str(canary), env.get("PATH", "")]),
            "AUDIT_CANARY_LOG": str(trips),
        })
        return {"home": home, "trips": trips, "env": env, "tmp": tmp_path}

    def _run(self, sandbox, argv, columns="120"):
        env = dict(sandbox["env"], COLUMNS=columns)
        return subprocess.run(
            [sys.executable, "-m", "hscc_daemon.hscc", *argv],
            env=env, cwd=str(sandbox["tmp"]),
            capture_output=True, text=True, timeout=120)

    # (argv, expected marker proving the VERB's help was printed)
    HELP_CASES = [
        (["heal-clear", "--help"], "hscc heal-clear"),
        (["heal-clear", "-h"], "hscc heal-clear"),
        (["heal-clear", SEEDED_NODE, "--help"], "hscc heal-clear"),
        (["heal-status", "--help"], "hscc heal-status"),
        (["help", "heal-clear"], "hscc heal-clear"),
        (["help", "heal-status"], "hscc heal-status"),
    ]

    @pytest.mark.parametrize("argv,marker", HELP_CASES)
    @pytest.mark.parametrize("columns", ["80", "120"])
    def test_help_inert_mutates_nothing(self, argv, marker, columns, sandbox):
        home = sandbox["home"]
        state = home / ".hscc" / "heal-breaker.json"
        blob = state.read_bytes()
        stamp = state.stat().st_mtime_ns
        before = {str(p.relative_to(home)) for p in home.rglob("*")
                  if p.is_file() and "trips.log" not in str(p)}

        proc = self._run(sandbox, argv, columns)

        combined = proc.stdout + proc.stderr
        assert proc.returncode == 0, (
            f"{argv!r} exited {proc.returncode}\n{combined}")
        assert marker in proc.stdout, (
            f"{argv!r} did not print the verb's help:\n{combined}")
        assert state.read_bytes() == blob, (
            f"B4-class regression: {argv!r} rewrote heal-breaker.json — the "
            "mutating verb executed behind the help request")
        assert state.stat().st_mtime_ns == stamp, (
            f"{argv!r} rewrote heal-breaker.json (mtime moved)")
        after = {str(p.relative_to(home)) for p in home.rglob("*")
                 if p.is_file() and "trips.log" not in str(p)}
        assert after == before, (
            f"{argv!r} wrote state under HOME: {sorted(after - before)}")
        trips = sandbox["trips"].read_text().strip()
        assert trips == "", f"{argv!r} shelled out: {trips}"
        for needle in EXEC_ECHO_NEEDLES:
            assert needle not in combined, (
                f"{argv!r} output matches the audit EXEC_ECHO sensor: "
                f"{needle!r}")
        assert "\x1b" not in combined, "help output carries ANSI"

    def test_positive_control_tripwire_has_teeth(self, sandbox):
        """The SAME file moves when the verb really runs (no help token).

        Without this control every inertness assertion above would also pass
        against a sandbox whose seed file nothing can touch. Mutates ONLY
        the sandbox copy of the seeded node.
        """
        state = sandbox["home"] / ".hscc" / "heal-breaker.json"
        blob = state.read_bytes()
        proc = self._run(sandbox, ["heal-clear", SEEDED_NODE])
        assert proc.returncode == 0
        assert state.read_bytes() != blob, (
            "positive control failed: the real verb did not mutate the "
            "seeded file — the tripwire above proves nothing")

    def test_help_never_prints_seeded_state(self, sandbox):
        """Read-side inertness: --help must not render the breaker panel.

        The seeded node is LIVE (in-window strikes), so a --help that still
        calls cmd_heal would print its row — a read that the bytes/mtime
        tripwire cannot see.
        """
        proc = self._run(sandbox, ["heal-clear", "--help"])
        assert SEEDED_NODE not in proc.stdout + proc.stderr, (
            "help rendered breaker STATE — a check ran while printing help")
