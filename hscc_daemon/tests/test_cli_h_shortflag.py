"""CLI-wide ``-h`` inertness for the unified ``hscc`` CLI — card t_5132deb3.

The gap (recorded by F5 in docs/audits/NOTES_t_fc307a51.md and confirmed by
the operator's final pass on PR #33): the audit oracle probed ``--help``
only, and main()'s help branches fired only on ``args[1] == "--help"``.
Measured on the base tree, ``-h`` EXECUTED instead of printing help:

  * ``escalate -h`` / ``autoscale -h`` ran the read-only scan/decision
    (operator-verbatim complaint; the audit never saw it);
  * ``start-daemon -h`` fell straight into cmd_start_daemon — THE DAEMON
    LOOP LAUNCHES (the recorded worst case);
  * ``start -h`` forked a background daemon, ``stop -h`` / ``install -h`` /
    ``uninstall -h`` reached the launchd verbs, ``watch -h`` HUNG (live
    tail), ``check -h`` tripped a sparkrun canary, ``status -h`` /
    ``verify -h`` wrote state under HOME, ``notify msg -h`` sent a
    notification, ``advanced -h`` printed "Unknown command", and
    ``profiles --help``/``-h`` loaded the engine and ran it (both flags);
  * a ``--help`` ANYWHERE past the first tail token fell through too
    (``verify --json --help``) — the B10 class one level deeper.

Three layers of proof, same shape as test_heal_cli_help.py /
test_cluster_cli_help.py:

  * unit — a RECORDING landmine on every real handler (daemon verbs,
    install, cluster engine, api/autodown/kanban dispatchers) driven through
    ``unified.main()``; the launch paths (``cli.cmd_start``,
    ``cli.cmd_start_daemon``, ``cli.cmd_stop``, install/uninstall, AND
    ``daemon_ops.run_daemon_loop``) RAISE, so a ``-h`` that reaches dispatch
    fails here and the loop can NEVER launch for real from this file. Every
    command x both flags must print help + exit 0 with ZERO landmine hits;
  * no-reword parity — ``-h`` and ``--help`` must print the SAME bytes (the
    chain's shape: one intercept, one COMMAND_HELP source); the printed
    block must also equal ``hscc help <cmd>``'s block wherever that topic
    renders from COMMAND_HELP;
  * positive dispatch — with NO help token every verb still reaches its
    handler (the intercept must not swallow the real path; this is also the
    intercept-off mutation control: delete the intercept and
    TestHelpNeverDispatches goes red while these stay green).

The launch-class six (start, stop, install, uninstall, start-daemon, watch)
are exempt from the audit's real-subprocess ``-h`` probe
(scripts/hscc_cli_help_audit.py LAUNCH_PROBE_EXEMPT) precisely because this
file pins them in-process with landmines instead.
"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

import hscc_daemon.cli as hscc_cli
import hscc_daemon.hscc as unified

REPO = Path(__file__).resolve().parents[2]


# ── the recording landmine fixture ───────────────────────────────────────
# Handlers that must NEVER be reached (launch/mutate the real world): RAISE.
RAISERS = {"cmd_start", "cmd_start_daemon", "cmd_stop", "cmd_install",
           "cmd_uninstall", "run_daemon_loop"}


class Landmine:
    """Records every handler the CLI reached; the launch paths RAISE."""

    def __init__(self, monkeypatch):
        self.hits = []

        def rec(what):
            raiser = what.split(".")[-1] in RAISERS

            def _f(*a, **k):
                self.hits.append((what, a, tuple(sorted(k))))
                if raiser:
                    raise AssertionError(
                        f"{what} reached dispatch from {a!r}")
            return _f

        import hscc_daemon.install as inst

        for name in ("cmd_check", "cmd_watch", "cmd_notify", "cmd_log",
                     "cmd_status", "cmd_heal", "cmd_start",
                     "cmd_start_daemon", "cmd_stop"):
            monkeypatch.setattr(hscc_cli, name, rec(f"cli.{name}"))
        for name in ("cmd_plist", "cmd_install", "cmd_uninstall"):
            monkeypatch.setattr(inst, name, rec(f"install.{name}"))
        # THE loop itself — belt and braces under cmd_start_daemon (the
        # card's landmine rule: the loop entry must raise, never run).
        import hscc_daemon.daemon_ops as dop
        monkeypatch.setattr(dop, "run_daemon_loop",
                            rec("daemon_ops.run_daemon_loop"))
        # Read-only-but-must-not-run-on-help handlers.
        for name in ("_handle_verify", "_handle_stats", "_handle_throughput",
                     "_handle_autoscale", "_handle_escalate"):
            monkeypatch.setattr(unified, name, rec(f"hscc.{name}"))
        # Engine + group dispatchers.
        monkeypatch.setattr(unified, "_load_cluster_engine",
                            rec("hscc._load_cluster_engine"))
        import hscc_daemon.api_cli as api
        monkeypatch.setattr(api, "cmd_api", rec("api.cmd_api"))
        import hscc_daemon.autodown_cli as ad
        monkeypatch.setattr(ad, "cmd_autodown", rec("autodown.cmd_autodown"))
        import hscc_daemon.kanban_blocked as kb
        monkeypatch.setattr(kb, "cmd_kanban", rec("kanban.cmd_kanban"))


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


# Commands whose bare tail reached a handler on help in the base-tree
# measurement (the operator-listed pair first — escalate/autoscale are the
# verbatim complaint; the launch-class six are the audit-exempt set pinned
# here instead of as subprocess probes).
EXEC_ON_HELP = [
    "escalate", "autoscale", "throughput", "stats", "verify", "check",
    "watch", "status", "log", "plist", "notify",
    "start", "stop", "install", "uninstall", "start-daemon",
]

# Verbs whose help the chain shape (any help token in the tail) must catch
# even when the flag is not the first tail token — the B10 class one level
# deeper, measured broken on base for `verify --json --help`.
TAIL_SHAPES = {
    "verify": ["--json"],
    "escalate": ["--json"],
    "autoscale": ["--json"],
    "stats": ["30"],
    "notify": ["msg"],
    "throughput": ["--json"],
}

FLAG = ("--help", "-h")


class TestHelpNeverDispatches:
    """Every command x both flags prints help rc=0 with ZERO handler hits."""

    @pytest.fixture()
    def mine(self, monkeypatch):
        return Landmine(monkeypatch)

    @pytest.mark.parametrize("cmd", EXEC_ON_HELP)
    @pytest.mark.parametrize("flag", FLAG)
    def test_help_prints_help_and_dispatches_nothing(self, cmd, flag, mine,
                                                     monkeypatch):
        rc, out = _main(monkeypatch, [cmd, flag])
        assert rc == 0, f"hscc {cmd} {flag} exited {rc}:\n{out}"
        assert out.strip(), f"hscc {cmd} {flag} printed nothing"
        assert f"hscc {cmd}" in out, (
            f"hscc {cmd} {flag} did not print the command's help header:\n{out}")
        assert "Traceback" not in out
        assert mine.hits == [], (
            f"hscc {cmd} {flag} reached handlers: {mine.hits}")

    @pytest.mark.parametrize("cmd,tail", sorted(TAIL_SHAPES.items()))
    @pytest.mark.parametrize("flag", FLAG)
    def test_help_anywhere_in_the_tail_is_help(self, cmd, tail, flag, mine,
                                               monkeypatch):
        """`verify --json --help` (flag NOT first) is still help — the
        base's args[1]-only branch dispatched this (measured on base)."""
        rc, out = _main(monkeypatch, [cmd, *tail, flag])
        assert rc == 0, f"hscc {cmd} {' '.join(tail)} {flag} exited {rc}"
        assert f"hscc {cmd}" in out
        assert mine.hits == [], (
            f"hscc {cmd} {' '.join(tail)} {flag} reached handlers: "
            f"{mine.hits}")

    def test_advanced_short_flag_prints_help(self, mine, monkeypatch):
        """`advanced -h` was 'Unknown command: advanced' rc=1 on base."""
        rc, out = _main(monkeypatch, ["advanced", "-h"])
        assert rc == 0
        assert "hscc advanced" in out
        assert mine.hits == []

    def test_profiles_help_loads_nothing(self, mine, monkeypatch):
        """profiles runs through _handle_profiles, not main()'s daemon
        branch: BOTH flags must print help and never load the engine
        (measured on base: BOTH flags ran cmd_profile_status)."""
        for flag in FLAG:
            rc, out = _main(monkeypatch, ["profiles", flag])
            assert rc == 0, f"profiles {flag} exited {rc}"
            assert "hscc profiles" in out
        assert mine.hits == [], (
            f"profiles help loaded/ran the engine: {mine.hits}")

    def test_launch_landmines_have_teeth(self, mine):
        """The exemption teeth control: the launch pair RAISES, so an
        un-caught `-h` fails THIS file instead of forking the loop. Proven
        by calling the patched entry points directly (never via main())."""
        with pytest.raises(AssertionError):
            hscc_cli.cmd_start_daemon()
        import hscc_daemon.daemon_ops as dop
        with pytest.raises(AssertionError):
            dop.run_daemon_loop()
        assert {h[0] for h in mine.hits} == {"cli.cmd_start_daemon",
                                             "daemon_ops.run_daemon_loop"}


class TestShortFlagParityNoReword:
    """The chain's shape: one intercept prints ONE text for both flags,
    sourced from COMMAND_HELP like `hscc help <cmd>` — so -h cannot drift
    from --help, and neither can drift from the help topic."""

    @pytest.mark.parametrize("cmd", EXEC_ON_HELP + ["advanced", "profiles"])
    def test_h_and_long_flag_are_byte_identical(self, cmd, monkeypatch):
        rc1, out1 = _main(monkeypatch, [cmd, "--help"])
        rc2, out2 = _main(monkeypatch, [cmd, "-h"])
        assert (rc1, out1) == (rc2, out2), (
            f"{cmd}: -h and --help diverged:\n--help: {out1!r}\n-h: {out2!r}")

    @pytest.mark.parametrize("cmd", EXEC_ON_HELP + ["profiles"])
    def test_intercept_text_equals_help_topic(self, cmd, monkeypatch):
        """The intercept block == the `hscc help <cmd>` block (same
        COMMAND_HELP source — the BROKEN-HEAL no-reword shape). `advanced`
        is exempt: `hscc help advanced` renders the Rich advanced page, not
        the COMMAND_HELP print — the dedicated test below pins advanced's
        intercept against its COMMAND_HELP source instead."""
        rc_h, out_h = _main(monkeypatch, [cmd, "--help"])
        rc_t, out_t = _main(monkeypatch, ["help", cmd])
        assert rc_h == rc_t == 0
        assert out_h == out_t, (
            f"{cmd}: intercept text drifted from `hscc help {cmd}`:\n"
            f"  intercept: {out_h!r}\n  topic:     {out_t!r}")

    def test_advanced_intercept_uses_command_help_source(self, monkeypatch):
        expected = f"hscc advanced\n  {unified.COMMAND_HELP['advanced']}\n"
        for flag in FLAG:
            _, out = _main(monkeypatch, ["advanced", flag])
            assert out == expected, f"advanced {flag} drifted from source"

    @pytest.mark.parametrize("cmd", EXEC_ON_HELP)
    def test_help_block_fits_the_gate_budget(self, cmd, monkeypatch):
        """Plain-print help: authored width IS rendered width. Keep every
        line inside the (B) gate's 78-col budget (COMMAND_HELP values may
        be multi-line; only the first gets the 2-space prefix, so lines are
        authored <=76 — the F3/F5 convention)."""
        _, out = _main(monkeypatch, [cmd, "-h"])
        for ln in out.splitlines():
            assert len(ln) <= 78, f"{cmd} -h line over 78 cols: {ln!r}"


class TestPositiveDispatchStillRuns:
    """Intercept-off teeth: with NO help token every verb still reaches its
    handler. If someone removes the intercept, TestHelpNeverDispatches goes
    red; if someone over-widens it (help eats real argv), THESE go red."""

    @pytest.mark.parametrize("cmd,tail", sorted([
        ("verify", ["--json"]), ("escalate", []), ("autoscale", []),
        ("throughput", []), ("stats", []), ("check", []), ("watch", []),
        ("status", []), ("log", []), ("plist", []), ("notify", ["msg"]),
        ("start", []), ("stop", []), ("install", []), ("uninstall", []),
        ("start-daemon", []),
    ]))
    def test_verb_without_help_token_reaches_handler(self, cmd, tail,
                                                     monkeypatch):
        mine = Landmine(monkeypatch)
        if cmd in ("start", "stop", "install", "uninstall", "start-daemon"):
            # The landmine RAISES for the launch set: reaching it IS the
            # positive proof.
            with pytest.raises(AssertionError):
                _main(monkeypatch, [cmd, *tail])
        else:
            _main(monkeypatch, [cmd, *tail])
        assert mine.hits, (
            f"hscc {cmd} {' '.join(tail)} reached NO handler — the help "
            "intercept is swallowing real dispatch")

    def test_cluster_up_bare_still_starts_via_engine(self, monkeypatch):
        """Same control through the cluster seam: `up` with no help token
        still calls the engine (the intercept is help-conditional)."""
        calls = []

        class Eng:
            def cmd_cluster_up(self, dry_run=False):
                calls.append(dry_run)
                return {"success": True, "dry_run": dry_run, "units": 0,
                        "issued": []}

        monkeypatch.setattr(unified, "_load_cluster_engine", lambda: Eng())
        _main(monkeypatch, ["cluster", "up"])
        assert calls == [False], f"bare `cluster up` did not dispatch: {calls}"

    def test_cluster_up_help_never_starts(self, monkeypatch):
        calls = []

        class Eng:
            def cmd_cluster_up(self, dry_run=False):
                calls.append(dry_run)
                return {"success": True}

        monkeypatch.setattr(unified, "_load_cluster_engine", lambda: Eng())
        for flag in FLAG:
            rc, out = _main(monkeypatch, ["cluster", "up", flag])
            assert rc == 0
            assert calls == [], f"cluster up {flag} dispatched!"
            assert "Usage: hscc cluster up" in out


class TestHShortFlagRealCLI:
    """Real subprocess probes for the SAFE half of the operator-listed pair
    (escalate/autoscale — read-only, sandbox HOME + exit-97 canaries, the
    audit's safety model). The launch-class six stay in-process above."""

    @pytest.fixture()
    def sandbox(self, tmp_path, monkeypatch):
        import json as _json
        import os
        monkeypatch.chdir(tmp_path)
        home = tmp_path / "home"
        (home / ".hscc").mkdir(parents=True)
        # 10.0.0.1 is the documented LAN placeholder, never a real host.
        (home / ".hscc" / "cluster.json").write_text(_json.dumps(
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

    @pytest.mark.parametrize("cmd", ("escalate", "autoscale"))
    @pytest.mark.parametrize("flag", ("--help", "-h"))
    def test_help_prints_help_and_executes_nothing(self, cmd, flag, sandbox):
        import subprocess
        proc = subprocess.run(
            [sys.executable, "-m", "hscc_daemon.hscc", cmd, flag],
            env=sandbox["env"], cwd=str(sandbox["tmp"]),
            capture_output=True, text=True, timeout=60)
        blob = proc.stdout + proc.stderr
        assert proc.returncode == 0, f"{cmd} {flag}: {blob}"
        assert f"hscc {cmd}" in proc.stdout
        assert "Traceback" not in blob
        assert "\x1b[" not in blob, "ANSI leaked into help"
        assert sandbox["trips"].read_text().strip() == "", (
            f"{cmd} {flag} shelled out: {sandbox['trips'].read_text()}")

    def test_escalate_without_help_still_scans(self, sandbox):
        """Positive control with teeth: the read-only verb DOES run when no
        help token is present (its scan renders the empty-state line —
        never the COMMAND_HELP header)."""
        import subprocess
        proc = subprocess.run(
            [sys.executable, "-m", "hscc_daemon.hscc", "escalate"],
            env=sandbox["env"], cwd=str(sandbox["tmp"]),
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert not proc.stdout.startswith("hscc escalate"), (
            "escalate printed help instead of scanning — the intercept "
            "swallowed the real verb")
