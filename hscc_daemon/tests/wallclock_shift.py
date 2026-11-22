"""Move the wall clock for a whole pytest session — the audit tool this repo
now keeps instead of re-deriving (card t_ed416771).

WHAT IT DOES
  ``HSCC_WALL_SHIFT_DAYS=-30 pytest ...`` runs the suite as if the operator's
  clock read 30 days earlier. The shift is an OFFSET, never a freeze: every
  clock still advances in real time, so ``while time.time() < deadline`` loops
  terminate instead of hanging the leg.

WHAT IT MOVES, AND WHY EACH ONE IS NEEDED
  * ``time.time`` — the epoch seam (``dispatcher_wedge``, ``heal_lease``,
    ``stream_silence`` …).
  * ``sys.modules["datetime"]`` — the seam a *function-local* ``import
    datetime`` resolves against. ``state.now_iso()`` and
    ``update_check._utcnow()`` read their clock that way, so a per-module
    attribute swap leaves the canonical stamp WRITER on the real date while the
    test side moves: writer and reader then disagree by the shift, and a case
    that is honest about using the wall clock on both ends goes red for a reason
    that is not a defect. That desync produced four false findings before this
    line existed.
  * every repo module's own ``datetime`` binding — for the modules imported
    before the swap.

  ``time.monotonic`` is deliberately NOT moved: it measures elapsed intervals,
  not dates, and the timeouts built on it must keep working. A verdict that
  depends on monotonic is not a fixture-clock question.

WHY THE SUBCLASS LOOKS OVERKILL
  ``ShiftedDatetime`` is a ``datetime`` SUBCLASS, and the metaclass keeps
  ``isinstance(x, ShiftedDatetime)`` true for a REAL datetime. Without that, a
  module doing ``isinstance(value, datetime)`` after the swap rejects every
  plain datetime its test handed it (test-local ``from datetime import
  datetime`` imports are not patchable), falls through to its wall-clock
  fallback, and the probe reports an artifact as a defect. Same trick
  t_3c11f9d1 uses in ``test_node_pause_expiry.py``.

THE CONTROL IS NOT OPTIONAL
  ``check_control()`` asserts the shipped seams actually answer the shifted
  instant, and raises if they do not. A shift that silently fails to shift
  turns this whole tool into a green that means nothing — the exact failure mode
  this card is about. ``test_single_clock_rule.py`` runs the two-sided control
  (green with the plugin, red without it).

USAGE
  Automated by ``test_single_clock_rule.py``. By hand::

      HSCC_WALL_SHIFT_DAYS=-30 HSCC_REPO_ROOT=. \\
        python -m pytest hscc_daemon/tests -q -p wallclock_shift
"""

import datetime as _real_datetime
import os
import sys
import time

SHIFT_ENV = "HSCC_WALL_SHIFT_DAYS"
_real_time = time.time


def _shift_days():
    raw = (os.environ.get(SHIFT_ENV) or "").strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        raise AssertionError(
            f"{SHIFT_ENV}={raw!r} is not a number of days — refusing to run a "
            "clock-shift audit that shifted nothing")


SHIFT_DAYS = _shift_days()
_OFFSET = SHIFT_DAYS * 86400.0
_ACTIVE = _OFFSET != 0.0


def shifted_now_epoch():
    """Where the wall clock claims it is, in epoch seconds."""
    return _real_time() + _OFFSET


class _RealDatetimeStillMatches(type(_real_datetime.datetime)):
    """``isinstance(x, ShiftedDatetime)`` stays true for a REAL datetime."""

    #: Captured on the METACLASS, deliberately not read off a module global.
    #: ``_adopt()`` rebinds module globals — and this file is repo-owned, so it
    #: adopts ITSELF: the first run from inside the tree replaced this module's
    #: ``_real_datetime`` with the swapped one, the check then asked
    #: ``isinstance(x, ShiftedDatetime)``, that called this method again, and
    #: every case touching ``parse_until`` died with a RecursionError. A class
    #: attribute is not a module global, so adoption cannot move it.
    _REAL = _real_datetime.datetime

    def __instancecheck__(cls, instance):
        return isinstance(instance, cls._REAL)

    def __subclasscheck__(cls, subclass):
        return issubclass(subclass, cls._REAL)


class ShiftedDatetime(_real_datetime.datetime,
                      metaclass=_RealDatetimeStillMatches):
    """The wall clock, answering the shifted instant.

    A SUBCLASS so ``fromisoformat`` / ``astimezone`` / comparisons keep working
    — the point is to move one read (``now``), never to replace the type.
    """

    @classmethod
    def now(cls, tz=None):
        base = _real_datetime.datetime.fromtimestamp(shifted_now_epoch(),
                                                     _real_datetime.timezone.utc)
        return base if tz is None else base.astimezone(tz)

    @classmethod
    def utcnow(cls):
        return _real_datetime.datetime.fromtimestamp(
            shifted_now_epoch(), _real_datetime.timezone.utc).replace(
                tzinfo=None)


def _shifted_datetime_module():
    """Stand-in for ``import datetime`` users: same namespace, moved clock."""
    import types
    mod = types.ModuleType("datetime")
    mod.__dict__.update(_real_datetime.__dict__)
    setattr(mod, "datetime", ShiftedDatetime)   # noqa: B010 - type-checked attr
    return mod


_SHIFT_MODULE = _shifted_datetime_module()
_swapped = set()


def _adopt(mod):
    """Re-point one module's ``datetime`` bindings at the moved clock."""
    if mod is None or getattr(mod, "_hscc_wall_shifted", False):
        return
    for name, value in list(vars(mod).items()):
        if value is _real_datetime.datetime:
            setattr(mod, name, ShiftedDatetime)
            _swapped.add(mod.__name__)
        elif value is _real_datetime:
            setattr(mod, name, _SHIFT_MODULE)
            _swapped.add(mod.__name__)
    setattr(mod, "_hscc_wall_shifted", True)


def _repo_root():
    root = os.environ.get("HSCC_REPO_ROOT")
    if root:
        return os.path.realpath(root)
    # hscc_daemon/tests/wallclock_shift.py -> the repo root.
    return os.path.realpath(os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(
            __file__))))))


#: This tool and its guard must never adopt themselves: `_adopt()` compares
#: against `_real_datetime`, and rebinding this module's own globals would leave
#: those comparisons pointing at the swap (a silent under-reach for every module
#: imported afterwards). The metaclass capture above is the loud version of the
#: same protection; this is the quiet one.
_NEVER_ADOPT = {"wallclock_shift", "test_single_clock_rule"}


def _adopt_loaded():
    """Adopt every already-imported module that belongs to this repo.

    Test modules import under their bare names (``test_cron_watchers``), not
    under ``hscc_daemon.``, so a name-prefix scan leaves the test side on the
    real clock and turns this tool into a desync generator. Match by file, which
    also keeps pytest and site-packages out of it.
    """
    root = _repo_root()
    for name in list(sys.modules):
        if name in _NEVER_ADOPT:
            continue
        mod = sys.modules.get(name)
        path = getattr(mod, "__file__", None)
        if not path:
            continue
        if name.startswith("hscc_daemon") or os.path.realpath(path).startswith(
                root + os.sep):
            _adopt(mod)


def install():
    """Move the clock. Idempotent. A no-op when no shift was asked for."""
    if not _ACTIVE:
        return False
    time.time = shifted_now_epoch
    sys.modules["datetime"] = _SHIFT_MODULE
    _adopt_loaded()
    return True


def check_control():
    """Prove the shipped clock seams moved. Raises ``AssertionError`` if not.

    Two seams, because they are reached by two different mechanisms and a tool
    that only checked the easy one would have hidden the writer desync:
    ``state.now_iso()`` (a function-local ``import datetime``) and
    ``node_pauses._utc(None)`` (the overlay's no-clock fallback, the exact read
    that reddened ``test_node_pause_expiry.py``).
    """
    if not _ACTIVE:
        return
    from hscc_daemon import node_pauses, state
    want = shifted_now_epoch()
    problems = []
    try:
        got_stamp = _real_datetime.datetime.fromisoformat(
            state.now_iso()).timestamp()
        if abs(got_stamp - want) > 120:
            problems.append(
                f"state.now_iso() is {got_stamp - want:+.0f}s off the shifted "
                "clock (the function-local-import writer did not move)")
    except Exception as exc:                                  # noqa: BLE001
        problems.append(f"state.now_iso() control raised {exc!r}")
    try:
        got_utc = node_pauses._utc(None).timestamp()
        if abs(got_utc - want) > 120:
            problems.append(
                f"node_pauses._utc(None) is {got_utc - want:+.0f}s off the "
                "shifted clock (the overlay fallback did not move)")
    except Exception as exc:                                  # noqa: BLE001
        problems.append(f"node_pauses._utc(None) control raised {exc!r}")
    if problems:
        raise AssertionError(
            "HSCC_WALL_SHIFT_DAYS was asked for and the clock did NOT move: "
            + "; ".join(problems)
            + ". Every green from this run would be a green that means nothing.")


# ── pytest hooks (the plugin surface) ──────────────────────────────────────

def pytest_configure(config):
    install()


def pytest_collection_finish(session):
    if not _ACTIVE:
        return
    _adopt_loaded()          # modules imported during collection
    check_control()


def pytest_runtest_setup(item):
    if _ACTIVE:
        _adopt_loaded()      # lazily imported shipped modules


def pytest_report_header(config):
    if not _ACTIVE:
        return None
    return (f"[wallclock-shift] wall clock = real {'+' if SHIFT_DAYS > 0 else ''}"
            f"{SHIFT_DAYS} days; {len(_swapped)} modules adopted")


# ── runnable control (``python -m wallclock_shift with|without``) ──────────
# The two sides of ONE assertion, so the shift can never become a green that
# means nothing: `with` proves the machinery moves the shipped clock, `without`
# proves nothing else moved it either. A tool that only ran the first side would
# pass happily in an environment where the shift silently did nothing.

def _shipped_stamp_epoch():
    from hscc_daemon import state
    return _real_datetime.datetime.fromisoformat(state.now_iso()).timestamp()


def main(argv):
    mode = (argv[1] if len(argv) > 1 else "with").strip().lower()
    if os.environ.get(SHIFT_ENV, "").strip() in ("", "0"):
        print(f"{SHIFT_ENV} is unset — the control needs a shift to look for")
        return 2
    want = shifted_now_epoch()
    if mode == "with":
        install()
        check_control()
        print("wall clock moved: state.now_iso() answers the shifted instant "
              f"({_shipped_stamp_epoch() - want:+.0f}s off its own target)")
        return 0
    if mode == "without":
        drift = _shipped_stamp_epoch() - want
        if abs(drift) < 120:
            print("the shipped clock reads the SHIFTED instant even with the "
                  "machinery absent — the environment is already shifted, so "
                  "this control cannot see anything")
            return 1
        print(f"machinery absent: state.now_iso() is {drift:+.0f}s off the "
              "shifted clock (as it must be)")
        return 0
    print(f"unknown mode {mode!r}; expected 'with' or 'without'")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
