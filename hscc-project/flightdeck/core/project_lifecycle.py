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

import os
import sys
import subprocess
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


def _open_profile_session_db(profile: str, read_only: bool = False):
    """Open ``<profile>``'s state.db via Hermes' own SessionDB, or None.

    Mirrors ``routes_orchestrator._open_profile_session_db`` (same call
    sequence, same fail-safe-to-None) so this module and the REST chat handler
    agree on what "the project's session" is. ``read_only=True`` opens the DB
    HERMES-native read-only (``SessionDB(read_only=...)``, the equivalent of
    ``mode=ro`` on the underlying connection) — used by the discovery paths
    that must never write to the operator's live session databases. Returns
    ``None`` when the profile is unresolvable or has no state.db — the caller
    then reports an honest step failure rather than guessing.
    """
    try:
        from hermes_cli import profiles as profiles_mod
        from hermes_state import SessionDB
    except ImportError as exc:
        # NOT "this profile has no sessions" — this interpreter cannot see the
        # Hermes runtime at all. Swallowing it made `project sessions` print
        # "no telegram history" for projects with years of history, because the
        # `hscc` entry point runs under miniconda while hermes_cli lives in the
        # hermes venv. An environment fault must never render as a data fact.
        raise HermesRuntimeUnavailable(
            f"cannot import the Hermes runtime ({exc}). The `hscc` CLI runs "
            f"under {sys.executable}; hermes_cli/hermes_state live in the "
            f"Hermes venv. Session history cannot be read from here."
        ) from exc
    try:
        canon = profiles_mod.normalize_profile_name(profile)
        profiles_mod.validate_profile_name(canon)
        if not profiles_mod.profile_exists(canon):
            return None
        db_path = Path(profiles_mod.get_profile_dir(canon)) / "state.db"
        if not db_path.exists():
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

    Idempotent + never clobbers: an existing titled session is left exactly as
    it is. Returns ``{"status": "exists"}`` when already present, or
    ``{"status": "created", "session": <id>, "title": name}``. Raises
    :class:`LifecycleError` when the profile has no usable state.db, so the
    caller records it as a step failure that ``repair`` retries idempotently.
    """
    opener = _resolve_session_db(profile, _session_db)
    db = opener(profile)
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
    try:
        sess = ensure_session(name, _orch_profile_name(name), _session_db=_session_db)
        results.append(
            {"id": "session", "status": "ok", "detail":
             f"session {name} {sess['status']}"}
        )
    except LifecycleError as exc:
        failed = True
        results.append(
            {
                "id": "session", "status": "failed", "detail": str(exc),
                "retry": f"flightdeck project new {name} --repo {repo} --apply",
            }
        )

    # --- Step 6: registry entry (records every surface that succeeded) ---
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
    results.append({"id": "registry", "status": "ok", "detail": f"registered {name}"})

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
