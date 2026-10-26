"""project_lifecycle.py — orchestration for `flightdeck project new`.

Wires the lifecycle steps — git repo, kanban board, ROADMAP.md, orchestrator
profile + chat session, registry entry — into one idempotent command, under
the hard rule that partial failure never leaves a half-registered project.

It does NOT edit the existing core modules (registry / git_state / kanban);
it composes them. The only new things here are the orchestration of the
lifecycle steps and a registry *upsert* helper (load + mutate + save is all
the existing registry's public API allows — there is no update function, so
it is built here from ``load_registry`` + ``save_registry``).

Every external effect flows through an injectable hook so tests stub them and
never touch git, the network, or the live cluster:

- ``_run``      ``(cmd_list, cwd) -> proc``  git + ``gh`` subprocesses
- ``_kanban``   ``() -> kb module``          providor for board operations

Each step is INDEPENDENTLY idempotent: re-running on state that already
satisfies it is a true no-op (adopt, not duplicate). The registry is the
record of what succeeded, so a step that fails is reported with the exact
command to retry — never silently dropped.

The registry ``topic`` field is retained for historical/imported data but is
no longer fed by a Telegram step (Telegram was removed).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from . import kanban, registry


class LifecycleError(Exception):
    """A step could not be completed and must be reported, not swallowed."""


# The git remote name used for the optional GitHub push.
DEFAULT_REMOTE = "origin"

# The seeded ROADMAP.md. Now / Next / Later, per the DESIGN addendum.
ROADMAP_TEMPLATE = (
    "# {name}\n"
    "\n"
    "## Now\n"
    "- [ ] First goal\n"
    "\n"
    "## Next\n"
    "- [ ] Upcoming\n"
    "\n"
    "## Later\n"
    "- [ ] Someday\n"
)


def _default_run(cmd, cwd) -> subprocess.CompletedProcess:
    """Production subprocess runner. ``_run=None`` falls back to this.

    Matches git_state's shape: ``(cmd_list, cwd) -> proc`` with ``.returncode``,
    ``.stdout``, ``.stderr``. Any OSError (missing dir, missing git) yields a
    synthetic failed process (rc 128) so callers degrade gracefully.
    """
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        return subprocess.CompletedProcess(cmd, returncode=128, stdout="", stderr=str(exc))


def _run_cmd(cmd, cwd, runner):
    if runner is not None:
        return runner(cmd, cwd)
    return _default_run(cmd, cwd)


def _kanban_provider():
    """Production kanban module provider: Hermes' own kanban library."""
    return kanban._load_kanban_db()


def _resolve_kanban(_kanban):
    return _kanban if _kanban is not None else _kanban_provider()


# --------------------------------------------------------------------------- #
# Registry upsert (the only registry primitive NOT provided by registry.py).
# --------------------------------------------------------------------------- #


def upsert_project(
    path: str,
    *,
    name: str,
    repo: str,
    board: Optional[str] = None,
    topic: Optional[int] = None,
    topic_name: Optional[str] = None,
    roadmap: Optional[str] = None,
) -> registry.Project:
    """Create or update the registry entry for one project. Idempotent.

    ``repo`` is required on create. On an existing entry only the explicitly
    passed non-None fields are updated — so a half-created project can be
    repaired one field at a time without ever duplicating or dropping a row.

    Never touches the repo, the board, or the topic — this is registry-only.
    """
    projects = registry.load_registry(path)
    existing = None
    for proj in projects:
        if proj.name == name:
            existing = proj
            break

    if existing is not None:
        if repo is not None:
            existing.repo = repo
        if board is not None:
            existing.board = board
        if topic is not None:
            existing.topic = topic
        if topic_name is not None:
            existing.topic_name = topic_name
        if roadmap is not None:
            existing.roadmap = roadmap
        registry.save_registry(projects, path)
        return existing

    # New entry: repo is mandatory.
    if not repo or not str(repo).strip():
        raise registry.MissingRepoError("project 'repo' is required")
    project = registry.Project(
        name=name,
        repo=os.path.expanduser(str(repo).strip()),
        board=board,
        topic=topic,
        topic_name=topic_name,
        roadmap=roadmap,
    )
    projects.append(project)
    registry.save_registry(projects, path)
    return project


# --------------------------------------------------------------------------- #
# Step 1 — git repo
# --------------------------------------------------------------------------- #

def is_git_repo(repo: str, _run=None) -> bool:
    """True if ``repo`` is a git work tree (or the dir can't be reached)."""
    cp = _run_cmd(["git", "rev-parse", "--is-inside-work-tree"], repo, _run)
    return cp.returncode == 0


def ensure_git_repo(
    repo: str,
    *,
    github: bool = False,
    private: bool = False,
    remote_name: str = DEFAULT_REMOTE,
    _run=None,
) -> dict:
    """Step 1 — init the repo (and optionally a GitHub remote + push).

    Idempotent: an existing repo is a no-op — no new commits, no re-init.
    Only a freshly-initialized repo gets a first commit (best-effort: an
    empty repo commits nothing rather than erroring).

    Returns ``{"status": "created"|"exists", "github": ...}`` where
    ``github`` is ``skipped`` | ``created`` | ``exists``. Raises
    :class:`LifecycleError` only when the repo cannot be left in a git state.
    """
    Path(repo).mkdir(parents=True, exist_ok=True)

    created = not is_git_repo(repo, _run)
    if created:
        init = _run_cmd(["git", "init"], repo, _run)
        if init.returncode != 0:
            raise LifecycleError(f"git init failed: {(init.stderr or '').strip()}")
        # Initial commit — best-effort; a genuinely empty repo has nothing to
        # stage, and that is not an error (step 4 may seed ROADMAP.md later).
        _run_cmd(["git", "add", "-A"], repo, _run)
        commit = _run_cmd(
            ["git", "commit", "-m", f"Initial commit for {Path(repo).name}"],
            repo,
            _run,
        )
        if commit.returncode != 0:
            # "nothing to commit" (fresh empty repo) is fine; anything else is
            # still surfaced in the report, not raised.
            pass

    github_status = "skipped"
    if github:
        github_status = ensure_github(repo, _run, private=private, remote_name=remote_name)

    return {"status": "created" if created else "exists", "github": github_status}


def ensure_github(repo: str, _run=None, *, private: bool = False,
                  remote_name: str = DEFAULT_REMOTE) -> str:
    """Step 1b — create the GitHub remote via ``gh`` and push.

    Idempotent against an already-created remote: ``gh repo create`` reporting
    the remote already exists is read as ``exists``, not an error. The repo is
    never corrupted by a failed GitHub step — the local repo is already valid.

    Returns ``created`` | ``exists``. Raises :class:`LifecycleError` if ``gh``
    fails for a reason other than the remote already existing (so the command
    can report it and give the retry command; the repo itself is untouched).
    """
    name = Path(repo).name
    args = ["gh", "repo", "create", name, "--source", repo, "--remote", remote_name, "--push"]
    if private:
        args.insert(2, "--private")
    cp = _run_cmd(args, repo, _run)
    if cp.returncode == 0:
        return "created"
    err = (cp.stderr or "").strip().lower()
    # gh is idempotent about an existing remote: report exists, not failure.
    if "already exists" in err:
        return "exists"
    raise LifecycleError(f"gh repo create failed: {err or '(no error output)'}")


# --------------------------------------------------------------------------- #
# Step 3 — kanban board
# --------------------------------------------------------------------------- #

def ensure_board(slug: str, _kanban=None) -> str:
    """Step 3 — ensure a kanban board with slug ``slug`` exists.

    Idempotent: Hermes' ``create_board`` is ``mkdir -p`` semantics — it
    returns the existing metadata when the board is already there, so a
    re-run never duplicates anything. Returns the normalized slug.
    """
    kb = _resolve_kanban(_kanban)
    try:
        exists = kb.board_exists(slug)
    except Exception as exc:  # board provider surfaced an error
        raise LifecycleError(f"could not check board {slug!r}: {exc}") from exc

    if not exists:
        kb.create_board(slug)
    return slug


# --------------------------------------------------------------------------- #
# Step 4 — ROADMAP.md
# --------------------------------------------------------------------------- #

def ensure_roadmap(repo: str, _run=None) -> str:
    """Step 4 — seed ROADMAP.md at the repo root if it does not exist.

    Idempotent: an existing ROADMAP.md is left untouched (never overwritten —
    the operator's edits are the source of truth once seeded). Returns
    ``created`` | ``exists``. Raises :class:`LifecycleError` on an unwritable
    repo directory.
    """
    path = Path(repo) / "ROADMAP.md"
    if path.exists():
        return "exists"
    try:
        path.write_text(
            ROADMAP_TEMPLATE.format(name=Path(repo).name),
            encoding="utf-8",
        )
    except OSError as exc:
        raise LifecycleError(f"could not write ROADMAP.md in {repo!r}: {exc}") from exc
    return "created"


# --------------------------------------------------------------------------- #
# Step 5 — registry entry
# --------------------------------------------------------------------------- #

def ensure_registry(
    registry_path: str,
    *,
    name: str,
    repo: str,
    board: Optional[str] = None,
    topic: Optional[int] = None,
    topic_name: Optional[str] = None,
    roadmap: Optional[str] = None,
) -> registry.Project:
    """Step 5 — write the registry entry binding repo <-> board <-> topic.

    Registry-only; never touches the repo/board/topic. Idempotent via
    :func:`upsert_project`: a half-registered project is repaired, never
    duplicated.
    """
    return upsert_project(
        registry_path,
        name=name,
        repo=repo,
        board=board,
        topic=topic,
        topic_name=topic_name,
        roadmap=roadmap,
    )


# --------------------------------------------------------------------------- #
# Step 6 — orchestrator profile (<name>-orch) + chat session (<name>)
# --------------------------------------------------------------------------- #

def _orch_profile_name(name: str) -> str:
    """The orchestrator profile name for a project: ``<name>-orch``."""
    return f"{name}-orch"


def ensure_profile(name: str, _run=None) -> str:
    """Step 6a — ensure the ``<name>-orch`` orchestrator profile exists.

    The chat transport is ``hermes -p <name>-orch ...``. Before this step,
    ``flightdeck project new`` registered a project but created no such
    profile, so a brand-new project could never be chatted with — hermes
    errors ``Profile '<name>-orch' does not exist`` and the transport has no
    fallback. This makes provisioning the profile part of the lifecycle.

    Idempotent via the injectable ``_run`` seam (same as every other
    subprocess step): if ``hermes profile list`` already shows the profile it
    is a no-op ``skipped``, NEVER an error. Returns ``created`` | ``skipped``.
    Raises :class:`LifecycleError` only when the create command itself fails
    for a reason other than the profile existing.
    """
    profile = _orch_profile_name(name)
    # Existence probe. A nonzero probe (unreadable environment) is NOT an
    # error — it just proceeds to create, and create reports the truth.
    listed = _run_cmd(["hermes", "profile", "list"], os.getcwd(), _run)
    exists = (
        listed.returncode == 0 and profile in (listed.stdout or "").split()
    )
    if exists:
        return "skipped"
    created = _run_cmd(["hermes", "profile", "create", profile], os.getcwd(), _run)
    if created.returncode != 0:
        raise LifecycleError(
            f"hermes profile create {profile} failed: "
            f"{(created.stderr or '').strip() or '(no error output)'}"
        )
    return "created"


class HermesRuntimeUnavailable(RuntimeError):
    """Raised when hermes_cli/hermes_state cannot be imported at all.

    Distinct from "profile has no state.db": this means the *interpreter* is
    wrong, so callers must say so rather than reporting an empty result.
    """

# --------------------------------------------------------------------------- #
# Hermes runtime delegation (t_9a9aeab1)
# --------------------------------------------------------------------------- #
# The brew keg installs hscc into an ISOLATED venv carrying only
# PyYAML/rich/psutil/markdown-it-py/mdurl/pygments (Formula/hscc.rb). Measured
# on the shipped keg (docs/audits/t_9a9aeab1_brew_venv_hermes_runtime.md):
#
#     import hermes_cli   -> ModuleNotFoundError
#     import hermes_state -> ModuleNotFoundError
#     import httpx        -> ModuleNotFoundError
#
# so an in-process ``from hermes_cli import ...`` / ``from hermes_state import
# SessionDB`` dies there. The fix is NOT to vendor hermes_cli/httpx into the
# keg: that duplicates a moving runtime's package set (version drift turns the
# "environment fault rendered as data fact" trap this class exists to prevent
# into a maintenance treadmill) and widens the tap's supply surface. Instead
# every runtime touch is executed as a small, bounded op under the HERMES venv
# interpreter — Hermes' OWN packages, so there is no second package set that
# can drift. Precedent for resolving that interpreter in-repo:
# hscc_daemon/install.py `_resolve_python`, hscc_daemon/api_launchd.py
# `recommend_api_python`, hscc_daemon/lifecycle.py `find_hermes_bin`.
#
# A suitable `hermes` CLI verb for "ensure a session titled X exists" does NOT
# exist (`hermes sessions` has list/rename/repair/recover but no create-titled;
# `hermes profile create` creates no session), so delegation — not CLI reuse —
# is the way to keep Hermes' own SessionDB machinery authoritative: the child
# calls the very same SessionDB APIs the in-process path calls.
#
# The delegation keeps the in-process contract exactly:
#   * unresolvable hermes runtime   -> raise HermesRuntimeUnavailable (an
#     environment fault, never rendered as "profile has no data" — the
#     semantic this class exists for);
#   * profile unresolvable / no state.db -> None (a data fact, as before);
#   * otherwise a duck-typed handle exposing the SessionDB methods the
#     lifecycle/discovery/digest/seed call paths use; each call is one op;
#   * a transport/op failure RAISES (a failed LOOK is never a false negative —
#     the e2a3251 honesty rule).

# Bounded wall-clock for one remote runtime op. A healthy op is ~1s (measured
# with the repo's audit probes); this is far above that while still failing a
# wedged interpreter instead of hanging `project new` forever.
_REMOTE_TIMEOUT_SECONDS = 120

# The child-side program executed under the hermes venv interpreter. STATIC
# text (no %-formatting of code) — the op name and its arguments arrive as one
# JSON document in sys.argv[1]; the agent-tree path comes from
# HERMES_AGENT_PATH (default ``~/.hermes/hermes-agent``, the same convention
# as flightdeck.core.kanban._HERMES_AGENT_PATH), which the child inherits
# exactly as the parent reads it.
#
# Protocol: prints exactly one JSON line on stdout.
#   {"ok": true,  "result": <json>}                        success
#   {"ok": false, "code": "no_runtime"|"no_db"|"op_error", "error": str}
# Classification contract (mirrors the in-process opener EXACTLY):
#   no_runtime -> the interpreter cannot host the runtime (environment fault)
#   no_db      -> profile unresolvable / missing / unusable state.db (a DATA
#                 fact; the parent maps it to None / an honest step failure)
#   op_error   -> a data op failed (a LOOK failure; the parent raises)
_REMOTE_OP_SCRIPT = r"""
import sys, json, os

def _emit(obj):
    print(json.dumps(obj, default=str))

def _main():
    args = json.loads(sys.argv[1])
    op = args.get("op")
    root = os.path.expanduser(
        os.environ.get("HERMES_AGENT_PATH", "~/.hermes/hermes-agent"))
    if os.path.isdir(root) and root not in sys.path:
        sys.path.insert(0, root)
    try:
        from hermes_cli import profiles as _p
        from hermes_state import SessionDB
    except Exception as e:
        _emit({"ok": False, "code": "no_runtime",
               "error": "cannot import the Hermes runtime: %s: %s"
                        % (type(e).__name__, e)})
        return 0
    try:
        canon = _p.normalize_profile_name(args["profile"])
        _p.validate_profile_name(canon)
    except Exception as e:
        _emit({"ok": False, "code": "no_db",
               "error": "profile %r unresolvable: %s: %s"
                        % (args.get("profile"), type(e).__name__, e)})
        return 0
    db = None
    try:
        if op == "profile_exists":
            _emit({"ok": True, "result": bool(_p.profile_exists(canon))})
            return 0
        if not _p.profile_exists(canon):
            _emit({"ok": False, "code": "no_db",
                   "error": "profile %r does not exist" % canon})
            return 0
        db_path = _p.get_profile_dir(canon) / "state.db"
        create_db = bool(args.get("create_db"))
        if not create_db and not db_path.exists():
            _emit({"ok": False, "code": "no_db",
                   "error": "profile %r has no state.db" % canon})
            return 0
        if op == "ensure_session":
            # create + title in ONE op (two separate ops could leak an
            # untitled row when the second fails). Always read-write:
            # ensuring means the row may be created — and a fresh orchestrator
            # profile legitimately has NO state.db yet (hermes itself creates
            # it on the profile's first run), so SessionDB(db_path) opening it
            # into existence is that native behaviour, not a repair.
            db = SessionDB(db_path=db_path)
            title = args["title"]
            if db.resolve_session_by_title(title):
                _emit({"ok": True, "result": {"status": "exists"}})
            else:
                import time as _t, uuid as _u
                new_id = "%s_first_%s" % (_t.strftime("%Y%m%d"),
                                          _u.uuid4().hex[:6])
                db.create_session(new_id, source="cli",
                                  model="orchestrator-model",
                                  profile_name=canon)
                db.set_session_title(new_id, title)
                _emit({"ok": True, "result": {"status": "created",
                                              "session": new_id,
                                              "title": title}})
            return 0
        read_only = bool(args.get("read_only"))
        db = SessionDB(db_path=db_path, read_only=read_only)
        if op == "open_check":
            _emit({"ok": True, "result": True})
        elif op == "resolve_by_title":
            _emit({"ok": True,
                   "result": db.resolve_session_by_title(args["title"])})
        elif op == "append_message":
            _emit({"ok": True,
                   "result": db.append_message(args["session_id"],
                                               args["role"],
                                               args["content"])})
        elif op == "get_session":
            _emit({"ok": True, "result": db.get_session(args["session_id"])})
        elif op == "list_sessions_rich":
            _emit({"ok": True, "result": db.list_sessions_rich(
                source=args.get("source"), limit=args.get("limit", 20))})
        elif op == "message_count":
            _emit({"ok": True,
                   "result": db.message_count(args.get("session_id"))})
        elif op == "get_messages":
            _emit({"ok": True, "result": db.get_messages(args["session_id"])})
        elif op == "get_session_title":
            _emit({"ok": True,
                   "result": db.get_session_title(args["session_id"])})
        else:
            _emit({"ok": False, "code": "op_error",
                   "error": "unsupported remote op %r" % op})
        return 0
    except Exception as e:
        detail = "%s: %s" % (type(e).__name__, e)
        # An open-time failure (db still unbound on a plain open) is the same
        # class of outcome the in-process opener returns None for; a failure
        # DURING a data op is a real LOOK failure.
        if db is None and op in ("open_check", "profile_exists"):
            _emit({"ok": False, "code": "no_db", "error": detail})
        else:
            _emit({"ok": False, "code": "op_error", "error": detail})
        return 0
    finally:
        try:
            if db is not None:
                db.close()
        except Exception:
            pass

try:
    sys.exit(_main())
except Exception as _boom:
    _emit({"ok": False, "code": "op_error",
           "error": "remote op harness failed: %s: %s"
                    % (type(_boom).__name__, _boom)})
    sys.exit(0)
"""


# Interpreter resolution cache: (env-key, resolved|None). Keyed on every env
# var that participates in resolution so a test (or a bootstrapping shell)
# that changes them re-resolves instead of inheriting a stale answer.
_HERMES_PY_CACHE: tuple = (None, None)


def reset_hermes_python_cache() -> None:
    """Drop the memoised interpreter resolution (tests / env changes)."""
    global _HERMES_PY_CACHE
    _HERMES_PY_CACHE = (None, None)


def resolve_hermes_python() -> Optional[str]:
    """Absolute path of an interpreter that can import the Hermes runtime.

    Candidates, each VERIFIED by a cheap ``import hermes_state,
    hermes_cli.profiles`` probe — the probe is the whole point: a *wrong*
    interpreter must never be chosen, or a delegation would fail somewhere the
    environment-vs-data distinction cannot see:

      1. ``HSCC_HERMES_PYTHON`` — explicit operator/test override;
      2. ``$HERMES_HOME/hermes-agent/venv/bin/python3`` then ``python``
         (HERMES_HOME-aware, like ``api_launchd.recommend_api_python``);
      3. the default ``~/.hermes/hermes-agent/venv/bin/`` layout;
      4. a ``python3``/``python`` on PATH whose site-packages can import the
         runtime (dev shells that ``pip install -e`` the agent).

    Returns None when nothing can host the runtime — callers must then raise
    :class:`HermesRuntimeUnavailable`, never fake a data answer. The result is
    memoised per relevant environment (resolution spawns probe processes).
    """
    global _HERMES_PY_CACHE
    key = tuple(os.environ.get(k, "") for k in
                ("HSCC_HERMES_PYTHON", "HERMES_HOME", "PATH", "HOME"))
    if _HERMES_PY_CACHE[0] == key:
        return _HERMES_PY_CACHE[1]
    result = _resolve_hermes_python_uncached()
    _HERMES_PY_CACHE = (key, result)
    return result


def _resolve_hermes_python_uncached() -> Optional[str]:
    seen: set = set()

    def _probe(cand: str) -> Optional[str]:
        if not cand:
            return None
        try:
            if cand in seen or not os.path.isfile(cand) \
                    or not os.access(cand, os.X_OK):
                return None
            seen.add(cand)
        except OSError:
            return None
        try:
            cp = subprocess.run(
                [cand, "-c", "import hermes_state, hermes_cli.profiles"],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return cand if cp.returncode == 0 else None

    hit = _probe(os.environ.get("HSCC_HERMES_PYTHON", "").strip())
    if hit:
        return hit

    homes: list = []
    env_home = os.environ.get("HERMES_HOME", "").strip()
    if env_home:
        homes.append(Path(env_home))
    homes.append(Path.home() / ".hermes")

    candidates: list = []
    for home in homes:
        bindir = home / "hermes-agent" / "venv" / "bin"
        candidates += [str(bindir / "python3"), str(bindir / "python")]
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    for cand in candidates:
        hit = _probe(cand)
        if hit:
            return hit
    return None


class RemoteRuntimeError(RuntimeError):
    """A remote runtime op failed with a classified error payload.

    ``code`` is the child's classification: ``no_db`` (data fact — the parent
    maps it to None / an honest step failure), ``op_error`` / ``transport`` (a
    failed LOOK — surfaced, never swallowed as empty data). ``no_runtime`` is
    never raised as this class: it converts to :class:`HermesRuntimeUnavailable`
    in :func:`_remote_runtime_op`.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _remote_default_run(cmd, cwd) -> subprocess.CompletedProcess:
    """Production runner for remote ops: same shape as ``_default_run`` plus a
    hard wall-clock timeout (a wedged interpreter must fail the step, never
    hang the lifecycle)."""
    try:
        return subprocess.run(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=_REMOTE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            cmd, returncode=124, stdout="",
            stderr=f"remote hermes runtime op timed out after "
                   f"{_REMOTE_TIMEOUT_SECONDS}s: {exc}")
    except (FileNotFoundError, OSError, ValueError) as exc:
        return subprocess.CompletedProcess(cmd, returncode=128, stdout="",
                                           stderr=str(exc))


def _remote_runtime_op(op: str, args: dict, runner=None):
    """Execute ONE runtime op under the hermes venv interpreter; return result.

    Raises:
      HermesRuntimeUnavailable — no interpreter can host the runtime (or the
        chosen one lost the runtime mid-flight): environment fault, never a
        data fact.
      RemoteRuntimeError(no_db) — profile unresolvable / missing state.db
        (data fact; callers translate to None / False / a step failure).
      RemoteRuntimeError(op_error|transport) — the op or the transport failed
        (a failed LOOK surfaces, per the e2a3251 honesty rule).
    """
    py = resolve_hermes_python()
    if py is None:
        raise HermesRuntimeUnavailable(
            "cannot import the Hermes runtime (hermes_cli/hermes_state). The "
            f"`hscc` CLI runs under {sys.executable}, and no interpreter with "
            "the Hermes runtime could be resolved (looked under "
            "$HSCC_HERMES_PYTHON, $HERMES_HOME/hermes-agent/venv/bin, "
            "~/.hermes/hermes-agent/venv/bin and PATH). Bootstrap Hermes or "
            "point HSCC_HERMES_PYTHON at its venv python; session history "
            "cannot be read from here.")
    payload_args = {"op": op, **args}
    run = runner or _remote_default_run
    proc = run([py, "-c", _REMOTE_OP_SCRIPT, json.dumps(payload_args)],
               os.getcwd())
    out = (getattr(proc, "stdout", "") or "").strip()
    err = (getattr(proc, "stderr", "") or "").strip()
    payload = None
    if out:
        try:
            payload = json.loads(out.splitlines()[-1])
        except (ValueError, IndexError):
            payload = None
    if payload is None:
        raise RemoteRuntimeError(
            "transport",
            f"remote hermes runtime op {op!r} produced no parseable result "
            f"(rc={getattr(proc, 'returncode', '?')}; stderr={err[:400]})")
    if payload.get("ok"):
        return payload.get("result")
    code = str(payload.get("code", "op_error"))
    message = str(payload.get("error", "unknown error"))
    if code == "no_runtime":
        # The probe passed but the chosen interpreter cannot import the
        # runtime — an environment fault all the same; say it as one.
        raise HermesRuntimeUnavailable(
            f"cannot import the Hermes runtime ({message}). The `hscc` CLI "
            f"runs under {sys.executable}; hermes_cli/hermes_state live in "
            "the Hermes venv. Session history cannot be read from here.")
    raise RemoteRuntimeError(code, message)


class RemoteSessionHandle:
    """Duck-typed ``SessionDB`` stand-in whose calls run as remote ops.

    Exists so the in-process call paths (session_discovery, digest, chat
    seed) keep their ``db.<method>()`` shape unchanged under an interpreter
    that cannot import the runtime. ``read_only`` is stamped per open, exactly
    like the real constructor; the child re-opens the DB per op.
    """

    def __init__(self, profile: str, read_only: bool, runner=None):
        self.profile = profile
        self.read_only = read_only
        self._runner = runner
        self.closed = False

    def _call(self, op: str, **extra):
        if self.closed:
            raise RuntimeError("session db handle is closed")
        return _remote_runtime_op(op, {"profile": self.profile,
                                       "read_only": self.read_only,
                                       **extra},
                                  runner=self._runner)

    def resolve_session_by_title(self, title):
        return self._call("resolve_by_title", title=title)

    def create_session(self, session_id, source="cli", **kwargs):
        # Deliberately unsupported on the remote handle: session creation must
        # go through ``ensure_session``, which does create+title in ONE atomic
        # remote op (two separate ops could leak an untitled row when the
        # second fails).
        raise NotImplementedError(
            "remote handle: use ensure_session (create+title in one op)")

    def set_session_title(self, session_id, title):
        raise NotImplementedError(
            "remote handle: use ensure_session (create+title in one op)")

    def append_message(self, session_id, role, content, **kwargs):
        return self._call("append_message", session_id=session_id,
                          role=role, content=content)

    def get_session(self, session_id):
        return self._call("get_session", session_id=session_id)

    def list_sessions_rich(self, source=None, limit=20, **kwargs):
        return self._call("list_sessions_rich", source=source, limit=limit)

    def message_count(self, session_id=None):
        return self._call("message_count", session_id=session_id)

    def get_messages(self, session_id, **kwargs):
        return self._call("get_messages", session_id=session_id)

    def get_session_title(self, session_id):
        return self._call("get_session_title", session_id=session_id)

    def close(self):
        self.closed = True


def _in_process_runtime():
    """Import the Hermes runtime in-process, or return (None, None, reason).

    ``reason`` is the import failure text so the honest environment-fault
    message can name the missing module (the original wording spirit). Any
    import-time exception — not just ImportError — counts as "this
    interpreter cannot host the runtime": what it lacks is delegation, not a
    second guess about whose package set is broken.
    """
    try:
        from hermes_cli import profiles as profiles_mod
        from hermes_state import SessionDB
        return profiles_mod, SessionDB, None
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def _open_profile_session_db(profile: str, read_only: bool = False,
                             create_db: bool = False):
    """Open ``<profile>``'s state.db via Hermes' own SessionDB, or None.

    Mirrors ``routes_orchestrator._open_profile_session_db`` (same call
    sequence, same fail-safe-to-None) so this module and the REST chat handler
    agree on what "the project's session" is. ``read_only=True`` opens the DB
    HERMES-native read-only (``SessionDB(read_only=...)``, the equivalent of
    ``mode=ro`` on the underlying connection) — used by the discovery paths
    that must never write to the operator's live session databases. Returns
    ``None`` when the profile is unresolvable or has no state.db — the caller
    then reports an honest step failure rather than guessing.

    ``create_db=True`` (used only by the ensure-session path) accepts a
    profile directory whose state.db does not exist yet: a freshly created
    orchestrator profile legitimately has none (hermes itself creates state.db
    on the profile's first run), and SessionDB opening it into existence IS
    that native behaviour. Read paths never pass it.

    In-process when this interpreter can import the runtime; otherwise
    DELEGATED to the hermes venv interpreter as remote ops behind a duck-typed
    handle (t_9a9aeab1 — the brew keg venv has no hermes_cli/httpx). The two
    semantics stay exactly distinct: a wrong/unresolvable interpreter raises
    :class:`HermesRuntimeUnavailable`; an unresolvable profile or a missing
    state.db returns None (a data fact).
    """
    profiles_mod, SessionDB, import_err = _in_process_runtime()
    if profiles_mod is None:
        # This interpreter cannot see the Hermes runtime at all. Swallowing it
        # made `project sessions` print "no telegram history" for projects
        # with years of history, because the `hscc` entry point ran under an
        # interpreter while hermes_cli lives in the hermes venv. An environment
        # fault must never render as a data fact — so first try delegating to
        # an interpreter that CAN host the runtime; when none resolves, raise
        # exactly the honest fault the original code raised.
        if resolve_hermes_python() is None:
            raise HermesRuntimeUnavailable(
                f"cannot import the Hermes runtime ({import_err}). The `hscc` "
                f"CLI runs under {sys.executable}; hermes_cli/hermes_state "
                "live in the Hermes venv, and no interpreter with the runtime "
                "could be resolved ($HSCC_HERMES_PYTHON, "
                "$HERMES_HOME/hermes-agent/venv/bin, "
                "~/.hermes/hermes-agent/venv/bin, PATH). Session history "
                "cannot be read from here.")
        # Mirror the in-process None semantics remotely: one cheap open_check
        # op classifies "profile/dir/db unusable" (None, a data fact) from a
        # real LOOK failure (raises).
        try:
            _remote_runtime_op("open_check",
                               {"profile": profile, "read_only": read_only,
                                "create_db": create_db})
        except RemoteRuntimeError as exc:
            if exc.code == "no_db":
                return None
            raise
        return RemoteSessionHandle(profile, read_only)

    try:
        canon = profiles_mod.normalize_profile_name(profile)
        profiles_mod.validate_profile_name(canon)
        if not profiles_mod.profile_exists(canon):
            return None
        db_path = Path(profiles_mod.get_profile_dir(canon)) / "state.db"
        if not create_db and not db_path.exists():
            return None
        return SessionDB(db_path=db_path, read_only=read_only)
    except Exception:
        return None


def _resolve_session_db(profile, _session_db):
    """Resolve the session-db provider seam (default: open the real profile)."""
    return _session_db if _session_db is not None else _open_profile_session_db


def ensure_session(name: str, profile: str, _session_db=None) -> dict:
    """Step 6b — ensure a Hermes session titled ``name`` exists on ``profile``.

    Identical to ``routes_orchestrator._ensure_session_exists`` (the REST
    chat handler's own back-fill, reused here rather than reimplemented
    divergent): open the profile's state.db and, when no session is titled
    ``name``, create one via ``create_session`` + ``set_session_title``.

    Under an interpreter that cannot host the Hermes runtime (the brew keg
    venv — t_9a9aeab1) the whole ensure runs as ONE remote op under the hermes
    venv interpreter: create + title land atomically there, so a partial
    failure can never leak an untitled row. A fresh profile with no state.db
    yet is legitimately ensure-able (hermes itself creates state.db on first
    run), so this path opens with ``create_db=True`` — unlike read paths.

    Idempotent + never clobbers: an existing titled session is left exactly as
    it is. Returns ``{"status": "exists"}`` when already present, or
    ``{"status": "created", "session": <id>, "title": name}``. Raises
    :class:`LifecycleError` when the profile has no usable state.db, so the
    caller records it as a step failure that ``repair`` retries idempotently.
    Raises :class:`HermesRuntimeUnavailable` when no interpreter can host the
    Hermes runtime at all — an environment fault, never a step-shaped lie.
    """
    if _session_db is None:
        profiles_mod, _SessionDB, _import_err = _in_process_runtime()
        if profiles_mod is None:
            # Delegation path: the atomic one-op ensure under the hermes venv.
            # create_db=True mirrors the in-process branch: a profile created
            # moments ago legitimately has no state.db yet (hermes itself
            # creates it on the profile's first run).
            try:
                return _remote_runtime_op(
                    "ensure_session",
                    {"profile": profile, "title": name, "create_db": True})
            except HermesRuntimeUnavailable as exc:
                # Name the module THIS interpreter is missing — the original
                # HermesRuntimeUnavailable wording spirit (an environment
                # fault must be diagnosable, not just announced).
                raise HermesRuntimeUnavailable(
                    f"cannot import the Hermes runtime ({_import_err}). "
                    f"{exc}") from None
            except RemoteRuntimeError as exc:
                if exc.code == "no_db":
                    raise LifecycleError(
                        f"profile {profile!r} has no usable state.db — "
                        f"cannot ensure session '{name}': {exc}"
                    ) from exc
                raise LifecycleError(
                    f"could not ensure session '{name}' on {profile!r}: {exc}"
                ) from exc
        db = _open_profile_session_db(profile, create_db=True)
    else:
        db = _session_db(profile)
    if db is None:
        raise LifecycleError(
            f"profile {profile!r} has no state.db — cannot ensure session "
            f"'{name}'"
        )
    import time as _t
    import uuid as _u
    try:
        if db.resolve_session_by_title(name):
            return {"status": "exists"}
        new_id = f"{_t.strftime('%Y%m%d')}_first_{_u.uuid4().hex[:6]}"
        db.create_session(
            new_id, source="cli", model="orchestrator-model", profile_name=profile
        )
        db.set_session_title(new_id, name)
        return {"status": "created", "session": new_id, "title": name}
    except Exception as exc:
        raise LifecycleError(
            f"could not ensure session '{name}' on {profile!r}: {exc}"
        ) from exc
    finally:
        try:
            db.close()
        except Exception:
            pass


def delete_profile(name: str, _run=None) -> bool:
    """Remove the ``<name>-orch`` orchestrator profile (atomicity rollback).

    Deletion goes through Hermes' OWN ``hermes profile delete -y`` as a
    subprocess — never in-process: the brew keg interpreter cannot import
    hermes_cli (t_9a9aeab1), and re-implementing hermes' profile lifecycle
    (gateway stop, service deregister, tombstone, multiplexer settle) here
    would diverge from it. Returns True when hermes reported success; False
    on a nonzero exit (the caller reports the leftover honestly).
    """
    profile = _orch_profile_name(name)
    proc = _run_cmd(["hermes", "profile", "delete", profile, "--yes"],
                    os.getcwd(), _run)
    return getattr(proc, "returncode", 1) == 0


def seed_session_with_digest(
    session_id: str, profile: str, text: str, _session_db=None
) -> bool:
    """Append the project digest as the opening message of an EMPTY orchestrator session.

    This is the ONE sanctioned read-write to a *session* DB in the chat flow:
    seeding the project's own empty orchestrator thread with its digest (a
    summary of the real history that stays on the DEFAULT profile). It is NOT a
    merge — the telegram sessions are never touched, and `text` is a bounded
    SYNTHESIS, not a raw transcript (bounded by ``DIGEST_MAX_CHARS`` upstream).

    Opens the profile's state.db via Hermes' own ``SessionDB`` read-write and
    appends ``text`` as a single ``user`` message, so the operator lands in a
    session that already knows the project history instead of a blank slate.

    Idempotency is the CALLER's contract: only call this when the session is
    confirmed empty. After this write the session is non-empty, so the next chat
    will not re-seed. Returns ``True`` only when the message was appended;
    ``False`` (never raises) when the profile/db is unusable or the append fails
    — the caller then records an honest "could not seed" rather than a fake one.
    """
    opener = _session_db if _session_db is not None else _open_profile_session_db
    db = opener(profile)
    if db is None:
        return False
    try:
        db.append_message(session_id, "user", text)
        return True
    except Exception:
        return False
    finally:
        try:
            db.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Orchestration — run the steps, record successes, never corrupt state
# --------------------------------------------------------------------------- #

def create_project(
    name: str,
    *,
    repo: str,
    registry_path: str,
    github: bool = False,
    private: bool = False,
    include_topic: bool = True,
    include_board: bool = True,
    include_roadmap: bool = True,
    remote_name: str = DEFAULT_REMOTE,
    _run=None,
    _client=None,
    _kanban=None,
    _session_db=None,
) -> dict:
    """Run the lifecycle steps and record each success/failure.

    The integrity core of the whole feature. Returns a result dict::

        {
          "steps": [ {"id", "status": "ok"|"failed"|"skipped",
                      "detail", "retry"(only on failure)} , ...],
          "repo": <resolved repo path>,
          "ok": bool,          # True iff no step failed
          "retry": <reconstructed retry command, or None>,
        }

    The registry is ALWAYS written at the end reflecting every step that
    succeeded — even when a later step failed — so a half-created project is
    recorded, never lost. Only a failed *repo* step withholds the registry
    entry (``repo`` is the one mandatory field). Functional step failures
    never escape as exceptions: they are collected into the report. Only a
    genuinely unexpected error (not a LifecycleError) propagates.

    Atomicity of the profile + session pair (t_9a9aeab1): a profile CREATED
    by this run is rolled back (``hermes profile delete -y``, Hermes' own
    verb) when the session step fails afterwards, so a failed run never
    leaves an orphan stub ``<name>-orch`` profile behind — the module
    docstring's "partial failure never leaves a half-registered project"
    promise, enforced for the profile surface. A profile that already existed
    (``skipped``) is never deleted: it is not ours to roll back. The registry
    write itself is a REPORTED step (its failure surfaces in the report
    instead of crashing the command), and the retry path stays idempotent:
    a re-run recreates the rolled-back profile and ensures the session.
    """
    results: list[dict] = []
    failed = False

    # --- Step 1: git repo (+ optional github) ---
    repo_succeeded = False
    try:
        git = ensure_git_repo(
            repo, github=github, private=private, remote_name=remote_name, _run=_run
        )
        repo_succeeded = True
        detail = f"repo {git['status']}"
        if github:
            detail += f", github {git['github']}"
        results.append({"id": "repo", "status": "ok", "detail": detail})
    except LifecycleError as exc:
        failed = True
        results.append(
            {
                "id": "repo",
                "status": "failed",
                "detail": str(exc),
                "retry": f"flightdeck project new {name} --repo {repo} --apply",
            }
        )

    # Half-registered-project guard: `repo` is the one mandatory registry
    # field, so if the repo step failed there is nothing valid to record.
    if not repo_succeeded:
        return {"steps": results, "repo": repo, "ok": False,
                "retry": f"flightdeck project new {name} --repo {repo} --apply"}

    # --- Step 2: topic (telegram) ---
    # The Telegram surface has been removed, so no topic is created and the
    # registry topic field is left unset (it is retained only for
    # historical/imported data). Recorded as skipped so the report shows where
    # a topic would have been.
    topic_val: int | None = None
    if include_topic:
        results.append({"id": "topic", "status": "skipped",
                        "detail": "skipped (Telegram removed)"})

    # --- Step 3: kanban board ---
    if include_board:
        try:
            ensure_board(name, _kanban=_kanban)
            results.append({"id": "board", "status": "ok", "detail": f"board {name}"})
        except LifecycleError as exc:
            failed = True
            results.append(
                {
                    "id": "board", "status": "failed", "detail": str(exc),
                    "retry": f"flightdeck project new {name} --repo {repo} --apply",
                }
            )
    else:
        results.append({"id": "board", "status": "skipped"})

    # --- Step 4: ROADMAP.md ---
    roadmap_val: str | None = None
    if include_roadmap:
        try:
            ensure_roadmap(repo, _run=_run)
            roadmap_val = "ROADMAP.md"
            results.append({"id": "roadmap", "status": "ok", "detail": "ROADMAP.md"})
        except LifecycleError as exc:
            failed = True
            results.append(
                {
                    "id": "roadmap", "status": "failed", "detail": str(exc),
                    "retry": f"flightdeck project new {name} --repo {repo} --apply",
                }
            )
    else:
        results.append({"id": "roadmap", "status": "skipped"})

    # --- Step 5a: orchestrator profile (<name>-orch) ---
    try:
        prof = ensure_profile(name, _run=_run)
        results.append(
            {"id": "profile", "status": "ok" if prof == "created" else "skipped",
             "detail": f"profile {name}-orch {prof}"}
        )
    except LifecycleError as exc:
        failed = True
        results.append(
            {
                "id": "profile", "status": "failed", "detail": str(exc),
                "retry": f"flightdeck project new {name} --repo {repo} --apply",
            }
        )

    # --- Step 5b: chat session titled <name> on the orchestrator profile ---
    session_failed = False
    try:
        sess = ensure_session(name, _orch_profile_name(name), _session_db=_session_db)
        results.append(
            {"id": "session", "status": "ok", "detail":
             f"session {name} {sess['status']}"}
        )
    except HermesRuntimeUnavailable as exc:
        # An environment fault stays an environment fault (t_9a9aeab1): it is
        # reported as its own step with the actionable fix, and it triggers the
        # same rollback as any other session failure — never a silent pass and
        # never dressed up as "profile has no data".
        failed = True
        session_failed = True
        results.append(
            {
                "id": "session", "status": "failed", "detail": str(exc),
                "retry": f"flightdeck project new {name} --repo {repo} --apply",
            }
        )
    except LifecycleError as exc:
        failed = True
        session_failed = True
        results.append(
            {
                "id": "session", "status": "failed", "detail": str(exc),
                "retry": f"flightdeck project new {name} --repo {repo} --apply",
            }
        )

    # --- Atomicity rollback (t_9a9aeab1) ---
    # A profile this run CREATED, followed by a FAILED session step, is rolled
    # back through Hermes' own delete verb: no orphan stub <name>-orch profile
    # survives a half-finished run (the operator's brew-venv repro left one).
    # An already-existing profile ("skipped") is never deleted — not ours.
    if session_failed and any(
        r["id"] == "profile" and r["status"] == "ok" for r in results
    ):
        if delete_profile(name, _run=_run):
            results = [r for r in results if r["id"] != "profile"]
            results.insert(
                next(i for i, r in enumerate(results) if r["id"] == "session"),
                {"id": "profile", "status": "rolled_back",
                 "detail": f"profile {name}-orch created then rolled back "
                           f"(session step failed)"}
            )
        else:
            results.append(
                {"id": "profile-cleanup", "status": "failed",
                 "detail": f"could not roll back created profile "
                           f"{name}-orch — remove it with: hermes profile "
                           f"delete {name}-orch --yes"}
            )

    # --- Step 6: registry entry (records every surface that succeeded) ---
    # A registry write failure is a REPORTED failure (rc=1 + retry hint), not
    # a traceback escaping the command (t_9a9aeab1 second-run confusion: the
    # operator's rerun "registered" only because the first run died before the
    # write; a crash here would leave the run state undocumented).
    try:
        ensure_registry(
            registry_path,
            name=name,
            repo=repo,
            board=name if include_board and any(
                r["id"] == "board" and r["status"] == "ok" for r in results
            ) else None,
            topic=topic_val,
            roadmap=roadmap_val,
        )
        results.append({"id": "registry", "status": "ok",
                        "detail": f"registered {name}"})
    except Exception as exc:
        failed = True
        results.append(
            {
                "id": "registry", "status": "failed",
                "detail": f"registry write failed: {exc}",
                "retry": f"flightdeck project new {name} --repo {repo} --apply",
            }
        )

    retry = None
    if failed:
        retry = f"flightdeck project new {name} --repo {repo} --apply"
    return {"steps": results, "repo": repo, "ok": not failed, "retry": retry}


# --------------------------------------------------------------------------- #
# Health (for `project list`)
# --------------------------------------------------------------------------- #

def project_health(
    project: registry.Project,
    *,
    _run=None,
    _client=None,
    _kanban=None,
) -> str:
    """A health token for one project: ``ok`` | ``partial`` | ``absent``.

    Honest to the "never report unverified" rule: only recorded dimensions
    are checked. A repo dir that is missing is ``absent`` outright. Otherwise
    each *recorded* surface (board, topic) is verified through the injected
    providers; if any fails to resolve the project is ``partial``.
    """
    if not os.path.isdir(project.repo):
        return "absent"

    recorded = 0
    missing = 0

    if project.board:
        recorded += 1
        try:
            if not _resolve_kanban(_kanban).board_exists(project.board):
                missing += 1
        except Exception:
            missing += 1

    # The topic field is retained for historical/imported data but is no
    # longer verified against a live Telegram surface (Telegram removed).

    if recorded == 0:
        return "ok"
    return "ok" if missing == 0 else "partial"
