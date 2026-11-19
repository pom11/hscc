"""The main orchestrator's kanban board (M3, t_7b55db57).

Bootstrap creates the board named by :func:`hscc_roles.main_orch.main_board`
and makes it the machine-wide current board, so a card created without an
explicit ``board=`` lands on the main orchestrator's board instead of whichever
project board was switched to last. The legacy ``default`` board is untouched:
it keeps every card it already holds.

Two surfaces:

* :func:`ensure` — the bootstrap stage. ``hermes kanban boards create`` is
  idempotent (an existing board is reported, never recreated), then
  ``hermes kanban boards switch`` writes the current pointer. Both go through
  the Hermes CLI, never a hand-rolled write of ``kanban/current``.
* :func:`doctor_check` — the read-only doctor row. It compares
  ``kanban_db.get_current_board()`` with the resolver's board and returns the
  five-field Check dict; drift is a WARNING (fix with one ``boards switch``),
  never a reason to abort an install.

No live state is written by the check. Tests drive both surfaces with an
injected runner / kanban library, so no suite needs the operator's boards.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

#: The doctor row's printed name. Kept here (not only in doctor.py) so the
#: verdict and the row cannot drift; doctor.py carries a fallback constant for
#: the case where this module itself is not deployable.
CHECK_NAME = "kanban current board (main orchestrator)"

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_ROLES_DIR = os.path.join(_REPO_ROOT, "hscc-roles")


def _load_main_orch():
    """Import ``hscc-roles/main_orch.py`` (the one identity resolver)."""
    if _ROLES_DIR not in sys.path:
        sys.path.insert(0, _ROLES_DIR)
    import main_orch  # noqa: PLC0415 — sibling plugin, path added above
    return main_orch


def main_board(hscc_dir=None) -> str:
    """The board slug bootstrap must create and switch to."""
    return _load_main_orch().main_board(hscc_dir)


def _hermes_root() -> str:
    """The Hermes install root from ``HERMES_HOME`` (profile-scoped aware)."""
    home = (os.environ.get("HERMES_HOME")
            or os.path.join(os.path.expanduser("~"), ".hermes"))
    home = os.path.expanduser(home)
    if os.path.basename(os.path.dirname(home)) == "profiles":
        return os.path.dirname(os.path.dirname(home))
    return home


def find_hermes_bin(explicit=None):
    """Locate the ``hermes`` CLI: explicit arg, ``HERMES_BIN``, venv, PATH.

    The venv candidate is the install's own Hermes venv (what bootstrap's
    ``PYBIN`` belongs to); PATH is the last resort so a test or an operator's
    custom layout can still point at a real binary.
    """
    root = _hermes_root()
    candidates = [
        explicit,
        os.environ.get("HERMES_BIN"),
        os.path.join(root, "hermes-agent", "venv", "bin", "hermes"),
        os.path.join(root, "bin", "hermes"),
        shutil.which("hermes"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _run(argv, timeout=120):
    """Run one Hermes CLI command; returns ``(returncode, stdout, stderr)``."""
    cp = subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                        timeout=timeout)
    return cp.returncode, cp.stdout or "", cp.stderr or ""


def ensure(hscc_dir=None, *, hermes_bin=None, runner=None) -> dict:
    """Create the main board (idempotent) and make it the current board.

    ``runner`` is injectable for tests; the production path shells out to the
    Hermes CLI. Returns ``{ok, board, action, detail}`` — a failure names the
    command that failed and never claims the board is current.
    """
    try:
        board = main_board(hscc_dir)
    except Exception as exc:  # noqa: BLE001 — report, never crash bootstrap
        return {"ok": False, "board": "", "action": "resolver-error",
                "detail": f"cannot resolve the main board: {exc}"}
    binary = find_hermes_bin(hermes_bin)
    if not binary:
        return {"ok": False, "board": board, "action": "missing-hermes",
                "detail": "hermes CLI not found (set HERMES_BIN or install "
                          "the Hermes venv)"}
    runner = runner or _run
    try:
        rc, out, err = runner(
            [binary, "kanban", "boards", "create", board])
        if rc != 0:
            return {"ok": False, "board": board, "action": "create-failed",
                    "detail": (err or out).strip()
                    or f"hermes kanban boards create exited {rc}"}
        already = "already exists" in (out or "").lower()
        rc, out, err = runner(
            [binary, "kanban", "boards", "switch", board])
        if rc != 0:
            return {"ok": False, "board": board, "action": "switch-failed",
                    "detail": (err or out).strip()
                    or f"hermes kanban boards switch exited {rc}"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "board": board, "action": "runner-error",
                "detail": str(exc)}
    return {"ok": True, "board": board,
            "action": "already-current" if already else "created",
            "detail": f"active board is now {board!r}"}


def _load_kanban_db():
    """Hermes' kanban library, or None when it is not importable here."""
    try:
        from hermes_cli import kanban_db  # noqa: PLC0415
        return kanban_db
    except Exception:  # noqa: BLE001 — the row reports unverifiable
        return None


def doctor_check(hscc_dir=None, kanban_db=None) -> dict:
    """Compare the kanban current board with the main orchestrator's board.

    ``kanban_db`` is the injectable fake (tests); production resolves Hermes'
    own library. A mismatch is ``ok=False, fatal=False`` — a WARNING with the
    exact one-line fix, never an install-stopping failure.
    """
    try:
        expected = main_board(hscc_dir)
    except Exception as exc:  # noqa: BLE001
        return {"name": CHECK_NAME, "ok": False, "fatal": False,
                "detail": f"cannot resolve the main board: {exc}",
                "fix": "Re-run hscc-bootstrap (or fix hscc-roles/main_orch.py)."}
    lib = kanban_db if kanban_db is not None else _load_kanban_db()
    if lib is None:
        return {"name": CHECK_NAME, "ok": False, "fatal": False,
                "detail": "Hermes' kanban library is not importable — the "
                          "current board cannot be verified",
                "fix": "Run doctor with the Hermes venv python (re-run "
                       "hscc-bootstrap)."}
    try:
        current = str(lib.get_current_board())
    except Exception as exc:  # noqa: BLE001
        return {"name": CHECK_NAME, "ok": False, "fatal": False,
                "detail": f"cannot read the kanban current board: {exc}",
                "fix": "hermes kanban boards show"}
    if current == expected:
        return {"name": CHECK_NAME, "ok": True, "fatal": False,
                "detail": f"kanban current board is {expected!r} (the main "
                          "orchestrator's board)",
                "fix": ""}
    return {"name": CHECK_NAME, "ok": False, "fatal": False,
            "detail": f"kanban current board is {current!r}, expected "
                      f"{expected!r} (the main orchestrator's board)",
            "fix": f"hermes kanban boards switch {expected}   "
                   "# or re-run hscc-bootstrap"}


#: Alias so callers can use the shorter feature-check spelling.
check = doctor_check


def _cli(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else ""
    if cmd == "ensure":
        result = ensure()
        print(json.dumps(result))
        return 0 if result["ok"] else 1
    if cmd in ("check", "doctor"):
        result = doctor_check()
        print(json.dumps(result))
        return 0 if result["ok"] else 1
    print("usage: main_board.py {ensure|check}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess
    raise SystemExit(_cli())
