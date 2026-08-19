"""start.py — `flightdeck start <project> --milestone <id> [--apply]`.

The operator's second button. ``decompose`` produced the cards; ``start`` puts
them to work — the release of a milestone's cards to the fleet, gated on
concurrency so the workers are not swamped.

Why the cap matters (measured on this cluster): the workers are ONE
tensor-parallel span serving ~2 concurrent requests at ~25 tok/s. Four active
cards put 12 requests in the queue and cards sat 20+ minutes writing nothing
while heartbeating normally. Over-dispatching does not go faster, it goes
slower. ``start`` therefore treats the fleet's declared capacity as a hard
ceiling and, on top of that, a per-run ``--max-concurrent`` cap (default 3).

What ``start`` does, all of it dry-run by default:

1. SELECT the cards linked to the milestone — a card whose body carries a line
   ``MILESTONE: <id>`` whose value equals the requested milestone id.
2. RESPECT what the fleet can absorb — read ``kanban.max_in_progress`` and
   ``kanban.max_in_progress_per_profile`` from the Hermes config (never
   hardcoded), and additionally cap the whole run at ``--max-concurrent``
   (default 3). The effective total ceiling is
   ``min(max_in_progress, --max-concurrent)``; per profile it is
   ``max_in_progress_per_profile``.
3. SPREAD cards across the fleet profiles (coder, backend-engineer,
   devops-engineer, qa, architect, technical-writer) round-robin rather than
   piling them on one, respecting each profile's in-progress cap.
4. HOLD any card whose declared dependency has not merged yet, naming the
   dependency that holds each one.
5. Print the release order with assignees; ``--apply`` performs the release.

A milestone with no matching cards says so — it is never reported as success.

Every external call is injectable (``_list_cards``, ``_run`` for git, the
config path / loader) so tests drive it against fixtures and never touch the
board, git, Telegram or the network.

    REPO: ~/dev/flightdeck
    CONTRACTS: docs/DESIGN.md, docs/FEATURES-2.md
    CLI WIRING: cli.py auto-discovers this module via build_subparser + run.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Callable, Optional

import yaml

from ..core import git_state, kanban, registry

# Hermes' own config, where the kanban concurrency knobs live. Overridable for
# tests via the ``_read_config`` seam.
DEFAULT_CONFIG = os.path.expanduser("~/.hermes/config.yaml")

# The fleet profiles ``start`` spreads cards across — the assignees the dispatcher
# actually knows how to spawn. Round-robin here, not piled on one profile.
FLEET_PROFILES = (
    "coder",
    "backend-engineer",
    "devops-engineer",
    "qa",
    "architect",
    "technical-writer",
)

# Body-line tags. ``MILESTONE: <id>`` links a card to a milestone (see M2);
# ``DEPENDS: <id>, <id>`` declares the card ids this card needs merged before it
# can start. Both are parsed with the same split-and-compare helper.
_MILESTONE_RE = re.compile(r"^\s*MILESTONE\s*:\s*(.+?)\s*$")
_DEPENDS_RE = re.compile(r"^\s*DEPENDS\s*:\s*(.+?)\s*$")

# A line that is not part of a tag/value pair (we only read the tag lines; every
# other line in the body is ignored).
_DEFAULT_MAX_IN_PROGRESS = 1  # super-safe fallback if config is unreadable


class StartError(Exception):
    """Base error for start (missing project, unknown milestone, ...)."""


# --------------------------------------------------------------------------- #
# Parsing the config (injectable)
# --------------------------------------------------------------------------- #


def _read_config(path: Optional[str] = None) -> dict:
    """Return ``{max_in_progress, max_in_progress_per_profile}`` from config.

    Reads the ``kanban`` section of the Hermes config yaml. Any missing key
    falls back to ``_DEFAULT_MAX_IN_PROGRESS`` (1) so a run never assumes more
    capacity than it can verify — the conservative reading. A missing file or
    unparseable yaml also falls back, never raises, so ``start`` always has a
    ceiling. ``path`` is injectable for tests.
    """
    p = path if path is not None else DEFAULT_CONFIG
    try:
        with open(p, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        doc = None

    kanban_cfg = {}
    if isinstance(doc, dict):
        section = doc.get("kanban")
        if isinstance(section, dict):
            kanban_cfg = section

    def _int(key: str) -> int:
        try:
            return int(kanban_cfg.get(key))
        except (TypeError, ValueError):
            return _DEFAULT_MAX_IN_PROGRESS

    return {
        "max_in_progress": _int("max_in_progress"),
        "max_in_progress_per_profile": _int("max_in_progress_per_profile"),
    }


def _ceiling(cfg: dict, max_concurrent: int) -> int:
    """The effective total cap: the smaller of the fleet ceiling and the run cap."""
    global_cap = max(1, int(cfg.get("max_in_progress", _DEFAULT_MAX_IN_PROGRESS)))
    return max(1, min(global_cap, max(1, int(max_concurrent))))


# --------------------------------------------------------------------------- #
# Parsing the card body (ONE function per tag)
# --------------------------------------------------------------------------- #


def milestone_tag(body: Optional[str]) -> Optional[str]:
    """The milestone id a card body declares, or None if it declares none.

    A card belongs to a milestone when its body has a line ``MILESTONE: <id>``
    whose value (stripped) equals the milestone. The value is returned verbatim
    so the caller can compare it to the requested milestone id. Every other
    line in the body is ignored; a body with no matching line yields None.
    """
    if not body:
        return None
    for line in body.splitlines():
        m = _MILESTONE_RE.match(line)
        if m:
            return m.group(1).strip()
    return None


def declared_deps(body: Optional[str]) -> list[str]:
    """The card ids a card declares as dependencies (``DEPENDS:`` line).

    Returns the comma/space-separated ids on the first ``DEPENDS:`` line,
    order-preserving, deduped, stripped. An empty body (or no DEPENDS line)
    yields an empty list. Invalid non-whitespace tokens are preserved as-is
    (they simply won't resolve to a real card and thus hold nothing).
    """
    if not body:
        return []
    deps: list[str] = []
    seen: set[str] = set()
    for line in body.splitlines():
        m = _DEPENDS_RE.match(line)
        if m:
            for token in re.split(r"[\s,]+", m.group(1).strip()):
                if token and token not in seen:
                    seen.add(token)
                    deps.append(token)
            # Only the first DEPENDS line is authoritative; stop after it.
            return deps
    return deps


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def select_milestone_cards(cards: list[dict], milestone: str) -> list[dict]:
    """The cards whose declared milestone equals ``milestone``.

    ``cards`` is any iterable of flightdeck card dicts (as produced by
    ``kanban.list_cards``). Order is preserved from the input. Never mutates
    the input. A card is included exactly when ``milestone_tag(card["body"])``
    equals ``milestone``.
    """
    return [c for c in cards if milestone_tag(c.get("body")) == milestone]


def _is_released(card: dict) -> bool:
    """True when a card is already in flight (running/claimed) — not releaseable.

    ``start`` only releases cards that have not been put to work yet (todo /
    triage / ready / blocked). A card already running or claimed is already on
    the board — releasing it again would double-dispatch.
    """
    status = str(card.get("status") or "")
    return status in kanban.ACTIVE_STATUSES


def _dependency_merged(
    dep_id: str,
    *,
    cards_by_id: dict[str, dict],
    repo: str,
    _run=None,
) -> tuple[bool, Optional[str]]:
    """Is dependency ``dep_id`` merged (its work landed)? Returns (ok, holder).

    ``ok`` is False when the dependency card does not exist, is still in flight,
    or its git branch has not merged into the project's main. ``holder`` is the
    dependency card id (or None when ok). A dependency with no resolvable card
    is treated as unmerged (we never release a card onto a dependency we cannot
    confirm landed).
    """
    dep = cards_by_id.get(dep_id)
    if dep is None:
        return False, dep_id
    branch = dep.get("branch")
    if not branch:
        return False, dep_id
    if _is_released(dep):
        # Still in flight — its work cannot have merged yet.
        return False, dep_id
    merged = git_state.is_merged(repo, branch, _run=_run)
    return merged, dep_id


def partition_releasable(
    milestone_cards: list[dict],
    *,
    all_cards: list[dict],
    repo: str,
    _run=None,
) -> tuple[list[dict], list[dict]]:
    """Split milestone cards into (releasable, held).

    A card is HELD when any of its declared dependencies has not merged: the
    dependency card is missing, in flight, or its branch is unmerged. Each held
    card is returned with a ``_holds`` list naming the dependency ids holding
    it. ``releasable`` keeps input order.

    Dependencies resolve against ``all_cards`` (the full board set), not just
    the milestone cards — a card may legitimately depend on a preceding card
    from an earlier milestone, and that card's merged status is a git fact, not
    a membership-of-this-milestone fact.
    """
    cards_by_id = {c.get("id"): c for c in all_cards if c.get("id") is not None}
    releasable: list[dict] = []
    held: list[dict] = []
    for card in milestone_cards:
        deps = declared_deps(card.get("body"))
        holding: list[str] = []
        for dep_id in deps:
            ok, holder = _dependency_merged(
                dep_id, cards_by_id=cards_by_id, repo=repo, _run=_run
            )
            if not ok:
                holding.append(holder)
        if holding:
            card = dict(card)
            card["_holds"] = holding
            held.append(card)
        else:
            releasable.append(card)
    return releasable, held


# --------------------------------------------------------------------------- #
# Spreading across profiles
# --------------------------------------------------------------------------- #


def _assign(releasable: list[dict], per_profile_cap: int, total_cap: int) -> list[dict]:
    """Assign a profile to each releasable card, respecting both caps.

    Round-robin across :data:`FLEET_PROFILES`. Release stops at ``total_cap``
    cards; no profile takes more than ``per_profile_cap``. Returns a NEW list
    of ``{**card, "_assignee": profile}`` dicts in input order (the first
    ``total_cap`` entries that can be assigned). Cards beyond the total cap, or
    that cannot fit a profile without breaking its cap, are left unassigned.
    """
    per_profile = max(1, int(per_profile_cap))
    counts: dict[str, int] = {p: 0 for p in FLEET_PROFILES}
    assigned: list[dict] = []
    idx = 0
    for card in releasable:
        if len(assigned) >= total_cap:
            break
        # Find the next profile with room, scanning round-robin from idx.
        for _ in range(len(FLEET_PROFILES)):
            profile = FLEET_PROFILES[idx % len(FLEET_PROFILES)]
            idx += 1
            if counts[profile] < per_profile:
                counts[profile] += 1
                new = dict(card)
                new["_assignee"] = profile
                assigned.append(new)
                break
    return assigned


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #


def _card_label(card: dict) -> str:
    return f"{card.get('id')}  {card.get('title')}"


def _print_plan(
    *,
    milestone: str,
    assigned: list[dict],
    held: list[dict],
    total_cap: int,
    per_profile_cap: int,
    remaining: int,
) -> None:
    print(f"RELEASE PLAN for milestone {milestone!r} (fleet ceiling {total_cap}, "
          f"max {per_profile_cap} per profile):")
    if not assigned and not held:
        print("  no cards for this milestone.")
        return
    print("\nRELEASE ORDER:")
    for i, card in enumerate(assigned, 1):
        print(f"  {i}. {_card_label(card)}  -> {card.get('_assignee')}")
    if held:
        print("\nHELD (dependency not merged):")
        for card in held:
            holder = ", ".join(card.get("_holds") or [])
            print(f"  {_card_label(card)}")
            print(f"       held by: {holder}")
    if remaining:
        print(f"\n{remaining} card(s) not released (beyond the concurrency ceiling "
              f"or no profile slot free).")


def _print_json(*, milestone, assigned, held, total_cap, per_profile_cap, remaining) -> None:
    import json

    print(
        json.dumps(
            {
                "milestone": milestone,
                "total_cap": total_cap,
                "per_profile_cap": per_profile_cap,
                "release": [
                    {"id": c.get("id"), "title": c.get("title"), "assignee": c.get("_assignee")}
                    for c in assigned
                ],
                "held": [
                    {"id": c.get("id"), "title": c.get("title"), "holds": c.get("_holds")}
                    for c in held
                ],
                "not_released": remaining,
            }
        )
    )


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #


def _release(card: dict, *, _kdb=None) -> None:
    """Release one card to the fleet: assign it to its spread profile.

    ``assign_task`` is Hermes' own "who is working this" mutation — setting the
    assignee is what makes the dispatcher spawn the right fleet profile. Routes
    through the same kanban library as every board mutation in flightdeck.
    ``_kdb`` is injectable (tests pass a fake). Raises :class:`StartError` when
    the card has no board (it cannot be released without a board).
    """
    kdb = _kdb if _kdb is not None else kanban._load_kanban_db()
    board = card.get("board")
    if not board:
        raise StartError(f"card {card.get('id')} has no board; cannot release")
    assignee = card.get("_assignee")
    conn = kdb.connect(board=board)
    try:
        # bool return: False means the assignment was refused (already claimed,
        # bad profile, etc.); report it rather than claiming success.
        ok = kdb.assign_task(conn, card.get("id"), assignee)
    finally:
        conn.close()
    if not ok:
        raise StartError(
            f"card {card.get('id')} could not be assigned to {assignee!r}"
        )


# --------------------------------------------------------------------------- #
# Command
# --------------------------------------------------------------------------- #

def _resolve_project(projects, name: str):
    for proj in projects:
        if proj.name == name:
            return proj, None
    return None, f"unknown project: {name!r} (check `flightdeck projects list`)"


def cmd_start(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    proj, err = _resolve_project(projects, args.project)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    assert proj is not None
    if not args.milestone:
        print("error: --milestone <id> is required.", file=sys.stderr)
        return 2
    if not proj.board:
        print(
            f"error: project {args.project} has no board; "
            f"run: flightdeck project repair {args.project}",
            file=sys.stderr,
        )
        return 2
    if not proj.repo:
        print(
            f"error: project {args.project} has no repo; cannot check dependencies.",
            file=sys.stderr,
        )
        return 2

    _list = getattr(args, "list_cards", None) or kanban.list_cards
    all_cards = _list(board=proj.board, include_archived=False)

    milestone_cards = select_milestone_cards(all_cards, args.milestone)
    if not milestone_cards:
        print(
            f"no cards for milestone {args.milestone!r} on project {args.project} "
            f"(board {proj.board!r}).",
            file=sys.stderr,
        )
        return 3

    cfg = _read_config(args.config_path)
    total_cap = _ceiling(cfg, args.max_concurrent)
    per_profile_cap = max(1, int(cfg.get("max_in_progress_per_profile", 1)))

    _run = getattr(args, "run", None)
    releasable, held = partition_releasable(
        milestone_cards, all_cards=all_cards, repo=proj.repo, _run=_run
    )

    assigned = _assign(releasable, per_profile_cap=per_profile_cap, total_cap=total_cap)
    remaining = max(0, len(releasable) - len(assigned))

    if getattr(args, "json", False):
        _print_json(
            milestone=args.milestone,
            assigned=assigned,
            held=held,
            total_cap=total_cap,
            per_profile_cap=per_profile_cap,
            remaining=remaining,
        )
    else:
        _print_plan(
            milestone=args.milestone,
            assigned=assigned,
            held=held,
            total_cap=total_cap,
            per_profile_cap=per_profile_cap,
            remaining=remaining,
        )

    if args.apply:
        _kdb = getattr(args, "kdb", None)
        released: list[str] = []
        for card in assigned:
            try:
                _release(card, _kdb=_kdb)
                released.append(card.get("id"))
            except StartError as exc:
                print(f"error: {exc}", file=sys.stderr)
        # Total failure: cards were planned for release but every one failed.
        # A zero-count success is a silent data-loss signal — surface it and
        # exit non-zero (matches decompose.py's --apply-created-nothing gate).
        if not released and assigned:
            print(
                "error: --apply released nothing (every assigned card failed to "
                "assign). resolve the errors above and re-run.",
                file=sys.stderr,
            )
            return 3
        print(
            f"applied: released {len(released)} card(s) to the fleet.",
            file=sys.stderr,
        )
        return 0
    else:
        print("\ndry-run: pass --apply to release these cards.", file=sys.stderr)
        return 0


def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "start",
        help="release a milestone's cards to the fleet, concurrency-aware",
        epilog="example: flightdeck start flightdeck --milestone 0.6.0 --apply",
    )
    p.add_argument("project", help="project name in the registry")
    p.add_argument(
        "--milestone",
        required=True,
        help="milestone id whose cards to release (matches a `MILESTONE: <id>` body line)",
    )
    p.add_argument(
        "--max-concurrent",
        type=int,
        default=3,
        metavar="N",
        help="additional cap on concurrent cards for this run (default: 3); "
        "effective ceiling = min(fleet max_in_progress, N)",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="release the planned cards (dry-run by default; nothing is released without this)",
    )
    p.set_defaults(func=cmd_start)
    return p


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py: run start with injectable seams attached.

    ``args.list_cards``/``args.run``/``args.kdb``/``args.config_path`` default
    to None (use the real kanban library, real git, real board, real config);
    tests set them to fakes so nothing here touches the board, git, Telegram,
    the network or the live config.
    """
    args.registry = registry_path
    args.list_cards = getattr(args, "list_cards", None)
    args.run = getattr(args, "run", None)
    args.kdb = getattr(args, "kdb", None)
    args.config_path = getattr(args, "config_path", None)
    projects = registry.load_registry(registry_path)
    return cmd_start(args, projects)
