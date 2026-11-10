"""Regression guard for MON-ENV-PIN (t_b014c2b8).

The bug: a dispatched worker shell carries ``HERMES_KANBAN_DB``, and hermes
resolves that env pin AHEAD of an explicit ``board=`` argument whenever a
worker/delegation fence marker is set (``hermes_cli/kanban_db.py:540-555`` +
``agent/delegation_context.py:76-93``). ``list_boards()`` is not affected, so a
fleet monitor enumerated 24 boards and opened the SAME pinned DB 24 times — a
well-formed panel (``errors: []``) reporting one board as the fleet.

Two families of tests, because two proofs are needed:

* the fake-lib family asserts the guard's contract with the conflict *built in
  the test* (no ambient env), and — for non-vacuity — that the SAME armed
  environment reads the WRONG board when the guard is not used. A guard test
  that only checked "we got board B" would also pass in an env where nothing was
  ever pinned; the paired assertion is what proves the conflict was real.
* the real-hermes family drives the actual ``hermes_cli`` resolver against a
  ``HERMES_KANBAN_HOME`` fixture root, so the precedence being defeated is
  hermes' own, not a mock's idea of it. It skips loudly (``pytest.skip``, per
  GAP-D-1) when hermes_cli cannot be imported.

Nothing here touches a live board: every fixture lives under ``tmp_path``, and
the fixture boards are created while no fence marker is set.
"""

import os
import pathlib
import sqlite3
import sys
import tempfile

import pytest

from hscc_daemon import board_pin

# The ONE "is this a planted fake?" rule, shared with conftest and test_autodown
# (t_b62da70c) — see the comment above ``_real_kanban_db`` for what this file
# adds on top of it (the borrow/restore, which is its own business).
from hscc_daemon.tests.hermes_cli_plant import (
    evict_planted, is_planted_path, restore_planted)

NOW = 1_800_000_000

# The two markers hermes checks (delegation_context.py:89-93). Tests arm them
# explicitly instead of inheriting them, so each test reads the same inside a
# worker shell and outside one.
FENCE_VARS = ("HERMES_KANBAN_TASK", "HERMES_DELEGATED_CHILD_CONTEXT")


class _Ctx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


def _board_conn(marker):
    """A connection whose only row is ``marker`` — so "which board did we
    actually read?" is answered by a query, never by an internal flag."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT)")
    c.execute("INSERT INTO tasks (id, title) VALUES ('t_x', ?)", (marker,))
    c.commit()
    return c


class _PinAwareLib:
    """Kanban lib reproducing hermes' MEASURED precedence rule.

    ``kanban_db_path`` copies the behaviour measured in
    ``docs/audits/board-pin-t_b014c2b8.md`` (the pin wins when no board was
    named or when a fence marker is set; otherwise the board dir, with
    ``default`` at the legacy flat file) rather than simplifying it, so a guard
    that passes here is a guard that works against the real resolver. Data
    follows the FILE — each board's rows live at that board's canonical path —
    so resolving to the pinned file really does return the pinned board's rows.
    """

    def __init__(self, root, slugs):
        self.root = str(root)
        self.slugs = list(slugs)
        self.connects = []                      # (board, resolved path, pin seen)
        self._by_path = {str(self._canonical(s)): _board_conn(s) for s in slugs}

    def _canonical(self, slug):
        if slug in (None, "default"):
            return pathlib.Path(self.root) / "kanban.db"
        return (pathlib.Path(self.root) / "kanban" / "boards" / str(slug)
                / "kanban.db")

    def _fenced(self):
        return any((os.environ.get(v) or "").strip() for v in FENCE_VARS)

    # --- the hermes-shaped surface -------------------------------------- #
    def list_boards(self):
        return [{"slug": s} for s in self.slugs]

    def kanban_db_path(self, board=None):
        pin = (os.environ.get(board_pin.PIN_ENV) or "").strip()
        if pin and (board is None or self._fenced()):
            return pathlib.Path(pin)
        return self._canonical(board)

    def connect_closing(self, board=None):
        path = str(self.kanban_db_path(board=board))
        self.connects.append((board, path, os.environ.get(board_pin.PIN_ENV)))
        return _Ctx(self._by_path[path])


class _NoResolverLib:
    """A lib predating ``kanban_db_path`` — the guard must not guess a path."""

    def __init__(self):
        self.connects = []

    def list_boards(self):
        return [{"slug": "alpha"}, {"slug": "beta"}]

    def connect_closing(self, board=None):
        self.connects.append(board)
        return _Ctx(_board_conn(board or "default"))


class _RaisingResolverLib(_PinAwareLib):
    """A resolver that explodes: the guard must not invent a conflict from it.
    Opens still work — this models a lib that can serve a board but cannot
    answer path questions, which is the case the guard must step aside for."""

    def kanban_db_path(self, board=None):
        raise RuntimeError("resolver is broken")

    def connect_closing(self, board=None):
        return _Ctx(self._by_path[str(self._canonical(board))])


@pytest.fixture
def lib(tmp_path):
    return _PinAwareLib(tmp_path / "kanban-home", ["alpha", "beta", "default"])


@pytest.fixture
def armed(tmp_path, monkeypatch):
    """Worker-shell state: pin at board alpha + a fence marker. Returns the pin."""
    pin = str(_PinAwareLib(tmp_path / "kanban-home", ["alpha"])._canonical("alpha"))
    monkeypatch.setenv(board_pin.PIN_ENV, pin)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    return pin


@pytest.fixture
def warnings(monkeypatch):
    """Capture what the guard says. Emission goes through ``daemon_ops.log``
    (daemon file + stderr, never stdout), so patching THAT keeps the real
    ``_emit`` path under test instead of replacing it. Also hands out a fresh
    dedupe set, since a monitor's once-per-process warning is stateful."""
    seen = []
    import hscc_daemon.daemon_ops as daemon_ops
    monkeypatch.setattr(daemon_ops, "log",
                        lambda msg, level="INFO", **kw: seen.append((level, msg)))
    monkeypatch.setattr(board_pin, "_WARNED", set())
    return seen


def _warn_text(seen):
    return " || ".join(m for _lvl, m in seen)


def _titles(ctx):
    with ctx as conn:
        return {r[0] for r in conn.execute("SELECT title FROM tasks")}


# --------------------------------------------------------------------------- #
# the conflict, built in the test
# --------------------------------------------------------------------------- #

def test_guarded_read_warns_and_reads_the_named_board(lib, armed, warnings):
    """The card's acceptance case: the env pins board A, we ask for board B."""
    assert _titles(board_pin.open_board(lib, "beta", multi=True)) == {"beta"}
    text = _warn_text(warnings)
    assert text, "the read must be loud, not merely correct"
    assert armed in text, f"the warning must name the pin it overrode: {text}"
    assert "beta" in text, f"the warning must name the board asked for: {text}"
    assert "kanban.db" in text, f"both resolved paths belong in the message: {text}"
    assert "HERMES_KANBAN_TASK" in text, (
        f"the message must say what triggered the fence: {text}")


def test_the_armed_env_really_reads_the_wrong_board_without_the_guard(
        lib, armed, warnings):
    """Non-vacuity: without the guard the same env hands us board alpha's rows.

    If this test ever fails, the fixture stopped reproducing the bug and every
    guarded test above stops proving anything.
    """
    assert _titles(lib.connect_closing(board="beta")) == {"alpha"}
    assert not warnings, "the unguarded path is silent — that is the bug"


def test_write_mode_refuses_naming_both_values(lib, armed):
    """A write is never scrubbed: it raises, and the message names both sides."""
    with pytest.raises(board_pin.BoardPinConflict) as exc:
        with board_pin.open_board(lib, "beta", multi=True, mode="write"):
            pass  # pragma: no cover — must not be reached
    msg = str(exc.value)
    assert armed in msg, f"must name the pin: {msg}"
    assert "beta" in msg, f"must name the requested board: {msg}"
    assert lib.connects == [], "refusal must happen before any board is opened"


def test_scrub_is_temporary_and_survives_a_raising_body(lib, armed):
    with pytest.raises(RuntimeError):
        with board_pin.open_board(lib, "beta", multi=True) as _conn:
            raise RuntimeError("body failed")
    assert os.environ[board_pin.PIN_ENV] == armed, "the pin must come back verbatim"
    # ...and a later unguarded open is pinned again (proves restoration, not
    # merely that the key exists).
    assert _titles(lib.connect_closing(board="beta")) == {"alpha"}


# --------------------------------------------------------------------------- #
# the companion cases: the guard must not eat legitimate runs
# --------------------------------------------------------------------------- #

def test_worker_reading_its_own_board_stays_silent(lib, armed, warnings):
    """The ordinary dispatched-worker case: the pin IS the board we asked for.

    This is the run the fence exists for — no conflict, no warning, no scrub.
    """
    assert _titles(board_pin.open_board(lib, "alpha", multi=True)) == {"alpha"}
    assert not warnings, f"nothing is being hidden here: {_warn_text(warnings)}"


def test_multi_board_run_without_a_pin_is_untouched(lib, monkeypatch, warnings):
    """The daemon/operator case: no pin at all → every board read, nothing said."""
    monkeypatch.delenv(board_pin.PIN_ENV, raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    for slug in ("alpha", "beta", "default"):
        assert _titles(board_pin.open_board(lib, slug, multi=True)) == {slug}
    assert not warnings
    assert [c[0] for c in lib.connects] == ["alpha", "beta", "default"], (
        "the guard must pass board= through exactly as before")
    assert all(c[2] is None for c in lib.connects)


def test_single_board_legacy_shape_is_the_old_call_verbatim(lib, armed, warnings):
    """``multi=False`` means the lib cannot enumerate boards and the caller
    named none — no intent to defend, so the pin is honoured."""
    calls = []

    class _Legacy:
        def connect_closing(self, board=None):
            calls.append(board)
            return _Ctx(_board_conn("legacy"))

    assert _titles(board_pin.open_board(_Legacy(), "beta", multi=False)) == {"legacy"}
    assert calls == [None], "the legacy shape must keep the no-board call"
    assert not warnings


def test_no_expressed_board_intent_is_left_to_the_pin(lib, armed, warnings):
    """``board=None`` = the caller expressed no intent → honouring the pin is
    correct, and warning about it would be noise."""
    assert _titles(board_pin.open_board(lib, None, multi=True)) == {"alpha"}
    assert not warnings


def test_lib_without_a_resolver_is_left_alone(armed, warnings):
    """A lib predating ``kanban_db_path`` cannot be interrogated, so the guard
    stays out of the way rather than inventing a path."""
    stub = _NoResolverLib()
    assert _titles(board_pin.open_board(stub, "beta", multi=True)) == {"beta"}
    assert stub.connects == ["beta"]
    assert not warnings


def test_a_broken_resolver_never_becomes_a_conflict(armed, warnings):
    """A resolver that raises is not evidence of a pin conflict; the caller's
    own open surfaces the real problem."""
    lib = _RaisingResolverLib("/tmp/kanban-home", ["alpha", "beta"])
    assert board_pin.pin_conflict(lib, "beta") is None
    assert _titles(board_pin.open_board(lib, "beta", multi=True)) == {"beta"}
    assert not warnings


def test_one_warning_per_pin_and_board(lib, armed, warnings):
    """Monitors re-render every couple of seconds; the panel must not drown."""
    for _ in range(5):
        with board_pin.open_board(lib, "beta", multi=True) as _conn:
            pass
    assert len(warnings) == 1
    with board_pin.open_board(lib, "default", multi=True) as _conn:
        pass
    assert len(warnings) == 2, "a different board is a different conflict"


def test_resolve_board_db_path_names_the_requested_board(lib, armed, warnings):
    """The PATH view (``autodown.board_db_paths`` / the waker) gets the same
    protection: the file it would open is the board it asked for."""
    path = board_pin.resolve_board_db_path(lib, "beta")
    assert path is not None and path.endswith(
        os.path.join("boards", "beta", "kanban.db")), path
    assert armed not in path
    assert len(warnings) == 1
    with pytest.raises(board_pin.BoardPinConflict):
        board_pin.resolve_board_db_path(lib, "beta", mode="write")


def test_resolve_board_db_path_without_a_pin_is_the_plain_resolver(
        lib, monkeypatch):
    monkeypatch.delenv(board_pin.PIN_ENV, raising=False)
    assert board_pin.resolve_board_db_path(lib, "beta") == str(
        lib.kanban_db_path(board="beta"))


def test_resolve_board_db_path_on_a_legacy_lib_returns_none():
    assert board_pin.resolve_board_db_path(_NoResolverLib(), "beta") is None


# --------------------------------------------------------------------------- #
# real hermes: the precedence being defeated is hermes' own
# --------------------------------------------------------------------------- #
#
# Which resident ``hermes_cli`` counts as a planted fake is decided by the ONE
# predicate — ``hermes_cli_plant``, shared with conftest and ``test_autodown``
# (t_b62da70c). This file used to keep a private substring rule of its own
# (``_PLANTED_MARKER``) and that was the third definition of the same hazard.
# What IS this file's own business, and why it calls ``evict_planted`` instead
# of leaning on conftest's autouse fixture, is the BORROW: ``real_root`` needs
# the real resolver for one test, so it takes the planted name for the length of
# the fixture and hands it back in ``finally`` — the stub belongs to the sibling
# file that planted it, and an unreturned borrow is exactly the cross-file
# corruption PR #270 was written against. Without the borrow the failure is
# loud but misleading: a later real ``hermes_cli.kanban_db_boards._kb`` stays
# bound to the fake and ``create_board`` dies with ``AttributeError: module
# 'hermes_cli.kanban_db' has no attribute '_require_slug'``, which reads like a
# hermes bug rather than a fixture bug.


def _real_kanban_db():
    """The real ``hermes_cli.kanban_db`` if the name currently holds it, else None."""
    from hscc_daemon import autodown
    kb = autodown._load_kanban_db_or_default()
    if kb is None:
        return None
    if hasattr(kb, "create_board") and hasattr(kb, "kanban_db_path"):
        return kb
    return None


def test_the_evict_helper_hands_the_module_name_back(monkeypatch):
    """Non-vacuity for the borrow: ``hermes_cli.kanban_db`` must come back.

    Without this the eviction is a quiet way of corrupting whichever sibling
    test planted the stub, and the corruption would surface as somebody else's
    order-dependent failure — the exact class of bug being worked around here.

    The stub's ``__file__`` is built under the temp root the SHARED predicate
    recognises, and that is asserted before anything else: the point is that
    this file no longer has a rule of its own to satisfy. (It used to hardcode a
    ``/tmp`` path that matched only its own private substring, and would not
    have looked planted to conftest at all.)
    """
    import types
    planted = types.ModuleType("hermes_cli.kanban_db")
    planted.__file__ = os.path.join(tempfile.gettempdir(), "hscc-planted-sim",
                                    "hermes_cli", "kanban_db.py")
    assert is_planted_path(planted.__file__), (
        "the stub does not look planted to the one predicate: "
        + planted.__file__)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", planted)

    evicted = evict_planted()
    assert evicted.get("hermes_cli.kanban_db") is planted, "the stub was not evicted"
    assert "hermes_cli.kanban_db" not in sys.modules, "it is still in the import cache"
    restore_planted(evicted)
    assert sys.modules["hermes_cli.kanban_db"] is planted, "the stub was not restored"


def _seed_board(kb, root, slug):
    """Create one board the way hermes lays it out, without hermes' board API.

    Deliberately not ``create_board``: that goes through
    ``hermes_cli.kanban_db_boards -> _kb.init_db`` and in a process where a
    sibling file has already planted a stub ``hermes_cli.kanban_db`` the two
    sides of hermes end up holding *different* module objects for the same name,
    which fails inside hermes with ``AttributeError`` on a perfectly real
    function. Passing ``db_path`` explicitly keeps every path decision with the
    resolver we are actually testing (``kanban_db_path``), and the ``board.json``
    is written the same way ``conftest.hermes_kanban_root`` writes it.
    """
    import json
    import time
    path = kb.kanban_db_path(board=slug) if slug else kb.kanban_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    kb.init_db(path)                            # real schema, explicit path
    if slug:
        (path.parent / "board.json").write_text(json.dumps({
            "slug": slug, "kind": "board", "created_at": int(time.time()),
            "archived": False,
        }))
    return path


@pytest.fixture
def real_root(tmp_path, monkeypatch):
    """A hermetic multi-board root via ``HERMES_KANBAN_HOME`` + real hermes.

    A real ``pytest.skip`` (GAP-D-1) when hermes_cli cannot be imported in this
    interpreter, or when the name cannot be made real even after the planted
    stubs are evicted — so the unit family still runs on a bare venv and a skip
    always says why.

    Everything goes through ``kb`` — the module autodown resolved — and NEVER
    through a direct ``import hermes_cli.<submodule>``: importing a real hermes
    submodule directly binds its ``_kb`` to whatever happens to be cached, which
    is how this fixture used to error in the full leg. The borrow (``evict_planted``
    — the shared predicate's, not a copy) is bounded by ``finally`` so the sibling
    that planted the stub still owns it when we are done.
    """
    from hscc_daemon import autodown
    kb = _real_kanban_db()
    evicted = {} if kb is not None else evict_planted()
    try:
        if kb is None:
            kb = _real_kanban_db()
        if kb is None:
            pytest.skip("real hermes_cli not importable in this interpreter: "
                        f"{autodown.kanban_check_state()!r}")
        root = tmp_path / "kanban-home"
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
        monkeypatch.delenv(board_pin.PIN_ENV, raising=False)
        monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
        for v in FENCE_VARS:
            monkeypatch.delenv(v, raising=False)
        # Created while the fence is still off: under an armed fence hermes opens
        # ``?mode=ro`` and refuses to create a missing board
        # (scripts/run_tests.sh:121-130 documents exactly that), so a fixture
        # that left the files to be created later would make the collect pass
        # report an unreadable board instead of the thing under test.
        try:
            for slug in ("alpha", "beta"):
                _seed_board(kb, root, slug)
            _seed_board(kb, root, "")           # the legacy flat ``default``
        except AttributeError as exc:
            # Coherence probe, not a swallowed failure. Once a sibling file has
            # planted a stub ``hermes_cli.kanban_db`` in this process, evicting
            # it can leave hermes' own modules holding *different* objects under
            # the same name, and a perfectly real hermes function then raises
            # ``AttributeError`` — a broken import cache we do not own and must
            # not mutate further (that is how the next test's fake gets
            # corrupted, i.e. the t_2be3b04e hazard from the other side). Loud
            # skip under -rs, and the pair runs for real in any clean process:
            # ``pytest hscc_daemon/tests/test_board_pin.py`` -> 17 passed.
            pytest.skip("real hermes_cli is incoherent in this process after a "
                        f"sibling test planted a stub under that name: {exc!r} "
                        "(runs clean in a fresh process; see t_2be3b04e)")
        yield root, kb
    finally:
        restore_planted(evicted)


def _seed(kb, slug, task_id, title):
    """Insert one card into a fixture board (fence still unset here)."""
    with kb.connect_closing(board=slug) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_by, "
            "created_at, workspace_kind) VALUES (?, ?, 'ready', 0, 'test', ?, "
            "'scratch')", (task_id, title, NOW))
        conn.commit()


def test_real_hermes_pin_outranks_board_and_the_guard_reads_the_named_one(
        real_root, monkeypatch, warnings):
    """The measured bug, then the fix, against the real resolver."""
    root, kb = real_root
    _seed(kb, "alpha", "t_alpha", "on-alpha")
    _seed(kb, "beta", "t_beta", "on-beta")
    pin = str(kb.kanban_db_path(board="alpha"))
    # Arm the worker state only now, after the fixtures are written.
    monkeypatch.setenv(board_pin.PIN_ENV, pin)
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(root))

    # 1. non-vacuity: hermes really does answer with the pinned board.
    assert _titles(kb.connect_closing(board="beta")) == {"on-alpha"}
    # 2. the guard: same env, the board we asked for, and a loud warning.
    assert _titles(board_pin.open_board(kb, "beta", multi=True)) == {"on-beta"}
    text = _warn_text(warnings)
    assert pin in text and "beta" in text, text


def test_real_hermes_monitor_pass_reports_every_board_not_the_pinned_one(
        real_root, monkeypatch, warnings):
    """The surface the QA gate hit: ``kanban_live.collect`` over every board.

    Without the guard the pass reads alpha once per slug (and the panel looks
    like a complete fleet). With it, both boards appear with their own cards —
    so the fleet-wide claim the panel makes is actually backed.
    """
    from hscc_daemon import kanban_live
    root, kb = real_root
    _seed(kb, "alpha", "t_alpha", "alpha-card")
    _seed(kb, "beta", "t_beta", "beta-card")
    monkeypatch.setenv(board_pin.PIN_ENV, str(kb.kanban_db_path(board="alpha")))
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(root))

    data = kanban_live.collect(kanban_db=kb, now=NOW, read_caps=lambda: (6, 2))
    assert data["errors"] == []
    by_board = {b["name"]: b["counts"] for b in data["boards"]}
    assert {"alpha", "beta"} <= set(by_board), sorted(by_board)
    assert by_board["alpha"].get("ready") == 1
    assert by_board["beta"].get("ready") == 1, (
        f"beta was re-read as alpha: {by_board}")
    assert {r["title"] for r in data["ready"]} == {"alpha-card", "beta-card"}
    text = _warn_text(warnings)
    assert "beta" in text, f"the override must be visible: {text}"


# --------------------------------------------------------------------------- #
# dispatcher_wedge._capped_assignees — a `default` read is a guarded read too
# (t_147201fa, item 2)
# --------------------------------------------------------------------------- #

def _cap_conn(runs):
    """One ``tasks`` table holding ``runs`` = [(id, assignee)], all running —
    the shape ``_capped_assignees`` counts."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, "
              "assignee TEXT)")
    for tid, assignee in runs:
        c.execute("INSERT INTO tasks (id, status, assignee) VALUES (?, "
                  "'running', ?)", (tid, assignee))
    c.commit()
    return c


class _CapLib:
    """Pin-aware lib for the wedge's per-profile-cap probe.

    Same measured precedence rule as :class:`_PinAwareLib`, but with the columns
    ``_capped_assignees`` selects. The two boards are deliberately answerable
    apart: ``alpha`` has two running cards for ``other``, ``default`` two for
    ``worker``, so the assignee set the probe returns states which file it
    actually read — the observable, not an internal flag.
    """

    def __init__(self, root, boards):
        self.root = str(root)
        self.slugs = list(boards)
        self.connects = []
        self._by_path = {str(self._canonical(s)): _cap_conn(rows)
                         for s, rows in boards.items()}

    def _canonical(self, slug):
        if slug in (None, "default"):
            return pathlib.Path(self.root) / "kanban.db"
        return (pathlib.Path(self.root) / "kanban" / "boards" / str(slug)
                / "kanban.db")

    def _fenced(self):
        return any((os.environ.get(v) or "").strip() for v in FENCE_VARS)

    def list_boards(self):
        return [{"slug": s} for s in self.slugs]

    def kanban_db_path(self, board=None):
        pin = (os.environ.get(board_pin.PIN_ENV) or "").strip()
        if pin and (board is None or self._fenced()):
            return pathlib.Path(pin)
        return self._canonical(board)

    def connect_closing(self, board=None):
        path = str(self.kanban_db_path(board=board))
        self.connects.append((board, path, os.environ.get(board_pin.PIN_ENV)))
        return _Ctx(self._by_path[path])


def _cap_fleet(root):
    return _CapLib(root, {
        "alpha": [("a1", "other"), ("a2", "other")],
        "default": [("d1", "worker"), ("d2", "worker")],
    })


def _arm_worker(monkeypatch, pin):
    monkeypatch.setenv(board_pin.PIN_ENV, pin)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)


def test_wedge_capped_assignees_on_default_is_not_steered_by_the_pin(
        tmp_path, monkeypatch, warnings):
    """The residual the card names: ``multi`` came from the SLUG, so a
    ``default`` read took the no-board path a pin still steers.

    ``multi`` is a property of the lib (``autodown.py:310``), so the fix is to
    stop guessing it from the slug — then ``default`` is resolved and guarded
    exactly like every other board this gate reads.
    """
    from hscc_daemon import dispatcher_wedge as dw
    lib = _cap_fleet(tmp_path / "kanban-home")
    pin = str(lib._canonical("alpha"))
    _arm_worker(monkeypatch, pin)

    # 1. non-vacuity — the shape this change replaced: with multi False a
    #    `default` read answered with the PINNED board's assignees, and the lib
    #    saw the pin on that call.
    with board_pin.open_board(lib, "default", multi=False) as conn:
        assert {r[0] for r in conn.execute("SELECT assignee FROM tasks")} == {
            "other"}, "the fixture stopped reproducing the residual"
    assert lib.connects[-1] == (None, pin, pin), lib.connects

    # 2. the fix — same armed env, same question, default's own rows, and the
    #    override stated out loud. The pin column is None because the guard took
    #    it out of the environment for the duration of this open only: that is
    #    the scrub, observed rather than asserted as a flag.
    assert dw._capped_assignees(lib, "default", 2) == {"worker"}
    text = _warn_text(warnings)
    assert "default" in text, f"the warning must name the board asked for: {text}"
    assert pin in text, f"the warning must name the pin it overrode: {text}"
    assert lib.connects[-1] == ("default", str(lib._canonical("default")), None), (
        f"the guard must open the file it was asked for: {lib.connects}")
    assert os.environ[board_pin.PIN_ENV] == pin, "the pin must come back verbatim"


def test_wedge_capped_assignees_legacy_shape_keeps_the_no_board_call(
        monkeypatch, warnings):
    """Companion: a lib that cannot enumerate boards (``multi`` False by the
    lib-capability rule) must still get the no-``board=`` call verbatim — the
    legacy single-board path, pin honoured, nothing said."""
    from hscc_daemon import dispatcher_wedge as dw
    calls = []

    class _LegacyNoEnum:
        def connect_closing(self, board=None):
            calls.append(board)
            return _Ctx(_cap_conn([("d1", "worker"), ("d2", "worker")]))

    _arm_worker(monkeypatch, "/elsewhere/pinned/kanban.db")
    assert dw._capped_assignees(_LegacyNoEnum(), "default", 2) == {"worker"}
    assert calls == [None], "the legacy shape must keep the no-board call"
    assert not warnings, _warn_text(warnings)


def test_wedge_capped_assignees_without_a_pin_passes_board_through(
        tmp_path, monkeypatch, warnings):
    """Companion for the daemon/operator environment: no pin → the probe just
    passes ``board=`` through and resolves the same file its sibling probes do
    for the same board (which the slug heuristic used to break)."""
    from hscc_daemon import dispatcher_wedge as dw
    monkeypatch.delenv(board_pin.PIN_ENV, raising=False)
    for v in FENCE_VARS:
        monkeypatch.delenv(v, raising=False)
    lib = _cap_fleet(tmp_path / "kanban-home")
    assert dw._capped_assignees(lib, "default", 2) == {"worker"}
    assert lib.connects == [("default", str(lib._canonical("default")), None)], (
        lib.connects)
    assert not warnings, _warn_text(warnings)
    assert dw._capped_assignees(lib, "alpha", 2) == {"other"}
    assert lib.connects[-1][0] == "alpha"
