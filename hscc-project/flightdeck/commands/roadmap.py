"""roadmap.py — `flightdeck roadmap {add,move,done,adopt}` : edit ROADMAP.md from the CLI.

Plain versioned markdown stays the source of truth — this command edits the
file in place, so the diff shows up in code review. We reuse
:mod:`flightdeck.core.roadmap` for *reading*; the editing here is line-accurate
so that everything except the touched line stays byte-identical.

Edits follow three hard rules (from the card):

  - UNRELATED LINES STAY BYTE-IDENTICAL. We operate on the raw line array and
    swap only the target line (or insert/delete exactly one element), so
    comments, blank lines, ordering and formatting elsewhere are untouched. A
    reformat here is a defect.
  - MATCH BY SUBSTRING. ``roadmap move foo "stripe"`` matches the first item
    whose text contains "stripe". If several items match the same substring the
    match is AMBIGUOUS: we list the candidates and change NOTHING.
  - MUTATIONS REQUIRE ``--apply`` and print the intended change first.

A project with no ROADMAP.md: ``add`` offers to create the file with the three
sections; ``move``/``done`` report "no roadmap" and exit non-zero. A malformed
roadmap must parse what it can and never crash.

No subcommand (bare ``flightdeck roadmap``) shows Now/Next/Later per project.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Optional

from ..core import kanban, registry, roadmap

# The three sections this command works with, and their markdown headings.
_SECTION_HEADING = {"now": "Now", "next": "Next", "later": "Later"}
_SECTION_NAMES = tuple(_SECTION_HEADING)
_DEFAULT_SECTION = "now"

# A `## Heading` line begins a new section (matches core/roadmap.py).
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
# A checklist item line, split so we can rewrite the checkbox without touching
# the rest of the line:     prefix  [ box ]   text
_ITEM_RE = re.compile(r"^(\s*[-*]\s+\[)([ xX])(\]\s*)(.*)$")

_NO_ROADMAP = "no roadmap: {path} does not exist"
_FILE_HEADER = "# Roadmap\n\n## Now\n- [ ] \n\n## Next\n- [ ] \n\n## Later\n- [ ] \n"


# --------------------------------------------------------------------------- #
# scanning — turn raw text into lines + a checklist-item index
# --------------------------------------------------------------------------- #


def _scan(text: str) -> tuple[list[str], list[dict]]:
    """Split ``text`` into lines and index every checklist item.

    Returns ``(lines, items)`` where ``lines`` round-trips the original bytes
    exactly when re-joined with ``"\\n"`` (split/join are inverses), and
    ``items`` is a list of ``{line, section, text, checked, prefix, rest}``.

    ``section`` is the milestone name (e.g. ``"Now"``) the item falls under, or
    None when it precedes any heading. ``prefix`` is the ``- [`` (or ``* [``,
    with indentation) portion of the line and ``rest`` everything after the
    checkbox — so ``done`` can flip ``[ ]`` to ``[x]`` in place.
    """
    lines = text.split("\n")
    items: list[dict] = []
    current: Optional[str] = None
    for i, line in enumerate(lines):
        heading = _HEADING_RE.match(line)
        if heading:
            current = heading.group(1).strip()
            continue
        m = _ITEM_RE.match(line)
        if m:
            items.append(
                {
                    "line": i,
                    "section": current,
                    "text": m.group(4).strip(),
                    "checked": m.group(2).lower() == "x",
                    "prefix": m.group(1),
                    "rest": m.group(3) + m.group(4),
                }
            )
    return lines, items


def _matches(items: list[dict], search_text: str) -> list[dict]:
    """All items whose text contains ``search_text`` (substring match)."""
    return [it for it in items if search_text in it["text"]]


def _heading_line(lines: list[str], section: str) -> Optional[int]:
    """The line index of the ``## <section>`` heading, or None if absent."""
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m and m.group(1).strip() == section:
            return i
    return None


def _insert_point(lines: list[str], items: list[dict], section: str) -> Optional[int]:
    """Line index to insert a new item for ``section``.

    Append after the section's heading when the section is empty, otherwise
    after the section's last item. Returns None when the section heading is
    missing.
    """
    heading = _heading_line(lines, section)
    if heading is None:
        return None
    sect_items = [it for it in items if it["section"] == section]
    if not sect_items:
        return heading + 1
    return max(it["line"] for it in sect_items) + 1


def _new_item_line(text: str) -> str:
    """The canonical line for a freshly-added item."""
    return f"- [ ] {text}"


# --------------------------------------------------------------------------- #
# mutation helpers — each returns the new line array, leaving the input intact
# --------------------------------------------------------------------------- #


def _apply_add(lines: list[str], items: list[dict], section: str, item_text: str) -> list[str]:
    """Insert ``item_text`` into ``section``. Returns a new line array."""
    at = _insert_point(lines, items, section)
    assert at is not None  # caller already checked the section exists
    new = list(lines)
    new.insert(at, _new_item_line(item_text))
    return new


def _apply_done(lines: list[str], item: dict) -> list[str]:
    """Tick the checkbox on the single matched item line."""
    new = list(lines)
    new[item["line"]] = item["prefix"] + "x" + item["rest"]
    return new


def _apply_move(lines: list[str], items: list[dict], item: dict, to_section: str) -> list[str]:
    """Remove ``item`` from its section and append it to ``to_section``.

    The checkbox state travels with the item. Target insert point is computed
    from the ORIGINAL coordinates; after removing the source line every index
    beyond it shifts down by one, so we adjust.
    """
    src = item["line"]
    at = _insert_point(lines, items, to_section)
    assert at is not None  # caller already checked the section exists
    moved_line = lines[src]
    if at > src:
        at -= 1
    new = list(lines)
    new.pop(src)
    new.insert(at, moved_line)
    return new


# --------------------------------------------------------------------------- #
# presentation
# --------------------------------------------------------------------------- #


def _render(rows: list[tuple[str, str]]) -> list[str]:
    """Human lines for the bare ``flightdeck roadmap`` view.

    ``rows`` is ``(name, path)`` for every registered project. Each is printed
    with its Now/Next/Later items, or ``no roadmap`` when absent.
    """
    out: list[str] = []
    for name, path in rows:
        r = roadmap.parse_roadmap(path)
        out.append(f"[{name}] {path}")
        if not r.present:
            out.append("  no roadmap")
            continue
        for section in _SECTION_HEADING.values():
            m = r.milestone(section)
            if m is None:
                out.append(f"  {section}: (no section)")
                continue
            counts = "" if not m.items else f" ({m.done_count}/{m.total} done)"
            out.append(f"  {section}:{counts}")
            for it in m.items:
                box = "x" if it.checked else " "
                out.append(f"    - [{box}] {it.text}")
    return out


def _render_json(rows: list[tuple[str, str]]) -> dict:
    out: dict[str, dict] = {}
    for name, path in rows:
        r = roadmap.parse_roadmap(path)
        if not r.present:
            out[name] = {"path": path, "present": False, "milestones": {}}
            continue
        milestones: dict[str, dict] = {}
        for section in _SECTION_HEADING.values():
            m = r.milestone(section)
            milestones[section] = {
                "items": [{"text": it.text, "checked": it.checked} for it in (m.items if m else [])],
                "done": m.done_count if m else 0,
                "total": m.total if m else 0,
            }
        out[name] = {"path": path, "present": True, "milestones": milestones}
    return out


# --------------------------------------------------------------------------- #
# roadmap progress — link cards by MILESTONE: tag, render per-milestone counts
# --------------------------------------------------------------------------- #
#
# A card links to a milestone when its body carries a `MILESTONE: <id>` line
# (decompose.py stamps this when it creates cards under a --milestone). The tag
# is matched case-insensitively with leading whitespace tolerated, anywhere in
# the body. A card with no such line is UNLINKED: it is reported as a count,
# never guessed into a milestone and never silently dropped.

# A `MILESTONE: <id>` line. `(\S+)` captures the milestone id; the id is a
# non-whitespace token (ids are slugs like `auth-hardening`), so a trailing
# comment would be included -- decompose never writes one.
_MILESTONE_TAG_RE = re.compile(r"^\s*milestone\s*:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)

# Status buckets for the per-milestone counts, aligned with kanban.core so a
# card's status means the same thing here as in reconcile.
_PROGRESS_DONE_STATUSES = frozenset({"done", "closed", "complete"})
_PROGRESS_REVIEW_STATUSES = kanban.REVIEW_STATUSES      # {"review", "blocked"}
_PROGRESS_RUNNING_STATUSES = kanban.IN_FLIGHT_STATUSES  # {"running"}


def _card_milestone_id(card: dict) -> Optional[str]:
    """The milestone id a card links to via a ``MILESTONE: <id>`` body line.

    Returns None when the card body carries no such line (the UNLINKED case) —
    we never guess a link the body does not state. Matching is tolerant of case
    and leading whitespace, and searches the whole body.
    """
    body = card.get("body")
    if not body:
        return None
    m = _MILESTONE_TAG_RE.search(body)
    return m.group(1) if m else None


def _aggregate_progress(
    projects: list[registry.Project], all_cards: list[dict]
) -> dict:
    """Per-project progress data.

    Returns ``{project_name: {"present": bool, "milestones": [dict...],
    "unlinked": int}}``. ``present`` is False when the project has no ROADMAP.md
    (``data`` still carries the name so the caller prints "no roadmap", never
    an empty success). Each milestone dict carries ``id/title/subproject/status``
    plus TWO independent sources of truth: the ITEM counts read from the
    roadmap file (``item_total``/``item_done``, from ``- [x]``/``- [ ]``) and
    the CARD counts derived from linked kanban cards (``total/done/
    awaiting_review/running``). Neither may hide the other — a freshly adopted
    roadmap has items but no cards yet, and a card-heavy milestone may lag its
    items.
    """
    data: dict[str, dict] = {}
    for proj in projects:
        r = roadmap.project_roadmap(proj)
        milestones_out = []
        if r.present:
            for m in r.milestones:
                milestones_out.append(
                    {
                        "id": m.id,
                        "title": m.title or m.name,
                        "subproject": m.subproject or "",
                        "status": m.status,
                        "item_total": m.total,
                        "item_done": m.done_count,
                        "total": 0,
                        "done": 0,
                        "awaiting_review": 0,
                        "running": 0,
                    }
                )
        data[proj.name] = {"present": r.present, "milestones": milestones_out, "unlinked": 0}

    # Attribute cards to projects, then link each to a milestone by id. A card
    # that attributes to no project is skipped entirely (never guessed); a card
    # attributed to a project but with no matching milestone id is UNLINKED.
    for card in all_cards:
        proj = kanban.project_for_card(card, projects)
        if proj is kanban.UNATTRIBUTED:
            continue
        proj_data = data.get(proj.name)
        if proj_data is None:
            continue
        mid = _card_milestone_id(card)
        # Compare ids case-insensitively: a card may stamp `MILESTONE:
        # Auth-Hardening` while the roadmap id is the lowercase slug
        # `auth-hardening`. Milestone ids (slugs) are effectively lowercase, so
        # folding both sides is the tolerant reading the card asks for.
        folded = mid.lower() if mid else None
        target = next(
            (m for m in proj_data["milestones"] if m["id"].lower() == folded),
            None,
        ) if folded else None
        if target is None:
            proj_data["unlinked"] += 1
            continue
        target["total"] += 1
        status = str(card.get("status") or "")
        if status in _PROGRESS_DONE_STATUSES:
            target["done"] += 1
        if status in _PROGRESS_REVIEW_STATUSES:
            target["awaiting_review"] += 1
        if status in _PROGRESS_RUNNING_STATUSES:
            target["running"] += 1
    return data


def _milestone_line(m: dict) -> str:
    """One human line for a milestone, e.g.::

        hscc / monitoring-daemon   [later]   items 9/9   ·   cards 0   ·   complete
        soconn / oracle-track      [now]     items 4/12  ·   cards 3 (1 awaiting review, 2 running)

    ``items X/Y`` counts the roadmap checklist items (``- [x]``/``- [ ]``);
    ``cards N`` counts the kanban cards linked by ``MILESTONE: <id>``. Both are
    ALWAYS shown so neither source of truth can hide the other. State words are
    reserved: ``complete`` for a milestone whose items are all ticked and that
    has no cards; ``not started`` for 0 items done AND 0 cards. A milestone
    mid-flight (some items done, or any cards) states neither and just shows the
    two counts.
    """
    label = f"{m['subproject']} / {m['title']}" if m["subproject"] else m["title"]
    status = f"[{m['status']}]" if m["status"] else ""
    head = f"{label}   {status}  ".rstrip()

    item_total = m["item_total"]
    item_done = m["item_done"]
    cards_total = m["total"]

    cards_part = f"cards {cards_total}"
    if m["awaiting_review"] or m["running"]:
        bits = []
        if m["awaiting_review"]:
            bits.append(f"{m['awaiting_review']} awaiting review")
        if m["running"]:
            bits.append(f"{m['running']} running")
        cards_part += f" ({', '.join(bits)})"

    line = f"{head}  items {item_done}/{item_total}  ·  {cards_part}"

    if cards_total == 0 and item_done == 0:
        # 0 items done AND 0 cards — genuinely untouched.
        line += "  ·  not started"
    elif cards_total == 0 and item_total > 0 and item_done == item_total:
        # All items ticked, no cards — complete, never "not started".
        line += "  ·  complete"

    return line


def _render_progress(data: dict) -> list[str]:
    lines: list[str] = []
    for name, proj in data.items():
        lines.append(f"[{name}]")
        if not proj["present"]:
            lines.append("  no roadmap")
            continue
        for m in proj["milestones"]:
            lines.append(_milestone_line(m))
        if proj["unlinked"]:
            count = proj["unlinked"]
            noun = "card" if count == 1 else "cards"
            lines.append(f"  {count} unlinked {noun}")
    return lines


def _progress_json(data: dict) -> dict:
    out: dict[str, dict] = {}
    for name, proj in data.items():
        out[name] = {
            "present": proj["present"],
            "milestones": [
                {
                    "id": m["id"],
                    "title": m["title"],
                    "subproject": m["subproject"],
                    "status": m["status"],
                    "items": {
                        "done": m["item_done"],
                        "total": m["item_total"],
                    },
                    "cards": {
                        "total": m["total"],
                        "done": m["done"],
                        "awaiting_review": m["awaiting_review"],
                        "running": m["running"],
                    },
                }
                for m in proj["milestones"]
            ],
            "unlinked": proj["unlinked"],
        }
    return out


# --------------------------------------------------------------------------- #
# command handlers
# --------------------------------------------------------------------------- #


def _no_roadmap(path: str) -> int:
    print(_NO_ROADMAP.format(path=path), file=sys.stderr)
    return 1


def _definite_path(proj: registry.Project) -> str:
    """The resolved ROADMAP path as a non-optional string."""
    return roadmap.project_roadmap(proj).path or "ROADMAP.md"


def cmd_show(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    # `roadmap show <project>` narrows to one project; bare `roadmap` shows all.
    # A name that matches nothing is an error, never a silently empty roadmap.
    wanted = getattr(args, "project", None)
    if wanted:
        projects = [p for p in projects if p.name == wanted]
        if not projects:
            print(
                f"no project named {wanted!r} in the registry", file=sys.stderr
            )
            return 2
    rows = [(p.name, _definite_path(p)) for p in projects]
    if args.json:
        import json

        print(json.dumps(_render_json(rows)))
        return 0
    for line in _render(rows):
        print(line)
    return 0


def cmd_progress(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    """Per-milestone card progress for one project (or all).

    Narrows to ``[project]`` when named; a name that matches nothing is an
    error, never a silently empty report. Cards are read once (every board) and
    attributed/linked by the shared core helpers, then rendered per milestone —
    or the project says \"no roadmap\" when it has none.
    """
    wanted = getattr(args, "project", None)
    if wanted:
        projects = [p for p in projects if p.name == wanted]
        if not projects:
            print(
                f"no project named {wanted!r} in the registry", file=sys.stderr
            )
            return 2

    all_cards = kanban.list_cards(board=None, include_archived=False)
    data = _aggregate_progress(projects, all_cards)

    if args.json:
        import json

        print(json.dumps(_progress_json(data)))
        return 0
    for line in _render_progress(data):
        print(line)
    return 0


def cmd_add(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    proj = _get_project(projects, args.project)
    if proj is None:
        return 2
    path = _definite_path(proj)
    section = args.section or _DEFAULT_SECTION
    heading = _SECTION_HEADING[section]

    # A missing ROADMAP.md: offer to create it with the three sections, and
    # with --apply actually create it. Either way we fall through to append the
    # item to the (now-existing) file.
    if not _exists(path):
        print(f"will create {path} and add {args.item!r} to '{heading}'", file=sys.stderr)
        if not args.apply:
            print(
                "dry-run: pass --apply to create the roadmap and add the item.",
                file=sys.stderr,
            )
            return 0
        _write_file(path, _FILE_HEADER)

    text = _read_file(path)
    if text is None:
        return _no_roadmap(path)
    lines, items = _scan(text)
    if _heading_line(lines, heading) is None:
        print(
            f"error: {path} has no '## {heading}' section to add to.",
            file=sys.stderr,
        )
        return 1
    print(f"will add {args.item!r} to '{heading}' in {path}", file=sys.stderr)
    if not args.apply:
        print("dry-run: pass --apply to add the item.", file=sys.stderr)
        return 0
    new_lines = _apply_add(lines, items, heading, args.item)
    _write_file(path, "\n".join(new_lines))
    print(f"added {args.item!r} to '{heading}' in {path}")
    return 0


def cmd_move(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    return _locate_and_mutate(args, projects, "move")


def cmd_done(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    return _locate_and_mutate(args, projects, "done")


def _locate_and_mutate(
    args: argparse.Namespace, projects: list[registry.Project], op: str
) -> int:
    """Shared location + apply path for ``move``/``done``.

    Handles the common contract: resolve the project, refuse a missing roadmap
    ("no roadmap", exit non-zero), locate the item by substring, list
    candidates when ambiguous and change nothing, then print the intended
    change and require ``--apply``.
    """
    proj = _get_project(projects, args.project)
    if proj is None:
        return 2
    path = _definite_path(proj)
    text = _read_file(path)
    if text is None:
        return _no_roadmap(path)

    lines, items = _scan(text)
    matched = _matches(items, args.item)

    if not matched:
        print(
            f"{op}: no roadmap item matching {args.item!r} in {path}",
            file=sys.stderr,
        )
        return 1
    if len(matched) > 1:
        _list_candidates(op, path, matched)
        print(f"{op}: ambiguous match for {args.item!r}; nothing changed.", file=sys.stderr)
        return 1

    item = matched[0]
    to_heading = ""
    if op == "move":
        to_heading = _SECTION_HEADING[args.to]
        if item["section"] == to_heading:
            print(f"item {args.item!r} is already in '{to_heading}'; nothing to do.")
            return 0
        if _heading_line(lines, to_heading) is None:
            print(
                f"error: {path} has no '## {to_heading}' section to move to.",
                file=sys.stderr,
            )
            return 1
        print(
            f"will move {args.item!r} from '{item['section'] or '(no section)'}' "
            f"to '{to_heading}' in {path}"
        )
    else:  # done
        if item["checked"]:
            print(f"item {args.item!r} is already checked off; nothing to do.")
            return 0
        print(
            f"will mark {args.item!r} done in '{item['section'] or '(no section)'}' "
            f"of {path}"
        )

    if not args.apply:
        print("dry-run: pass --apply to perform this change.", file=sys.stderr)
        return 0

    if op == "move":
        new_lines = _apply_move(lines, items, item, to_heading)
        outline = f"moved {args.item!r} to '{to_heading}' in {path}"
    else:  # done
        new_lines = _apply_done(lines, item)
        outline = f"marked {args.item!r} done in {path}"
    _write_file(path, "\n".join(new_lines))
    print(outline)
    return 0


def _list_candidates(op: str, path: str, matched: list[dict]) -> None:
    print(
        f"{op}: multiple items in {path} match — candidates:",
        file=sys.stderr,
    )
    for it in matched:
        sec = it["section"] or "(no section)"
        box = "x" if it["checked"] else " "
        print(f"  [{sec}] - [{box}] {it['text']}", file=sys.stderr)


def _get_project(projects: list[registry.Project], name: str) -> Optional[registry.Project]:
    for p in projects:
        if p.name == name:
            return p
    print(f"error: no project named {name!r} in the registry", file=sys.stderr)
    return None


# --------------------------------------------------------------------------- #
# injectable file access (tests stub these; production uses the real fs)
# --------------------------------------------------------------------------- #


def _read_file(path: str) -> Optional[str]:
    """Read the file, or None when absent/unreadable (the "no roadmap" marker)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, IOError, UnicodeDecodeError):
        return None


def _write_file(path: str, text: str) -> None:
    import os

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _exists(path: str) -> bool:
    import os

    return os.path.exists(path)


# --------------------------------------------------------------------------- #
# roadmap adopt — promote a reviewed docs/ROADMAP.draft.md into the real one
# --------------------------------------------------------------------------- #
#
# `ingest` writes a model-generated proposal to docs/ROADMAP.draft.md; there is
# no command to promote it, so the operator moves files by hand — and hand-
# moving is how a hand-written roadmap gets clobbered. `adopt` validates the
# draft, diffs it against the existing ROADMAP.md, backs the existing file up,
# and only then promotes. Dry-run by default; --apply performs it.

_DRAFT_RELPATH = os.path.join("docs", "ROADMAP.draft.md")


def _draft_path(proj: registry.Project) -> str:
    """The absolute path of the project's ROADMAP draft."""
    return os.path.join(proj.repo, _DRAFT_RELPATH)


def _target_path(proj: registry.Project) -> str:
    """The absolute path of the real (promotion target) ROADMAP file."""
    return os.path.join(proj.repo, proj.roadmap or "ROADMAP.md")


def _milestone_key(m: roadmap.Milestone) -> str:
    """The stable identity we match milestones on for the diff."""
    return m.id or m.title or m.name


def _has_items(r: roadmap.Roadmap) -> bool:
    """True when the roadmap yields at least one milestone holding an item."""
    return any(m.items for m in r.milestones)


def _diff_roadmaps(draft_r: roadmap.Roadmap, existing_r: roadmap.Roadmap) -> dict:
    """Structural diff of the draft against the existing roadmap.

    Milestones are matched by stable id (``m.id``, falling back to title then
    name); items within a matched milestone by exact text. Returns::

        {
          "milestones_added": [label...],
          "milestones_removed": [label...],
          "items_added": [(milestone_label, item_text)...],
          "items_removed": [(milestone_label, item_text)...],
          "checked_changed": [
            {"milestone": label, "item": text, "was": bool, "now": bool}...
          ],
        }

    ``checked_changed`` is the load-bearing one: the draft is model-generated,
    so it may un-tick an item the operator already finished — that must never
    pass silently.
    """
    dm = {_milestone_key(m): m for m in draft_r.milestones}
    em = {_milestone_key(m): m for m in existing_r.milestones}

    milestones_added = [k for k in dm if k not in em]
    milestones_removed = [k for k in em if k not in dm]

    items_added: list[tuple[str, str]] = []
    items_removed: list[tuple[str, str]] = []
    checked_changed: list[dict] = []

    for key, dmil in dm.items():
        label = dmil.title or dmil.name or key
        emil = em.get(key)
        if emil is None:
            # A whole new milestone: every item counts as added.
            for it in dmil.items:
                items_added.append((label, it.text))
            continue
        eitems = {it.text for it in emil.items}
        ditems = {it.text for it in dmil.items}
        items_added.extend((label, it.text) for it in dmil.items if it.text not in eitems)
        items_removed.extend((label, it.text) for it in emil.items if it.text not in ditems)
        emap = {it.text: it.checked for it in emil.items}
        for it in dmil.items:
            was = emap.get(it.text)
            if was is not None and was != it.checked:
                checked_changed.append(
                    {"milestone": label, "item": it.text, "was": was, "now": it.checked}
                )

    return {
        "milestones_added": sorted(milestones_added),
        "milestones_removed": sorted(milestones_removed),
        "items_added": sorted(items_added),
        "items_removed": sorted(items_removed),
        "checked_changed": checked_changed,
    }


def _render_diff(diff: dict, target: str) -> list[str]:
    """Human lines describing ``diff`` against ``target`` (never empty)."""
    out: list[str] = [f"diff vs {target}:"]

    def _milestone_list(label: str, items: list) -> None:
        if not items:
            return
        noun = "milestone" if len(items) == 1 else "milestones"
        out.append(f"  {label} {noun}: {', '.join(items)}")

    _milestone_list("added", diff["milestones_added"])
    _milestone_list("removed", diff["milestones_removed"])

    def _item_list(label: str, items: list[tuple[str, str]]) -> None:
        if not items:
            return
        noun = "item" if len(items) == 1 else "items"
        out.append(f"  {label} {noun} ({len(items)}):")
        for ms, it in items:
            out.append(f"    - {ms} / {it}")

    _item_list("added", diff["items_added"])
    _item_list("removed", diff["items_removed"])

    if diff["checked_changed"]:
        noun = "item" if len(diff["checked_changed"]) == 1 else "items"
        out.append(f"  CHECK-STATE CHANGED {noun} ({len(diff['checked_changed'])}):")
        for c in diff["checked_changed"]:
            was = "done" if c["was"] else "open"
            now = "done" if c["now"] else "open"
            out.append(f"    - {c['milestone']} / {c['item']}: was {was}, now {now}")

    return out


def cmd_adopt(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    """Promote a reviewed docs/ROADMAP.draft.md into the real ROADMAP.md.

    Refuses a missing or unparseable draft (never promotes garbage). Diffs the
    draft against the existing ROADMAP.md when one exists and flags items whose
    checked state would change — the draft is model-generated, so it may
    un-tick something already finished. Backs up an existing ROADMAP.md to
    ``<target>.bak`` before overwriting, and removes the draft only after a
    successful write. Dry-run by default; ``--apply`` performs it.
    """
    proj = _get_project(projects, args.project)
    if proj is None:
        return 2

    draft = _draft_path(proj)
    if not _exists(draft):
        print(
            f"no draft: {draft} does not exist — nothing to adopt.",
            file=sys.stderr,
        )
        return 1
    draft_text = _read_file(draft)
    if draft_text is None:
        print(f"no draft: could not read {draft}.", file=sys.stderr)
        return 1

    # VALIDATE before touching anything: an unparseable draft is refused, never
    # promoted. >=1 milestone with >=1 item is required.
    try:
        draft_r = roadmap.parse_roadmap(draft)
    except roadmap.DuplicateMilestoneIdError as exc:
        print(
            f"refusing to adopt: {draft} does not parse as a roadmap ({exc}).",
            file=sys.stderr,
        )
        return 1
    if not draft_r.present or not _has_items(draft_r):
        print(
            f"refusing to adopt: {draft} has no milestone with an item; "
            f"nothing to promote.",
            file=sys.stderr,
        )
        return 1

    target = _target_path(proj)
    if _exists(target):
        existing_r = roadmap.parse_roadmap(target)
        for line in _render_diff(_diff_roadmaps(draft_r, existing_r), target):
            print(line)
    else:
        print(f"no existing roadmap at {target} — adopting fresh.")

    if not args.apply:
        print(
            "dry-run: no files changed. pass --apply to back up the existing "
            "roadmap, promote the draft, and remove it.",
            file=sys.stderr,
        )
        return 0

    # Back up an existing roadmap before overwriting — never destroy one.
    bak = None
    if _exists(target):
        bak = target + ".bak"
        _write_file(bak, _read_file(target) or "")
        print(f"backed up existing roadmap to {bak}")

    _write_file(target, draft_text)
    # The write succeeded; only now remove the draft.
    try:
        os.remove(draft)
    except OSError as exc:
        print(
            f"warning: wrote {target}, but could not remove the draft {draft}: {exc}",
            file=sys.stderr,
        )
    print(f"adopted {draft} -> {target}")
    return 0


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #


def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "roadmap",
        help="show a roadmap, or add/move/done/adopt items in ROADMAP.md",
        epilog="example: flightdeck roadmap show flightdeck",
    )
    subsub = p.add_subparsers(dest="roadmap_cmd", metavar="ROADMAP_CMD")

    # Bare `roadmap` shows the Now/Next/Later view for every project.
    p.set_defaults(func=cmd_show, project=None)

    sp = subsub.add_parser("show", help="show one project's roadmap (or all)",
                           epilog="example: flightdeck roadmap show flightdeck")
    sp.add_argument("project", nargs="?", default=None,
                    help="project name in the registry (default: all)")
    sp.set_defaults(func=cmd_show)

    pp = subsub.add_parser("progress", help="per-milestone card progress",
                           epilog="example: flightdeck roadmap progress flightdeck")
    pp.add_argument("project", nargs="?", default=None,
                    help="project name in the registry (default: all)")
    pp.set_defaults(func=cmd_progress)

    ap = subsub.add_parser("add", help="add an item to Now/Next/Later",
                           epilog='example: flightdeck roadmap add flightdeck "ship 0.6.0" --apply')
    ap.add_argument("project", help="project name in the registry")
    ap.add_argument("item", help="item text to add")
    ap.add_argument(
        "--section", choices=_SECTION_NAMES, default=None,
        help="section to add to (default: now)",
    )
    ap.set_defaults(func=cmd_add)

    mp = subsub.add_parser("move", help="promote/demote an item between sections",
                           epilog='example: flightdeck roadmap move flightdeck "0.6.0" --to next --apply')
    mp.add_argument("project", help="project name in the registry")
    mp.add_argument("item", help="substring matching the item text")
    mp.add_argument(
        "--to", choices=_SECTION_NAMES, required=True,
        help="section to move the item to",
    )
    mp.set_defaults(func=cmd_move)

    dp = subsub.add_parser("done", help="tick an item's checkbox",
                           epilog='example: flightdeck roadmap done flightdeck "0.6.0" --apply')
    dp.add_argument("project", help="project name in the registry")
    dp.add_argument("item", help="substring matching the item text")
    dp.set_defaults(func=cmd_done)

    adp = subsub.add_parser(
        "adopt", help="promote a reviewed docs/ROADMAP.draft.md into ROADMAP.md",
        epilog="example: flightdeck roadmap adopt flightdeck --apply",
    )
    adp.add_argument("project", help="project name in the registry")
    adp.add_argument(
        "--apply",
        action="store_true",
        help="perform the promotion (dry-run by default; backs up the existing "
        "roadmap and removes the draft)",
    )
    adp.set_defaults(func=cmd_adopt)

    for sp in (ap, mp, dp):
        sp.add_argument(
            "--apply",
            action="store_true",
            help="perform the change (mutating commands are dry-run by default)",
        )


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py: run a roadmap command.

    File access goes through the module-level ``_read_file`` / ``_write_file`` /
    ``_exists``, which tests can monkeypatch (or, more simply, point at a
    tmp_path repo) so nothing here touches a live repo.

    When the narrowing ``[project]`` positional is omitted on the forms that
    accept one (``show`` / ``progress``), it is auto-detected from the cwd — the
    project whose repo contains the current directory — via the shared
    ``resolve_project_arg`` seam. An explicit argument always wins; when the cwd
    matches no registered repo the command falls through to its existing
    show-all default untouched.
    """
    args.registry = registry_path
    projects = registry.load_registry(registry_path)
    # Narrowing forms (show/progress) accept an optional [project]; the
    # mutating forms (add/move/done) require it and are never auto-detected.
    if not getattr(args, "project", None) and getattr(args, "func", None) in (cmd_show, cmd_progress):
        args.project, _detected = registry.resolve_project_arg(
            projects, None,
            cwd=getattr(args, "cwd", None),
            _print=lambda line: print(line, file=sys.stderr),
        )
    func = getattr(args, "func", None)
    if func is None:
        print("roadmap: command not recognised. Try `flightdeck roadmap --help`.", file=sys.stderr)
        return 2
    return func(args, projects)
