"""F5 (card t_fc307a51) — top-level help surface: blurbs, reflow, ed-* deletion.

Six findings from docs/audits/CLI_HELP_2026-10-15.md, all measured on the base
tree by scripts/hscc_cli_help_audit.py (owner=F5 in
scripts/tests/cli_help_expected.json) and pinned here so they cannot come back:

  (1) Class A rows A5/A6 — ``hscc --help`` printed "Project-portfolio
      (flightdeck) commands: ..." and ``hscc help project`` printed
      "(flightdeck)" twice plus "the relocated flightdeck CLI". Rendered help
      must not name the internal package (invariant A; the boundary is the
      RENDERED text — the hscc-project/flightdeck/ path in source and the
      error strings of _handle_project are internal, not findings).
  (2) Class B — ``hscc --help`` section lines are hard two-column strings that
      Rich spills at COLUMNS 80/120: 10 bare-wrap findings at 80, 2 at 120.
      Fixed with the two-line layout the cluster/template groups already use
      (t_875c5019 / t_90082309): usage alone on its line, description wrapped
      with a hanging indent, every line authored inside the 78-column budget.
  (3) Class B — ``hscc help kanban``: COMMAND_HELP["kanban"] lines overflow at
      both mandated widths. Re-flowed at the same budget (F3's
      COMMAND_HELP["cluster"] pattern — print adds the 2-space prefix, so
      every authored line stays <= 76).
  (4) Class B — ``hscc advanced --help`` printed the unknown-command
      "Available: ..." 220-char line and exited 1 (D-rc + B at both widths).
      ``advanced`` now has a real help path: COMMAND_HELP entry, rc 0.
  (5) Class DEAD — the ed-status / ed-install / ed-uninstall placeholder trio
      ("placeholder, not yet available") was advertised in ``hscc help
      advanced``, the advanced ``--help`` row, COMMAND_HELP, and dispatched at
      main(). Operator fan-out ruling: DELETE, not hide (the trio never did
      anything; an alert or a human following the help finds only "not yet
      available"). Commands gone from help AND dispatch;
      the library functions in cli.py stay for their own tests.
  (6) Surface honesty: scripts/hscc_cli_help_audit.py enumerates the ed-* trio
      by name. With the commands gone those probes would print "Unknown
      command" and turn into 6 NEW (D-rc) findings, so the audit enumeration
      loses the trio in the same commit (a removed command must not silently
      vanish — here it is amended loudly, and the surface floors below pin
      what the surface must still cover).

Safety: NOTHING here executes a mutating verb. The only dispatch-adjacent
execution is ``hscc start-daemon --help`` (inert help branch) and the
unknown-command probe (print + exit 1, verified inert by the canaries).
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# The (B) gate measures widths 80 and 120; every surface this card owns is
# authored to the stricter budget so the render is identical at both widths.
BUDGET = 78

# The three placeholder verbs — gone from help AND dispatch (F5 ruling).
ED_TRIO = ("ed-status", "ed-install", "ed-uninstall")

_ANSI = re.compile(r"\x1b\[")
_LOG_LINE = re.compile(
    r"^\[\d{4}-\d\d-\d\dT[^\]]+\]\s*\[\s*(INFO|WARN|ERROR|DEBUG)\]")


def _over_budget(text):
    return [ln for ln in text.splitlines() if len(ln) > BUDGET]


def _canon_words(text):
    """Whitespace-canonicalised blob: line breaks may move, words may not."""
    return " ".join(re.sub(r"\s+", " ", ln).strip()
                    for ln in text.splitlines() if ln.strip())


def _bare_wraps(text):
    """The (B) 'spill' signature this card fixes at the top level: a line
    ending in a space (Rich kept the wrap-space at EOL) followed by a
    column-0 continuation line."""
    lines = text.splitlines()
    out = []
    for i, ln in enumerate(lines[:-1]):
        if ln.endswith(" ") and lines[i + 1][:1] not in (" ", ""):
            out.append((ln, lines[i + 1]))
    return out


# ---------------------------------------------------------------------------
# (1) Class A — no rendered 'flightdeck' on the surfaces F5 owns
# ---------------------------------------------------------------------------

class TestNoRenderedFlightdeck:
    def test_main_help_has_no_flightdeck(self, monkeypatch, capsys):
        from hscc_daemon import hscc as hscc_mod
        monkeypatch.setattr(sys, "argv", ["hscc", "--help"])
        with pytest.raises(SystemExit) as exc:
            hscc_mod.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "flightdeck" not in out.lower(), out

    def test_project_blurb_reworded(self, monkeypatch, capsys):
        from hscc_daemon import hscc as hscc_mod
        monkeypatch.setattr(sys, "argv", ["hscc", "help", "project"])
        assert hscc_mod._handle_help() == 0
        out = capsys.readouterr().out
        assert "flightdeck" not in out.lower(), out
        # The surface still says what the group is and where the detail is.
        assert "Project-portfolio" in out
        assert "hscc project --help" in out
        # The hscc-project/ PATH may render (audit invariant A matches the
        # token "flightdeck" only; a repo-relative path is not the target —
        # the audit doc's boundary section says so explicitly).
        assert "hscc-project" in out

    def test_main_help_project_line_has_no_blurb(self, monkeypatch, capsys):
        from hscc_daemon import hscc as hscc_mod
        monkeypatch.setattr(sys, "argv", ["hscc", "--help"])
        with pytest.raises(SystemExit):
            hscc_mod.main()
        lines = capsys.readouterr().out.splitlines()
        proj = [ln for ln in lines if ln.strip().startswith("project")]
        assert proj, "the project entry line disappeared"
        for ln in proj:
            assert "flightdeck" not in ln


# ---------------------------------------------------------------------------
# (2) Class B — main help: two-line layout, 78-col budget, indented tails
# ---------------------------------------------------------------------------

# (usage, first-continuation) pairs that MUST render two-line because the
# pair cannot fit the 78 budget on one line (the two template verbs — the
# base's only over-budget section lines). The rest keep the base's one-line
# look — the (B) contract is "no line exceeds the budget, no unindented
# spill", which the budget/verbatim pins below check for every entry.
TWO_LINE_ENTRIES = [
    ("  template validate <name> [--structural-only] [--json]",
     "      Validate a template: structural (offline) layer always; placement (live)"),
    ("  template apply <name> [--confirm] [--force-recreate]",
     "      Apply a template (--confirm executes; --force-recreate stops+reruns"),
]

# Descriptions verbatim from the base _get_help_text (re-flow, never reword —
# the same contract F3/F4 pinned for their surfaces). The project line loses
# ONLY the "(flightdeck)" blurb (Class A rule); every other word survives.
DESCRIPTIONS = [
    "Fleet CPU/RAM/GPU; --live adds per-node, per-model load",
    ("Validate a template: structural (offline) layer always; placement "
     "(live) layer unless --structural-only. Exit non-zero if either layer "
     "fails."),
    ("Apply a template (--confirm executes; --force-recreate stops+reruns "
     "units so changed serve flags reach vLLM)"),
    "streams: dgx gateway local heartbeat nas watchdog triggers",
    "Run one check cycle now (default: all)",
    "Commit-time address-guard posture for that checkout",
    "Fleet activity — completions & tool usage over N days (default 7)",
    "Show autoscale decision from current queue depth (read-only)",
    "Show pending failure escalations (dry-run, no mutations)",
    "Project-portfolio commands: standup verify doctor ...",
    "HSCC HTTP API server: start stop status (external apps)",
    "Board hygiene: blocked (show/recover), stale (list/archive)",
]


class TestMainHelpLayout:
    def _render(self, monkeypatch, capsys, columns=None):
        from hscc_daemon import hscc as hscc_mod
        if columns:
            monkeypatch.setenv("COLUMNS", str(columns))
        monkeypatch.setattr(sys, "argv", ["hscc", "--help"])
        with pytest.raises(SystemExit) as exc:
            hscc_mod.main()
        assert exc.value.code == 0
        return capsys.readouterr().out

    @pytest.mark.parametrize("columns", (None, 80, 120))
    def test_no_line_over_budget(self, columns, monkeypatch, capsys):
        out = self._render(monkeypatch, capsys, columns)
        over = _over_budget(out)
        assert not over, f"lines over {BUDGET} cols: {over}"

    @pytest.mark.parametrize("columns", (None, 80, 120))
    def test_no_column0_spill_continuations(self, columns, monkeypatch, capsys):
        """The exact base signature: Rich kept the wrap-space at EOL and
        dropped the continuation at column 0 ('streams: …', 'doctor …',
        '(default 7)', …). A hard-authored <= 78 line cannot spill."""
        out = self._render(monkeypatch, capsys, columns)
        assert not _bare_wraps(out), _bare_wraps(out)

    def test_two_line_entries_have_hanging_indent(self, monkeypatch, capsys):
        out = self._render(monkeypatch, capsys)
        lines = out.splitlines()
        for usage, first_cont in TWO_LINE_ENTRIES:
            assert usage in lines, f"usage line not alone: {usage}"
            i = lines.index(usage)
            assert lines[i + 1] == first_cont, \
                f"bad continuation under {usage!r}: {lines[i + 1]!r}"

    @pytest.mark.parametrize("columns", (None, 80, 120))
    def test_descriptions_verbatim_canonically(self, columns, monkeypatch,
                                               capsys):
        """Re-flow only: every base description survives word-for-word at
        every width (canonical whitespace — line breaks may move, words may
        not; this is the identity the (A) gate matches on)."""
        joined = _canon_words(self._render(monkeypatch, capsys, columns))
        for desc in DESCRIPTIONS:
            assert " ".join(desc.split()) in joined, \
                f"width={columns}: description reworded/lost: {desc[:60]!r}"

    def test_examples_section_kept_real_names(self, monkeypatch, capsys):
        """The Examples block keeps the base's real template names (they are
        real per the registry inventory in the audit doc)."""
        out = self._render(monkeypatch, capsys)
        for ex in ("hscc status", "hscc check gateway", "hscc template list",
                   "hscc template preview 3node-coding",
                   "hscc template apply 3node-coding --confirm",
                   "hscc cluster status"):
            assert ex in _canon_words(out), f"example lost: {ex}"

    def test_sections_and_footer_survive(self, monkeypatch, capsys):
        out = self._render(monkeypatch, capsys)
        for marker in ("Daemon control", "Health & monitoring",
                       "Cluster & templates", "Utility", "Examples",
                       "hscc help advanced",
                       "Docs: ~/dev/hscc/README.md", "Theme:"):
            assert marker in out, f"section/footer marker lost: {marker}"
        # start-daemon itself is the ADVANCED surface's content, not the
        # main help's — the main help only points at `hscc help advanced`.
        assert "start-daemon" not in out


# ---------------------------------------------------------------------------
# (3) Class B — COMMAND_HELP reflow: kanban + project (<= 76 authored cols)
# ---------------------------------------------------------------------------

class TestCommandHelpBudget:
    @pytest.mark.parametrize("topic", ("kanban", "project", "cluster",
                                       "template", "advanced"))
    def test_every_entry_line_within_authored_budget(self, topic):
        from hscc_daemon import hscc as hscc_mod
        value = hscc_mod.COMMAND_HELP[topic]
        # _handle_help prints "  {value}": one 2-space prefix for the whole
        # value, so EVERY authored line must fit 78-2 = 76.
        for ln in value.splitlines():
            assert len(ln) <= BUDGET - 2, f"{topic}: {len(ln)} cols: {ln!r}"

    def test_kanban_help_renders_in_budget(self, monkeypatch, capsys):
        from hscc_daemon import hscc as hscc_mod
        monkeypatch.setattr(sys, "argv", ["hscc", "help", "kanban"])
        assert hscc_mod._handle_help() == 0
        out = capsys.readouterr().out
        assert not _over_budget(out), _over_budget(out)

    def test_kanban_content_verbatim_canonically(self, monkeypatch, capsys):
        from hscc_daemon import hscc as hscc_mod
        monkeypatch.setattr(sys, "argv", ["hscc", "help", "kanban"])
        assert hscc_mod._handle_help() == 0
        joined = _canon_words(capsys.readouterr().out)
        for frag in ("blocked [--json]: list BLOCKED cards with why",
                     "blocked --recover <id> [--reason <t>]: recover one "
                     "blocked card to ready",
                     "stale [--older-than <days>] [--json]: list "
                     "non-terminal cards",
                     "stale --archive <id>: archive one (t_e751e652)",
                     "Usage: hscc kanban <subcommand> [args]"):
            assert " ".join(frag.split()) in joined, f"lost: {frag[:50]!r}"

    def test_project_help_renders_in_budget(self, monkeypatch, capsys):
        from hscc_daemon import hscc as hscc_mod
        monkeypatch.setattr(sys, "argv", ["hscc", "help", "project"])
        assert hscc_mod._handle_help() == 0
        out = capsys.readouterr().out
        assert not _over_budget(out), _over_budget(out)
        assert "Usage: hscc project <subcommand> [args]" in out


# ---------------------------------------------------------------------------
# (4) `advanced` gains a help path (D-rc + B at both widths)
# ---------------------------------------------------------------------------

class TestAdvancedHelpPath:
    def test_advanced_flag_help_rc0(self, monkeypatch, capsys):
        from hscc_daemon import hscc as hscc_mod
        monkeypatch.setattr(sys, "argv", ["hscc", "advanced", "--help"])
        with pytest.raises(SystemExit) as exc:
            hscc_mod.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "hscc advanced" in out
        assert not _over_budget(out), _over_budget(out)
        assert "Available:" not in out  # the unknown-command blob is gone

    def test_help_advanced_rc0_lists_start_daemon(self, monkeypatch, capsys):
        from hscc_daemon import hscc as hscc_mod
        monkeypatch.setattr(sys, "argv", ["hscc", "help", "advanced"])
        assert hscc_mod._handle_help() == 0
        out = capsys.readouterr().out
        assert "start-daemon" in out
        assert "Run the daemon loop in the foreground (used by launchd)" in out
        assert not _over_budget(out), _over_budget(out)


# ---------------------------------------------------------------------------
# (5) ed-* placeholder trio — deleted from help AND dispatch
# ---------------------------------------------------------------------------

class TestEdTrioDeleted:
    def test_no_ed_entries_in_command_help(self):
        from hscc_daemon import hscc as hscc_mod
        for cmd in ED_TRIO:
            assert cmd not in hscc_mod.COMMAND_HELP, cmd

    def test_help_advanced_has_no_ed_rows(self, monkeypatch, capsys):
        from hscc_daemon import hscc as hscc_mod
        monkeypatch.setattr(sys, "argv", ["hscc", "help", "advanced"])
        assert hscc_mod._handle_help() == 0
        out = capsys.readouterr().out
        for cmd in ED_TRIO:
            assert cmd not in out, cmd
        assert "not yet available" not in out
        assert "Event-driven" not in out

    def test_help_ed_says_no_help_rc1(self, monkeypatch, capsys):
        """The commands are GONE: 'hscc help ed-*' behaves like any unknown
        topic (rc 1 + 'no help for'), not a placeholder notice."""
        from hscc_daemon import hscc as hscc_mod
        for cmd in ED_TRIO:
            monkeypatch.setattr(sys, "argv", ["hscc", "help", cmd])
            assert hscc_mod._handle_help() == 1, cmd
            out = capsys.readouterr().out
            assert f"no help for '{cmd}'" in out
            assert "not yet available" not in out

    def test_ed_dispatch_removed_from_main(self):
        """The dispatch surface must not contain the trio (source-level pin:
        a restored elif would bring execution back without touching help)."""
        from hscc_daemon import hscc as hscc_mod
        src = (Path(hscc_mod.__file__)).read_text()
        for cmd in ED_TRIO:
            assert f'"{cmd}"' not in src, cmd
        assert "cmd_ed_status" not in src
        assert "cmd_ed_install" not in src
        assert "cmd_ed_uninstall" not in src

    def test_library_functions_stay_for_their_own_tests(self):
        """DELETE scope is the CLI surface; the inert library functions and
        their unit tests in cli.py / test_cli.py are not this card's target."""
        from hscc_daemon import cli
        for name in ("cmd_ed_status", "cmd_ed_install", "cmd_ed_uninstall"):
            assert callable(getattr(cli, name)), name

    def test_help_advanced_lists_start_daemon_only(self, monkeypatch, capsys):
        """The advanced surface still exists (start-daemon), so the deletion
        is scoped to the trio, not to the group."""
        from hscc_daemon import hscc as hscc_mod
        monkeypatch.setattr(sys, "argv", ["hscc", "help", "advanced"])
        assert hscc_mod._handle_help() == 0
        assert "start-daemon" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# (6) audit surface honesty — the enumeration follows the CLI
# ---------------------------------------------------------------------------

class TestAuditSurfaceFollowsCli:
    @pytest.fixture()
    def audit_mod(self):
        sys.path.insert(0, str(REPO / "scripts"))
        import hscc_cli_help_audit as mod
        return mod

    def test_build_surface_excludes_ed_trio(self, audit_mod):
        paths, _ = audit_mod.build_surface()
        for _, argv, _, _ in paths:
            for cmd in ED_TRIO:
                assert cmd not in argv, argv

    def test_example_oracle_excludes_ed_trio(self, audit_mod):
        ex = audit_mod.ExampleParser(audit_mod.flightdeck_commands())
        for cmd in ED_TRIO:
            assert cmd not in ex.daemon_cmds, cmd

    def test_start_daemon_and_advanced_still_enumerated(self, audit_mod):
        paths, _ = audit_mod.build_surface()
        argvs = [tuple(argv) for _, argv, _, _ in paths]
        assert ("start-daemon",) in argvs
        assert ("advanced",) in argvs

    def test_surface_floors_match_a_deleted_trio(self, audit_mod, monkeypatch):
        """The deletion is honest, in both directions:
        (a) the trio is ENTIRELY gone from the enumeration (other cases
            assert this directly), and the enumeration still clears the
            gate's floors;
        (b) re-adding the trio to COMMAND_HELP (the seam build_surface
            reads) yields EXACTLY +3 paths / +3 probes — the trio's true
            contribution through that seam — so the prune removed precisely
            the trio and no other surface rode along. (The trio also sat in
            an explicit flag-list literal; its --help probes are covered by
            the deleted probes in the real-CLI class below.)"""
        paths, _ = audit_mod.build_surface()
        n_paths = len(set((g, tuple(a), k) for g, a, _, k in paths))
        # Same invocation model as audit(): flag = --help (+ help <cmd> when
        # argv is non-empty); flag2/help = one invocation.
        def count(ps):
            n = 0
            for _, argv, _, kind in ps:
                if kind == "flag":
                    n += 2 if argv else 1
                else:
                    n += 1
            return n
        n_probes = count(paths)
        sys.path.insert(0, str(REPO / "scripts" / "tests"))
        import test_cli_help_surface as gate
        assert n_paths >= gate.MIN_SURFACE_PATHS, (
            f"surface {n_paths} under floor {gate.MIN_SURFACE_PATHS}")
        assert n_probes >= gate.MIN_PROBES, (
            f"probes {n_probes} under floor {gate.MIN_PROBES}")
        from hscc_daemon import hscc as hscc_mod
        with monkeypatch.context() as mp:
            mp.setattr(hscc_mod, "COMMAND_HELP", {
                **hscc_mod.COMMAND_HELP,
                "ed-status": "x", "ed-install": "x", "ed-uninstall": "x"})
            base2, _ = audit_mod.build_surface()
        assert count(base2) - n_probes == 3, "trio probes moved by != 3"
        assert len(set((g, tuple(a), k) for g, a, _, k in base2)) - n_paths == 3, \
            "trio paths moved by != 3"


# ---------------------------------------------------------------------------
# real-CLI sandbox — the EXACT audit probe invocations at COLUMNS 80 and 120
# ---------------------------------------------------------------------------

class TestF5SurfacesRealCLI:
    """End-to-end proof on the invocations the gate runs (same fixture shape
    as test_cli_help_f4_surfaces.py): rc 0 everywhere, zero new HOME files,
    zero canary trips, no daemon-log-shaped line, no ANSI, and every
    top-level surface line inside the 78-column budget at BOTH widths."""

    SURFACES = {
        "help": ([], "Usage:  hscc <command> [args]"),
        "flag-help": (["--help"], "Usage:  hscc <command> [args]"),
        "short-help": (["-h"], "Usage:  hscc <command> [args]"),
        "help advanced": (["help", "advanced"], "start-daemon"),
        "help kanban": (["help", "kanban"], "Board hygiene for autodown."),
        "help project": (["help", "project"], "hscc project <subcommand>"),
        "advanced --help": (["advanced", "--help"], "hscc advanced"),
        "start-daemon --help": (["start-daemon", "--help"], "start-daemon"),
        # gone: the unknown-command path proves the trio no longer dispatches
        "ed-status help-gone": (["help", "ed-status"], "no help for 'ed-status'"),
        "ed-install gone": (["ed-install", "--help"], "Unknown command: ed-install"),
    }

    @pytest.fixture()
    def sandbox(self, tmp_path):
        home = tmp_path / "home"
        (home / ".hscc").mkdir(parents=True)
        # Seed cluster.json so module import never shells out (address guard:
        # 10.0.0.1 is the documented LAN placeholder, never a real host).
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
            "NO_COLOR": "1", "TERM": "dumb", "HSCC_THEME": "dark",
            "PATH": os.pathsep.join([str(canary), env.get("PATH", "")]),
            "AUDIT_CANARY_LOG": str(trips),
        })
        return {"home": home, "trips": trips, "env": env}

    def _run(self, sandbox, argv, columns):
        env = dict(sandbox["env"], COLUMNS=str(columns))
        return subprocess.run(
            [sys.executable, "-m", "hscc_daemon.hscc", *argv],
            env=env, cwd=str(sandbox["home"]),
            capture_output=True, text=True, timeout=60)

    @pytest.mark.parametrize("columns", (80, 120))
    @pytest.mark.parametrize("label", list(SURFACES))
    def test_surface_inert_and_within_budget(self, label, columns, sandbox):
        argv, marker = self.SURFACES[label]
        home = sandbox["home"]
        before = {str(p.relative_to(home)) for p in home.rglob("*")
                  if p.is_file() and "trips.log" not in str(p)}

        proc = self._run(sandbox, argv, columns)

        gone = "gone" in label
        assert proc.returncode == (1 if gone else 0), (
            f"rc {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
        blob = proc.stdout + proc.stderr
        assert marker in blob
        assert "Traceback" not in proc.stderr
        assert not [ln for ln in blob.splitlines() if _LOG_LINE.match(ln)], \
            f"help emitted a daemon log line: {blob[:200]}"
        assert not _ANSI.search(blob), "ANSI leaked into a help surface"
        assert "flightdeck" not in blob.lower(), (
            "invariant A: rendered help names the internal package")
        after = {str(p.relative_to(home)) for p in home.rglob("*")
                 if p.is_file() and "trips.log" not in str(p)}
        assert after == before, f"--help wrote state under HOME: {after - before}"
        assert sandbox["trips"].read_text().strip() == "", "--help shelled out"
        if gone:
            assert "not yet available" not in blob
        else:
            over = [ln for ln in proc.stdout.splitlines() if len(ln) > BUDGET]
            assert not over, f"COLUMNS={columns}: overflow {over}"

    @pytest.mark.parametrize("columns", (80, 120))
    def test_ed_flag_help_now_unknown_command(self, columns, sandbox):
        """The trio's --help probes used to print a placeholder and exit 0.
        The commands are deleted: they land on the unknown-command path
        (rc 1) exactly like any removed verb — the (D-rc) findings the audit
        would raise there are why the audit enumeration loses them too."""
        for cmd in ED_TRIO:
            proc = self._run(sandbox, [cmd, "--help"], columns)
            assert proc.returncode == 1, cmd
            assert "Unknown command" in proc.stdout
            assert "not yet available" not in proc.stdout

    @pytest.mark.parametrize("columns", (80, 120))
    def test_unknown_command_blob_still_works(self, columns, sandbox):
        """The unknown-command print is NOT a help surface (its long
        Available: line stays out of the gate because nothing legitimate
        reaches it): a bogus verb must still exit 1 and name the verbs."""
        proc = self._run(sandbox, ["bogus-verb"], columns)
        assert proc.returncode == 1
        assert "Available:" in proc.stdout

    def test_no_ansi_and_no_side_effects_across_widths(self, sandbox):
        for columns in (80, 120, 500):
            proc = self._run(sandbox, ["--help"], columns)
            assert proc.returncode == 0
            assert not _ANSI.search(proc.stdout)
