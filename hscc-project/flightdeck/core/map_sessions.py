"""map_sessions.py — PROPOSE an owner for each unmapped Telegram session.

``propose_owners`` reads the operator's Hermes session store (``state.db``)
READ-ONLY and proposes a project owner for every Telegram-originated session
whose ``thread_id`` is NULL — the sessions that :mod:`flightdeck.core.archive`
files under ``unmapped/`` because no topic can name them.

This is a PROPOSAL command, never an apply-on-read. It resolves owners in two
passes, cheapest and highest-confidence first, and never guesses where the
signals are missing:

1. **Deterministic (repo-path).** For every registry project we build the repo
   path patterns in every form it actually appears in session content (the full
   absolute path, ``/dev/<name>``, ``~/dev/<name>``, ``/tmp/<name>``,
   ``/Volumes/NAS/<name>``, plus the HSCC daemon's own home for the ``hscc``
   project). A session whose user/assistant messages mention ONE project's repo
   path is resolved at high confidence with **method ``repo-path``** — no model,
   no cost, and the matched path is the evidence.

2. **Orchestrator (model) for the remainder.** A session with no unambiguous
   repo-path hit (signal-free, or ambiguous between several) goes to an
   injectable ``ask`` callback: we hand it a BOUNDED sample (first N + last N
   user/assistant messages, tool dumps skipped, hard char cap) and it returns
   one of the registry project names, ``"unknown"``, or ``None``. When no
   orchestrator transport is reachable — cluster down, no reply, error — the
   default fails CLOSED to **method ``none`` / project ``unknown``**. A wrong
   attribution is worse than none, so the command would rather emit ``unknown``
   than let a hung or unreachable ask stall the whole run.

``unknown`` is a first-class answer. The output is a reviewable proposal: per
session, the msgs / dates / proposed project / method / confidence / evidence.
The proposal is written as Markdown (argues each call, for a human) plus JSON
(for tooling). The default run is READ-ONLY on ``state.db`` — it writes ONLY
the proposal report files, never any registry/board/DB state. ``--apply`` is a
separate, explicit step that persists the machine mapping + a changelog and is
reversible (files only). After the report's real totals it is honest to say how
many were resolved deterministically / by model / left unknown.

The only reason this module reads ``state.db`` at all is to answer "which
sessions are unmapped and what do they say"; every write it performs lands in
the proposal output directory, never in the store it reads from.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

# Where the operator's session history lives by default (same store archive.py
# reads). Overridable via injectable ``db_path`` for tests.
DEFAULT_STATE_DB = "~/.hermes/state.db"

# Proposal output root. Beside the archive export (which files the same sessions
# under unmapped/), private content — deliberately NOT a git checkout.
DEFAULT_OUT_DIR = "~/.hermes/archive/telegram/proposals"

# Bounded-sample limits (the card: "do not ship 343 messages into a prompt").
SAMPLE_HEAD = 6    # first N user/assistant messages
SAMPLE_TAIL = 4    # last N user/assistant messages
SAMPLE_MAX_CHARS = 20000  # hard cap on one session's sample text before leaving it

# The method vocabulary the card pins: deterministic path match / model / none.
_METHOD_REPO_PATH = "repo-path"
_METHOD_MODEL = "model"
_METHOD_NONE = "none"

# Confidence vocabulary.
_CONF_HIGH = "high"
_CONF_MEDIUM = "medium"

# Signature of the injectable orchestrator seam.
# ``(bounded_sample, session_id) -> one of the registry names | "unknown" | None``
AskFn = Callable[[str, str], str | None]


class MapError(Exception):
    """Base error for map_sessions."""


class UnparsedAskError(MapError):
    """The ask callback returned something that is not a valid project name."""


# --------------------------------------------------------------------------- #
# Proposal model
# --------------------------------------------------------------------------- #

@dataclass
class Proposal:
    """One session's proposed owner, with the evidence that drove it."""

    session_id: str
    msgs: int
    started_at: float | None
    ended_at: float | None
    project: str | None          # one of the registry names, or None (unknown)
    method: str                  # repo-path | model | none
    confidence: str              # high | medium
    evidence: list[str] = field(default_factory=list)


@dataclass
class MapResult:
    """What a run proposed, for the operator to review and the report to count."""

    total: int = 0
    resolved_repo_path: int = 0
    resolved_model: int = 0
    unknown: int = 0
    proposals: list[Proposal] = field(default_factory=list)
    deterministic_by_project: dict[str, int] = field(default_factory=dict)
    model_by_project: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# DB access (read-only — same seam as archive.py)
# --------------------------------------------------------------------------- #

def open_readonly(db_path: str) -> sqlite3.Connection:
    """Open ``state.db`` read-only; never create/write.

    ``uri=True`` + ``mode=ro`` fails rather than auto-creates a missing file,
    and keeps the journal read-only. This command never writes the store.
    """
    uri = f"file:{os.path.abspath(os.path.expanduser(db_path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_unmapped(conn: sqlite3.Connection) -> list[dict]:
    """Telegram sessions with ``thread_id IS NULL``, oldest first."""
    rows = conn.execute(
        "SELECT id, source, chat_id, thread_id, title, display_name, "
        "started_at, ended_at, message_count "
        "FROM sessions WHERE source='telegram' AND thread_id IS NULL "
        "ORDER BY started_at, id"
    ).fetchall()
    return [dict(r) for r in rows]


def _fetch_words(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    """User/assistant messages for a session (the operator's words, not dumps)."""
    rows = conn.execute(
        "SELECT role, content, timestamp FROM messages "
        "WHERE session_id=? AND role IN ('user','assistant') "
        "ORDER BY timestamp",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Pass 1 — deterministic repo-path matching
# --------------------------------------------------------------------------- #

def _repo_path_patterns(projects) -> list[tuple[str, re.Pattern]]:
    """``(project_name, compiled_pattern)`` for every repo-path form.

    A project's repo is matched in every shape it realistically appears in
    session content: the full absolute path and each of the common relative
    shorthands (``/dev/<name>``, ``~/dev/<name>``, ``/tmp/<name>``,
    ``/Volumes/NAS/<name>``). The ``hscc`` project additionally matches the
    daemon's own runtime home (``~/.hermes/plugins``), which is where hscc
    development visibly happens and would otherwise be miss-attributed.
    """
    pats: list[tuple[str, re.Pattern]] = []
    seen: set[tuple[str, str]] = set()
    for proj in projects:
        name = proj.name
        repo = getattr(proj, "repo", None) or ""
        if not repo:
            continue
        base = os.path.basename(repo.rstrip("/"))
        forms = {os.path.realpath(os.path.expanduser(repo)).rstrip("/")}
        for pre in ("/dev/", "~/dev/", "/tmp/", "/Volumes/NAS/", "/Users/desac/dev/"):
            forms.add((pre + base).rstrip("/"))
        if name == "hscc":
            # The HSCC daemon works out of ~/.hermes/plugins (its own runtime
            # home), so hscc development shows up there and not under a repo
            # path. Match it explicitly or those sessions fall to unknown.
            forms.update({"~/.hermes/plugins", "/Users/desac/.hermes/plugins"})
        for form in sorted(forms):
            key = (name, form)
            if not form or key in seen:
                continue
            seen.add(key)
            pats.append((name, re.compile(re.escape(form))))
    return pats


def _match_pass1(text: str, pats) -> dict[str, list[str]]:
    """Project name -> matched path substrings in ``text`` (deduped)."""
    found: dict[str, list[str]] = {}
    for name, pat in pats:
        for m in pat.finditer(text):
            _slice = text[max(0, m.start() - 20): m.end() + 10].replace("\n", " ")
            if _slice not in found.setdefault(name, []):
                found[name].append(_slice)
    return found


def _assess_repo_path(projects, msgs) -> tuple[str | None, list[str]]:
    """Pass 1: return ``(project_or_None, matched_evidence)``.

    Resolves only when the session's words reference EXACTLY ONE project's repo
    path — the card's "an unambiguous repo-path hit is high-confidence".
    Multiple distinct projects mentioned, or none, stays unresolved (pass 2).
    """
    pats = _repo_path_patterns(projects)
    by_project: dict[str, list[str]] = {}
    for m in msgs:
        text = m.get("content") or ""
        if not text:
            continue
        for name, ev in _match_pass1(text, pats).items():
            for e in ev:
                if e not in by_project.setdefault(name, []):
                    by_project[name].append(e)
    resolved = [n for n in by_project if by_project[n]]
    if len(resolved) == 1:
        return resolved[0], by_project[resolved[0]]
    return None, []


# --------------------------------------------------------------------------- #
# Pass 2 — bounded sample + orchestrator ask (fail-closed)
# --------------------------------------------------------------------------- #

def bounded_sample(msgs: list[dict], *, head: int = SAMPLE_HEAD,
                   tail: int = SAMPLE_TAIL,
                   max_chars: int = SAMPLE_MAX_CHARS) -> str:
    """First N + last N user/assistant messages, flat, bounded.

    The caller passes only user/assistant rows, so no tool dump leaks here;
    this additionally enforces the hard char cap so one huge session can never
    blow a prompt. Returns a flat transcript block labelled by sender, oldest
    first, with the omitted middle marked.
    """
    def line(m: dict) -> str:
        role = m.get("role", "user")
        body = m.get("content") or ""
        body = body.replace("\n", " ")[:1200]
        return f"[{role}] {body}"

    if not msgs:
        return ""
    rendered = [line(m) for m in msgs]
    head_chunk = rendered[:head]
    tail_chunk = rendered[-tail:] if len(rendered) > head + tail else []
    middle = ["(… %d messages omitted …)" % (len(rendered) - head - tail)] if tail_chunk else []
    sample = "\n".join(head_chunk + middle + tail_chunk)
    if len(sample) > max_chars:
        sample = sample[:max_chars] + f"\n[… sample truncated: over {max_chars} chars]"
    return sample


def _default_ask(sample: str, session_id: str) -> str | None:
    """Default orchestrator seam: FAIL CLOSED to ``None`` (= unknown/none).

    There is no reliably reachable per-session orchestrator transport when this
    CLI runs (the cluster may be down, an ask may hang, a broken transport must
    never stall the whole run). So the shipped default attempts nothing and
    returns ``None`` — the operator gets ``unknown`` with method ``none``, the
    card's explicit degraded answer — and a transport that IS available is
    injected via the ``ask`` seam (``--ask-module``) or programmatically.
    """
    return None


def _classify_ask(reply: str | None, project_names: set[str]) -> tuple[str | None, list[str]]:
    """Turn an ask reply into ``(project, evidence)``.

    ``None``/empty → unknown. An explicit ``unknown``/``none`` → unknown. A
    reply that is not (a prefix of) one of the registry names → raise
    :class:`UnparsedAskError` — never silently accept a made-up project, a wrong
    attribution is worse than none. Otherwise resolve case-insensitively and
    keep the whole reply as the one-line evidence.
    """
    if not reply:
        return None, ["no reply from the orchestrator (fail-closed to unknown)"]
    text = str(reply).strip()
    lower = text.lower()
    for name in project_names:
        # Match the reply as the name possibly followed by a one-line reason
        # (e.g. "ecofire-bc — Close Inventory Period work").
        if lower == name.lower() or lower.startswith(name.lower() + " ") \
                or lower.startswith(name.lower() + "—") or lower.startswith(name.lower() + "-"):
            return name, [text]
    flat = " ".join(lower.split())
    if flat in ("unknown", "none", "unclear", "n/a", "not sure"):
        return None, [f"orchestrator answered unknown: {text!r}"]
    raise UnparsedAskError(
        f"ask returned {text!r}, not one of the registry projects "
        f"{sorted(project_names)}"
    )


# --------------------------------------------------------------------------- #
# Aggregation + report
# --------------------------------------------------------------------------- #

def _mk(sess: dict, project: str | None, method: str, confidence: str,
        evidence: list[str]) -> Proposal:
    return Proposal(
        session_id=sess["id"],
        msgs=int(sess.get("message_count") or 0),
        started_at=sess.get("started_at"),
        ended_at=sess.get("ended_at"),
        project=project,
        method=method,
        confidence=confidence,
        evidence=evidence,
    )


def _aggregate(proposals: list[Proposal]) -> MapResult:
    res = MapResult(total=len(proposals), proposals=proposals)
    for p in proposals:
        if p.method == _METHOD_REPO_PATH and p.project:
            res.resolved_repo_path += 1
            res.deterministic_by_project[p.project] = \
                res.deterministic_by_project.get(p.project, 0) + 1
        elif p.method == _METHOD_MODEL and p.project:
            res.resolved_model += 1
            res.model_by_project[p.project] = res.model_by_project.get(p.project, 0) + 1
        else:
            res.unknown += 1
    return res


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def propose_owners(
    *,
    db_path: str = DEFAULT_STATE_DB,
    registry_path: str | None = None,
    project_names: set[str] | None = None,
    ask: AskFn | None = None,
    sample: Callable[[list[dict]], str] | None = None,
) -> MapResult:
    """Propose an owner for every unmapped Telegram session.

    Pass 1 resolves via unambiguous repo-path hits (method ``repo-path``).
    Pass 2 sends a bounded sample of the remainder to the injectable ``ask``
    (default :func:`_default_ask`, which fails closed to ``unknown``/``none``).
    Reads ``state.db`` READ-ONLY; never writes it.
    """
    projects: list = []
    reg_name: set[str] = set(project_names or [])
    if registry_path is not None:
        try:
            projects = _load_registry(registry_path)
        except Exception:
            projects = []
        for p in projects:
            if p.name:
                reg_name.add(p.name)
    if not reg_name:
        raise MapError(
            "no project names available: pass --registry or project_names so an "
            "orchestrator reply can be validated against the real set"
        )

    ask_fn = ask if ask is not None else _default_ask
    sample_fn = sample if sample is not None else bounded_sample

    proposals: list[Proposal] = []
    conn = open_readonly(db_path)
    try:
        sessions = _fetch_unmapped(conn)
        for sess in sessions:
            sid = sess["id"]
            msgs = _fetch_words(conn, sid)
            # Pass 1 — deterministic.
            project, evidence = _assess_repo_path(projects, msgs)
            if project is not None:
                proposals.append(_mk(sess, project, _METHOD_REPO_PATH, _CONF_HIGH, evidence))
                continue
            # Pass 2 — bounded sample to the orchestrator, fail-closed.
            smp = sample_fn(msgs)
            if not smp:
                proposals.append(_mk(sess, None, _METHOD_NONE, _CONF_MEDIUM,
                                     ["session has no user/assistant messages to sample"]))
                continue
            try:
                reply = ask_fn(smp, sid)
                project, evidence = _classify_ask(reply, reg_name)
            except UnparsedAskError:
                raise
            except Exception as exc:  # an ask backend crash must not sink the run
                proposals.append(_mk(sess, None, _METHOD_NONE, _CONF_MEDIUM,
                                     [f"orchestrator ask failed: "
                                      f"{type(exc).__name__}: {exc}"]))
                continue
            if project is not None:
                proposals.append(_mk(sess, project, _METHOD_MODEL, _CONF_MEDIUM, evidence))
            else:
                proposals.append(_mk(sess, None, _METHOD_NONE, _CONF_MEDIUM, evidence))
    finally:
        conn.close()

    return _aggregate(proposals)


# --------------------------------------------------------------------------- #
# Proposal report + apply (files only; never state.db)
# --------------------------------------------------------------------------- #

def _load_registry(registry_path: str | None):
    """Load the registry, tolerating a broken file (→ [])."""
    from . import registry as _registry

    try:
        return _registry.load_registry(registry_path)
    except Exception:
        return []


def _fmt_ts(ts: float | None) -> str:
    """ISO-ish UTC timestamp for the proposal (readable, not locale)."""
    import datetime

    if ts is None:
        return "(none)"
    try:
        return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    except (OverflowError, OSError, ValueError):
        return "(none)"


def _last_slug(evidence: list[str]) -> str:
    """Shortest first evidence line as a one-line slug (for the MD table)."""
    for e in evidence:
        if e:
            return e
    return ""


def proposal_to_markdown(result: MapResult, projects_sorted: list[str]) -> str:
    """The reviewable Markdown proposal: every session argued."""
    lines: list[str] = []
    lines.append("# Unmapped-session mapping proposal\n")
    lines.append(f"total sessions: **{result.total}** · resolved deterministic (repo-path): "
                 f"**{result.resolved_repo_path}** · resolved by model: "
                 f"**{result.resolved_model}** · left unknown: **{result.unknown}**\n")
    lines.append("method is `repo-path` / `model` / `none`. `unknown` is a "
                 "first-class answer — a wrong attribution is worse than none.\n")
    lines.append("## Deterministic (repo-path)\n")
    if result.resolved_repo_path:
        lines.append("| session | msgs | dates | project | confidence | evidence |")
        lines.append("|---|---|---|---|---|---|")
        for p in result.proposals:
            if p.method == _METHOD_REPO_PATH:
                lines.append("| `%s` | %d | %s → %s | %s | %s | `%s` |" % (
                    p.session_id, p.msgs, _fmt_ts(p.started_at), _fmt_ts(p.ended_at),
                    p.project, p.confidence, _last_slug(p.evidence).replace("|", "\\|"),
                ))
    else:
        lines.append("_(none — no unambiguous repo-path signals)_")
    lines.append("")
    lines.append("## By model (orchestrator)\n")
    if result.resolved_model:
        lines.append("| session | msgs | dates | project | confidence | reason |")
        lines.append("|---|---|---|---|---|---|")
        for p in result.proposals:
            if p.method == _METHOD_MODEL:
                lines.append("| `%s` | %d | %s → %s | %s | %s | %s |" % (
                    p.session_id, p.msgs, _fmt_ts(p.started_at), _fmt_ts(p.ended_at),
                    p.project, p.confidence, _last_slug(p.evidence).replace("|", "\\|"),
                ))
    else:
        lines.append("_(none — the orchestrator seam failed closed to unknown)_")
    lines.append("")
    lines.append("## Unknown (left unattributed)\n")
    if result.unknown:
        lines.append("| session | msgs | dates | method | evidence |")
        lines.append("|---|---|---|---|---|")
        for p in result.proposals:
            if p.method == _METHOD_NONE:
                lines.append("| `%s` | %d | %s → %s | %s | %s |" % (
                    p.session_id, p.msgs, _fmt_ts(p.started_at), _fmt_ts(p.ended_at),
                    p.method, _last_slug(p.evidence).replace("|", "\\|"),
                ))
    else:
        lines.append("_(none — every session was resolved)_")
    lines.append("")
    lines.append("_proposal — review before `--apply`; nothing here was applied._")
    return "\n".join(lines)


def write_proposal(result: MapResult, out_dir: str,
                   timestamp: str) -> tuple[str, str]:
    """Write the Markdown + JSON proposal files under ``out_dir``.

    Returns ``(md_path, json_path)``. Filenames are timestamped so each run
    keeps a distinct, reviewable proposal rather than clobbering the last. Only
    the proposal output directory is written — ``state.db`` is untouched.
    """
    out = Path(os.path.expanduser(out_dir))
    out.mkdir(parents=True, exist_ok=True)
    names = sorted({n for p in result.proposals for n in [p.project] if n})

    md_path = out / f"map-sessions-{timestamp}.md"
    md_path.write_text(proposal_to_markdown(result, names), encoding="utf-8")

    payload = {
        "timestamp": timestamp,
        "total": result.total,
        "resolved_repo_path": result.resolved_repo_path,
        "resolved_model": result.resolved_model,
        "unknown": result.unknown,
        "deterministic_by_project": result.deterministic_by_project,
        "model_by_project": result.model_by_project,
        "proposals": [asdict(p) for p in result.proposals],
    }
    json_path = out / f"map-sessions-{timestamp}.json"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return str(md_path), str(json_path)


def apply_mapping(result: MapResult, out_dir: str,
                  timestamp: str) -> tuple[str, str]:
    """Persist the reviewable mapping + a changelog (the explicit, reversible step).

    Writes two files under ``out_dir``: the machine ``mapping.json``
    (``session_id -> {project, method, confidence, evidence}``) that a future
    step (e.g. re-filing the archive) can consume, and a Markdown changelog that
    RECORDS every assignment it made. It touches ONLY files in ``out_dir`` —
    never ``state.db``, the registry, or a kanban board — so it is reversible by
    deleting the two files it wrote. Returns ``(mapping_path, changelog_path)``.
    """
    out = Path(os.path.expanduser(out_dir))
    out.mkdir(parents=True, exist_ok=True)

    mapping = {
        p.session_id: {
            "project": p.project,
            "method": p.method,
            "confidence": p.confidence,
            "evidence": p.evidence,
        }
        for p in result.proposals
    }
    mapping_path = out / f"mapping-{timestamp}.json"
    mapping_path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")

    applied = [p for p in result.proposals if p.project is not None]
    unknown = [p for p in result.proposals if p.project is None]
    lines = [
        f"# Mapping applied {timestamp}",
        "",
        f"Applied {len(applied)} assignment(s); left {len(unknown)} unknown "
        "(no owner proposed — wrong attribution is worse than none).",
        "",
        "This mapping is a FILE-ONLY, reversible record: it wrote the two files",
        f"`map-sessions-{timestamp}.json` and `mapping-{timestamp}.json` (and this "
        "changelog) into the proposal dir. It never wrote state.db, the registry, "
        "or a kanban board. Delete these files to revert.",
        "",
    ]
    for p in result.proposals:
        who = p.project if p.project is not None else "unknown"
        lines.append(f"- `{p.session_id}` -> {who}  [{p.method}, {p.confidence}]")
    changelog_path = out / f"mapping-APPLIED-{timestamp}.md"
    changelog_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(mapping_path), str(changelog_path)
