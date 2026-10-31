"""``hscc orch <verb>`` — the unified-CLI dispatch into the cluster engine.

The gap (found by t_75f17c83, whose card forbade touching this file): the
engine verb ``cmd_orch`` / ``cmd_orch_inject`` exists and works through the
standalone entry (``python3 hscc-cluster/hscc.py orch inject ...``) and is
registered in the engine's ``COMMANDS``, but ``hscc_daemon/hscc.py`` ``main()``
had no ``orch`` group branch. So the CLI the operator asked for —
``hscc orch inject <profile> "msg"`` — printed "Unknown command: orch" and
exited 1, while the very same verb worked one directory over.

Three layers of proof, the shape the cluster/api group tests use:

  * dispatch — a RECORDING stub engine behind ``_load_cluster_engine`` shows
    exactly what the branch forwards (the argv tail, ``--theme`` stripped,
    ``--json`` kept) and what it returns (rc 0 on a receipt, rc 1 on a
    refusal);
  * help inertness — every help spelling loads NO engine and runs NO verb
    (the t_5132deb3 rule: ``-h`` must never execute), asserted with a
    loader landmine so an engine import fails the test;
  * end-to-end into the REAL engine verb — the acceptance pin: with the real
    ``hscc-cluster/hscc.py`` loaded and only ``repl_inject`` stubbed,
    ``hscc orch inject demo-orch "msg" --dry-run`` reaches
    ``cmd_orch_inject`` -> the stub's ``inject()`` with ``dry_run=True``,
    and NOTHING is sent: the stub's send seams and ``subprocess.run`` are
    rigged to fail the test if anything touches them. No real tmux, ever.

Address doctrine: fictional profiles only (``demo-orch`` / ``fake-orch``);
no LAN/tailnet octets, no real profile data.
"""

import io
import json
import subprocess
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


class _InjectError(Exception):
    """Stand-in for repl_inject.InjectError (kind + message)."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind, self.message = kind, message


class StubEngine:
    """Stand-in for the hscc-cluster engine module; records cmd_orch calls."""

    def __init__(self, result=None, raises=None):
        self.calls = []
        self.result = result if result is not None else {
            "pane": "%7", "target": "s1:0.1", "profile": "demo-orch",
            "busy": False, "mode": "plain", "verb": "", "interrupts": False,
            "chars": 5, "line": "target: demo-orch @ s1:0.1 (idle) -> plain",
        }
        self.raises = raises

    def cmd_orch(self, argv=None):
        self.calls.append(list(argv or []))
        if self.raises:
            raise self.raises
        return self.result


def _with_engine(monkeypatch, eng):
    """Route the branch's engine load to `eng`; also records that it ran."""
    loaded = []

    def _load():
        loaded.append(True)
        return eng

    monkeypatch.setattr(unified, "_load_cluster_engine", _load)
    return loaded


# ── dispatch ───────────────────────────────────────────────────────────────

class TestOrchGroupDispatch:
    def test_inject_reaches_the_engine_router(self, monkeypatch):
        """The card's gap, directly: `hscc orch inject` must reach cmd_orch."""
        eng = StubEngine()
        _with_engine(monkeypatch, eng)
        rc, out = _main(monkeypatch, ["orch", "inject", "demo-orch", "hello",
                                      "--dry-run"])
        assert rc == 0, out
        assert eng.calls == [["inject", "demo-orch", "hello", "--dry-run"]]

    def test_orch_no_longer_an_unknown_command(self, monkeypatch):
        """Base behaviour: bare `hscc orch` printed 'Unknown command: orch'
        + exit 1. It now prints the group listing + exit 0."""
        eng = StubEngine()
        _with_engine(monkeypatch, eng)
        rc, out = _main(monkeypatch, ["orch"])
        assert rc == 0, out
        assert "Unknown command" not in out
        assert "orch inject" in out
        assert eng.calls == []          # help loads and runs nothing

    def test_plain_view_prints_the_receipt_line(self, monkeypatch):
        eng = StubEngine()
        _with_engine(monkeypatch, eng)
        rc, out = _main(monkeypatch, ["orch", "inject", "demo-orch", "hello",
                                      "--dry-run"])
        assert rc == 0
        # format_plan's line, verbatim — the same text the reference tool
        # prints for the same pane (the parent card's parity rule).
        assert "target: demo-orch @ s1:0.1 (idle) -> plain" in out
        assert "{" not in out           # the human view is not a JSON dump

    def test_json_flag_dumps_the_receipt(self, monkeypatch):
        eng = StubEngine()
        _with_engine(monkeypatch, eng)
        rc, out = _main(monkeypatch, ["orch", "inject", "demo-orch", "hello",
                                      "--dry-run", "--json"])
        assert rc == 0, out
        receipt = json.loads(out)       # machine contract: raw receipt JSON
        assert receipt["target"] == "s1:0.1"
        assert receipt["mode"] == "plain"
        assert receipt["chars"] == 5
        # --json is forwarded (the engine accepts it; rendering is ours)
        assert "--json" in eng.calls[0]

    def test_real_send_line_rebuilt_from_receipt(self, monkeypatch):
        """A real-send receipt has no `line` (only a dry run carries it), so
        the plain view rebuilds the same shape from the receipt fields."""
        eng = StubEngine(result={"pane": "%9", "target": "s1:0.4",
                                "profile": "demo-orch", "busy": True,
                                "mode": "queue", "verb": "/queue ",
                                "interrupts": False,
                                "sent_text": "/queue hello"})
        _with_engine(monkeypatch, eng)
        rc, out = _main(monkeypatch, ["orch", "inject", "demo-orch", "hello"])
        assert rc == 0
        assert "target: demo-orch @ s1:0.4 (busy) -> queue" in out
        assert "(12 chars)" in out      # len("/queue hello")

    def test_theme_flag_is_stripped_before_the_engine(self, monkeypatch):
        """`--theme` selects the human palette; the engine verb has no such
        flag, so forwarding it would turn the palette name into an argument."""
        eng = StubEngine()
        _with_engine(monkeypatch, eng)
        rc, out = _main(monkeypatch, ["orch", "inject", "demo-orch", "hello",
                                      "--theme", "light", "--dry-run"])
        assert rc == 0, out
        assert "--theme" not in eng.calls[0]
        assert "light" not in eng.calls[0]
        assert "--dry-run" in eng.calls[0]

    def test_non_inject_result_prints_as_is(self, monkeypatch):
        """A future orch verb (e.g. `orch waker status`, t_2b34b5c7) returns
        a dict that is NOT an inject receipt. The renderer must print it as
        it is — fabricating "target: None @ None (idle) -> None" would lie."""
        eng = StubEngine(result={"enabled": True, "last_tick": "2026-10-31T10:00:00+02:00"})
        _with_engine(monkeypatch, eng)
        rc, out = _main(monkeypatch, ["orch", "waker", "status"])
        assert rc == 0
        assert "target:" not in out
        assert json.loads(out) == {"enabled": True,
                                   "last_tick": "2026-10-31T10:00:00+02:00"}

    def test_dangling_theme_refuses_instead_of_sending(self, monkeypatch):
        """THE accident this guard exists for: `--theme` eats the token after
        it as the palette name, so `--theme --dry-run` strips away the dry-run
        flag and dispatches a REAL SEND while the operator believes they asked
        for a plan. Must refuse (rc 1) and call the engine ZERO times."""
        eng = StubEngine()
        loaded = _with_engine(monkeypatch, eng)
        rc, out = _main(monkeypatch, ["orch", "inject", "demo-orch", "hi",
                                      "--theme", "--dry-run"])
        assert rc == 1, out
        assert "refusing" in out
        assert eng.calls == []
        assert loaded == [], "the refusal must happen before the engine load"

    def test_dangling_theme_form_equals_refuses_too(self, monkeypatch):
        eng = StubEngine()
        _with_engine(monkeypatch, eng)
        rc, out = _main(monkeypatch, ["orch", "inject", "demo-orch", "hi",
                                      "--theme=--dry-run"])
        assert rc == 1, out
        assert eng.calls == []

    def test_valid_theme_still_works(self, monkeypatch):
        """The guard must not swallow the legitimate path (intercept-off
        teeth): `--theme light` is a palette, reaches the engine stripped."""
        eng = StubEngine()
        _with_engine(monkeypatch, eng)
        rc, out = _main(monkeypatch, ["orch", "inject", "demo-orch", "hi",
                                      "--theme", "light", "--dry-run"])
        assert rc == 0, out
        assert eng.calls == [["inject", "demo-orch", "hi", "--dry-run"]]

    @pytest.mark.parametrize("exc,needle", [
        (_InjectError("no_panes", "no pane for profile demo-orch"),
         "no pane for profile"),
        (_InjectError("ambiguous", "2 panes match demo-orch"), "2 panes"),
        (_InjectError("empty", "empty message"), "empty message"),
        (ValueError("unknown orch subcommand: frobnicate (available: inject)"),
         "unknown orch subcommand"),
    ])
    def test_refusals_exit_1(self, monkeypatch, exc, needle):
        """InjectError / bad-argv refusals map to exit 1 with one reported
        line — never a traceback (a traceback is a crash, not a refusal)."""
        eng = StubEngine(raises=exc)
        _with_engine(monkeypatch, eng)
        rc, out = _main(monkeypatch, ["orch", "inject", "demo-orch", "hi"])
        assert rc == 1
        assert needle in out
        assert "Traceback" not in out

    def test_refusal_json_path_keeps_the_machine_contract(self, monkeypatch):
        eng = StubEngine(raises=_InjectError("no_panes", "no pane here"))
        _with_engine(monkeypatch, eng)
        rc, out = _main(monkeypatch, ["orch", "inject", "demo-orch", "hi",
                                      "--json"])
        assert rc == 1
        assert json.loads(out) == {"error": "no pane here"}

    def test_missing_engine_reports_and_exits_1(self, monkeypatch):
        """_load_cluster_engine prints its own error and returns None — the
        branch must not crash on None (same shape as _handle_cluster)."""
        monkeypatch.setattr(unified, "_load_cluster_engine", lambda: None)
        rc, out = _main(monkeypatch, ["orch", "inject", "demo-orch", "hi"])
        assert rc == 1


# ── help inertness ─────────────────────────────────────────────────────────

HELP_SHAPES = [
    ["orch"],
    ["orch", "--help"],
    ["orch", "-h"],
    ["orch", "inject", "--help"],
    ["orch", "inject", "-h"],
    ["orch", "inject", "demo-orch", "msg", "--dry-run", "--help"],
    ["orch", "inject", "demo-orch", "msg", "--help", "--dry-run"],
    # `--theme` eats the token AFTER it as the palette name, so a help flag in
    # that slot is swallowed by stripping — the F6 hole. The intercept must
    # scan the RAW slice (t_5132deb3), exactly like _handle_cluster's up/down.
    ["orch", "inject", "--theme", "--help"],
    ["orch", "inject", "demo-orch", "msg", "--theme", "-h"],
]


class TestOrchHelpIsInert:
    """A help request must print the group listing and load NOTHING.

    The engine load is the dangerous half: ``cmd_orch_inject`` resolves a real
    tmux pane. An intercept that fires after ``_load_cluster_engine`` (or not
    at all) would import the engine — and a regressed one could reach the verb
    — which is exactly the class t_5132deb3 closed CLI-wide.
    """

    @pytest.mark.parametrize("argv", HELP_SHAPES)
    def test_help_prints_group_listing_and_loads_no_engine(self, argv,
                                                           monkeypatch):
        def boom():
            raise AssertionError("_load_cluster_engine ran for a help request")

        monkeypatch.setattr(unified, "_load_cluster_engine", boom)
        rc, out = _main(monkeypatch, argv)
        assert rc == 0, f"{argv} exited {rc}:\n{out}"
        assert "orch inject" in out
        assert "Traceback" not in out

    @pytest.mark.parametrize("flag", ("--help", "-h"))
    def test_both_flags_print_the_same_bytes(self, flag, monkeypatch):
        monkeypatch.setattr(unified, "_load_cluster_engine",
                            lambda: pytest.fail("engine loaded for help"))
        rc_a, out_a = _main(monkeypatch, ["orch", "inject", "--help"])
        rc_b, out_b = _main(monkeypatch, ["orch", "inject", flag])
        assert (rc_a, out_a) == (rc_b, out_b)

    def test_help_block_fits_the_gate_budget(self, monkeypatch):
        """Plain print: authored width IS rendered width. Every line inside
        the help-surface gate's 78-col budget (the F3/F5 convention)."""
        monkeypatch.setattr(unified, "_load_cluster_engine", lambda: None)
        _, out = _main(monkeypatch, ["orch", "--help"])
        for ln in out.splitlines():
            assert len(ln) <= 78, f"orch --help line over 78 cols: {ln!r}"

    def test_help_entry_lines_fit_the_gate_budget(self):
        """`hscc help orch` prints COMMAND_HELP with a 2-space prefix on the
        FIRST line only, so every authored line stays <=76."""
        entry = unified.COMMAND_HELP.get("orch")
        assert entry, "COMMAND_HELP is missing 'orch'"
        lines = entry.splitlines()
        assert len(lines[0]) + 2 <= 78
        for ln in lines[1:]:
            assert len(ln) <= 78, f"COMMAND_HELP[orch] over budget: {ln!r}"

    def test_help_orch_rc0_matches_the_entry(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["hscc", "help", "orch"])
        assert unified._handle_help() == 0
        out = capsys.readouterr().out
        assert out == f"hscc orch\n  {unified.COMMAND_HELP['orch']}\n"

    def test_main_banner_advertises_orch(self, monkeypatch):
        """Surface honesty (F5): a dispatched command must appear on the
        primary help surface too, not only in `hscc help orch`."""
        rc, out = _main(monkeypatch, [])
        assert rc == 0
        assert "orch inject" in out

    def test_unknown_command_blob_lists_orch(self, monkeypatch):
        """The card asked for the unknown-command list line: a human who
        mistypes a verb sees that `orch` is a real group."""
        rc, out = _main(monkeypatch, ["bogus-verb"])
        assert rc == 1
        blob = [ln for ln in out.splitlines() if ln.startswith("Available:")]
        assert blob
        assert "orch" in blob[0].split(", ")


# ── end-to-end into the REAL engine verb (the acceptance pin) ──────────────

class FakeReplModule:
    """Stub of the repl_inject MODULE — no tmux, ever.

    Mirrors the parity contract (receipt fields + dry-run shape) so the
    assertions read like the real thing, and carries tripwires on every send
    seam: if any code path tries to paste, send-keys or shell out, THIS file
    fails instead of touching a live pane.
    """

    def __init__(self, busy=False):
        self.calls = []
        self.busy = busy

    def inject(self, profile, text, mode="auto", pane_override=None,
               dry_run=False, sleep_fn=None):
        if not text or not text.strip():
            raise _InjectError("empty", "empty message")
        text = text.rstrip("\n")
        self.calls.append({"profile": profile, "text": text, "mode": mode,
                           "pane_override": pane_override,
                           "dry_run": dry_run})
        eff = "queue" if (mode == "auto" and self.busy) else \
            ("plain" if mode in ("auto", "interrupt") else mode)
        verb = {"plain": "", "queue": "/queue ", "steer": "/steer "}[eff]
        base = {"pane": "%7", "target": "s1:0.1", "profile": profile,
                "busy": self.busy, "mode": eff, "verb": verb,
                "interrupts": eff == "plain" and self.busy}
        if dry_run:
            return {**base, "dry_run": True, "text": text, "chars": len(text)}
        return {**base, "sent_text": (verb + text) if verb else text}

    @staticmethod
    def format_plan(p):
        line = (f"target: {p['profile']} @ {p['target']} "
                f"({'busy' if p['busy'] else 'idle'}) -> {p['mode']}")
        if p.get("interrupts"):
            line += " (interrupts the running turn)"
        return line

    # ── tripwires: any send attempt is a test failure ─────────────────────
    def paste(self, *a, **k):
        raise AssertionError(f"a dry run touched paste(): {a!r}")

    def deliver(self, *a, **k):
        raise AssertionError(f"a dry run touched deliver(): {a!r}")

    def run_tmux(self, *a, **k):
        raise AssertionError(f"a dry run shelled out to tmux: {a!r}")


@pytest.fixture(scope="module")
def real_engine():
    """The REAL hscc-cluster engine, loaded the way the CLI loads it.

    sys.path is restored at teardown (the loader inserts the plugin dir) so a
    later test in this process cannot accidentally resolve a bare cluster
    module name.
    """
    saved_path = list(sys.path)
    eng = unified._load_cluster_engine()
    assert eng is not None, (
        "hscc-cluster/hscc.py must be loadable from this worktree — the "
        "unified `hscc orch` verb has no other source")
    yield eng
    sys.path[:] = saved_path


class TestRealEngineVerbReach:
    """The card's acceptance: `hscc orch inject p msg --dry-run` reaches
    cmd_orch_inject with the stubbed repl and sends nothing."""

    def _drive(self, monkeypatch, real_engine, argv, busy=False):
        repl = FakeReplModule(busy=busy)
        monkeypatch.setattr(real_engine, "_load_repl_inject", lambda: repl)
        monkeypatch.setattr(unified, "_load_cluster_engine",
                            lambda: real_engine)

        # Nothing on this path may shell out. The real engine's own run_cmd,
        # the stub repl's tmux seam and subprocess.run are all rigged.
        def no_exec(*a, **k):
            raise AssertionError(f"an inject dry-run executed {a[0]!r}")

        monkeypatch.setattr(subprocess, "run", no_exec)
        monkeypatch.setattr(subprocess, "Popen", no_exec)
        monkeypatch.setattr(real_engine, "run_cmd", no_exec)

        rc, out = _main(monkeypatch, argv)
        return rc, out, repl

    def test_dry_run_reaches_the_verb_and_sends_nothing(self, monkeypatch,
                                                        real_engine):
        rc, out, repl = self._drive(
            monkeypatch, real_engine,
            ["orch", "inject", "demo-orch", "hello there", "--dry-run"])
        assert rc == 0, out
        # the verb ran once, with the flags it was given
        assert len(repl.calls) == 1
        call = repl.calls[0]
        assert call["profile"] == "demo-orch"
        assert call["text"] == "hello there"
        assert call["mode"] == "auto"          # default passes straight through
        assert call["dry_run"] is True
        # and the operator sees the plan, not a JSON blob
        assert "target: demo-orch @ s1:0.1 (idle) -> plain" in out

    def test_dry_run_busy_auto_reports_queue(self, monkeypatch, real_engine):
        """Mode logic stays in repl_inject: auto + busy -> queue, and the CLI
        prints exactly that decision."""
        rc, out, repl = self._drive(
            monkeypatch, real_engine,
            ["orch", "inject", "demo-orch", "hi", "--dry-run"], busy=True)
        assert rc == 0, out
        assert repl.calls[0]["mode"] == "auto"
        assert "(busy) -> queue" in out

    def test_mode_passes_straight_through(self, monkeypatch, real_engine):
        rc, out, repl = self._drive(
            monkeypatch, real_engine,
            ["orch", "inject", "demo-orch", "hi", "--mode", "steer",
             "--dry-run"])
        assert rc == 0, out
        assert repl.calls[0]["mode"] == "steer"

    def test_pane_override_passes_through(self, monkeypatch, real_engine):
        rc, out, repl = self._drive(
            monkeypatch, real_engine,
            ["orch", "inject", "demo-orch", "hi", "--pane", "%3",
             "--dry-run"])
        assert rc == 0, out
        assert repl.calls[0]["pane_override"] == "%3"

    def test_json_receipt_is_complete(self, monkeypatch, real_engine):
        rc, out, repl = self._drive(
            monkeypatch, real_engine,
            ["orch", "inject", "demo-orch", "hello", "--dry-run", "--json"])
        assert rc == 0, out
        receipt = json.loads(out)
        assert {"pane", "target", "profile", "busy", "mode", "verb",
                "interrupts", "chars", "dry_run", "line"} <= set(receipt)
        assert receipt["chars"] == 5
        assert repl.calls[0]["dry_run"] is True

    def test_real_send_path_reports_the_delivery(self, monkeypatch,
                                                real_engine):
        """The non-dry-run shape, still hermetic: the stub repl "delivers" and
        the receipt's sent_text is what the plain view reports."""
        rc, out, repl = self._drive(
            monkeypatch, real_engine,
            ["orch", "inject", "demo-orch", "hi"])
        assert rc == 0, out
        assert repl.calls[0]["dry_run"] is False
        assert "target: demo-orch @ s1:0.1 (idle) -> plain" in out
        assert "(2 chars)" in out

    def test_empty_message_refuses_with_exit_1(self, monkeypatch, real_engine):
        """repl_inject validates before deciding, and the CLI turns that
        refusal into exit 1 rather than a traceback."""
        rc, out, repl = self._drive(
            monkeypatch, real_engine, ["orch", "inject", "demo-orch", ""])
        assert rc == 1
        assert "empty message" in out
        assert "Traceback" not in out
        assert repl.calls == []

    def test_unknown_verb_refuses_with_exit_1(self, monkeypatch, real_engine):
        # A verb NO sibling card plans to add (`waker` is orch-waker W3's
        # verb and will become routable — pinning it here would hand that
        # child a guaranteed failure). A nonsense verb stays nonsense.
        rc, out, repl = self._drive(monkeypatch, real_engine,
                                    ["orch", "frobnicate", "status"])
        assert rc == 1
        assert "unknown orch subcommand" in out
        assert repl.calls == []
