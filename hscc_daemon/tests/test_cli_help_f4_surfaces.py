"""F4 (card t_a166be3d) — help-surface reflow for api/autodown + kanban B10.

Four findings from docs/audits/CLI_HELP_2026-10-15.md, all measured on the
base tree by scripts/hscc_cli_help_audit.py and pinned here so they cannot
come back:

  (a) ``hscc api --help`` — Class B: the two-column HELP_TEXT block overflowed
      at width 80 (5 lines) AND at width 120 (2 lines). Fixed with the
      two-line layout (usage alone, description indented under it).
  (b) ``hscc autodown --help`` — Class B: 16 overflow lines at width 80 from
      the paragraph-in-a-column block. Same reflow at the 78 budget.
  (c) ``hscc api uninstall-service`` — the absolute plist path embedded in a
      hard sentence overflowed whenever HOME is long (the audit's sandbox HOME
      measured 87-120+ cols). The path now gets its own indented line and is
      rendered ~-relative when under HOME.
  (d) B10 — ``hscc kanban blocked --help`` / ``stale --help`` emitted autodown
      interlock WARN lines and wrote ~/.hscc/daemon.log, because per-subcommand
      --help fell through into the verb (whose sibling branch --recover /
      --archive MUTATES). Now a per-subcommand --help branch, raw-argv scanned
      before dispatch — the same shape api_cli/autodown_cli already use.

Why the budgets are authored widths, not wrapped widths: ``autodown_cli``
emits HELP_TEXT with plain ``print`` (authored width IS rendered width) and
``api_cli``/kanban print through Rich, whose non-tty console is fixed at
width 200 (their _console helpers) — so COLUMNS cannot rescue a hard line
either. Rich also cannot wrap a filesystem path at all (no break opportunity).
Every line on the surfaces this card owns is therefore authored <= 78 columns,
and the real-CLI class below re-runs the EXACT audit invocations under a
sandbox HOME + recording canaries to prove it at COLUMNS 80 and 120.

NEVER runs a mutating verb here: the kanban landmine fixture makes ANY board
enumeration raise, and ``--recover``/``--archive`` are never invoked.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import hscc_daemon
from hscc_daemon import api_cli, autodown_cli, kanban_blocked as kbx
from hscc_daemon import kanban_cli as kcli

REPO = Path(__file__).resolve().parents[2]

# The (B) gate measures widths 80 and 120; every surface this card owns is
# authored to the stricter budget so the render is identical at BOTH widths
# (and at the width-200 non-tty Rich console the audit actually captures).
BUDGET = 78


def _over_budget(text, budget=BUDGET):
    return [ln for ln in text.splitlines() if len(ln) > budget]


def _canon(text):
    """Whitespace-canonicalised form: line breaks may move, words may not.

    Mirrors the audit's canonical_line() — the identity the gate matches on.
    """
    import re
    return [re.sub(r"\s+", " ", ln).strip()
            for ln in text.splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# (a) api group HELP_TEXT — two-line layout, both gate widths
# ---------------------------------------------------------------------------

API_VERB_LINES = [
    "hscc api start [--tailscale] [--bind <ip>] [--port <n>] [--no-qr]",
    "hscc api stop",
    "hscc api status [--no-qr]",
    "hscc api install-service",
    "hscc api uninstall-service",
    "hscc api --help",
]

# Descriptions verbatim from the base HELP_TEXT (Class B row: 'move the
# description to a wrapped second line per verb' — re-flow, never reword).
API_DESCRIPTIONS = [
    "Start the API server in the background",
    "Stop the running API server",
    "Show running/stopped + bound host:port",
    "OPT-IN: write the launchd unit (boot-resilient API; loading stays manual)",
    "Remove the launchd unit file written by install-service",
    "This help",
]


class TestApiGroupHelpLayout:
    @pytest.mark.parametrize("argv", ([], ["--help"], ["-h"]))
    def test_two_line_layout_and_budget(self, argv, capsys):
        assert api_cli.cmd_api(list(argv)) == 0
        out = capsys.readouterr().out
        assert not _over_budget(out), f"line over {BUDGET} cols: {_over_budget(out)}"
        lines = out.splitlines()
        for usage in API_VERB_LINES:
            assert f"  {usage}" in lines, f"usage line not alone: {usage}"
            i = lines.index(f"  {usage}")
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            # (B) 'bare-wrap' signature is a continuation at column 0; the
            # fix's contract is an indented continuation line under the usage.
            assert nxt.startswith("      "), f"no indented continuation: {nxt!r}"

    def test_descriptions_verbatim_canonically(self, capsys):
        """Re-flow only: every base description survives word-for-word."""
        assert api_cli.cmd_api(["--help"]) == 0
        joined = " ".join(_canon(capsys.readouterr().out))
        for desc in API_DESCRIPTIONS:
            assert desc in joined, f"description reworded/lost: {desc!r}"

    def test_no_verb_usage_leaks_into_descriptions(self, capsys):
        """A description line never re-starts a command (the old two-column
        padding is gone, so the extractor cannot see a second example)."""
        assert api_cli.cmd_api(["--help"]) == 0
        for ln in capsys.readouterr().out.splitlines():
            if ln.startswith("      "):
                assert not ln.strip().startswith(("hscc ", "flightdeck "))

    def test_group_marker_still_present(self, capsys):
        assert api_cli.cmd_api([]) == 0
        assert "Usage: hscc api <subcommand>" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# (b) autodown group HELP_TEXT — reflowed at 78/118
# ---------------------------------------------------------------------------

AUTODOWN_VERB_LINES = [
    "hscc autodown status [--json]",
    "hscc autodown enable [--idle-minutes <n>]",
    "hscc autodown enable --force",
    "hscc autodown disable",
    "hscc autodown wake",
    "hscc autodown cancel",
    "hscc autodown --help",
]

# Sentences verbatim from the base block (16 (B) overflow lines became these).
AUTODOWN_DESCRIPTIONS = [
    "Show enabled? state? idle_minutes? last activity? down_since?",
    "Arm idle autodown (default 10). Resets the idle timer so arming never "
    "immediately tears down. Non-acting: if serving is down it does NOT start "
    "it. Aborts non-zero if an ACTIVE model-requiring Hermes cron job exists "
    "(one that runs an agent and needs the GPU serving layer). CPU-only "
    "watchdogs (no_agent:true, model:null) do NOT block — they are noted.",
    "Arm even when active model-requiring Hermes cron jobs exist (autodown "
    "may power the cluster down when they are due). Overridden jobs are "
    "recorded and shown in 'status'.",
    "Disarm autodown and clear the intentional watchdog block so ordinary "
    "supervision resumes. Does NOT restart serving — if you want it up, run "
    "'hscc autodown wake' or bring it up another way.",
    "Force autoup now (also resets the idle timer)",
    "Abort an in-progress teardown",
    "This help",
]


class TestAutodownGroupHelpLayout:
    @pytest.mark.parametrize("argv", ([], ["--help"], ["-h"]))
    def test_two_line_layout_and_budget(self, argv, capsys):
        assert autodown_cli.cmd_autodown(list(argv)) == 0
        out = capsys.readouterr().out
        assert not _over_budget(out), f"line over {BUDGET} cols: {_over_budget(out)}"
        lines = out.splitlines()
        for usage in AUTODOWN_VERB_LINES:
            assert f"  {usage}" in lines, f"usage line not alone: {usage}"
            i = lines.index(f"  {usage}")
            assert lines[i + 1].startswith("      "), "no indented continuation"

    def test_descriptions_verbatim_canonically(self, capsys):
        assert autodown_cli.cmd_autodown(["--help"]) == 0
        joined = " ".join(_canon(capsys.readouterr().out))
        for desc in AUTODOWN_DESCRIPTIONS:
            assert " ".join(desc.split()) in joined, \
                f"description reworded/lost: {desc[:50]!r}"

    def test_closing_note_verbatim(self, capsys):
        assert autodown_cli.cmd_autodown(["--help"]) == 0
        joined = " ".join(_canon(capsys.readouterr().out))
        assert ("'status' is read-only and never mutates anything. Pass --json "
                "for machine-readable output (scripting).") in joined


# ---------------------------------------------------------------------------
# (c) uninstall-service — the path gets its own indented line
# ---------------------------------------------------------------------------

class _FakeLaunchd:
    """Stands in for api_launchd with a caller-chosen path (never touches the
    real ~/Library/LaunchAgents; the handlers import it lazily, so stubbing
    the PACKAGE attribute is the seam test_api_cli_help.py also uses)."""

    API_LABEL = "com.hscc.api"

    def __init__(self, path, removed):
        self._path, self._removed = path, removed

    def uninstall_api_plist(self):
        return {"path": self._path, "removed": self._removed}


class TestUninstallServicePathLine:
    # An absurdly long HOME is exactly what the audit sandbox produced and
    # what made the base sentence overflow at BOTH mandated widths.
    LONG_HOME = ("/" + "/".join(["very-long-sandbox-root-directory"] * 4)) + "/home"
    PLIST_TAIL = "/Library/LaunchAgents/com.hscc.api.plist"

    def _run(self, monkeypatch, path, removed):
        monkeypatch.setattr(hscc_daemon, "api_launchd",
                            _FakeLaunchd(path, removed), raising=False)
        return api_cli.cmd_api(["uninstall-service"])

    def test_missing_unit_path_on_own_indented_line(self, monkeypatch, capsys):
        monkeypatch.setenv("HOME", self.LONG_HOME)
        assert self._run(monkeypatch, self.LONG_HOME + self.PLIST_TAIL,
                         False) == 0
        lines = capsys.readouterr().out.splitlines()
        assert not _over_budget("".join(l + "\n" for l in lines)), \
            f"overflow: {_over_budget(lines)}"
        assert any(ln == "  ~" + self.PLIST_TAIL for ln in lines), \
            f"path not on its own indented ~-relative line: {lines}"
        assert "nothing to remove" in " ".join(lines)

    def test_removed_unit_path_on_own_indented_line(self, monkeypatch, capsys):
        monkeypatch.setenv("HOME", self.LONG_HOME)
        assert self._run(monkeypatch, self.LONG_HOME + self.PLIST_TAIL,
                         True) == 0
        lines = capsys.readouterr().out.splitlines()
        assert not _over_budget("\n".join(lines)), f"overflow: {_over_budget(lines)}"
        assert any(ln == "  ~" + self.PLIST_TAIL for ln in lines)
        assert "  launchctl bootout gui/$UID/com.hscc.api" in lines

    def test_path_outside_home_shown_verbatim(self, monkeypatch, capsys):
        """~-relativising is a display choice, never a lie: a path outside
        HOME (only possible if something rebinds API_PLIST_PATH) is printed
        exactly as the library reported it."""
        monkeypatch.setenv("HOME", self.LONG_HOME)
        outside = "/elsewhere/Library/LaunchAgents/com.hscc.api.plist"
        assert self._run(monkeypatch, outside, True) == 0
        assert any(ln == "  " + outside
                   for ln in capsys.readouterr().out.splitlines())

    def test_display_helper_is_idempotent_on_short_paths(self):
        assert api_cli._display_plist_path("~/x.plist") == "~/x.plist"
        assert api_cli._display_plist_path("/opt/x.plist") == "/opt/x.plist"


# ---------------------------------------------------------------------------
# (d) B10 — kanban per-subcommand --help must not enumerate boards
# ---------------------------------------------------------------------------

class _Boom:
    """Landmine: any board read during a help path is a FAILURE, not a slow
    probe. list_blocked_tasks/list_stale_tasks are the exact read seams the
    verbs use; recover/archive are the mutating siblings — a help path that
    reaches them is a release-class bug, so they landmine too."""

    def __init__(self):
        self.hits = []

    def _boom(self, name):
        def _f(*a, **k):
            self.hits.append(name)
            raise AssertionError(
                f"--help executed board code: {name}()")
        return _f


@pytest.fixture()
def board_landmines(monkeypatch):
    from hscc_daemon import autodown
    boom = _Boom()
    for fn in ("list_blocked_tasks", "list_stale_tasks", "recover_blocked_task",
               "archive_stale_task", "_load_kanban_db_or_default",
               "_enum_board_names"):
        monkeypatch.setattr(autodown, fn, boom._boom(fn), raising=False)
    for fn in ("recover_blocked_task", "list_blocked_tasks"):
        monkeypatch.setattr(kbx, fn, boom._boom(f"kbx.{fn}"), raising=False)
    return boom


class TestKanbanVerbHelpIsInert:
    @pytest.mark.parametrize("flag", ("--help", "-h"))
    def test_blocked_help_inert(self, flag, board_landmines, capsys):
        assert kbx.cmd_kanban(["blocked", flag]) == 0
        assert board_landmines.hits == []
        out = capsys.readouterr().out
        assert "hscc kanban blocked [--json]" in out
        assert "--recover" in out and "--reason" in out
        # It is the VERB's help, not the group blob.
        assert "Usage: hscc kanban <subcommand>" not in out

    @pytest.mark.parametrize("flag", ("--help", "-h"))
    def test_stale_help_inert_via_group_dispatcher(self, flag, board_landmines,
                                                   capsys):
        assert kbx.cmd_kanban(["stale", flag]) == 0
        assert board_landmines.hits == []
        out = capsys.readouterr().out
        assert "hscc kanban stale [--older-than <days>] [--json]" in out
        assert "--archive" in out
        assert "Usage: hscc kanban <subcommand>" not in out

    @pytest.mark.parametrize("flag", ("--help", "-h"))
    def test_stale_help_inert_via_own_dispatcher(self, flag, board_landmines,
                                                 capsys):
        assert kcli.cmd_kanban(["stale", flag]) == 0
        assert board_landmines.hits == []
        assert "hscc kanban stale" in capsys.readouterr().out

    @pytest.mark.parametrize("argv", [
        ["blocked", "--json", "--help"],       # help after --json
        ["blocked", "--theme", "dark", "-h"],  # help after --theme
        ["blocked", "--recover", "--help"],    # help in a flag-VALUE position
        ["stale", "--json", "-h"],
        ["stale", "--archive", "--help"],      # mutating sibling, same shape
    ])
    def test_help_anywhere_in_argv_is_help_never_execution(
            self, argv, board_landmines, capsys):
        """The raw-argv scan is the whole fix: a help flag placed where the
        verb would consume it as a value (--recover --help) must still be
        help. This is the class of bug one level deeper than B10."""
        assert kbx.cmd_kanban(list(argv)) == 0
        assert board_landmines.hits == []

    def test_unknown_verb_help_shows_group_help_rc0(self, board_landmines,
                                                    capsys):
        assert kbx.cmd_kanban(["bogus", "--help"]) == 0
        assert "Usage: hscc kanban <subcommand>" in capsys.readouterr().out

    def test_group_paths_unchanged(self, board_landmines, capsys):
        for argv in ([], ["--help"], ["-h"]):
            assert kbx.cmd_kanban(list(argv)) == 0
            assert "Usage: hscc kanban <subcommand>" in capsys.readouterr().out

    def test_recover_still_dispatches(self, monkeypatch, capsys):
        """The intercept must not swallow the mutating path it guards: a real
        --recover (no help flag) still reaches recover_blocked_task."""
        calls = []
        monkeypatch.setattr(kbx, "recover_blocked_task",
                            lambda tid, reason=None: (calls.append((tid, reason))
                                                      or ("board-x", True)))
        assert kbx.cmd_kanban(["blocked", "--recover", "t-1",
                               "--reason", "ok"]) == 0
        assert calls == [("t-1", "ok")]
        assert "recovered t-1" in capsys.readouterr().out

    def test_blocked_help_does_not_reach_recover(self, monkeypatch, capsys):
        """Mirror of the pin above: `blocked --recover --help` prints help and
        never touches the recovery seam."""
        def boom(*a, **k):
            raise AssertionError("--help reached recover_blocked_task")
        monkeypatch.setattr(kbx, "recover_blocked_task", boom)
        assert kbx.cmd_kanban(["blocked", "--recover", "--help"]) == 0
        assert "hscc kanban blocked [--json]" in capsys.readouterr().out

    def test_stale_delegation_missing_module_reports_clearly(
            self, monkeypatch, capsys):
        """With kanban_cli absent, `stale --help` says so instead of printing
        help for a verb this build does not have (single-source wording)."""
        # `from hscc_daemon import kanban_cli` only re-imports when the package
        # attribute is missing, and None in sys.modules makes that import raise
        # ImportError — together they simulate the pre-t_e751e652 tree.
        monkeypatch.delattr(hscc_daemon, "kanban_cli", raising=False)
        monkeypatch.setitem(sys.modules, "hscc_daemon.kanban_cli", None)
        assert kbx.cmd_kanban(["stale", "--help"]) == 1
        assert "stale is not available yet" in capsys.readouterr().err

    @pytest.mark.parametrize("argv", [
        ["blocked", "--help"], ["stale", "--help"]])
    def test_verb_help_lines_fit_convention(self, argv, capsys):
        assert kbx.cmd_kanban(list(argv)) == 0
        out = capsys.readouterr().out
        assert not _over_budget(out), f"line over {BUDGET} cols: {_over_budget(out)}"

    def test_stale_wording_single_source(self, capsys):
        """kanban_blocked delegates stale help to kanban_cli: both entry
        points must render the SAME text (no second copy to drift)."""
        assert kbx.cmd_kanban(["stale", "--help"]) == 0
        via_group = capsys.readouterr().out
        assert kcli.cmd_kanban(["stale", "--help"]) == 0
        assert capsys.readouterr().out == via_group


# ---------------------------------------------------------------------------
# real-CLI sandbox — the EXACT audit probe invocations at COLUMNS 80 and 120
# ---------------------------------------------------------------------------

class TestF4SurfacesRealCLI:
    """End-to-end proof on the invocations the gate runs:
    ``hscc api --help``, ``hscc autodown --help``, ``hscc kanban blocked
    --help``, ``hscc kanban stale --help`` (plus a couple of per-verb help
    paths that must stay inert). rc 0, zero new HOME files — above all no
    .hscc/daemon.log — zero canary trips, zero log-shaped lines, no ANSI, no
    line over the 78 budget on the re-flowed surfaces."""

    SURFACES = {
        "api --help": (["api", "--help"],
                       "Usage: hscc api <subcommand>"),
        "autodown --help": (["autodown", "--help"],
                            "Usage: hscc autodown <subcommand>"),
        "kanban blocked --help": (["kanban", "blocked", "--help"],
                                  "hscc kanban blocked [--json]"),
        "kanban stale --help": (["kanban", "stale", "--help"],
                                "hscc kanban stale [--older-than <days>]"),
        "api --help (short)": (["api", "-h"], "Usage: hscc api <subcommand>"),
        "autodown -h": (["autodown", "-h"], "Usage: hscc autodown <subcommand>"),
    }

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
            "PYTHONPATH": os.pathsep.join([str(REPO), str(REPO / "hscc-project")]),
            "NO_COLOR": "1", "TERM": "dumb", "HSCC_THEME": "dark",
            "PATH": os.pathsep.join([str(canary), env.get("PATH", "")]),
            "AUDIT_CANARY_LOG": str(trips),
        })
        return {"home": home, "trips": trips, "env": env, "tmp": tmp_path}

    def _run(self, sandbox, argv, columns):
        env = dict(sandbox["env"], COLUMNS=str(columns))
        return subprocess.run(
            [sys.executable, "-m", "hscc_daemon.hscc", *argv],
            env=env, cwd=str(sandbox["tmp"]),
            capture_output=True, text=True, timeout=60)

    @pytest.mark.parametrize("columns", (80, 120))
    @pytest.mark.parametrize("label", list(SURFACES))
    def test_surface_inert_and_within_budget(self, label, columns, sandbox):
        argv, marker = self.SURFACES[label]
        home = sandbox["home"]
        before = {str(p.relative_to(home)) for p in home.rglob("*")
                  if p.is_file() and "trips.log" not in str(p)}

        proc = self._run(sandbox, argv, columns)

        assert proc.returncode == 0, (
            f"--help exited {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
        assert marker in proc.stdout
        blob = proc.stdout + proc.stderr
        assert "Traceback" not in proc.stderr
        # B10: no interlock check may have run, so no daemon-log-shaped line.
        import re
        log_line = re.compile(r"^\[\d{4}-\d\d-\d\dT[^\]]+\]\s*\[\s*(INFO|WARN|ERROR|DEBUG)\]")
        assert not [ln for ln in blob.splitlines() if log_line.match(ln)], \
            f"help emitted a daemon log line: {blob[:200]}"
        assert "\x1b[" not in blob, "ANSI leaked into a help surface"
        after = {str(p.relative_to(home)) for p in home.rglob("*")
                 if p.is_file() and "trips.log" not in str(p)}
        assert after == before, (
            f"--help wrote state under HOME: {sorted(after - before)}")
        assert not (home / ".hscc" / "daemon.log").exists(), \
            "B10 regression: help wrote ~/.hscc/daemon.log"
        assert sandbox["trips"].read_text().strip() == "", \
            "--help shelled out"
        # (B) budget on every surface this card touched. Measured, not assumed:
        # the per-subcommand help this card added is within budget, so a help
        # path that regressed to the group blob (whose F5-owned COMMAND_HELP
        # lines overflow at 80) trips this pin too.
        over = [ln for ln in proc.stdout.splitlines() if len(ln) > BUDGET]
        assert not over, f"COLUMNS={columns}: overflow {over}"

    def test_uninstall_service_reports_path_on_own_line(self, sandbox):
        """The mutating verb (NOT --help — the audit never reaches it there):
        with a long sandbox HOME the removal notice must keep the path on its
        own indented line."""
        proc = self._run(sandbox, ["api", "uninstall-service"], 80)
        assert proc.returncode == 0, proc.stderr
        lines = proc.stdout.splitlines()
        assert "no unit file — nothing to remove:" in lines
        path_line = [ln for ln in lines if ln.startswith("  ~")]
        assert path_line == ["  ~/Library/LaunchAgents/com.hscc.api.plist"], lines
