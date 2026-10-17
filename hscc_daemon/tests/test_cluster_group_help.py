"""Cluster group help surface at the (B) gate widths — card t_90082309 (F3),
docs/audits/CLI_HELP_2026-10-15.md Class B rows for ``hscc cluster --help``,
``hscc cluster up/down --help`` and the COMMAND_HELP ``cluster`` line.

Measured on the base tree (audit findings, owner=F3 rules in
scripts/tests/cli_help_expected.json):

  * ``hscc cluster --help``     -> 2 overflow lines at width 80 (the monitor
    and ``up`` group entries were hard-printed single lines >80 cols);
  * ``hscc cluster up --help``  -> the F6 refusal message overflowed at 80
    (2 lines) and at 120 (1 line) — the refusal itself is correct and stays;
  * ``hscc cluster down --help``-> same refusal overflow (2 lines at 80, 1 at
    120);
  * ``hscc help cluster``       -> the COMMAND_HELP ``cluster`` value's single
    ``Subcommands: …`` line overflowed at width 80.

The F3 contract is LAYOUT ONLY: wording preserved (the refusal sentences
rejoin to their exact originals — pinned via a canonical-whitespace guard, so
a silent reword fails even though the wrap differs), rc preserved (``up``/
``down --help`` still exit 1 with the F6 refusal — their D-rc rules stay in
the gate), and the operator-owned monitor surfaces (_MONITOR_HELP,
_parse_monitor_flags, cluster_live.py) are not touched — the group ENTRY for
monitor keeps its description byte-identical, only re-flowed.

Safety: unit tests monkeypatch the engine loader to RAISE, so any group/refusal
help path that reaches dispatch fails here; real-CLI probes run only help argv
under a throwaway HOME with recording PATH canaries (exit 97). Never run a
real cluster up/down from here.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import hscc_daemon.hscc as unified

REPO = Path(__file__).resolve().parents[2]

# The two sentences of the F6 refusal, as they were BEFORE this card re-flowed
# them (base tree, `hscc cluster <sub> --help`). Re-flow may only change
# WHERE the lines break — canonically re-joined, the message must equal these.
REFUSAL_CANONICAL = (
    "Error: unknown argument for 'hscc cluster {sub}': --help "
    "'hscc cluster {sub}' accepts only: --dry-run "
    "(and --theme <name> for the human view). Nothing was started or stopped. "
    "Refusing to guess what --help meant — a destructive command runs only on "
    "arguments it understands.")


def canon(s):
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# unit-level: rendering + inertness
# ---------------------------------------------------------------------------

class TestGroupHelpLayout:
    """``hscc cluster --help``: every line <=78 cols, wording preserved,
    nothing executed."""

    @pytest.fixture(autouse=True)
    def _fixed_width(self, monkeypatch):
        """Deterministic wrap budget: shutil.get_terminal_size honours COLUMNS,
        so setting it steers the real code path (no stubbing)."""
        monkeypatch.setenv("COLUMNS", "80")

    @pytest.fixture(autouse=True)
    def _no_engine(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("group --help loaded/ran the cluster engine")
        monkeypatch.setattr(unified, "_load_cluster_engine", boom)

    def _run(self, monkeypatch, argv):
        import io
        from contextlib import redirect_stdout
        monkeypatch.setattr(sys, "argv", ["hscc", "cluster", *argv])
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                code = unified._handle_cluster()
        except SystemExit as e:
            code = e.code
        return out.getvalue(), code

    # The eight group entries, wording byte-identical to the base single lines.
    GROUP_ENTRIES = [
        ("  hscc cluster status", "Running workloads + idle hosts"),
        ("  hscc cluster hosts",
         "All cluster hosts and saved sparkrun clusters"),
        ("  hscc cluster monitor",
         "One CPU/RAM/GPU snapshot (--live: refreshing view + per-model load)"),
        ("  hscc cluster jobs", "All sparkrun jobs currently running"),
        ("  hscc cluster info", "Detailed resolved cluster configuration"),
        ("  hscc cluster stop <id>",
         "Stop a running workload by container id"),
        ("  hscc cluster down [--dry-run]",
         "Stop ALL sparkrun workloads fleet-wide"),
        ("  hscc cluster up [--dry-run]",
         "Start every unit in serving.json (orch + keepalive)"),
    ]

    def test_group_help_lists_every_subcommand_with_wrapped_entries(
            self, monkeypatch):
        text, code = self._run(monkeypatch, ["--help"])
        assert code == 0
        assert text.startswith("Cluster management commands:")
        lines = text.splitlines()
        # Two-line layout (template/verb-surface convention): usage line, then
        # the description on indented continuation line(s).
        for usage, desc in self.GROUP_ENTRIES:
            assert usage in lines, f"missing usage line {usage!r}"
            i = lines.index(usage)
            cont = lines[i + 1]
            assert cont.startswith("      "), (
                f"desc for {usage!r} not indented: {cont!r}")
            assert desc in canon(" ".join(
                ln for ln in lines[i + 1:i + 4] if ln.startswith("      "))), (
                f"description wording changed for {usage!r}")
            # the usage line is short and the entry is followed by either the
            # next usage or a blank — never by a continuation at col 0
            nxt = lines[i + 2] if i + 2 < len(lines) else ""
            assert not (nxt and not nxt.startswith(" ")
                        and nxt != "Cluster management commands:")

    def test_group_help_lines_fit_the_wrap_gate(self, monkeypatch):
        """The two base overflows (monitor + up entries) must be gone at BOTH
        gate widths 80 and 120: no line may reach 80."""
        for cols in (80, 120):
            monkeypatch.setenv("COLUMNS", str(cols))
            text, code = self._run(monkeypatch, ["--help"])
            assert code == 0
            for ln in text.splitlines():
                assert len(ln) <= 78, f"COLUMNS={cols}: line over 78: {ln!r}"

    def test_monitor_group_entry_wording_preserved(self, monkeypatch):
        """The monitor GROUP entry is re-flowed, not re-worded: the operator
        owns _MONITOR_HELP; the group line's description stays byte-identical
        to the audit-quoted text."""
        text, _ = self._run(monkeypatch, ["--help"])
        assert ("One CPU/RAM/GPU snapshot (--live: refreshing view "
                "+ per-model load)") in canon(text)

    def test_group_help_loads_no_engine(self, monkeypatch):
        # _no_engine makes any load fatal; reaching here proves inertness.
        text, code = self._run(monkeypatch, [])
        assert code == 0
        assert "Cluster management commands:" in text


class TestF6RefusalWrap:
    """``cluster up/down --help``: F6 refusal kept (rc=1, wording preserved),
    only the LINE BREAKS change."""

    @pytest.fixture(autouse=True)
    def _no_dispatch(self, monkeypatch):
        """Fake engine whose every verb RAISES. `up`/`down` import the engine
        before parsing flags (the F6 parser runs after the load — same
        contract B6 pinned), so what must never happen is a verb CALL."""
        def boom(name):
            def _f(*a, **k):
                raise AssertionError(f"refusal path called {name}()")
            return _f

        class FakeEng:
            cmd_cluster_up = boom("eng.cmd_cluster_up")
            cmd_cluster_down = boom("eng.cmd_cluster_down")

        monkeypatch.setattr(unified, "_load_cluster_engine",
                            lambda: FakeEng())

    def _run(self, monkeypatch, sub):
        import io
        from contextlib import redirect_stdout
        monkeypatch.setenv("COLUMNS", "80")
        monkeypatch.setattr(sys, "argv", ["hscc", "cluster", sub, "--help"])
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                code = unified._handle_cluster()
        except SystemExit as e:
            code = e.code
        return out.getvalue(), code

    @pytest.mark.parametrize("sub", ("up", "down"))
    def test_refusal_still_rc1(self, sub, monkeypatch):
        text, code = self._run(monkeypatch, sub)
        assert code == 1, f"{sub} --help returned {code}: {text}"
        assert "Nothing was started or stopped" in text
        assert "--dry-run" in text

    @pytest.mark.parametrize("sub", ("up", "down"))
    def test_refusal_lines_fit_the_wrap_gate(self, sub, monkeypatch):
        for cols in (80, 120):
            monkeypatch.setenv("COLUMNS", str(cols))
            text, code = self._run(monkeypatch, sub)
            assert code == 1
            for ln in text.splitlines():
                assert len(ln) <= 78, (
                    f"{sub} --help @COLUMNS={cols}: line over 78: {ln!r}")

    @pytest.mark.parametrize("sub", ("up", "down"))
    def test_refusal_message_is_rewrapped_not_reworded(self, sub, monkeypatch):
        """Canonical-whitespace guard: the base-tree sentences, joined back and
        collapsed, must equal the new rendering collapsed — so this card can
        only move line breaks, never reword the F6 refusal."""
        text, code = self._run(monkeypatch, sub)
        assert code == 1
        assert canon(text) == canon(REFUSAL_CANONICAL.format(sub=sub)), (
            f"{sub} --help refusal text changed beyond wrapping:\n"
            f"  got:  {canon(text)}\n  want: {canon(REFUSAL_CANONICAL.format(sub=sub))}")

    def test_dangling_theme_message_also_wrapped(self, monkeypatch):
        """The '--theme' needs-a-value refusal comes from the same parser and
        was a 90+ char single line; it stays rc=1 and gains no long line."""
        import io
        from contextlib import redirect_stdout
        monkeypatch.setenv("COLUMNS", "80")
        monkeypatch.setattr(sys, "argv", ["hscc", "cluster", "up", "--theme"])
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                code = unified._handle_cluster()
        except SystemExit as e:
            code = e.code
        assert code == 1
        assert "needs a palette name" in out.getvalue()
        for ln in out.getvalue().splitlines():
            assert len(ln) <= 78, f"line over 78: {ln!r}"


class TestCommandHelpClusterLine:
    """``hscc help cluster`` / COMMAND_HELP ``cluster`` (audit F5-family rule
    for this surface; card t_90082309 body assigns COMMAND_HELP "cluster"
    to F3): no line of the rendered block may reach 80."""

    def _rendered(self):
        text = unified.COMMAND_HELP["cluster"]
        # Exactly how _handle_help renders it: "hscc <topic>" then
        # print(f"  {help_text}") — first line prefixed, rest baked.
        return ["hscc cluster", *(f"  {text}".splitlines())]

    def test_all_lines_fit_78(self):
        for ln in self._rendered():
            assert len(ln) <= 78, f"line over 78: {ln!r}"

    def test_verb_list_preserved(self):
        blob = canon("\n".join(self._rendered()))
        for verb in ("status", "hosts", "monitor", "jobs", "info",
                     "stop <id>", "down [--dry-run]", "up [--dry-run]"):
            assert verb in blob, f"verb {verb!r} vanished from COMMAND_HELP"
        assert "Usage: hscc cluster <subcommand> [args]" in blob


# ---------------------------------------------------------------------------
# real CLI — exact audit invocations under sandbox HOME + recording canaries
# ---------------------------------------------------------------------------

class TestClusterHelpSurfaceRealCLI:
    """Probes the audit makes: ``hscc cluster --help``, ``hscc cluster
    up/down --help`` and ``hscc help cluster`` at COLUMNS 80/120. rc and
    inertness are pinned; the (B) gate re-checks widths in-tree."""

    @pytest.fixture()
    def sandbox(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        home = tmp_path / "home"
        (home / ".hscc").mkdir(parents=True)
        # 10.0.0.1 is the documented LAN placeholder, never a real host.
        (home / ".hscc" / "cluster.json").write_text(json.dumps(
            {"gateway": {"host": "10.0.0.1"}, "workers": [],
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
            "NO_COLOR": "1", "TERM": "dumb",
            "HSCC_THEME": "dark",
            "PATH": os.pathsep.join([str(canary), env.get("PATH", "")]),
            "AUDIT_CANARY_LOG": str(trips),
        })
        return {"home": home, "trips": trips, "env": env, "tmp": tmp_path}

    def _run(self, sandbox, argv, cols):
        env = dict(sandbox["env"], COLUMNS=str(cols))
        return subprocess.run(
            [sys.executable, "-m", "hscc_daemon.hscc", *argv],
            env=env, cwd=str(sandbox["tmp"]),
            capture_output=True, text=True, timeout=60)

    @pytest.mark.parametrize("cols", (80, 120))
    @pytest.mark.parametrize("argv,rc", [
        (["cluster", "--help"], 0),
        (["cluster", "up", "--help"], 1),     # F6 refusal: rc stays 1
        (["cluster", "down", "--help"], 1),
        (["help", "cluster"], 0),
    ])
    def test_real_cli_surface(self, argv, rc, cols, sandbox):
        home = sandbox["home"]
        before = {str(p.relative_to(home)) for p in home.rglob("*")
                  if p.is_file() and "trips.log" not in str(p)}
        proc = self._run(sandbox, argv, cols)
        blob = proc.stdout + proc.stderr
        assert proc.returncode == rc, (
            f"{argv} @COLUMNS={cols} exited {proc.returncode}\n{blob}")
        assert "Traceback" not in proc.stderr
        assert "\x1b[" not in blob, "ANSI leaked into the help surface"
        for ln in proc.stdout.splitlines():
            assert len(ln) <= 78, f"{argv} @COLUMNS={cols}: >78: {ln!r}"
        assert sandbox["trips"].read_text().strip() == "", (
            f"{argv} shelled out: {sandbox['trips'].read_text()}")
        after = {str(p.relative_to(home)) for p in home.rglob("*")
                 if p.is_file() and "trips.log" not in str(p)}
        assert after == before, (
            f"{argv} wrote state under HOME: {sorted(after - before)}")
