"""init.py — `flightdeck init [--apply]` : one-command bootstrap for a new machine.

A newcomer runs this right after ``pip install flightdeck``. Until now there was
no installer: flightdeck silently did nothing useful until ``~/.flightdeck/``
existed. ``init`` creates that home and seeds it, checks the environment
flightdeck runs in, and prints the exact next steps — so the first five minutes
after install are guided instead of mysterious.

Behaviour contract:

- **Idempotent and never-clobbering.** ``init`` creates ``~/.flightdeck/`` and,
  when absent, seeds ``config.yaml`` from ``docs/config.example.yaml`` and
  ``registry.yaml`` from ``docs/registry.example.yaml``. An existing file is
  NEVER overwritten — it is reported as ``kept`` and we move on. The shipped
  prompt templates are copied into ``~/.flightdeck/templates/`` only when that
  directory is absent (same never-overwrite rule, reusing
  :func:`flightdeck.core.templates.ensure_seeded`).
- **``--apply`` writes; without it nothing does.** Without ``--apply`` the
  command prints exactly what WOULD be created, then stops — a dry run. No
  directory is created, no file is written, no template is copied.
- **Environment checks are REPORTED, not enforced.** Each check reports its
  state plainly — ``[ok]``, ``[MISSING]`` (nothing there) or ``[UNVERIFIED]``
  (something there, but not confirmable, with the reason) — and only a missing
  ``~/.flightdeck`` is fatal to flightdeck's own use. git, roadmap and lint
  keep working with no Hermes at all — those absences are
  reported but never block.
- Everything external is injectable (``home``, and per-check ``_*`` handles) so
  tests build pass and fail worlds without touching the network, a real board,
  or the operator's real ``~/.flightdeck`` / ``~/.hermes``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from ..core import templates as _templates

# The example files init seeds from. Located relative to the package root so a
# source checkout (and ``pip install .`` from a checkout) always finds them;
# docs are not shipped as wheel package-data, so a missing example is reported
# gracefully rather than treated as fatal.
_DOCS_DIR = Path(__file__).resolve().parent.parent.parent / "docs"

# Default Hermes path the ``hermes-kanban`` check probes. Module-level so tests
# can redirect it away from the operator's real ~/.hermes via monkeypatch (the
# command reads this as its production default).
_HERMES_DB_DEFAULT = "~/.hermes/kanban.db"

# The MCP registration block init tells the user to paste into their MCP
# client config. This is how an agent drives flightdeck.
_MCP_REGISTRATION = '"flightdeck": { "command": "flightdeck-mcp", "args": [] }'

DEFAULT_HOME = "~/.flightdeck"

_NEXT_STEPS = """\
Next steps:
  1. Run `flightdeck project sync --apply` to adopt your existing repos
     into the registry.
  2. Register flightdeck's MCP server with your MCP client so an agent can
     drive flightdeck:{nl}{nl}    {mcp}{nl}
     Hermes takes the same shape under its `mcp:` config key.
"""


# --------------------------------------------------------------------------- #
# Filesystem seeding (never overwrites; all injectable for tmp_path as HOME)
# --------------------------------------------------------------------------- #

def _home_dir(home: str | None) -> Path:
    """The resolved ~/.flightdeck home (or an injected override)."""
    return Path(os.path.expanduser(home if home is not None else DEFAULT_HOME))


def _example_path(name: str) -> Path:
    """The seed source for ``config.yaml`` / ``registry.yaml`` in docs/."""
    return _DOCS_DIR / f"{name}.example.yaml"


def _would_create(home_root: Path, ses: dict) -> list[str]:
    """The files that WOULD be created (dry-run projection), in order.

    ``ses`` is the resolved seeding plan (see ``cmd_init``): ``config.yaml`` /
    ``registry.yaml`` hold the resolved seed Path or None (None = the example
    file itself is missing, so nothing can be seeded from it even on apply).
    """
    plan: list[str] = []
    if not (home_root / "config.yaml").exists() and ses["config.yaml"] is not None:
        plan.append(str(home_root / "config.yaml"))
    if not (home_root / "registry.yaml").exists() and ses["registry.yaml"] is not None:
        plan.append(str(home_root / "registry.yaml"))
    if not (home_root / "templates").exists():
        # ensure_seeded would create the templates dir and copy every shipped
        # template into it, so the whole dir is a dry-run creation.
        plan.append(str(home_root / "templates") + "/")
    return plan


def _seed_file(dest: Path, src: Path, created: list[str], kept: list[str]) -> None:
    """Copy src -> dest UNLESS dest exists; report created|kept.

    Src is never overwritten in the home (never-clobber rule); an existing
    dest is left byte-identical and reported as ``kept``.
    """
    if dest.exists():
        kept.append(dest.name)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    created.append(dest.name)


def _run_seed(home_root: Path, ses: dict) -> dict:
    """Apply the seeding plan. Returns ``{"created": [...], "kept": [...]}``."""
    created: list[str] = []
    kept: list[str] = []
    home_root.mkdir(parents=True, exist_ok=True)
    if ses["config.yaml"] is not None:
        _seed_file(home_root / "config.yaml", ses["config.yaml"], created, kept)
    if ses["registry.yaml"] is not None:
        _seed_file(home_root / "registry.yaml", ses["registry.yaml"], created, kept)
    if not (home_root / "templates").exists():
        # ensure_seeded creates the templates dir and copies every shipped
        # template into it. Never-overwrite is its built-in rule.
        _templates.ensure_seeded(str(home_root / "templates"))
        created.append("templates/")
    else:
        kept.append("templates/")
    return {"created": created, "kept": kept}


# --------------------------------------------------------------------------- #
# Environment checks (each REPORTS pass/fail; injectable for tests)
# --------------------------------------------------------------------------- #

def _check_python(*, _info=None) -> dict:
    """{ok, detail} for the Python runtime: version + interpreter path."""
    info = _info if _info is not None else sys.version_info
    version = ".".join(str(p) for p in info[:3])
    interp = sys.executable or "unknown"
    return {"ok": True, "detail": f"Python {version} ({interp})"}


def _check_git(*, _which=None) -> dict:
    """{ok, detail} for ``git`` being on PATH."""
    git = _which("git") if _which else shutil.which("git")
    if git:
        return {"ok": True, "detail": f"git on PATH: {git}"}
    return {"ok": False, "detail": "git NOT found on PATH"}


def _mcp_layout() -> dict:
    """What client/server symbol layout the installed ``mcp`` SDK exposes.

    The ``mcp`` 2.0.0 upgrade renamed ``FastMCP`` to
    ``mcp.server.mcpserver.MCPServer``, which once broke this repo. Report which
    of the two the installed SDK exposes so an upgrade surfacing that rename is
    caught as a clear fact instead of an opaque ImportError later.
    """
    try:
        import mcp  # noqa: PLC0415
        import mcp.server.mcpserver  # noqa: F401, PLC0415
    except ImportError:
        return {"ok": False, "detail": "mcp SDK NOT importable"}
    version = getattr(mcp, "__version__", "?")
    if hasattr(mcp, "FastMCP"):
        layout = "FastMCP (pre-2.0 layout)"
    elif hasattr(mcp.server.mcpserver, "MCPServer"):
        layout = "MCPServer (2.0 layout)"
    else:
        layout = "unknown layout (neither FastMCP nor MCPServer)"
    return {"ok": True, "detail": f"mcp SDK present (v{version}): {layout}"}


def _check_mcp(*, _layout=None) -> dict:
    """{ok, detail} for the mcp SDK + its client/server symbol layout."""
    layout = _layout if _layout is not None else _mcp_layout()
    return layout


def _open_hermes_db(db_path: str) -> None:
    """Verify ``db_path`` opens as a readable SQLite DB (throws if not).

    Read-only (``mode=ro``) so probing the operator's real ``~/.hermes/kanban.db``
    can never create or modify anything in it. Runs ``SELECT name FROM
    sqlite_master`` inside that read-only connection so a path that merely exists
    but is NOT a real SQLite file (a leftover, a truncated download, a plain text
    file) is caught here instead of being reported as a reachable board.
    ``sqlite_master`` is queried (rather than a constant expression like
    ``SELECT 1``) because it forces SQLite to actually parse and validate the
    file's schema/btree page — which raises consistently across libsqlite
    versions/platforms for a non-database file, whereas ``SELECT 1`` can be
    answered from the parser without touching storage and is not reliably
    validated.
    """
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
    finally:
        conn.close()


def _check_hermes_kanban(*, _db_path=None, _open=None) -> dict:
    """{ok, status, detail} for whether a Hermes kanban DB is reachable.

    Flightdeck reads Hermes boards through ``hermes_cli.kanban_db``, which
    reads ``~/.hermes/kanban.db``. This bootstrap check verifies the DB — not
    by the path merely existing, but by actually OPENING it as a readable
    SQLite database (read-only, non-destructive). ``_db_path`` and ``_open``
    are injectable so tests probe a scratch path, never the operator's real
    ``~/.hermes``. A missing/unopenable DB is REPORTED, not fatal: flightdeck's
    git/roadmap/lint commands work with no Hermes at all.
    """
    db = _db_path if _db_path is not None else os.path.expanduser(_HERMES_DB_DEFAULT)
    if not os.path.isfile(db):
        return {"ok": False, "status": "missing",
                "detail": f"no Hermes kanban DB at {db}"}
    open_db = _open if _open is not None else _open_hermes_db
    try:
        open_db(db)
    except Exception as exc:  # exists but won't open as a valid DB
        return {"ok": False, "status": "unverified",
                "detail": f"cannot open Hermes kanban DB at {db}: {type(exc).__name__}: {exc}"}
    return {"ok": True, "status": "ok",
            "detail": f"Hermes kanban DB reachable and readable at {db}"}


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #

def _env_report(args: argparse.Namespace) -> list[tuple[str, dict]]:
    """Run every environment check, returning ``[(label, {ok, detail})]``.

    Each check reads its injectable ``args._<name>`` handle when present so
    tests build pass and fail worlds without external state. In production the
    handles are None (defaults) and the checks probe the real environment.
    """
    checks: list[tuple[str, dict]] = []
    checks.append(("python", _check_python(_info=getattr(args, "_py_info", None))))
    checks.append(("mcp-sdk", _check_mcp(_layout=getattr(args, "_mcp_layout", None))))
    checks.append(("git", _check_git(_which=getattr(args, "_which", None))))
    checks.append((
        "hermes-kanban",
        _check_hermes_kanban(
            _db_path=getattr(args, "_hermes_db", None),
            _open=getattr(args, "_hermes_open", None),
        ),
    ))
    return checks


def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "init",
        help="one-command bootstrap for a new machine: create ~/.flightdeck, seed it, check the environment",
        epilog="example: flightdeck init --apply",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="write files; without it, print what WOULD be created and change nothing",
    )
    p.add_argument(
        "--home",
        default=None,
        metavar="PATH",
        help="flightdeck home to create/seed (default: ~/.flightdeck)",
    )
    p.set_defaults(func=cmd_init)


def _render_env(checks: list[tuple[str, dict]]) -> str:
    lines = ["Environment check:"]
    for label, res in checks:
        status = res.get("status", "ok" if res["ok"] else "missing")
        mark = {"ok": "ok", "missing": "MISSING", "unverified": "UNVERIFIED"}.get(
            status, "MISSING"
        )
        lines.append(f"  {label:<16} [{mark}] {res['detail']}")
    return "\n".join(lines)


def _render_plan(plan: list[str]) -> str:
    if not plan:
        return "  (nothing to create — everything already in place)"
    return "\n".join(f"  would create {p}" for p in plan)


def cmd_init(args: argparse.Namespace) -> int:
    home_root = _home_dir(args.home)

    # Resolve seed sources up front (missing example files degrade gracefully).
    ses = {
        "config.yaml": _example_path("config"),
        "registry.yaml": _example_path("registry"),
    }
    # Drop seed sources that don't exist on disk.
    for key in ("config.yaml", "registry.yaml"):
        p = ses[key]
        if not p.exists():
            ses[key] = None
            print(
                f"init: warning: seed source {p} not found; cannot seed "
                f"{key} (operating from a wheel? docs/ is not shipped).",
                file=sys.stderr,
            )
        else:
            ses[key] = p

    if args.apply:
        result = _run_seed(home_root, ses)
        created = result["created"]
        kept = result["kept"]
        print(f"flightdeck initialized at {home_root}")
        if created:
            print("  created: " + ", ".join(created))
        if kept:
            print("  kept (not overwritten): " + ", ".join(kept))
    else:
        plan = _would_create(home_root, ses)
        print(f"flightdeck home: {home_root} (dry run — use --apply to write)")
        print("Would create:")
        print(_render_plan(plan))

    # Environment report is always shown, attach (apply) or preview (dry run).
    checks = _env_report(args)
    print()
    print(_render_env(checks))

    # Next steps always printed — the point of init is guiding the newcomer.
    print()
    print(
        _NEXT_STEPS.format(
            config=str(home_root / "config.yaml"),
            nl="\n",
            mcp=_MCP_REGISTRATION,
        )
    )
    return 0


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py. init needs no registry, but run() receives its path."""
    args.registry = registry_path
    args.home = getattr(args, "home", None)
    args.apply = getattr(args, "apply", False)
    # Injectable handles for tests (all None in production -> real probes).
    args._py_info = getattr(args, "_py_info", None)
    args._mcp_layout = getattr(args, "_mcp_layout", None)
    args._which = getattr(args, "_which", None)
    args._hermes_db = getattr(args, "_hermes_db", None)
    args._hermes_open = getattr(args, "_hermes_open", None)
    return cmd_init(args)
