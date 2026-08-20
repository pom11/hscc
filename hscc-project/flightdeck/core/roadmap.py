"""roadmap.py — parse a repo's ROADMAP.md.

Reads ``## Now`` / ``## Next`` / ``## Later`` sections containing ``- [ ]`` /
``- [x]`` checklist items and returns structured milestones plus counts.

A missing file returns a clear "no roadmap" marker (``present=False``) —
never an exception, never silence. A malformed file must not crash: we parse
every ``##`` heading and every checklist item we can recognize, and collect
everything else in ``unparsed`` so it is reported rather than silently dropped.

The parser is ADDITIVE over the legacy flat format. A new-style file may also
use subprojects and milestones with stable ids::

    # Subproject: client-portal

    ## Milestone: auth-hardening        <!-- id: auth-hardening -->
    status: now
    - [ ] server-side key enforcement
    - [x] password reset restricted to self/admin

The milestone id comes from the ``<!-- id: ... -->`` comment when present,
else it is slugified from the title. The id is STABLE — renaming a milestone's
prose must not orphan its cards, which is exactly why the explicit id comment
exists. A duplicate id in one file is an error naming both lines, never a
silent last-wins.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# A `## Heading` line begins a new milestone section. Only H2 counts — a `#
# # Title` or `### Sub` is not a milestone section, so it is reported as
# unparsed rather than misread as one.
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
# `## Milestone: title <!-- id: ... -->` — the new-style milestone heading.
_MILESTONE_RE = re.compile(r"^##\s+Milestone\s*:\s*(.+?)\s*$", re.IGNORECASE)
# `# Subproject: name` opens a new subproject scope (H1, not H2).
_SUBPROJECT_RE = re.compile(r"^#\s+Subproject\s*:\s*(.+?)\s*$", re.IGNORECASE)
# `<!-- id: ... -->` — the explicit stable id attached to a milestone heading.
_ID_COMMENT_RE = re.compile(r"<!--\s*id\s*:\s*([^>\s]+?)\s*-->", re.IGNORECASE)
# `status: now` — a milestone's declared status (now | next | later | done).
_STATUS_RE = re.compile(r"^status\s*:\s*(\S+)\s*$", re.IGNORECASE)
# Allowed milestone statuses (the declared `status:` values).
_STATUSES = frozenset({"now", "next", "later", "done"})
# Legacy flat sections carry their status in the heading itself.
_LEGACY_STATUS = {"Now": "now", "Next": "next", "Later": "later"}
# A checklist item: "- [ ] text" or "- [x] text" (space, x or X accepted).
_ITEM_RE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s*(.*)$")


def _slugify(text: str) -> str:
    """Turn a milestone title into a stable, human-readable id.

    Lowercases and maps every run of non-alphanumerics to a single hyphen,
    trimming leading/trailing hyphens. e.g. ``Auth Hardening!`` -> ``auth-hardening``.
    """
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


class DuplicateMilestoneIdError(ValueError):
    """Raised when two milestones resolve to the same id in one file.

    A duplicate id is an error naming both source lines — never a silent
    last-wins, because that would orphan the first milestone's cards.
    """

    def __init__(self, milestone_id: str, first: str, second: str):
        self.milestone_id = milestone_id
        self.first = first
        self.second = second
        super().__init__(
            f"duplicate milestone id {milestone_id!r}: {first!r} and {second!r}"
        )


def _parse_milestone_heading(line: str):
    """Parse a ``## Milestone: ...`` heading, or return None for a plain H2.

    Returns ``(title, explicit_id)`` where ``explicit_id`` is the id from a
    ``<!-- id: ... -->`` comment if present, else None (meaning "slugify the
    title"). Returns None when the line is not a ``## Milestone:`` heading.
    """
    m = _MILESTONE_RE.match(line)
    if m is None:
        return None
    raw = m.group(1)
    id_m = _ID_COMMENT_RE.search(raw)
    explicit = id_m.group(1).strip() if id_m else None
    title = _ID_COMMENT_RE.sub("", raw).strip()
    return title, explicit


@dataclass(frozen=True)
class RoadmapItem:
    """A single checklist item within a milestone."""

    text: str
    checked: bool


@dataclass
class Milestone:
    """One milestone: a ``## `` section plus the items under it.

    ``name`` is the heading text (kept for backward compatibility — it is the
    section name for legacy ``## Now`` files and the title for new-style
    ``## Milestone: ...`` headings). ``id`` is the stable id: the value of an
    explicit ``<!-- id: ... -->`` comment when present, else the slug of the
    title. ``title`` is the display title. ``subproject`` is the enclosing
    ``# Subproject:`` name, or ``""`` for the implicit single subproject when
    no subproject heading precedes the milestone. ``status`` is the declared
    status (``now`` | ``next`` | ``later`` | ``done``) or None when unknown.
    """

    name: str
    id: str = ""
    title: str = ""
    subproject: str = ""
    status: Optional[str] = None
    items: list[RoadmapItem] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def open_count(self) -> int:
        """Unchecked items — what still needs doing in this milestone."""
        return sum(1 for it in self.items if not it.checked)

    @property
    def done_count(self) -> int:
        return self.total - self.open_count


@dataclass
class Roadmap:
    """The parsed roadmap, plus a clear signal for a missing file.

    ``present`` is False when the file does not exist (or cannot be read) —
    the "no roadmap" marker. ``unparsed`` collects any lines that could not be
    attributed to a milestone, so a malformed file is reported, never silently
    swallowed.
    """

    present: bool
    path: Optional[str]
    milestones: list[Milestone] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)

    def milestone(self, name: str) -> Optional[Milestone]:
        """The milestone with the given name, or None if absent."""
        for m in self.milestones:
            if m.name == name:
                return m
        return None


def parse_roadmap(path) -> Roadmap:
    """Parse the ROADMAP.md at ``path``.

    A missing or unreadable file returns a ``Roadmap`` with ``present=False``
    — a clear "no roadmap" marker, never an exception, never silence. Any
    non-blank line that is neither a heading nor a recognized checklist item is
    recorded in ``unparsed``.

    The parser is ADDITIVE over the legacy flat format: ``## Now`` / ``## Next``
    / ``## Later`` with ``- [ ]`` items still parse exactly as before, in a
    single implicit subproject. It additionally understands:

      - ``# Subproject: name``  — opens a subproject scope (the implicit
        subproject is used when none precedes a milestone);
      - ``## Milestone: title <!-- id: ... -->`` — a milestone whose id comes
        from the comment when present, else from a stable slug of the title;
      - ``status: now`` — a milestone's declared status.

    The id is STABLE: an explicit ``<!-- id: ... -->`` comment wins, otherwise
    the id is the slug of the title, so renaming prose must not orphan cards.
    A duplicate id in one file raises :class:`DuplicateMilestoneIdError` naming
    both source lines — never a silent last-wins.
    """
    p = Path(path)
    if not p.exists():
        return Roadmap(present=False, path=str(p))

    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except (OSError, IOError, UnicodeDecodeError):
        # Unreadable counts as no roadmap, reported, never silent.
        return Roadmap(present=False, path=str(p))

    milestones: list[Milestone] = []
    current: Optional[Milestone] = None
    unparsed: list[str] = []
    subproject: str = ""            # "" = the implicit single subproject
    seen_ids: dict[str, str] = {}   # id -> source heading line (for dup errors)

    def _add_milestone(name, title, the_id, status, source_line) -> Milestone:
        if the_id in seen_ids:
            raise DuplicateMilestoneIdError(the_id, seen_ids[the_id], source_line)
        seen_ids[the_id] = source_line
        m = Milestone(
            name=name,
            id=the_id,
            title=title,
            subproject=subproject,
            status=status,
        )
        milestones.append(m)
        return m

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        sub = _SUBPROJECT_RE.match(line)
        if sub:
            subproject = sub.group(1).strip()
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            milestone = _parse_milestone_heading(line)
            if milestone is not None:
                title, explicit = milestone
                the_id = explicit if explicit else _slugify(title)
                current = _add_milestone(title, title, the_id, None, line)
            else:
                # Legacy flat section: `## Now` etc.
                name = heading.group(1).strip()
                the_id = _slugify(name)
                status = _LEGACY_STATUS.get(name)
                current = _add_milestone(name, name, the_id, status, line)
            continue

        status_m = _STATUS_RE.match(line)
        if status_m and current is not None:
            value = status_m.group(1).strip().lower()
            if value in _STATUSES:
                current.status = value
                continue
            # An unrecognized status value is not consumed here; fall through
            # to unparsed so it is reported rather than silently dropped.

        item = _ITEM_RE.match(line)
        if item:
            if current is None:
                # A checklist item before any section heading.
                unparsed.append(line)
            else:
                current.items.append(
                    RoadmapItem(
                        text=item.group(2).strip(),
                        checked=item.group(1).lower() == "x",
                    )
                )
            continue

        # A non-blank line that is neither a heading nor a checklist item.
        unparsed.append(line)

    return Roadmap(
        present=True,
        path=str(p),
        milestones=milestones,
        unparsed=unparsed,
    )


def milestones(project) -> list[Milestone]:
    """Parse a registry project's roadmap and return its milestones.

    Thin wrapper over :func:`project_roadmap` exposing just the milestone list
    — the primary interface the digest/standup pipeline consumes. Raises
    :class:`DuplicateMilestoneIdError` when the project's ROADMAP.md carries a
    duplicate milestone id.
    """
    return project_roadmap(project).milestones


def project_roadmap(project) -> Roadmap:
    """Parse a registry project's roadmap file.

    Resolves ``project.roadmap`` (a path within the repo, default
    ``ROADMAP.md``) against the repo root, so callers get a ``Roadmap`` without
    managing path assembly themselves.
    """
    rel = project.roadmap or "ROADMAP.md"
    path = os.path.join(project.repo, rel)
    return parse_roadmap(path)
