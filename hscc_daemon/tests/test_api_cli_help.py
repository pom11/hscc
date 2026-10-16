"""Per-verb ``--help`` inertness for ``hscc api`` — BROKEN B1/B2/B3 (t_bf9f1909).

Measured on the base tree (docs/audits/CLI_HELP_2026-10-15.md §Class D):
``cmd_api`` dispatches the verb BEFORE any ``--help`` check, so every verb
``--help`` EXECUTED the verb:

  * ``api start --help``            -> forked the server / bound the API port;
  * ``api status --help``           -> GENERATED the credential ``~/.hscc/api-token`` + QR;
  * ``api install-service --help``  -> wrote the LaunchAgent plist;
  * ``api uninstall-service --help``-> removed the LaunchAgent plist.

The unit-level guard is a landmine: every mutating / acting / forking / shelling
entry point an api verb touches is monkeypatched to RAISE, so a ``--help`` that
reaches any of them fails here even in a sandbox where the side effect itself
would be invisible. The real-CLI class re-runs the exact audit probe invocations
under a throwaway HOME + recording PATH canaries + bound-port tripwires on the
API default port (8787) and the smoke port (8788) — the strongest possible
evidence that a help request binds nothing, writes nothing, forks nothing.

NEVER runs a real ``api start`` here: on the FIXED tree the intercept makes the
invocation inert before any fork; on the BROKEN tree the fork is caught either
by the ``os.fork`` landmine (unit) or by the port tripwire / subprocess timeout
(real CLI) long before anything operator-visible can happen.
"""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import hscc_daemon  # PACKAGE ONLY on purpose: never import
                    # ``hscc_daemon.api_launchd`` here. The install/uninstall
                    # handlers import it lazily inside the function, and the
                    # sibling test_api_launchd.py CLI-seam tests rely on that
                    # first import happening under their private-HOME fixture
                    # (API_PLIST_PATH bakes HOME at import time). An eager
                    # import in this module would bake the operator's REAL
                    # ~/Library/LaunchAgents path and make those tests write
                    # there — the landmines below therefore stub the package
                    # ATTRIBUTE instead of the submodule.
import hscc_daemon.api_cli as api_cli_mod
from hscc_daemon.api_cli import VALID_SUBCOMMANDS, cmd_api

# Verb-help marker: every verb's own help must START with its own usage line.
# Distinct per verb and never a substring of another verb's marker.
USAGE_MARKERS = {
    "start": "Usage: hscc api start",
    "stop": "Usage: hscc api stop",
    "status": "Usage: hscc api status",
    "install-service": "Usage: hscc api install-service",
    "uninstall-service": "Usage: hscc api uninstall-service",
}

# Group marker as it RENDERS: Rich consumes the bare-word "[args]" token in
# the group HELP_TEXT usage line as markup (probe-verified), so the durable
# substring both paths can be checked against is up to <subcommand>.
GROUP_MARKER = "Usage: hscc api <subcommand>"

# Ports a help path must never touch (8787 = api_server DEFAULT_PORT, the
# audit tripwire; 8788 = scripts/api_smoke.py default). Bound IN-TEST so a
# fork+bind either fails loudly (EADDRINUSE in the child) or is visible.
TRIPWIRE_PORTS = (8787, 8788)


# ---------------------------------------------------------------------------
# unit-level: cmd_api intercepts --help before dispatch (landmines)
# ---------------------------------------------------------------------------

class TestVerbHelpIsInert:
    """``<verb> --help`` prints the verb's help and executes NOTHING."""

    @pytest.fixture(autouse=True)
    def _landmines(self, monkeypatch):
        """Every entry point a help path must never reach: RAISE on call.

        Declared as the test-class fixture so it layers on top of the
        package-wide ~/.hscc isolation fixture; monkeypatch teardown keeps the
        blast radius inside each test.
        """
        def boom(name):
            def _f(*a, **k):
                raise AssertionError(
                    f"--help executed mutating/acting code: {name}()")
            return _f
        monkeypatch.setattr(api_cli_mod.os, "fork", boom("os.fork"))
        monkeypatch.setattr(api_cli_mod.daemon_ops, "log", boom("daemon_ops.log"))
        monkeypatch.setattr(api_cli_mod.daemon_ops, "save_pid",
                            boom("daemon_ops.save_pid"))
        monkeypatch.setattr(api_cli_mod.daemon_ops, "write_stopped",
                            boom("daemon_ops.write_stopped"))
        monkeypatch.setattr(api_cli_mod.daemon_ops, "get_pid",
                            boom("daemon_ops.get_pid"))
        monkeypatch.setattr(api_cli_mod, "_load_api_server",
                            boom("_load_api_server"))
        monkeypatch.setattr(api_cli_mod, "_load_gateway_bridge",
                            boom("_load_gateway_bridge"))
        monkeypatch.setattr(api_cli_mod, "_warn_payload_drift",
                            boom("_warn_payload_drift"))
        monkeypatch.setattr(api_cli_mod.verify, "check_plugin_payload",
                            boom("verify.check_plugin_payload"))
        monkeypatch.setattr(api_cli_mod, "_print_api_qr", boom("_print_api_qr"))
        # Stub at the PACKAGE-attribute seam: the handlers do
        # ``from hscc_daemon import api_launchd`` inside the function, which
        # resolves the package attribute BEFORE any submodule import — so the
        # real submodule is never imported (and never bakes a real HOME path).
        class _RaisingLaunchd:
            API_LABEL = "com.hscc.api"

            def install_api_plist(self, *a, **k):
                raise AssertionError(
                    "--help executed mutating/acting code: "
                    "api_launchd.install_api_plist()")

            def uninstall_api_plist(self, *a, **k):
                raise AssertionError(
                    "--help executed mutating/acting code: "
                    "api_launchd.uninstall_api_plist()")
        monkeypatch.setattr(hscc_daemon, "api_launchd", _RaisingLaunchd(),
                            raising=False)

    @pytest.mark.parametrize("verb", VALID_SUBCOMMANDS)
    def test_verb_help_exits_zero_with_its_own_help(self, verb, capsys):
        assert cmd_api([verb, "--help"]) == 0
        out = capsys.readouterr().out
        assert USAGE_MARKERS[verb] in out
        # It is the VERB's help, not the whole group blob.
        assert GROUP_MARKER not in out

    @pytest.mark.parametrize("verb", VALID_SUBCOMMANDS)
    def test_verb_help_short_flag(self, verb, capsys):
        assert cmd_api([verb, "-h"]) == 0
        assert USAGE_MARKERS[verb] in capsys.readouterr().out

    @pytest.mark.parametrize("verb", VALID_SUBCOMMANDS)
    def test_verb_help_never_leaks_another_verbs_usage(self, verb, capsys):
        assert cmd_api([verb, "--help"]) == 0
        out = capsys.readouterr().out
        for other, marker in USAGE_MARKERS.items():
            if other != verb:
                assert marker not in out, f"{verb} help printed {other} usage"

    @pytest.mark.parametrize("argv", [
        ["start", "--port", "9999", "--help"],      # help after a flag VALUE
        ["start", "--theme", "dark", "--help"],     # help after --theme
        ["stop", "--theme=dark", "-h"],
        ["status", "--no-qr", "--help"],
        ["install-service", "--help"],
    ])
    def test_help_anywhere_in_argv_is_help(self, argv, capsys):
        """A --help ANYWHERE after the verb is help, never execution.

        The intercept must scan the RAW subcommand argv (before --theme
        stripping) so a help flag can never be swallowed as a flag value —
        exactly the class of bug one level deeper than B1-B3.
        """
        assert cmd_api(list(argv)) == 0
        out = capsys.readouterr().out
        assert USAGE_MARKERS[argv[0]] in out

    @pytest.mark.parametrize("verb", VALID_SUBCOMMANDS)
    def test_verb_help_text_fits_group_width_convention(self, verb, capsys):
        """Verb help lines stay <= 78 cols (keeps the (B) wrap gate clean at
        width 80 — the same convention autodown_cli's SUBCOMMAND_HELP uses)."""
        assert cmd_api([verb, "--help"]) == 0
        for ln in capsys.readouterr().out.splitlines():
            assert len(ln) <= 78, f"line over 78 cols: {ln!r}"

    def test_no_state_files_touched(self, tmp_path, monkeypatch):
        """PID/log files that dispatch would write stay byte-identical."""
        pid = tmp_path / "api.pid"
        log = tmp_path / "api.log"
        pid.write_text("4242\n")
        log.write_text("pre-existing\n")
        monkeypatch.setattr(api_cli_mod, "API_PID_FILE", str(pid))
        monkeypatch.setattr(api_cli_mod, "API_LOG_FILE", str(log))
        stamps = {p: (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in (pid, log)}
        for verb in VALID_SUBCOMMANDS:
            assert cmd_api([verb, "--help"]) == 0
        for p, (blob, ns) in stamps.items():
            assert p.read_bytes() == blob, f"--help rewrote {p.name}"
            assert p.stat().st_mtime_ns == ns, f"--help rewrote {p.name} (mtime)"

    @pytest.mark.parametrize("argv", ([], ["--help"], ["-h"]))
    def test_group_help_unchanged_for_bare_and_flag(self, argv, capsys):
        """Non-help behaviour byte-identical: group paths still print HELP_TEXT."""
        assert cmd_api(list(argv)) == 0
        assert GROUP_MARKER in capsys.readouterr().out

    def test_unknown_verb_help_shows_group_help_rc0(self, capsys):
        assert cmd_api(["bogus", "--help"]) == 0
        assert GROUP_MARKER in capsys.readouterr().out


# ---------------------------------------------------------------------------
# real-CLI sandbox — `python -m hscc_daemon.hscc api <verb> [--help|-h]`
# (audit-engine safety model: throwaway HOME + PATH canaries + port tripwires)
# ---------------------------------------------------------------------------

class TestVerbHelpRealCLI:
    """End-to-end inertness of the EXACT audit probe invocations (B1/B2/B3).

    Pre-seeds a fake PID file + token under the sandbox HOME so a --help that
    still acts is measurable: rc must be 0, zero new files, zero changed
    bytes, zero canary trips, and both API ports stay ours.
    """

    REPO = Path(__file__).resolve().parents[2]

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
        # PID 99999999 > macOS max_pid: get_pid() treats it as dead on every
        # host, so even a regressed tree that dispatches `stop --help` can
        # never signal a real process — the repro stays inert by design.
        (home / ".hscc" / "api.pid").write_text("99999999\n")
        (home / ".hscc" / "api-token").write_text("sanitized-dummy-token\n")
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
                [str(self.REPO), str(self.REPO / "hscc-project")]),
            "COLUMNS": "120", "NO_COLOR": "1", "TERM": "dumb",
            "HSCC_THEME": "dark",
            "PATH": os.pathsep.join([str(canary), env.get("PATH", "")]),
            "AUDIT_CANARY_LOG": str(trips),
        })
        return {"home": home, "trips": trips, "env": env, "tmp": tmp_path}

    @pytest.fixture()
    def tripwires(self):
        """Bind the API ports IN-TEST: a fork+bind during --help either lands
        on our backlog (acceptable) or fails EADDRINUSE — either way visible.
        A machine already running the API on 8787 degrades that one port to
        'not ours' (the file/canary detectors still cover token/plist effects).
        """
        socks = {}
        for port in TRIPWIRE_PORTS:
            s = socket.socket()
            try:
                s.bind(("127.0.0.1", port))
                s.listen(1)
            except OSError:
                s.close()
                continue  # port already owned elsewhere; the rest still apply
            socks[port] = s
        yield socks
        for s in socks.values():
            s.close()

    def _run(self, sandbox, verb, flag):
        return subprocess.run(
            [sys.executable, "-m", "hscc_daemon.hscc", "api", verb, flag],
            env=sandbox["env"], cwd=str(sandbox["tmp"]),
            capture_output=True, text=True, timeout=60)

    @pytest.mark.parametrize("verb", VALID_SUBCOMMANDS)
    @pytest.mark.parametrize("flag", ["--help", "-h"])
    def test_real_cli_help_inert(self, verb, flag, sandbox, tripwires):
        home = sandbox["home"]
        state = [home / ".hscc" / "api.pid", home / ".hscc" / "api-token"]
        stamps = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in state}
        before = {str(p.relative_to(home)) for p in home.rglob("*")
                  if p.is_file() and "trips.log" not in str(p)}

        try:
            proc = self._run(sandbox, verb, flag)
        except subprocess.TimeoutExpired:
            # A --help that forked a server keeps the captured pipe open past
            # the timeout — that IS the B1 repro. Fail, don't hang the suite.
            pytest.fail(f"api {verb} {flag} hung — the verb executed "
                        "(forked process holding stdout open)")

        assert proc.returncode == 0, (
            f"--help exited {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
        assert USAGE_MARKERS[verb] in proc.stdout
        assert "Traceback" not in proc.stderr
        # Non-tty Rich degrades to plain text: no ANSI in the help path.
        assert "\x1b[" not in proc.stdout + proc.stderr

        for p, (blob, ns) in stamps.items():
            assert p.read_bytes() == blob, (
                f"B2-class regression: --help rewrote {p.name} (credential/"
                "PID state must be untouched)")
            assert p.stat().st_mtime_ns == ns, f"--help rewrote {p.name} (mtime)"
        after = {str(p.relative_to(home)) for p in home.rglob("*")
                 if p.is_file() and "trips.log" not in str(p)}
        assert after == before, f"--help wrote state under HOME: {sorted(after - before)}"
        # B3: the plist path INSIDE the sandbox HOME must not appear.
        assert not (home / "Library" / "LaunchAgents" / "com.hscc.api.plist").exists(), \
            "--help wrote a LaunchAgent plist"
        trips = sandbox["trips"].read_text().strip()
        assert trips == "", f"--help shelled out: {trips}"
        # B1: nothing may have connected to (or taken over) the API ports.
        for port, s in tripwires.items():
            s.settimeout(0.1)
            try:
                conn, _ = s.accept()
                conn.close()
                pytest.fail(f"--help connected to the api port {port}")
            except socket.timeout:
                pass
