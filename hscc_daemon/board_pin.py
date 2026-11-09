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


def _emit(message, warned, key):
    """Warn once per key. Emission goes through ``daemon_ops.log`` (daemon log
    file + stderr echo) — never stdout, which is the machine channel for every
    HSCC CLI (``--json`` is a raw dump there; see ``daemon_ops.log`` docstring
    and the t_18318fd9 JSON-corruption incident). Logging must never break the
    read, so any failure is swallowed."""
    if key in warned:
        return
    warned.add(key)
    try:
        from hscc_daemon.daemon_ops import log
        log(message, "WARN")
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
