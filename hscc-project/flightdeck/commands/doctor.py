"""doctor.py — `flightdeck doctor` : self-check, is flightdeck's view trustworthy?

Checks every registered project on the dimensions flightdeck reports on, and
reports a loud, clearly-labelled problem when any of them cannot be read.

For each project it verifies:

  - the repo path exists and is a git repository        (via git_state)
  - its board slug exists on this host                  (via kanban.list_boards)
  - its recorded topic (the registry ``topic`` field) is reported; it is
    retained for historical import reads but, with Telegram removed, can no
    longer be verified against a live topic list.

Beyond those checks, doctor verifies the THREE-WAY BINDING — the
topic <-> board <-> repo triangle — that decides where a project's work
actually lands:

  - the board's ``default_workdir`` matches the project's ``repo`` (a mismatch
    means new cards get a worktree in the WRONG repo);
  - no two projects share the same board slug (a mis-binding that silently
    merges two projects' work into one board);
  - no two projects share the same recorded topic id (work attributed to one
    topic would be double-attributed to more than one project).

The triangle is checked per project with a clear reason, and a healthy fleet
prints an explicit ALL-CLEAR line naming how many projects passed, so a clean
result is *evidence* rather than absence of output.

The hard rule from DESIGN: a false all-clear is the worst output this tool
can produce. So an unreadable project is reported *distinctly* from a healthy
one, never silently downgraded to "clean", and the command exits non-zero the
moment any dimension is unverifiable.

Everything is injectable (`_run`, `_boards`, `_workdirs`, `_topics`)
so tests build healthy and broken worlds without touching a live repo, board,
or the network.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import sys
import time
from pathlib import Path

from ..core import git_state, kanban, probe, registry
from ._theme import escape, make_console, panel


def _repo_ok(proj: registry.Project, *, _run=None) -> dict:
    """{ok, detail} for the repo dimension: path exists AND is a git repo.

    A repo that is not a git checkout (no .git) reads as a problem — we cannot
    derive branch/ancestry facts, so the project's git views are untrustworthy.
    """
    if not os.path.isdir(proj.repo):
        return {"ok": False, "detail": f"repo path missing: {proj.repo}"}
    head = git_state.head_sha(proj.repo, _run=_run)
    if head is None:
        return {"ok": False, "detail": f"not a git repository: {proj.repo}"}
    return {"ok": True, "detail": f"{proj.repo} @ {head[:12]}"}


def _board_ok(proj: registry.Project, boards: list[str]) -> dict:
    if not proj.board:
        return {"ok": True, "detail": "no board mapped (unknown, not an error)"}
    if str(proj.board) in boards:
        return {"ok": True, "detail": f"board {proj.board!r} exists"}
    return {"ok": False, "detail": f"board {proj.board!r} NOT found on this host"}


def _topic_ok(proj: registry.Project, topics=None) -> dict:
    """The recorded topic field is reported; it can no longer be verified.

    Telegram (the only live source of topic truth) has been removed, so the
    registry's ``topic`` field is retained for historical import reads but is
    not verifiable against a live topic list. A project with a topic is
    reported as recorded-but-unverifiable; a project with none is simply
    unmapped. The ``topics`` argument is accepted for backward-compatible call
    signature but is no longer consulted.
    """
    if proj.topic is None:
        return {"ok": True, "detail": "no topic mapped (unknown, not an error)"}
    return {"ok": True, "detail": f"topic {proj.topic} recorded (Telegram removed; unverifiable)"}


def _workdir_ok(proj: registry.Project, workdirs: dict | None) -> dict:
    """Board's ``default_workdir`` matches the project's ``repo``.

    A mismatch is the failure that bites: new cards for this project would get
    a worktree in the *wrong* repo, so work discussed in a topic lands
    somewhere the operator isn't looking. Comparison is on resolved,
    normalised paths (expand ~, resolve symlinks, strip trailing slashes) via
    the shared ``kanban._resolve_path`` helper — never a raw string compare.

    ``workdirs`` is a ``slug -> default_workdir`` map (or None when not
    available); ``None`` means the workdir dimension is omitted by the caller.
    """
    if not proj.board:
        return {"ok": True, "detail": "no board mapped (unknown, not an error)"}
    if not workdirs:
        return {"ok": False, "detail": "unverifiable — cannot read board metadata"}
    workdir = workdirs.get(str(proj.board))
    repo_resolved = kanban._resolve_path(proj.repo)
    workdir_resolved = kanban._resolve_path(workdir)
    if workdir_resolved == repo_resolved:
        return {"ok": True, "detail": f"board {proj.board!r} default_workdir matches repo"}
    shown_workdir = workdir if workdir else "(no default_workdir)"
    return {
        "ok": False,
        "detail": (
            f"board {proj.board!r} default_workdir={shown_workdir!r} "
            f"!= repo={proj.repo!r}"
        ),
    }


def _fleet_binding(projects: list[registry.Project]) -> dict:
    """Fleet-wide binding facts: who shares a board slug, who shares a topic id.

    Returns ``{"board_owners": {slug: [names]}, "topic_owners": {topic: [names]}}``
    mapping each bound slug / topic to the project names bound to it. A value
    with more than one name is a mis-binding that silently merges two
    projects' work — surfaced per project in the triangle check.
    """
    board_owners: dict = {}
    topic_owners: dict = {}
    for proj in projects:
        if proj.board:
            board_owners.setdefault(str(proj.board), []).append(proj.name)
        if proj.topic is not None:
            topic_owners.setdefault(int(proj.topic), []).append(proj.name)
    return {"board_owners": board_owners, "topic_owners": topic_owners}


def _unique_board_detail(board: str, owners: list[str]) -> str | None:
    """Human detail of a shared board slug, or None when it is unique/absent."""
    if len(owners) > 1:
        return f"board {board!r} is bound to MULTIPLE projects: {', '.join(owners)}"
    return None


def _unique_topic_detail(topic: int, owners: list[str]) -> str | None:
    """Human detail of a shared topic id, or None when it is unique/absent."""
    if len(owners) > 1:
        return f"topic {topic} is bound to MULTIPLE projects: {', '.join(owners)}"
    return None


# --------------------------------------------------------------------------- #
# Learning pipeline: is Hermes' memory actually ingesting?
# --------------------------------------------------------------------------- #
#
# These checks are the tripwire for the class of silent failure that took two
# weeks to notice: Hermes' memory ingestion died and nothing surfed it. They
# report, per check, ok / PROBLEM / UNVERIFIED.
#
# They are READ-ONLY: never write to ~/.hermes, never restart anything, never
# install anything. The command reports; fixing is out of its scope.
#
# Every source of truth (the memory MEMORY.md files and their mtimes, the
# memory-DB config file, the DB file itself and its directory's writability,
# the gateway launchd plist, the augment HTTP endpoint) is injectable so tests
# build healthy and broken worlds without touching the real ~/.hermes, the real
# plist, or the network.

_LEARNING_CONFIG = "memori_byodb.json"          # under ~/.hermes (informational)
_LEARNING_DB_KEY = "dbPath"
_MEMORY_FILENAME = "MEMORY.md"                  # where learning actually lands
_GATEWAY_PLIST = "~/Library/LaunchAgents/ai.hermes.gateway.plist"
ENV_AUGMENT_URL = "HSCC_MEMORI_AUGMENT_URL"
ENV_AUGMENT_MODEL = "HSCC_MEMORI_AUGMENT_MODEL"

# Learning lands in MEMORY.md files, one per profile plus a global one. A
# store that has gone this long without ANY of them changing is the honest
# signal that the learning pipeline has stopped (faults #1-#4).
_STALE_THRESHOLD_SECONDS = 7 * 24 * 3600

# One rendered mark per status. UNVERIFIED exists so a check we could not even
# attempt is surfaced loudly — never silently downgraded to "clean".
_STATUS_MARK = {"ok": "ok", "problem": "PROBLEM", "unverified": "UNVERIFIED"}


def _read_db_path(cfg_path: Path, *, _read=None) -> str | None:
    """The memory DB path from the config file's ``dbPath``, or None.

    ``None`` covers every "cannot read" shape — file missing, unparseable, or
    without a ``dbPath`` key — and is reported as UNVERIFIED rather than a
    crash. Reading the config is read-only.
    """
    if _read is None:
        try:
            raw = cfg_path.read_text(encoding="utf-8")
        except OSError:
            return None
    else:
        try:
            raw = _read(cfg_path)
        except Exception:
            return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict):
        db_path = data.get(_LEARNING_DB_KEY)
        if isinstance(db_path, str) and db_path.strip():
            return os.path.expanduser(db_path.strip())
    return None


def _dir_writable(path: str, *, _access=None) -> bool:
    """Whether the DB file's parent DIRECTORY is writable.

    Writable means the process could actually append/rewrite the DB file. A
    read-only mount (fault #1) makes this False — and because the DB file
    itself still exists and reads fine, the failure was invisible until now.
    """
    if _access is not None:
        return bool(_access(path, os.W_OK))
    return os.access(path, os.W_OK)


def _file_exists(path: str, *, _exists=None) -> bool:
    if _exists is not None:
        return bool(_exists(path))
    return os.path.exists(path)


def _db_mtime(path: str, *, _mtime=None) -> float | None:
    """The DB file's last-modification time, or None when it cannot be read."""
    if _mtime is not None:
        try:
            return float(_mtime(path))
        except Exception:
            return None
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _memory_candidate_files(
    home: Path, *, _scandir=None
) -> list[Path]:
    """All MEMORY.md files learning writes to: the global one plus one per profile.

    Global memory lives at ``<home>/memories/MEMORY.md``; per-profile memory at
    ``<home>/profiles/<profile>/memories/MEMORY.md``. Profile discovery is
    injectable (``_scandir``) so tests enumerate a fixed set of profiles without
    ever touching the real ``~/.hermes``. Files that don't exist are left in
    the list; the caller filters them. An unreadable profiles dir simply yields
    no per-profile candidates (the global file may still be present).
    """
    files: list[Path] = [home / "memories" / _MEMORY_FILENAME]

    profiles_dir = home / "profiles"
    scandir = _scandir if _scandir is not None else os.scandir
    try:
        with scandir(profiles_dir) as it:
            names = sorted(e.name for e in it if e.is_dir())
    except OSError:
        names = []
    for name in names:
        files.append(profiles_dir / name / "memories" / _MEMORY_FILENAME)
    return files


def _newest_memory(
    files: list[Path], *, _exists=None, _mtime=None
) -> tuple[str | None, float | None]:
    """``(path, newest mtime)`` across the memory files, or ``(None, None)``.

    Only existing files with a readable mtime count. The newest mtime across
    all of them is the honest "is learning still landing" signal — a fresh
    write to any one profile (or the global file) means the pipeline is alive.
    """
    best_path, best_mtime = None, None
    for f in files:
        p = str(f)
        if not _file_exists(p, _exists=_exists):
            continue
        mt = _db_mtime(p, _mtime=_mtime)
        if mt is None:
            continue
        if best_mtime is None or mt > best_mtime:
            best_path, best_mtime = p, mt
    return best_path, best_mtime


def _agent_home(*, _home: str | None) -> Path:
    """The Hermes home dir (where the learning-pipeline state lives).

    Production default is ``~/.hermes`` (where ``memori_byodb.json`` lives);
    tests inject a scratch dir so nothing under the real ``~/.hermes`` is ever
    touched.
    """
    if _home is not None:
        return Path(_home)
    return Path(os.path.expanduser("~")) / ".hermes"


def _memori_db_check(
    db_path: str,
    now: int,
    *,
    _exists=None,
    _access=None,
    _mtime=None,
    threshold: int,
) -> dict:
    """Informational note on the memori DB — NEVER a freshness failure.

    The memori DB (``~/.hermes/memori_byodb.db``) is inert and NOT where
    Hermes actually learns; its age is reported for the operator's awareness
    but is deliberately NOT a failure signal. Freshness is read from the
    MEMORY.md files (`_staleness_check`). We still surface genuine hardware
    faults — a missing DB or a read-only directory — as problems.
    """
    if not _file_exists(db_path, _exists=_exists):
        return {
            "status": "problem",
            "detail": f"memory DB missing: {db_path}",
        }
    db_dir = os.path.dirname(db_path) or "."
    if not _dir_writable(db_dir, _access=_access):
        return {
            "status": "problem",
            "detail": (
                f"memory DB directory NOT writable (read-only mount?): {db_dir} "
                f"— a dead learning pipeline that only fails on write is otherwise invisible"
            ),
        }
    mtime = _db_mtime(db_path, _mtime=_mtime)
    age_txt = "unknown age"
    if mtime is not None:
        age = max(0, now - int(mtime))
        age_txt = f"modified {age/86400:.1f}d ago"
    return {
        "status": "ok",
        "detail": (
            f"memori DB present and writable: {db_path} ({age_txt} — informational, not a "
            f"freshness signal; freshness is read from the MEMORY.md files)"
        ),
    }


def _staleness_check(
    newest_path: str | None,
    newest_mtime: float | None,
    *,
    now: int,
    threshold: int,
) -> dict:
    """Is learning fresh? Based on the NEWEST MEMORY.md across all profiles.

    Hermes writes memory per-profile (``profiles/<profile>/memories/MEMORY.md``)
    plus a global ``memories/MEMORY.md``. Watching the *newest* of them is the
    honest "is learning still landing" signal. If no memory file is readable we
    say UNVERIFIED, never ok. Stale = the newest is older than the threshold —
    learning has stopped.
    """
    if newest_mtime is None:
        return {
            "status": "unverified",
            "detail": (
                "no readable MEMORY.md file found across profiles/global — "
                "cannot verify learning"
            ),
        }
    age = max(0, now - int(newest_mtime))
    days = age / 86400
    if age <= threshold:
        return {
            "status": "ok",
            "detail": (
                f"memory fresh: newest MEMORY.md is {newest_path} "
                f"(modified {days:.1f}d ago, within {threshold/86400:.0f}d threshold)"
            ),
        }
    return {
        "status": "problem",
        "detail": (
            f"memory STALE: newest MEMORY.md is {newest_path} "
            f"(modified {days:.1f}d ago, over {threshold/86400:.0f}d threshold) "
            f"— learning has stopped"
        ),
    }


def _probe_models(url: str, *, timeout: float = 5.0, _urlopen=None) -> list[str] | None:
    """The model ids served for ``url`` via the shared probe helper.

    Returns a list of served ids on success, or ``None`` when the endpoint is
    unreachable (the UNVERIFIED case — never ``ok``). Read-only HTTP; nothing
    is written or restarted.

    This delegates to :mod:`flightdeck.core.probe` — the single place that
    knows how to classify reachability — so it never produces the false
    "unreachable" verdict this task is about. Deriving the models URL from the
    configured chat-completions URL means we GET the GET-safe ``/models``
    endpoint rather than GETting the POST-only chat-completions URL; if the
    models URL cannot be derived we POST a minimal probe to the configured URL
    directly, never GET it.
    """
    models_url = probe.derive_models_url(url)
    if models_url is not None:
        status, resp_status, payload = probe.probe_http(
            models_url, method="GET", timeout=timeout, _urlopen=_urlopen
        )
    else:
        # Cannot derive a models URL — POST a minimal chat request to the
        # configured URL instead. NEVER GET a chat-completions URL.
        minimal = json.dumps(
            {"model": "probe", "messages": [{"role": "user", "content": "ping"}]}
        ).encode("utf-8")
        status, resp_status, payload = probe.probe_http(
            url, method="POST", data=minimal, timeout=timeout, _urlopen=_urlopen
        )
    if status != probe.REACHABLE:
        # Transport-level failure (connection refused / DNS / timeout): the
        # endpoint is genuinely unreachable — the UNVERIFIED case.
        return None
    # An HTTP response of ANY status proves the endpoint is up. If it isn't a
    # valid models list payload this is read as "no ids served" (a PROBLEM),
    # never as "unreachable".
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [
        str(item["id"])
        for item in data
        if isinstance(item, dict) and item.get("id")
    ]


def _is_concrete_under_alias(model: str, served: list[str]) -> bool:
    """True when ``model`` is a concrete id pinned under a served logical alias.

    Fault #3 was a pinned CONCRETE id (``gpt-4o-<date>``) that broke on the
    next model swap while a stable logical alias (``gpt-4o``) was available.
    We detect that shape: another served id is a strict prefix of the
    configured one, i.e. the same model exists as a shorter, stable alias.
    """
    for other in served:
        if other == model:
            continue
        if other and model.startswith(other + "-"):
            return True
    return False


def _read_gateway_env(*, _plist_source=None, _load=None) -> dict:
    """The augment env vars from the gateway launchd plist, or ``{}``.

    The gateway sets ``HSCC_MEMORI_AUGMENT_URL`` / ``_MODEL`` in its launchd
    plist's ``EnvironmentVariables``, so reading ``os.environ`` from outside
    the gateway process always finds nothing. We parse the plist with
    ``plistlib`` instead. Everything is injectable (``_plist_source`` a plist
    path, ``_load`` a parser) so tests use a scratch plist, never the real one.

    ``{}`` covers every "cannot read or absent" shape — missing plist,
    unreadable, unparseable, no ``EnvironmentVariables``, or the keys absent —
    and is reported as not-configured rather than guessed.
    """
    if _plist_source is not None:
        path = Path(_plist_source)
    else:
        path = Path(os.path.expanduser(_GATEWAY_PLIST))
    load = _load if _load is not None else plistlib.load
    try:
        with path.open("rb") as f:
            data = load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    env_vars = data.get("EnvironmentVariables")
    if not isinstance(env_vars, dict):
        return {}
    out: dict = {}
    for env_var in (ENV_AUGMENT_URL, ENV_AUGMENT_MODEL):
        val = env_vars.get(env_var)
        if isinstance(val, str) and val.strip():
            out[env_var] = val.strip()
    return out


def _augment_env(*, _env=None, _plist_source=None) -> dict:
    """Resolve the augment config: os.environ overrides the gateway plist.

    Source order (highest priority wins): the injected ``_env`` snapshot /
    ``os.environ`` first, then the gateway launchd plist as a fallback. Returns
    a dict with ``ENV_AUGMENT_URL`` and ``ENV_AUGMENT_MODEL`` present only when
    found in either source. Both absent -> ``{}`` (genuinely not configured).
    """
    resolved: dict = {}

    shell_env = dict(os.environ) if _env is None else _env
    for env_var in (ENV_AUGMENT_URL, ENV_AUGMENT_MODEL):
        val = shell_env.get(env_var)
        if val and str(val).strip():
            resolved[env_var] = str(val).strip()

    # os.environ absent -> fall back to the plist. If os.environ HAS a value it
    # already won above, so we only fill keys still missing.
    for env_var, plist_val in _read_gateway_env(
        _plist_source=_plist_source
    ).items():
        if env_var not in resolved:
            resolved[env_var] = plist_val
    return resolved


def _augment_check(env: dict, *, _probe=None) -> dict:
    """Is the configured augmentation model actually served at its URL?

    Everything is injected (``_probe``), so tests never hit the network. The
    two failure shapes we watch for:
      - configured model NOT in the served set  -> PROBLEM (names both ids);
      - configured model served but pinned as a CONCRETE id under a logical
        alias that is also served -> PROBLEM (fault #3 — it will break on swap).
    An unreachable endpoint is UNVERIFIED, never ``ok``.
    """
    url = (env.get(ENV_AUGMENT_URL) or "").strip()
    model = (env.get(ENV_AUGMENT_MODEL) or "").strip()
    if not url or not model:
        return {
            "status": "unverified",
            "detail": (
                f"augmentation not configured ({ENV_AUGMENT_URL} / "
                f"{ENV_AUGMENT_MODEL} unset) — cannot verify"
            ),
        }
    probe = _probe if _probe is not None else _probe_models
    served = probe(url)
    if served is None:
        return {
            "status": "unverified",
            "detail": f"augment endpoint unreachable at {url} — cannot verify model {model!r}",
        }
    if model in served:
        if _is_concrete_under_alias(model, served):
            alias = next(
                (o for o in served if o != model and model.startswith(o + "-")),
                None,
            )
            return {
                "status": "problem",
                "detail": (
                    f"model {model!r} is pinned as a CONCRETE id though logical "
                    f"alias {alias!r} is served at {url} — concrete ids break on "
                    f"every model swap"
                ),
            }
        return {
            "status": "ok",
            "detail": f"model {model!r} served at {url}",
        }
    served_txt = ", ".join(repr(s) for s in served) if served else "(none served)"
    return {
        "status": "problem",
        "detail": (
            f"configured model {model!r} NOT served; served: {served_txt} at {url}"
        ),
    }


def _learning_checks(
    *,
    _home: str | None = None,
    _env: dict | None = None,
    _now: int | None = None,
    _probe=None,
    _exists=None,
    _access=None,
    _mtime=None,
    _memory_files=None,
    _scandir=None,
    _plist_source=None,
) -> list[dict]:
    """All learning-pipeline checks: a list of ``{check, status, detail}``.

    Every source is injectable. The default (production) values read the real
    ``~/.hermes`` memory files, the gateway launchd plist, and probe the real
    augment endpoint over HTTP — all read-only.
    """
    home = _agent_home(_home=_home)
    env: dict = _augment_env(_env=_env, _plist_source=_plist_source)
    now = int(time.time()) if _now is None else _now

    checks: list[dict] = []

    # memori DB: informational only — its age is NOT a freshness signal.
    cfg_path = home / _LEARNING_CONFIG
    db_path = _read_db_path(cfg_path)
    if db_path is None:
        checks.append({
            "check": "memory-db",
            "status": "unverified",
            "detail": f"no {cfg_path} with a dbPath — cannot read the memori DB (informational)",
        })
    else:
        db_check = _memori_db_check(
            db_path, now, _exists=_exists, _access=_access,
            _mtime=_mtime, threshold=_STALE_THRESHOLD_SECONDS,
        )
        db_check["check"] = "memory-db"
        checks.append(db_check)

    # memory-stale: the real signal — watch the NEWEST MEMORY.md across profiles.
    if _memory_files is not None:
        memory_candidates = list(_memory_files)
    else:
        memory_candidates = _memory_candidate_files(home, _scandir=_scandir)
    newest_path, newest_mtime = _newest_memory(
        memory_candidates, _exists=_exists, _mtime=_mtime
    )
    stale_check = _staleness_check(
        newest_path, newest_mtime, now=now, threshold=_STALE_THRESHOLD_SECONDS
    )
    stale_check["check"] = "memory-stale"
    checks.append(stale_check)

    augment_check = _augment_check(env, _probe=_probe)
    augment_check["check"] = "augment-model"
    checks.append(augment_check)
    return checks


# --------------------------------------------------------------------------- #
# tmux/REPL section — is the operator's chat reachable from the iOS app?
# --------------------------------------------------------------------------- #

def _tmux_repl_checks(*, projects=None, _which=None, _pane_ttys=None,
                      _chat_procs=None) -> list[dict]:
    """One check per registry project: is its orchestrator REPL running
    inside a tmux pane?

    Why this exists: the iOS app delivers chat messages by typing into the
    project's REPL pane (hscc-api/repl_inject). A REPL started OUTSIDE tmux
    is invisible to that path — iPhone messages then fall back to a hidden
    serve-hosted session the operator never sees, and any /goal loop in the
    terminal never runs. This makes that silent split loud, in the place an
    operator already looks for "is my setup trustworthy".

    Statuses (the hard rule: an unverifiable answer is never silently ok):
      ok        — this project's REPL is live in a tmux pane.
      problem   — REPL found but OUTSIDE tmux (consequence named); or no
                  REPL running at all (iOS falls back — hint at the fix).
      unverified— tmux itself cannot be consulted (not installed / broken
                  server), or the ONE pane matcher (hscc-api/repl_inject,
                  which tmux_chat delegates to) cannot be loaded: we refuse
                  to claim the REPL is unreachable.

    ``_which`` / ``_pane_ttys`` / ``_chat_procs`` are injectable seams so the
    check is hermetically testable (the guarded tmux_chat seams refuse to run
    under pytest).
    """
    from ..core import tmux_chat as tc
    which = _which or __import__("shutil").which
    projects = projects or []
    checks: list[dict] = []

    def _add(check: str, status: str, detail: str) -> None:
        checks.append({"check": check, "status": status, "detail": detail})

    tmux_ok = bool(which("tmux"))
    # Initialised empty: the per-project loop only reads them when tmux_ok,
    # but empty containers keep the flow safe (and statically honest) either
    # way — an unread world is reported via the tmux check above, never as
    # silently-ok.
    ttys: set = set()
    procs: list = []
    if tmux_ok:
        try:
            pane_fn = _pane_ttys or tc.pane_ttys
            proc_fn = _chat_procs or tc.chat_processes
            ttys, procs = pane_fn(), proc_fn()
        except tc.PaneMatcherMissing as exc:
            # The ONE pane matcher (hscc-api/repl_inject) is not loadable —
            # this module no longer owns a REPL rule (GAP-C-3), so a rule it
            # cannot consult is UNVERIFIED, never a private fallback verdict.
            _add("tmux", "unverified",
                 f"{exc} — REPL visibility from the iOS app is unknown")
            return checks
        except Exception as exc:  # tmux/ps refused or broke — never claim ok
            _add("tmux", "unverified",
                 f"tmux cannot be consulted ({exc}) — REPL visibility from "
                 "the iOS app is unknown")
            return checks
    else:
        _add("tmux", "problem",
             "tmux is not installed (brew install tmux) — the iOS app can "
             "only deliver into a REPL that runs inside tmux")

    for proj in projects:
        profile = f"{proj.name}-orch"
        if not tmux_ok:
            _add(f"repl:{proj.name}", "problem",
                 f"no tmux: REPL for '{profile}' (if any) is invisible to the "
                 "iOS app — start it with: hscc project chat " + proj.name)
            continue
        # `procs` is [(tty, profile)]: every live REPL chat, tmux or not.
        on_tty = [t for t, p in procs if p == profile]
        if not on_tty:
            # No chat running is a NORMAL state (the operator is not always
            # in chat) — not a problem. The launch door guarantees the next
            # `hscc project chat` opens inside tmux; say so, don't cry wolf.
            _add(f"repl:{proj.name}", "ok",
                 f"no REPL running for '{profile}' right now — "
                 f"`hscc project chat {proj.name}` opens one inside tmux")
        elif all(t not in ttys for t in on_tty):
            _add(f"repl:{proj.name}", "problem",
                 f"REPL for '{profile}' runs OUTSIDE tmux (tty "
                 f"{', '.join(sorted(on_tty))}) — iOS messages will NOT "
                 f"reach it; reopen with: hscc project chat {proj.name}")
        else:
            _add(f"repl:{proj.name}", "ok",
                 f"REPL for '{profile}' is in a tmux pane — iOS delivery "
                 "targets it")
    return checks


def _learning_has_problem(learning: list[dict]) -> bool:
    """True when any learning check is not ``ok``.

    UNVERIFIED counts as a problem: an unverifiable pipeline must be loud, not
    silently \"clean\" — the hard rule this command is built around.
    """
    return any(c["status"] != "ok" for c in learning)


def _declaration_checks(applied_state_path=None, config=None,
                        profiles_dir=None, proxy_config=None,
                        litellm_config=None, balancer_module=None,
                        _http_get=None, _run_agreement=None) -> list[dict]:
    """Template declaration <-> live agreement (spec §6 legs + the t_be4b94ca
    per-unit alias-coverage leg).

    ``hscc project doctor`` is the operator's "is this setup trustworthy"
    surface, and the v4 template declarations (docs/design/2026-10-15-
    template-declarations.md) are part of what must be trustworthy: what the
    applied template DECLARED must agree with what hermes configs,
    ``proxy.yaml``, the generated litellm config and the live proxy ACTUALLY
    are. The logic itself is ONE implementation in
    ``hscc_daemon.declaration_agreement`` (C3 / t_3306cd3c) — this function is
    the presentation adapter: leg ok True/False/None -> status
    ok/problem/unverified, and the leg's remediation (which names
    ``hscc cluster monitor --live`` as the load-instrument, spec §3.2) is
    surfaced in the detail.

    PURE READ on both sides: doctor never repairs and never restarts anything
    here (same hard rule as the command itself).

    Statuses (the hard rule: an unverifiable answer is never silently ok):
      ok          — every leg reads and agrees (or is a documented not-applicable).
      problem     — a leg disagrees (the consequence is named in the detail).
      unverified  — the module or a leg's inputs cannot be read (no applied
                    state, no proxy.yaml, unreachable proxy) — fail-closed,
                    the provider_routing doctrine.

    ``_run_agreement`` is the injectable seam (tests inject a fake, or plant
    tmp state files and let the real module run); the default resolves
    ``hscc_daemon.declaration_agreement`` the way ``_theme``/``message``
    resolve the peer plugin — a standalone flightdeck install without
    hscc_daemon reports UNVERIFIED rather than crashing or claiming health.
    """
    run_agreement = _run_agreement
    if run_agreement is None:
        try:
            from hscc_daemon import declaration_agreement as _da
        except Exception:  # ImportError or a broken peer package
            return [{"check": "declaration_agreement",
                     "status": "unverified",
                     "detail": "hscc_daemon.declaration_agreement unavailable — "
                               "the four spec §6 legs cannot be consulted "
                               "(a false all-clear is the worst output this "
                               "command can produce)"}]
        run_agreement = _da.run_declaration_agreement
    try:
        res = run_agreement(
            "all", applied_state_path=applied_state_path, config=config,
            profiles_dir=profiles_dir, proxy_config=proxy_config,
            litellm_config=litellm_config, balancer_module=balancer_module,
            _http_get=_http_get)
    except Exception as exc:  # never let a check crash the whole doctor
        return [{"check": "declaration_agreement", "status": "unverified",
                 "detail": f"the agreement run raised ({exc}) — unverified, "
                           "never assumed ok"}]
    status_map = {True: "ok", False: "problem", None: "unverified"}
    out: list[dict] = []
    for leg in res.get("legs", []):
        status = status_map.get(leg.get("ok"), "unverified")
        detail = leg.get("detail", "")
        if status != "ok" and leg.get("remediation"):
            detail = f"{detail} — remediation: {leg['remediation']}"
        out.append({"check": leg.get("name", "leg"),
                    "status": status, "detail": detail})
    return out


_NOT_PROVIDED = object()


def _run_checks(projects: list[registry.Project], *, _run=None, _client=None,
                _boards=_NOT_PROVIDED, _workdirs=_NOT_PROVIDED,
                _topics=_NOT_PROVIDED, _repo_check=None) -> list[dict]:
    """Per-project report: [{name, checks: {dim: {ok, detail}}}].

    ``_boards`` / ``_workdirs`` / ``_topics`` are injectable snapshots with
    three states: a real value is used as-is; ``None`` means "this source was
    *tried and failed*" (unverifiable); and the ``_NOT_PROVIDED`` sentinel (the
    default) means the caller wants this command to go read it itself. The
    distinction matters: ``None`` must be treated as an unverifiable problem,
    never conflated with "not injected".

    The ``workdir`` triangle dimension (board ``default_workdir`` vs project
    ``repo``) is only added to the report when workdir metadata is actually
    available — i.e. when ``_workdirs`` is a real map. When the caller injects
    board *slugs only* (as the pre-existing doctor tests do) the workdir
    dimension is omitted, not faked. In production doctor always reads
    workdirs, so the dimension is always present there.

    ``_repo_check`` defaults to :func:`_repo_ok`; tests inject a fake so they
    never need a real git checkout on disk.
    """
    boards_injected = _boards is not _NOT_PROVIDED
    if _boards is _NOT_PROVIDED:
        try:
            _boards = kanban.list_boards()
        except kanban.KanbanError:
            _boards = None  # boards unreadable -> all board checks unverifiable

    if _workdirs is _NOT_PROVIDED:
        if boards_injected or _boards is None:
            # Injected slugs only (tests) -> no workdir data; or boards already
            # unreadable -> workdir is redundant. Either way, omit the workdir
            # dimension rather than fake it.
            _workdirs = None
        else:
            try:
                _workdirs = kanban.board_workdirs()
            except kanban.KanbanError:
                _workdirs = None  # unverifiable -> workdir check flags it

    # Topics are no longer read from a live Telegram transport (removed). The
    # ``topic`` check now only reports the recorded field; any injected
    # ``_topics`` value is ignored by ``_topic_ok``.
    _topics = None

    repo_check = _repo_check if _repo_check is not None else _repo_ok

    binding = _fleet_binding(projects)
    board_owners = binding["board_owners"]
    topic_owners = binding["topic_owners"]

    report: list[dict] = []
    for proj in projects:
        repo = repo_check(proj, _run=_run)

        if _boards is None:
            board = {"ok": False, "detail": "unverifiable — cannot read board list"}
        else:
            board = _board_ok(proj, _boards)
            # Strictly, a registry naming a board that another project also
            # binds is a mis-binding — two projects' work would merge into one
            # board. Surface it on this project even when the slug exists.
            shared_board = None
            if proj.board:
                shared_board = _unique_board_detail(
                    str(proj.board), board_owners.get(str(proj.board), [])
                )
            if shared_board and board["ok"]:
                board = {"ok": False, "detail": shared_board}

        topic = _topic_ok(proj, _topics)
        if proj.topic is not None:
            shared_topic = _unique_topic_detail(
                int(proj.topic), topic_owners.get(int(proj.topic), [])
            )
            if shared_topic and topic["ok"]:
                topic = {"ok": False, "detail": shared_topic}

        checks: dict = {"repo": repo, "board": board, "topic": topic}
        if isinstance(_workdirs, dict):
            checks["workdir"] = _workdir_ok(proj, _workdirs)

        report.append({"name": proj.name, "checks": checks})
    return report


def _any_problem(report: list[dict]) -> bool:
    return any(not d["ok"] for row in report for d in row["checks"].values())


def _all_clear_count(report: list[dict]) -> int:
    """Projects whose EVERY check is ok — the triangle-passing count.

    A project that passes all checks (repo, board, workdir, topic) contributes
    to the count. This is the number the all-clear line names, so a clean
    result is *evidence* — an explicit count — rather than mere absence of
    output.
    """
    return sum(
        1 for row in report if all(d["ok"] for d in row["checks"].values())
    )


def _render(report: list[dict]) -> list[str]:
    lines: list[str] = []
    for row in report:
        lines.append(f"{escape(row['name'])}:")
        for dim, res in row["checks"].items():
            mark = "ok" if res["ok"] else "PROBLEM"
            lines.append(f"  {dim:<6} {escape('[' + mark + ']')} {escape(str(res['detail']))}")
        lines.append("")
    return lines


def _render_json(report: list[dict]) -> dict:
    return {
        row["name"]: {dim: {"ok": res["ok"], "detail": res["detail"]}
                      for dim, res in row["checks"].items()}
        for row in report
    }


def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "doctor",
        help="self-check: is hscc project's view of the fleet trustworthy",
        epilog="example: hscc project doctor",
    )
    p.set_defaults(func=cmd_doctor)


def cmd_doctor(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    report = _run_checks(
        projects,
        _run=args.run,
        _client=args.client,
        _boards=(args.boards if getattr(args, "boards", None) is not None else _NOT_PROVIDED),
        _workdirs=(args.workdirs if getattr(args, "workdirs", None) is not None else _NOT_PROVIDED),
        _topics=(args.topics if getattr(args, "topics", None) is not None else _NOT_PROVIDED),
        _repo_check=getattr(args, "repo_check", None),
    )

    # The learning-pipeline section runs only when the caller opts in
    # (production always does; tests inject a world). When it is skipped the
    # existing triangle-only behaviour is left completely untouched.
    learning = None
    if getattr(args, "learning", _NOT_PROVIDED) is not _NOT_PROVIDED:
        learning = _learning_checks(
            _home=getattr(args, "home", None),
            _env=getattr(args, "env", None),
            _now=getattr(args, "now", None),
            _probe=getattr(args, "probe", None),
            _exists=getattr(args, "exists", None),
            _access=getattr(args, "access", None),
            _mtime=getattr(args, "mtime", None),
            _memory_files=getattr(args, "memory_files", None),
            _scandir=getattr(args, "scandir", None),
            _plist_source=getattr(args, "plist_source", None),
        )

    # tmux/REPL section (same opt-in sentinel as `learning`): the iOS app can
    # only deliver into a REPL that lives in a tmux pane — surface REPLs that
    # would silently miss iPhone messages.
    tmux_repl = None
    if getattr(args, "tmux_repl", _NOT_PROVIDED) is not _NOT_PROVIDED:
        tmux_repl = _tmux_repl_checks(
            projects=projects,
            _which=getattr(args, "which", None),
            _pane_ttys=getattr(args, "pane_ttys", None),
            _chat_procs=getattr(args, "chat_procs", None),
        )

    # Template declaration <-> live agreement (spec §6, C3 / t_3306cd3c). Same
    # opt-in sentinel; production (`run`) always includes it. Pure-read — this
    # section never repairs and never restarts.
    declaration = None
    if getattr(args, "declaration", _NOT_PROVIDED) is not _NOT_PROVIDED:
        declaration = _declaration_checks(
            applied_state_path=getattr(args, "applied_state_path", None),
            config=getattr(args, "config", None),
            profiles_dir=getattr(args, "profiles_dir", None),
            proxy_config=getattr(args, "proxy_config", None),
            litellm_config=getattr(args, "litellm_config", None),
            balancer_module=getattr(args, "balancer_module", None),
            _http_get=getattr(args, "http_get", None),
            _run_agreement=getattr(args, "run_agreement", None),
        )

    if args.json:
        payload = _render_json(report)
        if learning is not None:
            payload["_learning"] = {
                c["check"]: {"status": c["status"], "detail": c["detail"]}
                for c in learning
            }
        if tmux_repl is not None:
            payload["_tmux_repl"] = {
                c["check"]: {"status": c["status"], "detail": c["detail"]}
                for c in tmux_repl
            }
        if declaration is not None:
            payload["_declaration"] = {
                c["check"]: {"status": c["status"], "detail": c["detail"]}
                for c in declaration
            }
        print(json.dumps(payload))
    else:
        if not report:
            make_console().print(panel(
                "doctor", "no projects in the registry — nothing to check."))
            return 0
        lines = _render(report)
        if learning is not None:
            lines.append("learning pipeline:")
            for c in learning:
                mark = _STATUS_MARK.get(c["status"], c["status"])
                mark_str = str(mark)
                lines.append(
                    f"  {escape(c['check'])} {escape('[' + mark_str + ']')} "
                    f"{escape(c['detail'])}"
                )
            lines.append("")
        if tmux_repl is not None:
            lines.append("iOS chat reachability (tmux/REPL):")
            for c in tmux_repl:
                mark = _STATUS_MARK.get(c["status"], c["status"])
                mark_str = str(mark)
                lines.append(
                    f"  {escape(c['check'])} {escape('[' + mark_str + ']')} "
                    f"{escape(c['detail'])}"
                )
            lines.append("")
        if declaration is not None:
            lines.append("template declaration <-> live agreement (spec §6):")
            for c in declaration:
                mark = _STATUS_MARK.get(c["status"], c["status"])
                mark_str = str(mark)
                lines.append(
                    f"  {escape(c['check'])} {escape('[' + mark_str + ']')} "
                    f"{escape(c['detail'])}"
                )
            lines.append("")
        make_console().print(panel("doctor", "\n".join(lines)))

    problems = _any_problem(report) or (
        learning is not None and _learning_has_problem(learning)
    ) or (
        tmux_repl is not None and any(
            c["status"] in ("problem", "unverified") for c in tmux_repl)
    ) or (
        declaration is not None and any(
            c["status"] in ("problem", "unverified") for c in declaration)
    )
    all_clear = _all_clear_count(report)
    passes = f"{all_clear} of {len(report)} projects passed the triangle check"
    if problems:
        # The loud signal: exit non-zero so a clean-looking summary can never
        # be mistaken for all-clear. Human text goes to stdout; the exit code
        # is the programmable "not trustworthy" flag.
        print(f"doctor: NOT ALL CLEAR — {passes}.", file=sys.stderr)
        return 1
    print(f"doctor: TRIANGLE all clear — {passes}.", file=sys.stderr)
    return 0


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py: run doctor with injectable handles attached."""
    args.registry = registry_path
    args.run = getattr(args, "run", None)
    args.client = getattr(args, "client", None)
    args.boards = getattr(args, "boards", None)
    args.workdirs = getattr(args, "workdirs", None)
    args.topics = getattr(args, "topics", None)
    args.repo_check = getattr(args, "repo_check", None)
    # Doctor ALWAYS includes the learning-pipeline section in production. The
    # sentinel gating (in cmd_doctor) is what lets tests opt out via _args().
    args.learning = True
    # Same for the tmux/REPL section: iOS chat reachability is a first-class
    # "is this setup trustworthy" answer, not an extra.
    args.tmux_repl = True
    args.which = getattr(args, "which", None)
    args.pane_ttys = getattr(args, "pane_ttys", None)
    args.chat_procs = getattr(args, "chat_procs", None)
    # Declaration<->live agreement section (template-declarations spec §6,
    # C3): production always includes it — "what the template declared vs
    # what the fleet actually runs" is part of "is this trustworthy".
    # Sentinel-gated like the other two so the pre-existing doctor tests
    # (which inject a world without it) stay untouched. Pure-read.
    args.declaration = True
    args.applied_state_path = getattr(args, "applied_state_path", None)
    args.config = getattr(args, "config", None)
    args.profiles_dir = getattr(args, "profiles_dir", None)
    args.proxy_config = getattr(args, "proxy_config", None)
    args.litellm_config = getattr(args, "litellm_config", None)
    args.balancer_module = getattr(args, "balancer_module", None)
    args.http_get = getattr(args, "http_get", None)
    args.run_agreement = getattr(args, "run_agreement", None)
    projects = registry.load_registry(registry_path)
    return cmd_doctor(args, projects)
