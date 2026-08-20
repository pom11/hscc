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
  keep working with no Telegram and no Hermes at all — those absences are
  reported but never block.
- Everything external is injectable (``home``, and per-check ``_*`` handles) so
  tests build pass and fail worlds without touching the network, a real board,
  Telegram, or the operator's real ``~/.flightdeck`` / ``~/.hermes``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from ..core import probe as _probe
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
  1. Set `telegram.group_id` in {config} -- it has no default, and every
     Telegram command (topics, message, ask, ingest, sync, standup,
     decompose) fails with a clear message until it is set. Find your
     group id; a private group id is usually a large negative number like
     -1001234567890.
  2. Run `flightdeck project sync --apply` to adopt your existing repos
     into the registry.
  3. Register flightdeck's MCP server with your MCP client so an agent can
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


def _default_probe_client() -> int:
    """Run a REAL MCP handshake against the configured daemon; return tool count.

    Reuses the SAME transport telegram.py uses to talk to the daemon —
    ``flightdeck.core.telegram._streamable_http_client`` (the factory helper
    ``telegram._default_client`` is built on). This is NOT a second transport
    and NOT a hand-rolled HTTP request: it is the shared streamable-HTTP MCP
    connection, doing a genuine client ``initialize()`` handshake and a
    ``list_tools`` so the reported tool count is actually verified.

    Returns the number of tools the daemon exposes. Raises on any failure:
    connection refused raises (likely a nested ``ConnectionRefusedError``) and
    a live port that fails the handshake raises the protocol/transport error —
    the caller classifies which.
    """
    import asyncio

    from mcp import ClientSession

    from ..core import config as _cfg
    from ..core import telegram as _tg

    http_client = _tg._streamable_http_client()

    async def _run() -> int:
        async with http_client(_cfg.telegram_mcp_url()) as streams:
            # mcp < 2 yields (read, write, get_session_id); 2.0 yields
            # (read, write). Take the two stream ends positionally so the
            # extra element is tolerated rather than required.
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()          # the handshake
                result = await session.list_tools()  # tool count
        # mcp < 2 exposes tools under .result.tools; 2.x at .tools.
        tools = getattr(result, "tools", None)
        if tools is None:
            inner = getattr(result, "result", None)
            tools = getattr(inner, "tools", None)
        return len(tools) if tools is not None else 0

    return asyncio.run(_run())


def _probe_telegram_daemon(_client=None) -> dict:
    """Production probe: real MCP handshake to the configured daemon.

    Returns a TRI-STATE dict — never collapses distinct outcomes:

    - ``status="ok"``        the handshake completed (and tools were listed);
                             ``tools`` holds the verified tool count.
    - ``status="missing"``   nothing is listening (connection refused).
    - ``status="unverified"`` something IS listening but the handshake did not
                             complete; ``detail`` names the reason.

    A protocol-level error against a live port is UNVERIFIED, never MISSING.
    ``_client`` is injectable (tests pass a stub that follows the same contract
    as ``_default_probe_client``: return tool count or raise). The probe never
    raises — init always exits 0: git/roadmap/lint work with no Telegram at all.
    """
    try:
        from ..core import config as _cfg
    except ImportError:  # pragma: no cover - defensive
        return {"ok": False, "status": "unverified", "tools": 0,
                "detail": "flightdeck core import failed"}
    try:
        url = _cfg.telegram_mcp_url()
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "status": "unverified", "tools": 0,
                "detail": f"could not resolve MCP URL: {exc}"}
    if not url:
        return {"ok": False, "status": "unverified", "tools": 0,
                "detail": "telegram not configured (no mcp_url)"}

    if _client is None:
        _client = _default_probe_client
    try:
        n_tools = _client()
    except Exception as exc:
        if _probe.is_connection_refused(exc):
            return {"ok": False, "status": "missing", "tools": 0,
                    "detail": f"no Telegram MCP daemon listening at {url} (connection refused)"}
        return {"ok": False, "status": "unverified", "tools": 0,
                "detail": (f"something is listening at {url} but the MCP "
                           f"handshake did not complete: "
                           f"{type(exc).__name__}: {exc}")}
    return {"ok": True, "status": "ok", "tools": n_tools,
            "detail": f"Telegram MCP daemon answered ({n_tools} tools)"}


def _check_telegram_daemon(*, _answers=None) -> dict:
    """Normalize the injected/probed Telegram result into a TRI-STATE dict.

    ``_answers`` is the injectable probe result. Accepted: ``None`` = not
    probed; a ``callable`` = production probe (attached by ``run``), called to
    get a result; a ``dict`` = already a tri-state result (production, or a
    test stub); a bare bool for backward compatibility (``True`` = ok,
    ``False`` = missing). The dict always carries ``ok``, ``status``
    (ok|missing|unverified), ``tools``, and ``detail`` — so a live port whose
    handshake failed is reported UNVERIFIED, never collapsed into MISSING.
    """
    if _answers is None:
        return {"ok": False, "status": "unverified", "tools": 0,
                "detail": "Telegram MCP daemon not probed (missing config)"}
    if callable(_answers):
        _answers = _answers()  # production -> _probe_telegram_daemon tri-state dict
    if isinstance(_answers, dict):
        return _answers  # already a tri-state result (production or a dict stub)
    # Backward-compatible bare bools for tests that predate the tri-state.
    if _answers is True:
        return {"ok": True, "status": "ok", "tools": 0,
                "detail": "Telegram MCP daemon answered"}
    if _answers is False:
        return {"ok": False, "status": "missing", "tools": 0,
                "detail": "Telegram MCP daemon did not answer (connection refused)"}
    # A legacy string detail survives as UNVERIFIED: something was reachable
    # but did not complete, so it is NOT reported as missing.
    if not _answers:
        return {"ok": True, "status": "ok", "tools": 0,
                "detail": "Telegram MCP daemon answered"}
    return {"ok": False, "status": "unverified", "tools": 0, "detail": str(_answers)}


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
    checks.append(("telegram-daemon", _check_telegram_daemon(_answers=getattr(args, "_tg_answer", None))))
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
    # Production Telegram probe: `_probe_telegram_daemon` (a callable, called
    # by _check_telegram_daemon). Tests may substitute a bare bool.
    args._tg_answer = (
        _probe_telegram_daemon
        if getattr(args, "_tg_answer", None) is None
        else getattr(args, "_tg_answer", None)
    )
    return cmd_init(args)
