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

import pytest

from hscc_daemon import board_pin

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

@pytest.fixture
def real_root(tmp_path, monkeypatch):
    """A hermetic multi-board root via ``HERMES_KANBAN_HOME`` + real hermes.

    A real ``pytest.skip`` (GAP-D-1) when hermes_cli cannot be imported in this
    interpreter, so the unit family still runs on a bare venv.
    """
    from hscc_daemon import autodown
    kb = autodown._load_kanban_db_or_default()
    if kb is None:
        pytest.skip("real hermes_cli not importable: "
                    f"{autodown.kanban_check_state()!r}")
    import importlib
    boards_mod = None
    try:
        boards_mod = importlib.import_module("hermes_cli.kanban_db_boards")
    except Exception as exc:  # pragma: no cover — hermes layout changed
        pytest.skip(f"hermes_cli.kanban_db_boards unavailable: {exc}")
    if boards_mod is None or not hasattr(boards_mod, "create_board"):
        pytest.skip("this hermes has no create_board to build fixtures with")
    root = tmp_path / "kanban-home"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.delenv(board_pin.PIN_ENV, raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    for v in FENCE_VARS:
        monkeypatch.delenv(v, raising=False)
    for slug in ("alpha", "beta"):
        boards_mod.create_board(slug)          # tmp root, fence unset
    # The legacy flat ``default`` DB too, created while the fence is still off:
    # under an armed fence hermes opens ``?mode=ro`` and refuses to create a
    # missing board (scripts/run_tests.sh:121-130 documents exactly that), so a
    # fixture that skipped it would make the collect pass report an unreadable
    # board instead of the thing under test.
    with kb.connect_closing() as _conn:
        assert _conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    return root, kb


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
