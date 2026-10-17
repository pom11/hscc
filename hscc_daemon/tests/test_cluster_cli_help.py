"""Per-subcommand ``--help`` inertness for ``hscc cluster`` — BROKEN B6 + B12
(card t_875c5019, docs/audits/CLI_HELP_2026-10-15.md Class D).

t_5132deb3 (operator ruling on PR #33's final pass) extends this surface:
``up``/``down`` joined HELP_VERBS — a help token now prints their usage
(rc 0, engine never loaded, nothing shelled) instead of the F6 refusal,
while the refusal stays byte-identical for every NON-help unknown token.

Measured on the base tree, in a sandbox HOME with recording PATH canaries:

  * ``cluster stop --help``    -> INVOKED ``sparkrun stop --help``. The help
    token was consumed as the CONTAINER ID and forwarded to the mutating
    sparkrun verb (B6 — a canary trip on a destructive call);
  * ``cluster status --help``  -> shelled ``sparkrun status``;
  * ``cluster hosts --help``   -> shelled ``sparkrun cluster list`` + ``status``;
  * ``cluster info --help``    -> shelled ``sparkrun cluster list`` + ``cluster show``;
  * ``cluster jobs --help``    -> shelled ``sparkrun cluster status``.

Read-only shelling is still execution: it violates the operator's "help
executes nothing" invariant and costs seconds per help request (B12).

Two layers of proof, same shape as test_api_cli_help.py (t_bf9f1909):

  * unit — a landmine fixture that makes ``_load_cluster_engine`` and EVERY
    engine verb RAISE, so a ``--help`` that reaches dispatch fails here even
    where the side effect would be invisible in a sandbox. Plus the argv-shape
    landmines (help after a flag value, help before a destructive flag, help
    swallowed as a ``--theme`` value) and the 78-column line budget that keeps
    the (B) wrap gate clean.
  * real CLI — the exact audit probe invocations under a throwaway HOME with
    recording ``sparkrun``/``hermes``/``gh``/``git`` canaries on PATH: rc 0,
    zero canary trips, zero new files, no ANSI.

Safety: the real-CLI class runs ONLY help argv, and ``hscc cluster stop
--help`` is safe on both trees — on the fixed tree the intercept returns
before the engine loads; on the broken tree the subprocess resolves
``sparkrun`` to the recording canary (exit 97), which never touches the fleet.
Never run a real ``cluster stop``/``up``/``down`` from here.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import hscc_daemon.hscc as unified

REPO = Path(__file__).resolve().parents[2]

# Verbs whose per-subcommand help prints their own usage (B12 read-only half
# + B6 mutating half). `monitor` is NOT included: it already had its own
# inert --help branch and its help text is operator-owned (t_1cc33407).
# `up`/`down` joined with t_5132deb3 (operator ruling on PR #33's final
# pass: help wins over the F6 refusal for a help token — the refusal itself
# stays, byte-identical, for every NON-help unknown token).
HELP_VERBS = ("status", "hosts", "jobs", "info", "stop", "up", "down")

# Each verb's own usage line — distinct, and never a substring of another
# verb's marker, so "it printed the VERB's help, not the group's" is checkable.
USAGE_MARKERS = {
    "status": "Usage: hscc cluster status",
    "hosts": "Usage: hscc cluster hosts",
    "jobs": "Usage: hscc cluster jobs",
    "info": "Usage: hscc cluster info",
    "stop": "Usage: hscc cluster stop",
    "up": "Usage: hscc cluster up [--dry-run]",
    "down": "Usage: hscc cluster down [--dry-run]",
}

# The group help heading — used to assert verb help is NOT the group blob.
# (t_90082309 re-flowed the group entries into the two-line layout, so the
# old padded single line no longer exists; the heading is the stable marker.)
GROUP_MARKER = "Cluster management commands:"


# ---------------------------------------------------------------------------
# unit-level: _handle_cluster intercepts --help before engine load / dispatch
# ---------------------------------------------------------------------------

class TestVerbHelpIsInert:
    """``cluster <verb> --help`` prints the verb's help and executes NOTHING."""

    @pytest.fixture(autouse=True)
    def _landmines(self, monkeypatch):
        """Anything a help path must never reach: RAISE on call.

        ``_load_cluster_engine`` is recorded rather than raised: on the base
        tree EVERY read-only verb help imported the cluster engine (and
        through it the sparkrun subprocess), so "help for the inert verbs
        loads nothing" is pinned by ``engine_loads`` below rather than
        inferred from a sandbox that would hide the import. ``up``/``down``
        load the engine on both trees (the F6 parser runs after the load), so
        a blanket raise there would pin the wrong contract — what must never
        happen for them is a verb CALL, which the fake engine makes fatal.
        """
        def boom(name):
            def _f(*a, **k):
                raise AssertionError(
                    f"--help executed mutating/acting code: {name}()")
            return _f

        loads = []
        self.engine_loads = loads

        class FakeEng:
            """Every verb raises: reaching dispatch during --help is a bug."""
            cmd_stop = boom("eng.cmd_stop")
            cmd_cluster_up = boom("eng.cmd_cluster_up")
            cmd_cluster_down = boom("eng.cmd_cluster_down")
            cmd_cluster_status = boom("eng.cmd_cluster_status")
            cmd_hosts = boom("eng.cmd_hosts")
            cmd_jobs = boom("eng.cmd_jobs")
            cmd_info = boom("eng.cmd_info")
            cmd_monitor = boom("eng.cmd_monitor")

        def fake_load():
            loads.append(1)
            return FakeEng()

        monkeypatch.setattr(unified, "_load_cluster_engine", fake_load)
        for fn in ("render_stop", "render_up", "render_down",
                   "render_cluster_status", "render_hosts", "render_jobs",
                   "render_info", "render_monitor"):
            monkeypatch.setattr(unified.cluster_render, fn,
                                boom(f"cluster_render.{fn}"), raising=False)

    def _run(self, monkeypatch, argv):
        import io
        from contextlib import redirect_stdout
        monkeypatch.setattr(sys, "argv", ["hscc", "cluster", *argv])
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                code = unified._handle_cluster()
        except SystemExit as e:  # dispatch paths may sys.exit
            code = e.code
        return out.getvalue(), code

    @pytest.mark.parametrize("verb", HELP_VERBS)
    def test_verb_help_exits_zero_with_its_own_help(self, verb, monkeypatch):
        text, code = self._run(monkeypatch, [verb, "--help"])
        assert code == 0, f"{verb} --help returned {code}: {text}"
        assert USAGE_MARKERS[verb] in text
        # It is the VERB's help, not the whole group blob.
        assert GROUP_MARKER not in text
        # B12 at the seam this card fixed: help must not even LOAD the engine
        # (the load is what pulled in the sparkrun subprocess on base).
        assert self.engine_loads == [], (
            f"cluster {verb} --help loaded the cluster engine")

    @pytest.mark.parametrize("verb", HELP_VERBS)
    def test_verb_help_short_flag(self, verb, monkeypatch):
        text, code = self._run(monkeypatch, [verb, "-h"])
        assert code == 0
        assert USAGE_MARKERS[verb] in text

    @pytest.mark.parametrize("verb", HELP_VERBS)
    def test_verb_help_never_leaks_another_verbs_usage(self, verb, monkeypatch):
        text, _ = self._run(monkeypatch, [verb, "--help"])
        for other, marker in USAGE_MARKERS.items():
            if other != verb:
                assert marker not in text, f"{verb} help printed {other} usage"

    @pytest.mark.parametrize("verb", HELP_VERBS)
    def test_verb_help_says_whether_it_is_read_only(self, verb, monkeypatch):
        """A help reader must be able to tell a read-only view from a verb
        that stops a live container (B6's whole failure mode)."""
        text, _ = self._run(monkeypatch, [verb, "--help"])
        if verb == "stop":
            assert "live container" in text
        elif verb in ("up", "down"):
            # t_5132deb3: up/down joined the help surface with the operator
            # ruling, so their entries carry the same honesty contract — the
            # text must name what the verb does to a LIVE fleet and pin the
            # --dry-run escape hatch (B6's failure mode is a help request
            # that really started or stopped workloads).
            assert "fleet" in text, f"{verb} help hides the blast radius"
            assert "--dry-run" in text, f"{verb} help omits the dry-run hatch"
        else:
            assert "Read-only" in text

    @pytest.mark.parametrize("verb", HELP_VERBS)
    def test_help_lines_fit_the_wrap_gate(self, verb, monkeypatch):
        """Every help line stays <= 78 cols (the (B) gate's width-80 budget;
        same convention as the template and api verb surfaces)."""
        text, _ = self._run(monkeypatch, [verb, "--help"])
        for ln in text.splitlines():
            assert len(ln) <= 78, f"line over 78 cols: {ln!r}"

    def test_stop_help_does_not_stop_anything(self, monkeypatch):
        """B6, pinned at the unit seam: the help token must never become the
        container id handed to the mutating engine verb."""
        calls = []

        class Eng:
            def cmd_stop(self, container_id):
                calls.append(container_id)
                return {"error": "should never be reached"}

        monkeypatch.setattr(unified, "_load_cluster_engine", lambda: Eng())
        text, code = self._run(monkeypatch, ["stop", "--help"])
        assert calls == [], f"cluster stop --help CALLED cmd_stop: {calls}"
        assert code == 0

    @pytest.mark.parametrize("argv", [
        ["stop", "--id", "--help"],        # help AFTER a flag value
        ["stop", "--help", "--dry-run"],   # help BEFORE a stray flag
        ["status", "--theme", "dark", "--help"],
        ["info", "--theme=dark", "-h"],
        ["hosts", "--json", "--help"],
    ])
    def test_help_anywhere_in_argv_is_help(self, argv, monkeypatch):
        """A --help anywhere after the verb is help, never execution."""
        text, code = self._run(monkeypatch, argv)
        assert code == 0, f"{argv} -> rc {code}"
        assert USAGE_MARKERS[argv[0]] in text

    def test_help_after_theme_never_becomes_the_container_id(self, monkeypatch):
        """`cluster stop --theme --help` used to swallow the help token as the
        palette name and dispatch the mutating verb with '--theme' as its
        container id (the F6 hole, one verb deeper)."""
        calls = []

        class Eng:
            def cmd_stop(self, container_id):
                calls.append(container_id)
                return {"error": "should never be reached"}

        monkeypatch.setattr(unified, "_load_cluster_engine", lambda: Eng())
        text, code = self._run(monkeypatch, ["stop", "--theme", "--help"])
        assert calls == [], f"stop dispatched with {calls}"
        assert code == 0
        assert USAGE_MARKERS["stop"] in text

    @pytest.mark.parametrize("sub", ("up", "down"))
    def test_destructive_up_down_help_prints_help_not_refusal(self, sub,
                                                               monkeypatch):
        """t_5132deb3 (operator ruling on PR #33's final pass): a HELP token
        on up/down prints the verb's usage (rc 0) — it never reaches the F6
        refusal and never starts/stops anything. The refusal itself stays for
        everything that is not a help token (test_destructive_*_below)."""
        calls = []

        class Eng:
            def cmd_cluster_up(self, dry_run=False):
                calls.append(("up", dry_run))

            def cmd_cluster_down(self, dry_run=False):
                calls.append(("down", dry_run))

        monkeypatch.setattr(unified, "_load_cluster_engine", lambda: Eng())
        text, code = self._run(monkeypatch, [sub, "--help"])
        assert code == 0, f"{sub} --help returned {code}: {text}"
        assert calls == [], f"{sub} --help DISPATCHED: {calls}"
        assert f"Usage: hscc cluster {sub}" in text
        assert "Nothing was started or stopped" not in text, (
            "help must not print the F6 refusal any more (operator ruling)")

    @pytest.mark.parametrize("sub", ("up", "down"))
    @pytest.mark.parametrize("tok", ("--foo", "--hosts", "3node-coding",
                                     "--dry", "--dryrun"))
    def test_destructive_unknown_nonhelp_token_still_refuses_rc1(
            self, sub, tok, monkeypatch):
        """The other half of the ruling: with NO help token in argv the F6
        refusal is untouched — rc=1, inert, and byte-identical wording (the
        canonical-whitespace pin below locks the words)."""
        text, code = self._run(monkeypatch, [sub, tok])
        assert code == 1, f"{sub} {tok} returned {code}: {text}"
        assert "Nothing was started or stopped" in text
        assert "--dry-run" in text

    @pytest.mark.parametrize("sub", ("up", "down"))
    def test_destructive_help_refusal_message_is_not_reworded(self, sub,
                                                              monkeypatch):
        """The F6 refusal wording belongs to F3's gate rules
        (cli_help_expected.json quotes it), so wording changes are forbidden —
        t_90082309 (F3) legitimately re-wrapped it to pass the (B) gate, so
        the pin is on the CANONICAL whitespace form: identical words, the
        physical line breaks may move.

        t_5132deb3 moved the probe token from --help (help now wins over the
        refusal) to --foo — an unknown token with no help meaning. The refusal
        text is token-parameterised, so the canonical form is re-templated."""
        import re
        text, code = self._run(monkeypatch, [sub, "--foo"])
        assert code == 1
        canon = re.sub(r"\s+", " ", text).strip()
        want = (
            "Error: unknown argument for 'hscc cluster {sub}': --foo "
            "'hscc cluster {sub}' accepts only: --dry-run (and --theme "
            "<name> for the human view). Nothing was started or stopped. "
            "Refusing to guess what --foo meant — a destructive command runs "
            "only on arguments it understands.").format(sub=sub)
        assert canon == want, f"refusal reworded:\n{text}"

    def test_unknown_subcommand_help_still_errors(self, monkeypatch):
        text, code = self._run(monkeypatch, ["bogus", "--help"])
        assert code == 1
        assert "unknown cluster subcommand" in text

    def test_group_help_unchanged(self, monkeypatch):
        """`hscc cluster --help` (group level) is untouched by this card."""
        text, code = self._run(monkeypatch, [])
        assert code == 0
        assert GROUP_MARKER in text


# ---------------------------------------------------------------------------
# real-CLI sandbox — `python -m hscc_daemon.hscc cluster <verb> [--help|-h]`
# (audit-engine safety model: throwaway HOME + recording PATH canaries)
# ---------------------------------------------------------------------------

class TestVerbHelpRealCLI:
    """End-to-end inertness of the EXACT audit probe invocations (B6 + B12).

    The canaries are the point: on the base tree ``cluster stop --help`` wrote
    `sparkrun stop --help` into trips.log and each read-only verb wrote its
    own sparkrun call. Here the trip log must stay EMPTY.
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

    def _run(self, sandbox, argv):
        return subprocess.run(
            [sys.executable, "-m", "hscc_daemon.hscc", "cluster", *argv],
            env=sandbox["env"], cwd=str(sandbox["tmp"]),
            capture_output=True, text=True, timeout=60)

    @pytest.mark.parametrize("verb", HELP_VERBS)
    @pytest.mark.parametrize("flag", ["--help", "-h"])
    def test_real_cli_help_shells_out_nothing(self, verb, flag, sandbox):
        home = sandbox["home"]
        before = {str(p.relative_to(home)) for p in home.rglob("*")
                  if p.is_file() and "trips.log" not in str(p)}

        proc = self._run(sandbox, [verb, flag])

        assert proc.returncode == 0, (
            f"cluster {verb} {flag} exited {proc.returncode}\n"
            f"{proc.stdout}\n{proc.stderr}")
        assert USAGE_MARKERS[verb] in proc.stdout
        assert "Traceback" not in proc.stderr
        # Non-tty Rich degrades to plain text: no ANSI in the help path.
        assert "\x1b[" not in proc.stdout + proc.stderr
        # B6/B12: the trip log is the whole point — nothing may be shelled.
        trips = sandbox["trips"].read_text().strip()
        assert trips == "", f"--help shelled out: {trips}"
        after = {str(p.relative_to(home)) for p in home.rglob("*")
                 if p.is_file() and "trips.log" not in str(p)}
        assert after == before, (
            f"--help wrote state under HOME: {sorted(after - before)}")

    def test_real_cli_stop_help_never_reports_a_stop_attempt(self, sandbox):
        """B6's visible symptom: on the base tree the help path rendered the
        cluster engine's 'stop failed' panel (it really called sparkrun stop)."""
        proc = self._run(sandbox, ["stop", "--help"])
        assert proc.returncode == 0
        blob = proc.stdout + proc.stderr
        assert "stop failed" not in blob
        assert "ERROR" not in blob
        assert USAGE_MARKERS["stop"] in proc.stdout

    @pytest.mark.parametrize("verb", ("up", "down"))
    @pytest.mark.parametrize("flag", ("--help", "-h"))
    def test_real_cli_destructive_help_prints_help_without_shelling(
            self, verb, flag, sandbox):
        """t_5132deb3 (operator ruling): a help token on the destructive
        verbs prints their usage rc=0 AND shells out nothing (engine never
        loaded, zero canary trips). The refusal half moved to the NON-help
        token below — the F6 guard stays, it just no longer fires on --help."""
        proc = self._run(sandbox, [verb, flag])
        assert proc.returncode == 0, (
            f"{verb} {flag} exited {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
        assert USAGE_MARKERS[verb] in proc.stdout
        assert "Nothing was started or stopped" not in proc.stdout
        assert sandbox["trips"].read_text().strip() == "", (
            f"{verb} {flag} shelled out to sparkrun")

    @pytest.mark.parametrize("verb", ("up", "down"))
    def test_real_cli_destructive_unknown_nonhelp_refuses_without_shelling(
            self, verb, sandbox):
        """The F6 half stays a refusal (rc=1) AND shells out nothing — for a
        token that is NOT help (help now wins; --foo must not)."""
        proc = self._run(sandbox, [verb, "--foo"])
        assert proc.returncode == 1
        assert "Nothing was started or stopped" in proc.stdout
        assert sandbox["trips"].read_text().strip() == "", (
            f"{verb} --foo shelled out to sparkrun")

    def test_real_cli_help_is_fast(self, sandbox):
        """B12's other half: a help request that shells out costs seconds.
        A generous 10 s budget (interpreter startup alone is ~1 s) still fails
        loudly on any regression that resumes calling sparkrun, whose canary
        exits 97 only after a subprocess spawn per verb."""
        import time
        t0 = time.monotonic()
        for verb in HELP_VERBS:
            proc = self._run(sandbox, [verb, "--help"])
            assert proc.returncode == 0
        assert time.monotonic() - t0 < 10 * len(HELP_VERBS), (
            "cluster --help is slow — a help path is executing something")

