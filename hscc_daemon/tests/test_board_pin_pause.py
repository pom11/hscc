"""MON-ENV-PIN's pause half (t_70c97264): ``kanban_pause``'s CLI calls cross the
same fork the wedge gate's do — and one of them is a WRITE.

The bug, at ``fe530887``: ``_run_hermes`` ran ``subprocess.run([...], timeout=60)``
with the INHERITED environment, ``list_cards`` parsed whatever came back as the
board's inventory, and ``_hold`` (``reclaim`` + ``block --kind dependency``) plus
``resume``'s ``unblock`` wrote under it. A worker/delegated shell carries
``HERMES_KANBAN_DB`` and hermes resolves that pin ahead of ``--board`` inside the
child (``hermes_cli/kanban_db.py:540-555`` via ``agent/delegation_context.py:76-93``),
so the pause invented an inventory of the PINNED board and then blocked — or
released — same-id cards THERE while reporting the board it had been asked to
pause. Task ids are per-board, so the mutation lands.

The shape that fixed it splits by access mode, like ``board_pin``'s own ``mode``:
the READ gets a scrubbed child environment (option a), the WRITE is refused before
anything is spawned (option b). Scrubbing the write is the one thing ``board_pin``
2c rules out, so it never happens here.

Every assertion is on an observable — the environment the child was handed, the
argv it was handed, what the report says, whether anything was spawned AT ALL —
never on an internal flag. ``scripts/run_tests.sh`` clears the ambient pin for
this leg and ``conftest.py``'s autouse fixture deletes it per test, so each case
here arms or clears the worker-shell state explicitly: neither ambient shape can
decide an outcome, and a case cannot pass quietly because the harness scrubbed the
pin. Fixtures are imported from ``test_board_pin.py`` (shared) and the lib here is
the same pin-aware resolver, extended with per-board cards.
"""

import json
import os
import pathlib

import pytest

from hscc_daemon import board_pin
from hscc_daemon import kanban_pause as kp
from hscc_daemon.tests.test_board_pin import (
    FENCE_VARS, _PinAwareLib, _warn_text,
)

#: Stands in for "the rest of the environment": a scrub must remove ONE key, not
#: hand the child an empty world (PATH, HOME, HERMES_HOME all matter to hermes).
MARKER = "HSCC_PAUSE_CHILD_ENV_MARKER"
LIST_ARGV = ["--board", "beta", "list", "--json"]


class _PauseLib(_PinAwareLib):
    """The pin-aware resolver, plus the cards each board owns.

    Data follows the FILE — every board owns ``t_<slug>_1`` (ready) and
    ``r_<slug>_1`` (running) at that board's canonical path — so a probe or a
    write that resolved the pinned file really does answer for, and mutate, the
    pinned board. That is what makes the wrong-board harm visible in the
    observables instead of only in a flag.
    """

    def __init__(self, root, slugs):
        super().__init__(root, slugs)
        self.cards = {s: {f"t_{s}_1": "ready", f"r_{s}_1": "running"} for s in slugs}

    def card_ids(self, slug):
        return [f"t_{slug}_1", f"r_{slug}_1"]

    def pristine(self, slug):
        """What this board's cards looked like before any call — the untouched
        state a refusal must leave behind, compared as a WHOLE dict so a stray
        same-id card is caught too."""
        return {f"t_{slug}_1": "ready", f"r_{slug}_1": "running"}

    def path_in(self, env, slug):
        """hermes' MEASURED precedence rule, evaluated against a GIVEN env."""
        pin = (env.get(board_pin.PIN_ENV) or "").strip()
        fenced = any((env.get(v) or "").strip() for v in FENCE_VARS)
        if pin and (slug is None or fenced):
            return pathlib.Path(pin)
        return self._canonical(slug)

    def resolved_slug(self, env, slug):
        """Which board a ``hermes kanban`` child running in ``env`` answers for."""
        path = str(self.path_in(env, slug))
        for candidate in self.slugs:
            if str(self._canonical(candidate)) == path:
                return candidate
        return f"unmapped@{pathlib.Path(path).name}"


class _CliChild:
    """The sanctioned CLI as hermes behaves: ``--board`` is honoured only by the
    environment the child was ACTUALLY handed.

    Two subclasses differ ONLY in the runner signature they present, which is the
    whole subject of the read half. Both record ``(argv, env)`` — the card's
    acceptance fake — and ``env is None`` is recorded as None, because that is
    today's call: no environment was handed over, so the child inherits (pin and
    all).
    """

    def __init__(self, lib):
        self.lib = lib
        self.calls = []                       # [(argv, env)]

    def record(self, argv, env):
        self.calls.append((list(argv), env))
        run_env = dict(os.environ) if env is None else env
        asked = argv[argv.index("--board") + 1] if "--board" in argv else None
        cards = self.lib.cards[self.lib.resolved_slug(run_env, asked)]
        verb = argv[2] if len(argv) > 2 else ""

        class R:
            returncode, stdout, stderr = 0, "", ""
        r = R()
        if verb == "list":
            r.stdout = json.dumps([{"id": i, "status": s} for i, s in cards.items()])
        elif verb == "reclaim":
            cards[argv[3]] = "ready"
        elif verb == "block":
            assert argv[3] == "--kind"        # --kind must precede the id (argparse)
            cards[argv[5]] = "blocked"
        elif verb == "unblock":
            cards[argv[3]] = "ready"
        else:
            r.returncode = 2
        return r


class _EnvCli(_CliChild):
    """The grown runner contract: ``runner(argv, env=None)`` — can be scrubbed."""

    def __call__(self, argv, env=None):
        return self.record(argv, env)


class _LegacyCli(_CliChild):
    """A runner written against the pre-t_70c97264 contract: ``runner(argv)``.

    It cannot be handed an environment, which is exactly the state the read must
    refuse rather than trust — and it records through ``record``, so a call
    reaching it is observable.
    """

    def __call__(self, argv):
        return self.record(argv, None)


class _Recorder:
    """Records the calls the pause pass would make for a board it does not own."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args):
        self.calls.append(list(args))
        if len(args) == 3:                     # inject(profile, message, mode)
            return {"ok": True, "error": None, "mode": args[2], "pane": "%1"}

        class R:                              # cron(profile, argv)
            returncode, stdout, stderr = 0, "", ""
        return R()


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def shell(tmp_path, monkeypatch):
    """Deterministic pause environment: no ambient pin, no fence, marker armed.

    The board-pin decision must be taken from the state each case ARMS, never from
    whatever shell the leg happens to run in.
    """
    cfg = tmp_path / "config.yaml"
    cfg.write_text("kanban:\n  auto_decompose: true\n")
    monkeypatch.delenv(board_pin.PIN_ENV, raising=False)
    for var in FENCE_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(MARKER, "must-reach-the-child")
    return {"state_path": str(tmp_path / "paused.json"), "config_path": str(cfg),
            "cfg": cfg, "dir": str(tmp_path)}


@pytest.fixture
def lib(tmp_path):
    return _PauseLib(tmp_path / "kanban-home", ["alpha", "beta"])


@pytest.fixture
def worker_shell(monkeypatch, lib, shell):
    """Worker-shell state: pin at board ``alpha`` + a fence marker.

    Requests ``shell`` so it runs AFTER that fixture cleared the ambient state —
    arming first would let ``shell`` delete the pin again and silently turn every
    conflict case below into a no-pin case.
    """
    pin = str(lib._canonical("alpha"))
    monkeypatch.setenv(board_pin.PIN_ENV, pin)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    return pin


@pytest.fixture
def db(monkeypatch, lib):
    """The detection seam: ``board_pin`` is asked THIS lib, never the operator's."""
    monkeypatch.setattr(kp, "_load_kanban_db", lambda: lib)
    return lib


@pytest.fixture
def warnings(monkeypatch):
    """Capture every line the guard and this module emit, with fresh dedupe state.

    Both dedupe sets are process-global (one warning per pin+board per process is
    the documented posture), so a test asserting the warning fired must not
    inherit another test's silence — and one asserting silence must not be passing
    because of it.
    """
    import hscc_daemon.daemon_ops as daemon_ops
    seen = []
    monkeypatch.setattr(daemon_ops, "log",
                        lambda msg, level="INFO", **kw: seen.append((level, msg)))
    monkeypatch.setattr(board_pin, "_WARNED", set())
    monkeypatch.setattr(kp, "_PIN_WARNED", set())
    return seen


def _pause_kwargs(shell, cli, **over):
    kw = dict(run=cli, state_path=shell["state_path"], config_path=shell["config_path"],
              now=100, inject=None, cron=None)
    kw.update(over)
    return kw


def _seed_paused(shell, board, ids=("t_beta_1",)):
    """A paused-board record, written through the module's OWN writer."""
    kp.save_state({board: {"paused_at": 1, "reason": "r",
                           "cards": [{"id": i, "was": "ready"} for i in ids]}},
                  shell["state_path"])


# --------------------------------------------------------------------------- #
# non-vacuity: the same armed environment, the pre-guard call, the wrong board
# --------------------------------------------------------------------------- #

def test_the_pre_guard_calls_answer_and_write_for_the_pinned_board(lib, worker_shell,
                                                                   shell, db):
    """Pair for every case below: the pre-t_70c97264 calls, armed env.

    The child inherits the pin, so the ``list`` that ASKED for ``beta`` comes back
    with ``alpha``'s cards — and the exact ``block`` argv ``_hold`` sends lands on
    ``alpha``'s same-id card. Both halves of the harm, shown before either is
    claimed fixed.
    """
    cli = _LegacyCli(lib)
    proc = cli(LIST_ARGV)                      # the pre-guard list_cards call
    assert [r["id"] for r in json.loads(proc.stdout)] == lib.card_ids("alpha"), (
        "the armed env stopped pinning the child — every case below is vacuous")
    assert cli.calls[-1][1] is None, "today's call hands over no environment"

    cli(["--board", "beta", "block", "--kind", "dependency", "t_beta_1", "PAUSED"])
    assert lib.cards["alpha"]["t_beta_1"] == "blocked", (
        "the wrong-board write is the bug; if alpha is untouched, the write cases "
        "below prove nothing")
    assert lib.cards["beta"]["t_beta_1"] == "ready"


# --------------------------------------------------------------------------- #
# the read: option (a) — the child is handed an environment with no pin
# --------------------------------------------------------------------------- #

def test_list_cards_hands_the_child_an_environment_with_no_pin(lib, worker_shell,
                                                               shell, db, warnings):
    """Acceptance (a): armed pin + fence, injected ``run`` records the env."""
    cli = _EnvCli(lib)
    rows = kp.list_cards("beta", run=cli)
    assert [r["id"] for r in rows] == lib.card_ids("beta"), (
        "the inventory must be the board ASKED FOR, not the pinned one")
    assert len(cli.calls) == 1, cli.calls
    argv, env = cli.calls[0]
    assert argv == LIST_ARGV, "argv is untouched by this change"
    assert env is not None, "a pinned shell must hand the child an env"
    assert board_pin.PIN_ENV not in env, f"the child's env still carries the pin: {env}"
    assert env.get(MARKER) == "must-reach-the-child", (
        "the scrub must remove ONE key, not the child's world")
    assert env == {k: v for k, v in os.environ.items() if k != board_pin.PIN_ENV}
    # Loud, in board_pin's wording, naming both values.
    text = _warn_text(warnings)
    assert worker_shell in text and "beta" in text, (
        f"the override must name the pin and the board asked for: {text}")
    # The scrub is a hand-off, not a mutation of our own process.
    assert os.environ[board_pin.PIN_ENV] == worker_shell


def test_a_legacy_runner_is_never_asked_for_a_read(lib, worker_shell, shell, db,
                                                   warnings):
    """Where (a) is impossible, the read is refused — not answered by P.

    A legacy ``runner(argv)`` cannot be handed a scrubbed environment, so its
    answer would describe the pinned board while being reported as ``beta``.
    """
    cli = _LegacyCli(lib)
    assert kp.list_cards("beta", run=cli) == []
    assert cli.calls == [], f"the probe would have answered for alpha: {cli.calls}"
    text = _warn_text(warnings)
    assert worker_shell in text and "beta" in text and "cannot be handed" in text, text


# --------------------------------------------------------------------------- #
# the write: option (b) — refuse before spawning anything, per board
# --------------------------------------------------------------------------- #

def test_the_pause_pass_refuses_rather_than_blocking_the_pinned_board(
        lib, worker_shell, shell, db, warnings):
    """Acceptance (b): the pause reports the refusal and spawns NOTHING."""
    cli, inj, cron = _EnvCli(lib), _Recorder(), _Recorder()
    rep = kp.pause(["beta"], "priority", **_pause_kwargs(shell, cli, inject=inj,
                                                          cron=cron))
    why = rep["beta"]["refused"]
    assert why.startswith("refusing to write kanban board 'beta'"), why
    assert worker_shell in why and "alpha" in why, why     # pin + both boards named
    assert rep["beta"]["held"] == [] and rep["beta"]["failed"] == []
    assert cli.calls == [], f"nothing may be spawned for a refused board: {cli.calls}"
    assert inj.calls == [] and cron.calls == [], (
        "a board that was not paused must not steer its orchestrator or park its crons")
    assert kp.load_state(shell["state_path"]) == {}, (
        "a refused board must not enter the paused-state file")
    assert "auto_decompose: true" in shell["cfg"].read_text(), (
        "a pause that did not happen must not switch the decomposer off")
    assert lib.cards["alpha"] == lib.pristine("alpha"), (
        "the pinned board is untouched — not one write reached it")


def test_the_report_names_every_board_it_refused_and_pauses_the_rest(
        lib, worker_shell, shell, db, warnings):
    """Pair: the same fake, the same armed shell — ``alpha`` (the pin) is paused,
    ``beta`` is refused. The refusal is caused by the conflict, not by the fake.
    """
    cli, inj = _EnvCli(lib), _Recorder()
    rep = kp.pause(["alpha", "beta"], **_pause_kwargs(shell, cli, inject=inj))
    assert sorted(rep["alpha"]["held"]) == sorted(lib.card_ids("alpha")), rep
    assert "refused" in rep["beta"] and "refused" not in rep["alpha"], rep
    # alpha's own cards: t_alpha_1 ready (block), r_alpha_1 running (reclaim then
    # block) — the board behind the pin really was held, by this same fake.
    assert [c[0][2] for c in cli.calls if c[0][2] in ("reclaim", "block")] == [
        "block", "reclaim", "block"], cli.calls
    st = kp.load_state(shell["state_path"])
    assert "alpha" in st and "beta" not in st, st
    assert [c[0] for c in inj.calls] == ["alpha-orch"], inj.calls
    # A worker pausing the board it is pinned to is the honest case: nothing is
    # overridden for it, so no environment is handed over for its probes.
    assert all(env is None for _a, env in cli.calls), cli.calls
    # And the ONLY thing said is beta's refusal, naming the pin and the board it
    # refused — the alpha half produced no read-scrub line, because there was
    # nothing to scrub for the board behind the pin.
    text = _warn_text(warnings)
    assert len(warnings) == 1, warnings
    assert "refusing to write" in text and worker_shell in text and "beta" in text, text
    assert "unset for the duration" not in text, (
        f"an alpha probe must never be scrubbed: {text}")


def test_resume_refuses_and_keeps_the_paused_record(lib, worker_shell, shell, db,
                                                    warnings):
    """``unblock`` is the same 2c write class — and worse, it RELEASES a hold.

    A refused resume must also forget NOTHING: the board stays in the paused-state
    file, or the operator loses the record of a board that is still paused.
    """
    _seed_paused(shell, "beta")
    cli = _EnvCli(lib)
    rep = kp.resume(["beta"], run=cli, state_path=shell["state_path"])
    assert rep["beta"]["refused"].startswith("refusing to write kanban board 'beta'")
    assert rep["beta"]["released"] == [] and rep["beta"]["failed"] == []
    assert cli.calls == [], f"no unblock may be spawned: {cli.calls}"
    st = kp.load_state(shell["state_path"])
    assert "beta" in st, "a refused resume must not forget the board is paused"
    assert lib.cards["alpha"] == lib.pristine("alpha")


def test_enforce_refuses_and_the_stream_says_the_hold_is_not_applied(
        lib, worker_shell, shell, db, warnings):
    """The daemon's 60 s loop must not become the thing that writes the pinned
    board every minute — and it must not look healthy while it cannot enforce.
    """
    _seed_paused(shell, "beta")
    cli = _EnvCli(lib)
    rep = kp.enforce(run=cli, state_path=shell["state_path"])
    assert list(rep) == [kp.REFUSED_KEY], rep
    assert rep[kp.REFUSED_KEY]["beta"].startswith("refusing to write kanban board")
    assert cli.calls == [], f"enforce may not write under the pin: {cli.calls}"

    stamped = []
    entry = kp.enforce_tick(now=1000.0, state_path=shell["state_path"], run=cli,
                            write=lambda name, e: stamped.append((name, dict(e))))
    assert entry["ok"] is False, entry
    assert entry["held"] == 0 and entry["paused"] == 0, entry
    assert "REFUSED 1 paused board(s)" in entry["message"] and "beta" in entry["message"]
    assert worker_shell in entry["message"], entry["message"]
    assert entry["refused"]["beta"].startswith("refusing to write")
    assert stamped == [(kp.STREAM_NAME, entry)], stamped
    # doctor's EXISTING red row carries it — no new shape for the reader half.
    row = kp.doctor_check(now=1001.0, paused_path=shell["state_path"], stream_state=entry)
    assert row["ok"] is False and "NOT being enforced" in row["detail"], row


def test_a_direct_hold_call_refuses_before_spawning(lib, worker_shell, shell, db):
    """``_hold`` has no report to write into, so it fails LOUDLY (2c posture)."""
    cli = _EnvCli(lib)
    with pytest.raises(board_pin.BoardPinConflict) as exc:
        kp._hold("beta", {"id": "t_beta_1", "status": "ready"}, "PAUSED", cli)
    assert "refusing to write kanban board 'beta'" in str(exc.value)
    assert worker_shell in str(exc.value)
    assert cli.calls == [], cli.calls
    assert lib.cards["beta"] == lib.pristine("beta")
    assert lib.cards["alpha"] == lib.pristine("alpha")


# --------------------------------------------------------------------------- #
# companions: the paths that must not change
# --------------------------------------------------------------------------- #

def test_without_a_pin_argv_and_env_are_exactly_todays(lib, shell, db, warnings):
    """No pin, no fence → ``run(argv)`` with no environment keyword at all, and
    the same argv bytes — the child inherits, as it always has."""
    cli = _EnvCli(lib)
    assert [r["id"] for r in kp.list_cards("beta", run=cli)] == lib.card_ids("beta")
    assert cli.calls == [(LIST_ARGV, None)], cli.calls
    rep = kp.pause(["beta"], **_pause_kwargs(shell, cli))
    assert sorted(rep["beta"]["held"]) == sorted(lib.card_ids("beta")), rep
    assert all(env is None for _a, env in cli.calls), cli.calls
    # (calls[0] is the direct list_cards above; pause re-lists before it holds)
    assert [c[0] for c in cli.calls if c[0][2] in ("reclaim", "block")] == [
        # list_cards keeps the CLI's order: t_beta_1 (ready) is blocked, then
        # r_beta_1 (running) is reclaimed and blocked — the reclaim still
        # immediately precedes its own card's block.
        ["--board", "beta", "block", "--kind", "dependency", "t_beta_1",
         "PAUSED by operator — resume with `hscc kanban resume <board>`"],
        ["--board", "beta", "reclaim", "r_beta_1", "--reason",
         "PAUSED by operator — resume with `hscc kanban resume <board>`"],
        ["--board", "beta", "block", "--kind", "dependency", "r_beta_1",
         "PAUSED by operator — resume with `hscc kanban resume <board>`"],
    ], cli.calls[1:]
    assert not warnings, _warn_text(warnings)


def test_a_worker_pausing_its_own_board_is_never_scrubbed_and_never_speaks(
        lib, shell, db, warnings, monkeypatch):
    """The pin already IS the board asked for: the fence is doing its job, so
    nothing is overridden, nothing is scrubbed and nothing is said."""
    monkeypatch.setenv(board_pin.PIN_ENV, str(lib._canonical("beta")))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    cli = _EnvCli(lib)
    rep = kp.pause(["beta"], **_pause_kwargs(shell, cli))
    assert sorted(rep["beta"]["held"]) == sorted(lib.card_ids("beta")), rep
    assert cli.calls and all(env is None for _a, env in cli.calls), cli.calls
    assert not warnings, _warn_text(warnings)


def test_a_lib_that_cannot_answer_falls_through_untouched(lib, worker_shell, shell,
                                                          warnings, monkeypatch):
    """No resolver means no evidence — the guard steps aside rather than guessing
    (``board_pin``'s documented posture), and the pass runs as it always did."""
    class _NoResolver:
        def list_boards(self):
            return [{"slug": s} for s in lib.slugs]

    monkeypatch.setattr(kp, "_load_kanban_db", lambda: _NoResolver())
    cli = _EnvCli(lib)
    rep = kp.pause(["beta"], **_pause_kwargs(shell, cli))
    # The guard has no evidence to act on, so it steps aside — and that IS the
    # old behaviour, down to the child answering for the board its OWN
    # environment pins (alpha). Pinned deliberately: the fall-through must be
    # the untouched path, not a half-guard that scrubs one call and not the next.
    assert sorted(rep["beta"]["held"]) == sorted(lib.card_ids("alpha")), rep
    assert all(env is None for _a, env in cli.calls), cli.calls
    assert not warnings, _warn_text(warnings)


# --------------------------------------------------------------------------- #
# the real runner: the scrub has to survive subprocess.run
# --------------------------------------------------------------------------- #

def test_the_default_runner_forwards_the_env_and_inherits_without_one(
        lib, shell, db, warnings, monkeypatch):
    """End-to-end through ``subprocess.run``'s own kwargs — no child is spawned.

    The injected-fake cases prove the module DECIDES the child env; this one proves
    the production runner PASSES it, which is the half that actually reaches
    hermes. The fake answers the way hermes does: from the environment the child
    was ACTUALLY handed.
    """
    spawns = []

    def fake_run(argv, **kw):
        spawns.append((list(argv), kw))
        handed = dict(os.environ) if kw.get("env") is None else kw["env"]
        asked = argv[argv.index("--board") + 1] if "--board" in argv else None
        cards = lib.cards[lib.resolved_slug(handed, asked)]

        class R:
            returncode, stderr = 0, ""
        r = R()
        r.stdout = json.dumps([{"id": i, "status": s} for i, s in cards.items()])
        return r

    monkeypatch.setattr(kp.subprocess, "run", fake_run)

    # 1. nothing handed over -> byte-identical to the pre-guard call (env=None
    #    means "inherit", which is what the child did before).
    assert [r["id"] for r in kp.list_cards("beta")] == lib.card_ids("beta")
    assert spawns[-1][1].get("env", "MISSING") is None, spawns[-1][1]
    assert spawns[-1][0] == ["hermes", "kanban"] + LIST_ARGV, spawns[-1][0]

    # 2. armed, with NO injected run — the real ``_run_hermes``.
    monkeypatch.setenv(board_pin.PIN_ENV, str(lib._canonical("alpha")))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    assert lib.resolved_slug(dict(os.environ), "beta") == "alpha", (
        "the armed env must still pin an inherited child, or case 2 is vacuous")
    spawns.clear()
    assert [r["id"] for r in kp.list_cards("beta")] == lib.card_ids("beta")
    argv, kw = spawns[-1]
    env = kw.get("env")
    assert isinstance(env, dict), f"the child must be handed an env: {kw}"
    assert board_pin.PIN_ENV not in env, env
    assert env.get(MARKER) == "must-reach-the-child", env
    assert argv == ["hermes", "kanban"] + LIST_ARGV, argv
