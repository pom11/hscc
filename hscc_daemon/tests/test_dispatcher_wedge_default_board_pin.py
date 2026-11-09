"""t_dad0da32 — ``board="default"`` through the wedge's board-state gate is a
REAL board and must be pin-guarded like every other slug.

The residual #268 left behind: the layer-2 gate rebuilt ``multi`` from the slug
(``bool(board) and board != "default"``) instead of asking the lib. ``multi`` is
documented to mean "the caller named a board" (``board_pin.py:231-242``), so for
``default`` the guard was told *no intent* and deferred to the worker's
``HERMES_KANBAN_DB`` pin. Every other slug got warn-and-scrub; ``default`` read
the pinned board while the panel labelled the answer ``default`` — and because a
per-profile-cap explanation needs no further evidence, the gate would then
suppress a gateway restart using running counts taken from a board it never
read. Decision record: ``docs/audits/wedge-default-board-pin-t_dad0da32.md``.

Assertion style follows ``test_board_pin.py``: the observable is a ``WARN`` line
naming both boards, the argv a human would type, and which rows the probe
actually read — never an internal flag. Each conflict case is PAIRED with a
non-vacuity assertion that the same armed environment really does read the wrong
board unguarded, because otherwise the suite would pass just as happily with
nothing pinned at all.
"""

import json
import os
import sqlite3

import pytest

from hscc_daemon import board_pin, dispatcher_wedge as dw

NOW = 1_800_000_000.0
PIN_BOARD = "hscc"        # what the worker's pin points at
ASKED = "default"         # what the gate is asked about

FENCE_VARS = ("HERMES_KANBAN_TASK", "HERMES_DELEGATED_CHILD_CONTEXT")


class _Ctx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


def _board_conn(slug):
    """One board's DB: a task table in the shape the gate queries, plus a
    ``title`` marker so "which board did we actually read?" is answered by a
    query rather than by any flag."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, "
              "status TEXT, assignee TEXT, claim_lock TEXT)")
    # `slug` rows: worker-a has ready work and NOTHING running, so the honest
    # verdict for this board is "unexplained" (a live wedge).
    c.execute("INSERT INTO tasks VALUES ('t_ready', ?, 'ready', 'worker-a', "
              "NULL)", (slug,))
    if slug == PIN_BOARD:
        # The pinned board is the one carrying worker-a at its cap — the data
        # that, if read by mistake, "explains" a stall on the asked board.
        for i in range(2):
            c.execute("INSERT INTO tasks VALUES (?, ?, 'running', 'worker-a', "
                      "NULL)", (f"run-{i}", slug))
    c.commit()
    return c


class _PinAwareBase:
    """Kanban lib reproducing hermes' MEASURED precedence rule (same shape as
    ``test_board_pin._PinAwareLib``, with the wedge's task schema).

    ``default`` resolves to the legacy flat ``<root>/kanban.db`` and every other
    slug to ``<root>/kanban/boards/<slug>/kanban.db``, and the pin wins whenever
    it is set and either no board was named or a fence marker is active — copied
    from the behaviour measured in docs/audits/board-pin-t_b014c2b8.md rather
    than simplified, so a guard that passes here is a guard that works against
    the real resolver.

    Deliberately WITHOUT ``list_boards``: the enumeration answer is what
    distinguishes the two subclasses below, and that is exactly what ``multi``
    is defined from (``autodown.py:310``).
    """

    def __init__(self, root, slugs):
        self.root = str(root)
        self.slugs = list(slugs)
        self.connects = []          # (board_arg, resolved_path, pin_seen)
        self._by_path = {str(self._canonical(s)): _board_conn(s) for s in slugs}

    def _canonical(self, slug):
        if slug in (None, "default"):
            return f"{self.root}/kanban.db"
        return f"{self.root}/kanban/boards/{slug}/kanban.db"

    def _fenced(self):
        return any((os.environ.get(v) or "").strip() for v in FENCE_VARS)

    def kanban_db_path(self, board=None):
        pin = (os.environ.get(board_pin.PIN_ENV) or "").strip()
        if pin and (board is None or self._fenced()):
            return pin
        return self._canonical(board)

    def connect_closing(self, board=None):
        path = str(self.kanban_db_path(board=board))
        self.connects.append((board, path, os.environ.get(board_pin.PIN_ENV)))
        return _Ctx(self._by_path[path])


class PinAwareLib(_PinAwareBase):
    """A modern lib: it enumerates boards, so a slug names a real board."""

    def list_boards(self):
        return [{"slug": s} for s in self.slugs]


class LegacyLib(_PinAwareBase):
    """The single-board shape: NO ``list_boards`` at all, so autodown's
    ``_enum_board_names`` answers ``([None], False)`` and a slug is not
    something this lib can be asked to honour — the pin must stay honoured."""


class FakeProc:
    def __init__(self, payload, rc=0):
        self.returncode = rc
        self.stdout = json.dumps(payload)
        self.stderr = ""


class FakeCli:
    """Fake sanctioned CLI: matches subcommand-key tuples against argv."""

    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def __call__(self, argv):
        self.calls.append([str(a) for a in argv])
        for key, payload in self.payloads.items():
            if all(k in argv for k in key):
                return FakeProc(payload)
        return FakeProc({"error": "unmapped argv"}, rc=1)


def _ready_row(tid, assignee="worker-a"):
    return {"id": tid, "status": "ready", "assignee": assignee}


NO_HOLD = {"task": {"id": "t_ready"}, "events": [], "runs": [], "comments": []}


@pytest.fixture
def warnings(monkeypatch):
    """Capture the guard's words through the real emission path (``daemon_ops
    .log``), with a fresh once-per-process dedupe set."""
    seen = []
    import hscc_daemon.daemon_ops as daemon_ops
    monkeypatch.setattr(daemon_ops, "log",
                        lambda msg, level="INFO", **kw: seen.append((level, msg)))
    monkeypatch.setattr(board_pin, "_WARNED", set())
    return seen


@pytest.fixture
def armed(tmp_path, monkeypatch):
    """Worker-shell state: the pin points at board ``hscc``, a fence marker is
    up, and the gate is being asked about ``default``. Returns (lib, pin)."""
    lib = PinAwareLib(tmp_path / "kanban-home", [PIN_BOARD, "beta", ASKED])
    pin = lib._canonical(PIN_BOARD)
    monkeypatch.setenv(board_pin.PIN_ENV, pin)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.setattr(dw, "_hermes_cli_argv", lambda: ["hermes", "kanban"])
    monkeypatch.setattr(dw, "_assignee_is_nonspawnable", lambda a: False)
    return lib, pin


def _drive(lib, cli, board=ASKED, per_profile=2):
    """Run the REAL gate for one board of a red stream."""
    return dw.board_state_explains_stall(
        {"ok": False, "declared": True, "spawnable": [board],
         "total_running": 0},
        kanban_db=lib, max_in_progress=6, max_per_profile=per_profile,
        cli_fn=cli, now=NOW)


def _titles(ctx):
    with ctx as conn:
        return {r["title"] for r in conn.execute("SELECT title FROM tasks")}


def _warn_text(seen):
    return " || ".join(m for _lvl, m in seen)


# --------------------------------------------------------------------------- #
# the acceptance case: ask the wedge for `default` under an armed pin
# --------------------------------------------------------------------------- #

def test_default_is_guarded_warning_names_both_boards(armed, warnings):
    """The card's acceptance case, asserted on observables only.

    Board ``hscc`` is pinned and carries worker-a at its per-profile cap; board
    ``default`` has ready work and nothing running. Asking for ``default`` must
    (a) say so out loud — naming the pin and both boards — and (b) keep the
    wedge actionable, because on the board actually asked about the ready card
    is unexplained.
    """
    lib, pin = armed
    cli = FakeCli({
        ("list", "--status", "ready"): [_ready_row("t_ready")],
        ("show", "t_ready"): NO_HOLD,
    })
    explained, why = _drive(lib, cli)

    text = _warn_text(warnings)
    assert text, "the read must be loud, not merely correct"
    assert pin in text, f"the warning must name the pin it overrode: {text}"
    assert ASKED in text, f"the warning must name the board asked for: {text}"
    assert PIN_BOARD in text, f"and the board it would have read: {text}"
    assert "HERMES_KANBAN_TASK" in text, f"and the trigger: {text}"

    assert explained is False, (
        f"the pinned board's cap must not silence a wedge on default: {why}")
    assert "unexplained" in why, why


def test_argv_states_the_default_slug_too(armed, warnings):
    """The CLI half of the same leak: the argv must name the board the panel is
    reporting, exactly as it already did for every other slug."""
    lib, _pin = armed
    cli = FakeCli({
        ("list", "--status", "ready"): [_ready_row("t_ready")],
        ("show", "t_ready"): NO_HOLD,
    })
    _drive(lib, cli)
    listing = [c for c in cli.calls if "list" in c]
    showing = [c for c in cli.calls if "show" in c]
    assert listing and showing, cli.calls
    for argv in listing + showing:
        i = argv.index("--board")
        assert argv[i + 1] == ASKED, (
            f"argv must carry --board {ASKED} so the CLI reads the board the "
            f"panel names: {argv}")


def test_the_armed_env_really_reads_the_wrong_board_without_the_guard(
        armed, warnings):
    """Non-vacuity: with the guard not consulted, ``default`` answers with the
    pinned board's rows — including the running rows that would have explained
    the stall. If this goes red, the fixture stopped reproducing the bug and the
    two tests above prove nothing."""
    lib, pin = armed
    assert _titles(lib.connect_closing(board=ASKED)) == {PIN_BOARD}, (
        "the armed env must pin: board=default must resolve to the pinned file")
    assert pin in lib._canonical(PIN_BOARD)
    assert not warnings, "the unguarded path is silent — that was the bug"
    # And the pinned rows are exactly the ones that flip the verdict: worker-a
    # at cap, which is why the leak suppressed restarts rather than merely
    # mislabelling a panel.
    with lib.connect_closing(board=ASKED) as conn:
        running = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status='running' "
            "AND assignee='worker-a'").fetchone()["n"]
    assert running == 2


def test_other_slugs_stay_guarded(armed, warnings):
    """No regression in the direction that already worked: an explicit non-
    default slug keeps warning and keeps its slug in the argv."""
    lib, pin = armed
    cli = FakeCli({
        ("list", "--status", "ready"): [_ready_row("t_ready")],
        ("show", "t_ready"): NO_HOLD,
    })
    _drive(lib, cli, board="beta")
    text = _warn_text(warnings)
    assert pin in text and "beta" in text, text
    assert any("beta" in c[c.index("--board") + 1:] for c in cli.calls
               if "--board" in c), cli.calls


# --------------------------------------------------------------------------- #
# companions: the fix must not eat legitimate runs
# --------------------------------------------------------------------------- #

def test_worker_pinned_to_default_stays_silent(tmp_path, monkeypatch, warnings):
    """The ordinary dispatched-worker case: the pin IS ``default``, the fence is
    doing its job, nothing is being hidden — so no warning and no refusal."""
    lib = PinAwareLib(tmp_path / "kanban-home", [PIN_BOARD, ASKED])
    monkeypatch.setenv(board_pin.PIN_ENV, lib._canonical(ASKED))
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(dw, "_hermes_cli_argv", lambda: ["hermes", "kanban"])
    monkeypatch.setattr(dw, "_assignee_is_nonspawnable", lambda a: False)
    cli = FakeCli({
        ("list", "--status", "ready"): [_ready_row("t_ready")],
        ("show", "t_ready"): NO_HOLD,
    })
    _drive(lib, cli)
    assert not warnings, _warn_text(warnings)
    assert all(c[2] == lib._canonical(ASKED) for c in lib.connects), lib.connects


def test_no_pin_means_every_board_read_and_nothing_said(
        tmp_path, monkeypatch, warnings):
    """The daemon/operator case (no worker env at all): ``default`` is still
    stated on the argv, but there is no conflict to report."""
    lib = PinAwareLib(tmp_path / "kanban-home", [PIN_BOARD, ASKED])
    monkeypatch.delenv(board_pin.PIN_ENV, raising=False)
    for v in FENCE_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(dw, "_hermes_cli_argv", lambda: ["hermes", "kanban"])
    monkeypatch.setattr(dw, "_assignee_is_nonspawnable", lambda a: False)
    cli = FakeCli({
        ("list", "--status", "ready"): [_ready_row("t_ready")],
        ("show", "t_ready"): NO_HOLD,
    })
    explained, why = _drive(lib, cli)
    assert explained is False and "unexplained" in why, why
    assert not warnings
    assert [c[0] for c in lib.connects] == [ASKED], (
        "the guard must pass the slug through, not drop it")


def test_legacy_single_board_shape_is_unchanged(tmp_path, monkeypatch,
                                                warnings):
    """``multi=False`` (a lib that cannot enumerate boards) keeps the byte-for-byte
    old behaviour: no ``--board`` in the argv, ``connect_closing()`` with NO
    board argument, the pin honoured and nothing said.

    This is the case the card's suggested ``multi = bool(board)`` would have
    broken — it hands ``multi=True`` to a lib that has no board argument to
    honour — so it is pinned here rather than only argued in the decision record.
    """
    lib = LegacyLib(tmp_path / "kanban-home", [PIN_BOARD, ASKED])
    assert not hasattr(lib, "list_boards"), "legacy shape must not enumerate"
    monkeypatch.setenv(board_pin.PIN_ENV, lib._canonical(PIN_BOARD))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.setattr(dw, "_hermes_cli_argv", lambda: ["hermes", "kanban"])
    monkeypatch.setattr(dw, "_assignee_is_nonspawnable", lambda a: False)
    cli = FakeCli({
        ("list", "--status", "ready"): [_ready_row("t_ready")],
        ("show", "t_ready"): NO_HOLD,
    })
    explained, why = _drive(lib, cli)
    assert not warnings, _warn_text(warnings)
    assert [c[0] for c in lib.connects] == [None], (
        "the legacy shape must keep the no-board call")
    assert all("--board" not in c for c in cli.calls), cli.calls
    # The legacy answer is also the CORRECT one here: this lib has exactly one
    # board, so the file the pin points at is not "another" board. If the fix
    # ever leaked multi=True into this shape the connect above would have asked
    # for board="default" and the guard would have warned about a conflict that
    # does not exist.
    assert explained is True, why


def test_a_broken_enumeration_does_not_switch_the_guard_off(
        armed, warnings, monkeypatch):
    """If ``list_boards`` raises mid-gate, ``multi`` must still answer from the
    same rule autodown uses (``hasattr(lib, "list_boards")``), so a lib that
    enumerates but cannot enumerate RIGHT NOW cannot quietly reopen the leak."""
    lib, pin = armed

    def boom():
        raise RuntimeError("enumeration exploded")

    monkeypatch.setattr(lib, "list_boards", boom)
    cli = FakeCli({
        ("list", "--status", "ready"): [_ready_row("t_ready")],
        ("show", "t_ready"): NO_HOLD,
    })
    _drive(lib, cli)
    text = _warn_text(warnings)
    assert pin in text and ASKED in text, (
        f"the guard must still be in the loop: {text or '(silent)'}")
    assert dw._gate_multi(lib) is True
