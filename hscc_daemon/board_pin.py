"""The board-pin guard: never read (or write) a board silently different from the
one a caller named.

Why this module exists
----------------------
A dispatched kanban worker shell carries ``HERMES_KANBAN_DB`` (the dispatcher
injects it), and hermes resolves *that env pin ahead of an explicit* ``board=``
argument whenever a worker/delegation fence is active
(``hermes_cli/kanban_db.py:540-555`` ``_board_path()`` returns ``Path(pin)`` when
``_explicit_board_intent_pinned()`` — see also ``kanban_db.py:522`` and
``agent/delegation_context.py:76-93``). The fence is deliberate — the pin is part
of "workers physically cannot see other boards" — but it is invisible to a
programmatic caller, and ``list_boards()`` is *not* affected by it. So a fleet
reader inside a worker shell enumerates all 24 boards and opens the SAME pinned
DB 24 times, then renders a well-formed panel (populated rows, ``errors: []``)
whose numbers describe one board. Measured in t_b014c2b8 and written up in
``docs/audits/board-pin-t_b014c2b8.md``.

What it does
------------
Two shapes, split by access mode, both *loud*:

* ``mode="read"`` (monitors, stale lists, idle probes, path views) — emits one
  warning per process naming the pin value, the board we asked for and both
  resolved paths, then opens with ``HERMES_KANBAN_DB`` removed from
  ``os.environ`` for the duration of our own open, so hermes resolves the board
  the caller actually named. This is the "scrub for our own store opens" shape.
* ``mode="write"`` (archive / unblock / card creation) — raises
  :class:`BoardPinConflict` naming both values **before** any write. A reader
  that silently reads one board misinforms; a writer that silently writes one
  board mutates the wrong store (task ids are per-board, so
  ``archive_stale_task``/``recover_blocked_task`` scanning under a pin can hit a
  different board's card with the same id). Scrubbing a write would hand a worker
  shell cards on boards it does not own, so writes keep the fence and the
  operator gets an actionable error instead.

Nothing here changes hermes' precedence: we never patch its resolver and never
argue with it — we either take the pin out of our own environment or refuse.

How conflict is detected (no re-derived path rules)
---------------------------------------------------
``pin_conflict`` asks hermes' own ``kanban_db_path(board=X)`` twice — once with
the pin visible, once with it scrubbed — and reports a conflict only when the two
answers differ. The repo's standing contract that "hscc never re-derives a
path" (``autodown.board_db_paths``, ``autodown.py:338-342``) therefore holds:
both sides of the comparison are hermes' answers, and the guard stays correct if
hermes later changes its precedence rules.

Consequences worth stating, because they bound how aggressive this may be:

* **No conflict is reported when the pin already IS the board we asked for** —
  the ordinary single-board worker case, where the fence is doing its job and
  nothing is being hidden. Same when no pin is set, when no fence marker is set
  (then hermes already honours our ``board=``), when ``board`` is ``None``
  (the caller expressed no intent, so the pin is the answer), and when the lib
  predates ``kanban_db_path``. Those all fall through to the untouched code path.
* **The mutation half of the fence is not weakened.** Under the delegated-child
  marker hermes still opens every board ``?mode=ro``
  (``kanban_db_connect.py:713-727``, ``kanban_path_is_fenced``), and every write
  surface keeps the fence by using ``mode="write"``.
* ``os.environ`` is process-global, so the scrub window is a thread-safety
  hazard in principle. It is bounded to our own ``connect_closing`` + query, and
  every caller in this repo is a single-threaded loop or a one-shot CLI. A
  multi-threaded consumer must pass its own isolation instead of relying on this.
"""

from __future__ import annotations

import contextlib
import os

#: The env var the dispatcher injects into a worker shell, and the one hermes
#: resolves ahead of an explicit ``board=`` under a fence.
PIN_ENV = "HERMES_KANBAN_DB"

#: Env vars that switch hermes' explicit-``board=`` intent into "resolve through
#: the pin" (``agent/delegation_context.py:76-93``). Read only to name the
#: trigger in the message — the guard never branches on which one fired.
FENCE_ENVS = ("HERMES_KANBAN_TASK", "HERMES_DELEGATED_CHILD_CONTEXT")

#: Dedupe key -> True. One warning per (pin, board) per process: the monitors
#: re-render every couple of seconds and the daemon loop runs every ~30 s, so an
#: unthrottled warning would bury the panel. Injectable via ``warned=``.
_WARNED: set = set()


class BoardPinConflict(RuntimeError):
    """A write was refused because ``HERMES_KANBAN_DB`` pins a different board
    than the caller named. Both values are named in the message."""


class PinConflict:
    """The measured disagreement between the pin and the caller's ``board=``.

    Every field is a value hermes itself produced or the env verbatim — nothing
    here is inferred from path shapes, so a hermes path-rule change cannot make
    the guard lie.
    """

    __slots__ = ("board", "pin", "pinned_path", "board_path", "fence")

    def __init__(self, board, pin, pinned_path, board_path, fence):
        self.board = board            # the slug the caller asked for
        self.pin = pin                # HERMES_KANBAN_DB, verbatim
        self.pinned_path = pinned_path  # what hermes resolves board= to today
        self.board_path = board_path    # what it resolves to without the pin
        self.fence = fence            # which fence env vars are set

    def fences_named(self) -> str:
        return " + ".join(self.fence) if self.fence else "(no fence marker set)"

    def read_warning(self) -> str:
        """The observable for the read path: names both values, both resolved
        paths, the trigger and what we are about to do about it."""
        return (
            f"kanban board-pin conflict: {PIN_ENV}={self.pin!r} outranks the "
            f"explicit board={self.board!r} this caller asked for "
            f"(trigger: {self.fences_named()}). Hermes would open "
            f"{self.pinned_path!r} — the pinned board — and report it as "
            f"{self.board!r}. Reading {self.board_path!r} instead: {PIN_ENV} is "
            f"unset for the duration of this read only, then restored. "
            f"Writes are NOT scrubbed."
        )

    def write_error(self) -> str:
        """The observable for the write path: both values named, plus the way out."""
        return (
            f"refusing to write kanban board {self.board!r}: {PIN_ENV}="
            f"{self.pin!r} pins another board and outranks the explicit board= "
            f"under a worker/delegation fence (trigger: "
            f"{self.fences_named()}), so this write would land on "
            f"{self.pinned_path!r} while reporting board {self.board!r}. "
            f"Unset {PIN_ENV} (or run outside the worker shell) to write "
            f"{self.board_path!r}."
        )


def _fences(env) -> tuple:
    return tuple(n for n in FENCE_ENVS if (env.get(n) or "").strip())


@contextlib.contextmanager
def _scrubbed_pin(env):
    """Remove ``PIN_ENV`` from ``env`` for the duration of the block.

    Restored verbatim on exit, including when the body raises. Deliberately
    restores the *previous value*, not merely "unset", so a caller that had a
    pin of its own is unaffected by our read.
    """
    had = PIN_ENV in env
    saved = env.get(PIN_ENV)
    env.pop(PIN_ENV, None)
    try:
        yield
    finally:
        if had:
            env[PIN_ENV] = saved
        else:
            env.pop(PIN_ENV, None)


def pin_conflict(kanban_db, board, *, env=None):
    """``PinConflict`` when the env pin silently outranks ``board``, else None.

    Detection is hermes' own resolver asked twice — with the pin visible and with
    it scrubbed — so no hscc code re-derives a board path. A lib without
    ``kanban_db_path`` (a minimal injected stub, or a hermes predating it) cannot
    be interrogated and returns None: the guard stays out of the way rather than
    guessing.
    """
    env = os.environ if env is None else env
    board = "" if board is None else str(board)
    pin = (env.get(PIN_ENV) or "").strip()
    resolve = getattr(kanban_db, "kanban_db_path", None)
    if not board or not pin or resolve is None:
        return None
    try:
        with_pin = str(resolve(board=board))
        with _scrubbed_pin(env):
            unpinned = str(resolve(board=board))
    except Exception:
        # A resolver that raises is not a conflict signal — and the caller's own
        # open will surface the real problem. Never let the guard invent one.
        return None
    if with_pin == unpinned:
        # The pin already IS the board we asked for (the ordinary worker case),
        # or hermes honoured board= because no fence is active. Nothing to say.
        return None
    return PinConflict(board, pin, with_pin, unpinned, _fences(env))


def _warn_route(message):
    """This module's ONE nameable WARN route — the only call :func:`silent_read` mutes.

    An indirection rather than a bare ``log(...)`` at the call site for the same
    reason ``autodown._kanban_warn`` is one (PR #723): the seam a borrowed read must
    not speak through has to be nameable, so a door can borrow exactly this route and
    nothing else. Deferred import kept: the guard must never become a reason a read
    cannot open a board.
    """
    from hscc_daemon.daemon_ops import log
    log(message, "WARN")


#: Messages a muted read already echoed in THIS process. Separate from ``_WARNED``
#: on purpose: ``_WARNED`` is the once-per-(pin, board) *file* alarm budget and a
#: read may not spend it (see :func:`silent_read`), while a ``--live`` monitor
#: re-enters a muted window every few seconds and must not reprint one line per
#: tick into the operator's terminal.
_MUTED_ECHOED: set = set()


def _echo_muted_warn(message):
    """The muted route: the operator's terminal, never the log file.

    Deliberately NOT a silent stub. ``board_pin``'s conflict has no payload
    anywhere — no door publishes a ``PinConflict`` (``kanban_live.py:404`` names
    THIS warning as the operator's notice) — so muting the fact as well as the file
    would turn a loud operator misconfiguration into a silent one. ``stderr`` is not
    the invariant; the file is (t_88abe739). And unlike ``daemon_ops.log``'s echo
    this one is not gated on the daemon's pid file (``daemon_ops.py:630``), so a CLI
    read on a host with a live daemon gets *more* notice than before, not less.
    """
    if message in _MUTED_ECHOED:
        return
    _MUTED_ECHOED.add(message)
    try:
        import sys
        print(f"[WARN] {message}", file=sys.stderr)
    except Exception:  # noqa: BLE001 — logging must never break the read
        pass


@contextlib.contextmanager
def silent_read():
    """Borrow this module's WARN route for one documented read, then give it back.

    Inside the window :func:`_warn_route` is :func:`_echo_muted_warn`, so the
    once-per-process pin warning a read emits goes to stderr instead of being
    appended to ``$HSCC_HOME/daemon.log`` — the door's read creates no file. Used by
    the documented read doors (``kanban_blocked.list_blocked_tasks``,
    ``kanban_cli._cmd_stale`` / ``_cmd_monitor``, ``cards_live.Monitor``,
    ``autodown_cli._cmd_status``), never by a write or a daemon loop: a
    ``mode="write"`` conflict raises ``BoardPinConflict`` and never reaches
    :func:`_emit`, so what a write path owns is untouched.

    Restoring the budget is the half that keeps this honest. ``_emit`` adds the key
    to ``_WARNED`` *before* it logs, so a caller that muted only the route would also
    silently spend the process's single warning per (pin, board): a long-lived
    process importing a read door would then never tell its operator about the
    conflict from a path that does own the file. The read is allowed to be quiet; it
    is not allowed to spend somebody else's alarm (the rule t_2c218de8's fix shipped
    with, and the reason ``autodown.silent_read`` restores ``_KANBAN_LOAD`` too).

    Scoped to the ONE call and undone in ``finally``. It mutates module globals, so
    a process with concurrent logging threads would see the window — the CLI doors
    this guards are one-shot processes (``--live`` is one long read, and
    ``_MUTED_ECHOED`` is what keeps that shape from spamming).
    """
    global _warn_route
    real_route = _warn_route
    saved = set(_WARNED)
    _warn_route = _echo_muted_warn
    try:
        yield
    finally:
        _warn_route = real_route
        _WARNED.clear()
        _WARNED.update(saved)


def _emit(message, warned, key):
    """Warn once per key. Emission goes through :func:`_warn_route` (daemon log
    file + stderr echo) — never stdout, which is the machine channel for every
    HSCC CLI (``--json`` is a raw dump there; see ``daemon_ops.log`` docstring
    and the t_18318fd9 JSON-corruption incident). Logging must never break the
    read, so any failure is swallowed. A documented read door wraps its call in
    :func:`silent_read`, which routes the same warning to stderr instead."""
    if key in warned:
        return
    warned.add(key)
    try:
        _warn_route(message)
    except Exception:  # noqa: BLE001
        try:
            import sys
            print(f"[WARN] {message}", file=sys.stderr)
        except Exception:  # pragma: no cover — last-resort echo
            pass


def guard_board(kanban_db, board, *, mode="read", env=None, warned=None):
    """Apply the guard and return the ``PinConflict`` it acted on (or None).

    ``mode="read"`` warns once and leaves the caller in scrubbed-pin state only
    if the caller also enters :func:`scrub_for_read`; this function alone does
    *not* mutate the environment — it is the decision + the loud part. Use
    :func:`open_board` / :func:`resolve_board_db_path`, which pair the decision
    with the action, instead of calling this directly.
    """
    conflict = pin_conflict(kanban_db, board, env=env)
    if conflict is None:
        return None
    if mode == "write":
        raise BoardPinConflict(conflict.write_error())
    _emit(conflict.read_warning(), _WARNED if warned is None else warned,
          f"read|{conflict.pin}|{conflict.board}")
    return conflict


@contextlib.contextmanager
def open_board(kanban_db, board, *, multi=True, mode="read", env=None,
               warned=None):
    """Yield a connection to exactly ``board``, guarding the env pin.

    Drop-in for ``with kanban_db.connect_closing(board=b) as conn:``. The
    ``multi`` flag is autodown's ``_enum_board_names`` answer: when it is False
    the lib is the documented single-board shape and the caller passed no
    ``board=`` at all, so there is no intent to defend and the pin is honoured —
    that path is byte-for-byte the old code. Injected test libs keep working:
    the scrub only touches the environment, never the lib's signature.
    """
    env = os.environ if env is None else env
    if not multi or board is None:
        with kanban_db.connect_closing() as conn:
            yield conn
        return
    conflict = guard_board(kanban_db, board, mode=mode, env=env, warned=warned)
    if conflict is None:
        with kanban_db.connect_closing(board=board) as conn:
            yield conn
        return
    with _scrubbed_pin(env), kanban_db.connect_closing(board=board) as conn:
        yield conn


def open_board_ro(kanban_db, board, *, multi=True, env=None, warned=None):
    """A read-only connection to exactly ``board``, without ``hermes_state``.

    :func:`open_board` goes through ``kanban_db.connect_closing()``, the
    write-capable door whose writability preflight imports ``hermes_state``.
    The brew libexec venv does not ship that module, so a listing run as
    ``/opt/homebrew/bin/hscc kanban blocked`` / ``kanban stale`` failed every
    board with ``No module named 'hermes_state'``. Listings only SELECT, so
    they open the file hermes resolves for ``board`` with ``mode=ro`` (the
    same path :func:`resolve_board_db_path` guards). A lib that cannot answer
    the path (an injected fake, an older Hermes) falls back to
    :func:`open_board` unchanged.
    """
    if multi and board is not None:
        try:
            path = resolve_board_db_path(kanban_db, board, env=env,
                                         warned=warned)
        except Exception:  # noqa: BLE001 — resolver failure = use the old door
            path = None
        if path:
            import sqlite3
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
            conn.row_factory = sqlite3.Row
            return contextlib.closing(conn)
    return open_board(kanban_db, board, multi=multi, env=env, warned=warned)


def resolve_board_db_path(kanban_db, board, *, mode="read", env=None,
                          warned=None):
    """Path view of the same guard: the DB file hermes resolves for ``board``,
    with the pin removed when it would outrank the caller.

    For consumers that must open the file themselves (``orch_waker`` opens
    ``file:...?mode=ro`` URIs and may not touch a board, so it cannot use
    ``connect_closing``) and for ``autodown.board_db_paths``, which is the repo's
    single PATH enumeration. Returns None when the resolver cannot answer,
    letting the caller fall back to whatever hermes put on the board entry.
    """
    env = os.environ if env is None else env
    conflict = guard_board(kanban_db, board, mode=mode, env=env, warned=warned)
    resolve = getattr(kanban_db, "kanban_db_path", None)
    if resolve is None:
        return None
    if conflict is None:
        return str(resolve(board=board))
    with _scrubbed_pin(env):
        return str(resolve(board=board))
