"""MON-ENV-PIN's third residual (t_8616f2a5): the wedge gate's CLI probes run
``hermes`` in a SUBPROCESS, and ``board_pin`` cannot reach across the fork.

The bug: ``_default_gate_cli_runner`` did ``subprocess.run(argv)`` with the
INHERITED environment, and ``_gate_cli_json`` parsed whatever came back as fact.
A worker/delegated shell carries ``HERMES_KANBAN_DB`` + a fence marker, and
hermes resolves that pin ahead of ``--board`` inside the child
(``hermes_cli/kanban_db.py:540-555`` via ``agent/delegation_context.py:76-93``),
so the child printed the PINNED board's ready list and hold events while the
gate labelled them with the board it asked about. This gate decides whether to
RESTART THE GATEWAY.

Why a separate file: ``board_pin``'s own guards are in-process (they scrub
``os.environ`` around *our* connection), and the whole point here is that no
in-process scrub survives a fork — so these cases are about the environment the
CHILD is handed. They are the wedge half of the ``test_board_pin.py`` family
(shared fixtures imported from there) in a file whose diff does not collide with
the sibling cards working the same seam.

Every assertion is on an observable — the environment the CLI was handed, the
argv it was handed, what the gate's verdict says, whether it asked at all — never
on an internal flag. ``scripts/run_tests.sh`` does not clear the ambient pin for
this leg, so each case arms or clears the worker-shell state explicitly and
neither ambient shape can decide the outcome.
"""

import json
import os
import pathlib

import pytest

from hscc_daemon import board_pin
from hscc_daemon import dispatcher_wedge as dw
from hscc_daemon.tests.test_board_pin import (
    FENCE_VARS, _PinAwareLib, _warn_text,
)

NOW = 1_800_000_000.0
FAKE_BIN = "/fake/bin/hermes"
#: Stands in for "the rest of the environment": a scrub must remove ONE key, not
#: hand the child an empty world (PATH, HOME, HERMES_HOME all matter to hermes).
MARKER = "HSCC_GATE_CHILD_ENV_MARKER"


class _GateLib(_PinAwareLib):
    """The pin-aware lib plus the data the gate's two CLI probes answer apart.

    Each board owns exactly one ready card, ``t_<slug>_1``, and a card only has
    a benign hold event on the board that owns it — which is how a probe that
    resolved the wrong board becomes visible in the gate's own sentence instead
    of only in a flag.
    """

    def path_in(self, env, slug):
        """hermes' MEASURED precedence rule, evaluated against a GIVEN env."""
        pin = (env.get(board_pin.PIN_ENV) or "").strip()
        fenced = any((env.get(v) or "").strip() for v in FENCE_VARS)
        if pin and (slug is None or fenced):
            return pathlib.Path(pin)
        return self._canonical(slug)

    def resolved_slug(self, env, slug):
        """Which board a ``hermes kanban`` child running in ``env`` answers for."""
        path = self.path_in(env, slug)
        for candidate in self.slugs:
            if str(self._canonical(candidate)) == str(path):
                return candidate
        return f"unmapped@{path.name}"

    def ready_rows(self, slug):
        return [{"id": f"t_{slug}_1", "status": "ready", "assignee": "worker-a"}]

    def show_payload(self, slug, task_id):
        if task_id != f"t_{slug}_1":
            # The card is not on this board: no task, no events — which the gate
            # classifies as "no recent hold event" (unexplained).
            return {"task": None, "events": [], "runs": [], "comments": []}
        return {"task": {"id": task_id, "status": "ready", "assignee": "worker-a"},
                "events": [{"kind": "respawn_guarded",
                            "payload": {"reason": "active_pr"},
                            "created_at": NOW - 60}],
                "runs": [], "comments": []}


class _Proc:
    def __init__(self, payload, rc=0):
        self.returncode = rc
        self.stdout = json.dumps(payload)
        self.stderr = ""


class _CliChild:
    """The sanctioned CLI as hermes actually behaves: ``--board`` is honoured
    only by the environment the child was ACTUALLY handed.

    ``record`` is the shared half; the three subclasses below differ ONLY in the
    runner signature they present to the gate, which is the whole subject of this
    card. Each records ``(argv, env)`` — the card's acceptance fake. ``env is
    None`` is recorded as None, because that is today's call: no environment was
    handed over, so the child inherits (pin and all).
    """

    def __init__(self, lib):
        self.lib = lib
        self.calls = []                       # [(argv, env)]

    def record(self, argv, env):
        run_env = dict(os.environ) if env is None else env
        self.calls.append((list(argv), env))
        asked = argv[argv.index("--board") + 1] if "--board" in argv else None
        seen = self.lib.resolved_slug(run_env, asked)
        if "list" in argv:
            return _Proc(self.lib.ready_rows(seen))
        return _Proc(self.lib.show_payload(seen, argv[argv.index("show") + 1]))


class _EnvCli(_CliChild):
    """The t_8616f2a5 runner contract: ``runner(argv, env=None)``."""

    def __call__(self, argv, env=None):
        return self.record(argv, env)


class _LegacyCli(_CliChild):
    """A runner written against the pre-t_8616f2a5 contract: ``runner(argv)``.

    It cannot be handed an environment, which is exactly the state the gate must
    refuse rather than trust — and it records argv only through ``record``, so a
    call reaching it is observable.
    """

    def __call__(self, argv):
        return self.record(argv, None)


class _KwCli(_CliChild):
    """A ``**kwargs`` runner — signature-readers must not mistake it for blind."""

    def __call__(self, argv, **kw):
        return self.record(argv, kw.get("env"))


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def gate(monkeypatch, warnings):
    """Deterministic gate environment: fake CLI locator, no ambient pin.

    Mirrors ``test_dispatcher_wedge_benign_holds.cli_available`` — the fake
    assignee is not a real Hermes profile, and an unpatched
    ``_read_max_per_profile`` would read the OPERATOR's config and explain the
    card by the per-profile cap instead of the hold under test.
    """
    monkeypatch.setattr(dw, "_hermes_cli_argv", lambda: [FAKE_BIN, "kanban"])
    monkeypatch.setattr(dw, "_assignee_is_nonspawnable", lambda assignee: False)
    monkeypatch.setattr(dw, "_read_max_per_profile", lambda: None)
    monkeypatch.delenv(board_pin.PIN_ENV, raising=False)
    for var in FENCE_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(MARKER, "must-reach-the-child")
    return None


@pytest.fixture
def lib(tmp_path):
    return _GateLib(tmp_path / "kanban-home", ["alpha", "beta", "default"])


@pytest.fixture
def worker_shell(monkeypatch, lib, gate):
    """Worker-shell state: pin at board ``alpha`` + a fence marker.

    Requests ``gate`` so it runs AFTER that fixture cleared the ambient state —
    arming first would let ``gate`` delete the pin again and silently turn every
    conflict case below into a no-pin case.
    """
    pin = str(lib._canonical("alpha"))
    monkeypatch.setenv(board_pin.PIN_ENV, pin)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    return pin


@pytest.fixture
def warnings(monkeypatch):
    """Capture every line the guard and the gate emit, with fresh dedupe state.

    Both dedupe sets are process-global (one warning per pin+board per process is
    the documented posture), so a test that asserts the warning fired must not
    inherit another test's silence — and a test that asserts it did NOT fire must
    not be passing because of it.
    """
    import hscc_daemon.daemon_ops as daemon_ops
    seen = []
    monkeypatch.setattr(daemon_ops, "log",
                        lambda msg, level="INFO", **kw: seen.append((level, msg)))
    monkeypatch.setattr(board_pin, "_WARNED", set())
    monkeypatch.setattr(dw, "_GATE_WARNED", set())
    return seen


def _stream(board):
    return {"ok": False, "declared": True, "spawnable": [board],
            "total_running": 0}


def _explain(lib, cli, board="beta", max_per_profile=None):
    """The whole gate, as recovery runs it (``max_in_progress`` named so the
    fleet-cap short circuit cannot answer for us)."""
    return dw.board_state_explains_stall(
        _stream(board), kanban_db=lib, max_in_progress=6,
        max_per_profile=max_per_profile, cli_fn=cli, now=NOW)


LIST_BOARD_ARGV = ["--board", "beta", "list", "--status", "ready", "--json"]
#: What the child is actually exec'd with: the sanctioned prefix, bound by
#: ``board_state_explains_stall`` (a test driving ``_board_ready_facts`` directly
#: sees the board-level half only — that is that function's documented contract).
LIST_ARGV = [FAKE_BIN, "kanban"] + LIST_BOARD_ARGV


# --------------------------------------------------------------------------- #
# non-vacuity: the same armed environment, the pre-guard call, the wrong board
# --------------------------------------------------------------------------- #

def test_the_pre_guard_call_answers_for_the_pinned_board(lib, worker_shell):
    """Pair for every case below: today's ``cli_fn(argv)`` call, armed env.

    The child inherits the pin, so a probe that ASKED for ``beta`` comes back
    with ``alpha``'s card — and the hold evidence is then read for that card on
    that board, so the gate would confidently explain a board it never looked at.
    """
    cli = _EnvCli(lib)
    payload = dw._gate_cli_json(cli, LIST_ARGV)          # the pre-t_8616f2a5 call
    assert [r["id"] for r in payload] == ["t_alpha_1"], (
        "the armed env stopped pinning the child — every case below is vacuous")
    assert cli.calls[-1][1] is None, "today's call hands over no environment"
    # ... and the same for the hold-evidence probe: beta's card is not on alpha.
    status, why = dw._card_hold_from_cli(cli, "beta", "t_beta_1", NOW)
    assert (status, why) == ("unexplained", "no recent hold event"), (
        "the wrong-board show must not find beta's hold")


# --------------------------------------------------------------------------- #
# the card's acceptance case: option (a) — the child's env has no pin
# --------------------------------------------------------------------------- #

def test_the_child_is_handed_an_environment_with_no_pin(lib, worker_shell,
                                                        gate, warnings):
    """Acceptance (a): armed pin + fence, injected ``cli_fn`` records the env.

    Driven through ``_board_ready_facts`` (the gate's own board probe) AND the
    full gate, so both surfaces the card names are covered by one observable:
    the environment the child was handed.
    """
    for probe in ("facts", "gate"):
        cli = _EnvCli(lib)
        if probe == "facts":
            explained, why = dw._board_ready_facts(
                cli, "beta", lib, 6, None, NOW)
        else:
            explained, why = _explain(lib, cli, "beta")
        assert explained is True, why
        assert cli.calls, "the gate must still have asked"
        for _argv, env in cli.calls:
            assert env is not None, "a pinned shell must hand the child an env"
            assert board_pin.PIN_ENV not in env, (
                f"the child's env still carries the pin: {env}")
            assert env.get(MARKER) == "must-reach-the-child", (
                "the scrub must remove ONE key, not the child's world")
        # argv is untouched by this change — including for the board it
        # names (the prefix half is bound by the caller, see LIST_ARGV).
        assert cli.calls[0][0] == (
            LIST_BOARD_ARGV if probe == "facts" else LIST_ARGV), (
            cli.calls[0][0])
        # The evidence is the ASKED board's card, not the pinned board's.
        assert "t_beta_1" in why and "t_alpha_1" not in why, why
        # Loud, in board_pin's wording, naming both values.
        text = _warn_text(warnings)
        assert worker_shell in text and "beta" in text, (
            f"the override must name the pin and the board asked for: {text}")
        # The scrub is a hand-off, not a mutation of our own process.
        assert os.environ[board_pin.PIN_ENV] == worker_shell


def test_the_hold_evidence_probe_gets_the_same_environment(lib, worker_shell,
                                                           gate, warnings):
    """``show`` is scrubbed too — the hold window is the evidence attributed
    to a named card, so it must come from the board the ``list`` named."""
    cli = _EnvCli(lib)
    explained, why = dw._board_ready_facts(cli, "beta", lib, 6, None, NOW)
    assert explained is True, why
    shows = [c for c in cli.calls if "show" in c[0]]
    assert shows, "the hold evidence must have been read via the CLI"
    for _argv, env in shows:
        assert env is not None and board_pin.PIN_ENV not in env, shows
    assert shows[0][0][-3:] == ["show", "t_beta_1", "--json"], shows[0][0]
    assert "respawn_guarded:active_pr" in why, why


# --------------------------------------------------------------------------- #
# option (b) is what happens when (a) is impossible: refuse, do not explain
# --------------------------------------------------------------------------- #

def test_a_runner_that_cannot_take_an_env_never_sees_the_probe(lib,
                                                                worker_shell,
                                                                gate, warnings):
    """The gate's "board state unverifiable" verdict, and ZERO wrong-board asks.

    A legacy ``runner(argv)`` cannot be handed a scrubbed environment, so its
    answer would describe the pinned board while being labelled ``beta``. The
    gate fails closed — the restart proceeds — and the cause names the pin.
    """
    cli = _LegacyCli(lib)
    explained, why = _explain(lib, cli, "beta")
    assert explained is False
    assert "unverifiable" in why, why
    assert worker_shell in why and "beta" in why, why
    assert cli.calls == [], (
        f"the gate must not ask the wrong board at all: {cli.calls}")


def test_that_refusal_is_the_pin_and_not_the_runner(lib, gate, warnings):
    """Pair: the SAME legacy runner explains the board when nothing is pinned,
    so the refusal above is caused by the conflict and not by the fake."""
    cli = _LegacyCli(lib)
    explained, why = _explain(lib, cli, "beta")
    assert explained is True, why
    assert cli.calls and "t_beta_1" in why
    assert not warnings, _warn_text(warnings)


def test_a_var_keyword_runner_is_handed_the_scrub(lib, worker_shell, gate,
                                                  warnings):
    """A ``**kwargs`` runner CAN take the environment, so it must get it — the
    capability test must not mistake an open signature for a closed one."""
    cli = _KwCli(lib)
    explained, why = _explain(lib, cli, "beta")
    assert explained is True, why
    assert all(env is not None and board_pin.PIN_ENV not in env
               for _a, env in cli.calls), cli.calls


# --------------------------------------------------------------------------- #
# companions: the paths that must not change
# --------------------------------------------------------------------------- #

def test_without_a_pin_argv_and_env_are_exactly_todays(lib, gate, warnings):
    """No pin, no fence → ``cli_fn(argv)`` with no environment keyword at all,
    and the same argv bytes — the child inherits, as it always has."""
    cli = _EnvCli(lib)
    explained, why = _explain(lib, cli, "beta")
    assert explained is True, why
    assert [c[0] for c in cli.calls] == [
        LIST_ARGV,
        [FAKE_BIN, "kanban", "--board", "beta", "show", "t_beta_1", "--json"],
    ], cli.calls
    assert [c[1] for c in cli.calls] == [None, None], (
        "an un-pinned probe must not start passing env")
    assert not warnings, _warn_text(warnings)


def test_a_worker_reading_its_own_board_is_never_scrubbed_and_never_speaks(
        lib, gate, warnings, monkeypatch):
    """The pin already IS the board asked for (the ordinary worker case): the
    fence is doing its job, so nothing is overridden, nothing is scrubbed and
    nothing is said — same posture as ``board_pin``'s own reads."""
    pin = str(lib._canonical("beta"))
    monkeypatch.setenv(board_pin.PIN_ENV, pin)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    cli = _EnvCli(lib)
    explained, why = _explain(lib, cli, "beta")
    assert explained is True, why
    assert [c[1] for c in cli.calls] == [None] * len(cli.calls), cli.calls
    assert not warnings, _warn_text(warnings)


def test_a_probe_with_no_board_intent_is_left_to_the_pin(lib, worker_shell,
                                                         gate, warnings):
    """Legacy single-board shape: no ``board=`` was expressed, so the pin is the
    answer and the pin stays in the child's environment — untouched."""
    cli = _EnvCli(lib)
    explained, why = dw._board_ready_facts(cli, None, lib, 6, None, NOW)
    assert explained is True, why
    assert "--board" not in cli.calls[0][0], cli.calls[0][0]
    assert [c[1] for c in cli.calls] == [None] * len(cli.calls), cli.calls
    assert "t_alpha_1" in why, "the pin is the intended answer here"
    assert not warnings, _warn_text(warnings)


# --------------------------------------------------------------------------- #
# the real runner: the scrub has to survive subprocess.run
# --------------------------------------------------------------------------- #

def test_the_default_runner_forwards_the_env_and_inherits_without_one(
        lib, worker_shell, gate, warnings, monkeypatch):
    """End-to-end through ``subprocess.run``'s own kwargs.

    The injected-fake cases above prove the gate DECIDES the child env; this one
    proves the production runner PASSES it, which is the half that actually
    reaches hermes. No child is ever spawned — ``subprocess.run`` is stubbed.
    """
    import subprocess
    spawns = []

    def fake_run(argv, **kw):
        spawns.append((list(argv), kw))
        if "list" in argv:
            return _Proc(lib.ready_rows("beta"))
        return _Proc(lib.show_payload("beta", argv[argv.index("show") + 1]))

    monkeypatch.setattr(subprocess, "run", fake_run)

    # 1. no pin handed over -> the call is byte-identical to the pre-guard one
    #    (env=None means "inherit", which is what the child did before).
    assert dw._default_gate_cli_runner(LIST_ARGV) is not None
    assert spawns[-1][1].get("env", "MISSING") is None, spawns[-1][1]
    assert spawns[-1][0] == LIST_ARGV

    # 2. the whole gate, armed, with NO injected cli_fn — the real runner.
    spawns.clear()
    explained, why = dw.board_state_explains_stall(
        _stream("beta"), kanban_db=lib, max_in_progress=6,
        max_per_profile=None, cli_fn=None, now=NOW)
    assert explained is True, why
    assert len(spawns) == 2, spawns
    for _argv, kw in spawns:
        env = kw.get("env")
        assert isinstance(env, dict), f"the child must be handed an env: {kw}"
        assert board_pin.PIN_ENV not in env, env
        assert env.get(MARKER) == "must-reach-the-child", env
        assert env == {k: v for k, v in os.environ.items()
                       if k != board_pin.PIN_ENV}
    assert "t_beta_1" in why, why
    text = _warn_text(warnings)
    assert worker_shell in text and "beta" in text, text
