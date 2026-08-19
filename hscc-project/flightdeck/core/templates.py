"""templates.py — prompt templates with auto-filled project context.

The ``ask`` command stops the operator retyping the same framings into Telegram
topics ("decompose this task…", "here is the project and where I want to reach…",
"please review X"). It renders a stored markdown template, fills it with context
flightdeck ALREADY knows about the project, and sends it to that project's
topic. The operator supplies only what is genuinely new (via ``--set``).

This module owns the pure logic — where templates live, how they are seeded,
what auto-fill context flightdeck can derive, and how a template is rendered.
The ``ask`` command is presentation only, mirroring how every other command
composes core modules.

Three invariants (see docs/FEATURES-2.md "P0 — prompt templates"):

1. Auto-fill from the registry + repo, never retyped by the operator: project
   name, repo path, current branch, HEAD sha, ROADMAP.md "Now" items, open
   cards on the board + what awaits review, and the verify command.
2. ``--set key=value`` overrides the auto-fill for that slot.
3. An unfilled slot is an ERROR listing what the template expects. A message
   containing a literal ``{{slot}}`` is never sent.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from . import git_state, kanban, roadmap

# --------------------------------------------------------------------------- #
# Template store
# --------------------------------------------------------------------------- #

# Shipped templates ship inside the package: flightdeck/templates/*.md.
SHIPPED_DIR = Path(__file__).resolve().parent.parent / "templates"

# User-editable home. Seeded from SHIPPED_DIR on first use.
DEFAULT_HOME = "~/.flightdeck/templates"

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class TemplateError(Exception):
    """Base class for template errors."""


class UnknownTemplateError(TemplateError):
    """The named template does not exist in the store."""


class UnfilledSlotError(TemplateError):
    """A template has one or more slots with no value to fill them."""


def templates_home(path: str | None = None) -> Path:
    """The user-editable templates directory (default ~/.flightdeck/templates)."""
    p = path if path is not None else DEFAULT_HOME
    return Path(os.path.expanduser(p))


def ensure_seeded(home: str | None = None) -> None:
    """Copy the shipped template set into the user home if any are missing.

    Seeding only ever ADDS — an existing user-edited template is never
    overwritten, and templates the user created themselves are never deleted.
    Idempotent: a re-run does nothing once every shipped template is present.
    """
    dest = templates_home(home)
    dest.mkdir(parents=True, exist_ok=True)
    for src in sorted(SHIPPED_DIR.glob("*.md")):
        dst = dest / src.name
        if not dst.exists():
            shutil.copyfile(src, dst)


def list_templates(home: str | None = None) -> list[str]:
    """Template names available in the store (seeded first), sorted."""
    ensure_seeded(home)
    names = sorted(p.stem for p in templates_home(home).glob("*.md"))
    return names


def _template_path(name: str, home: str | None = None) -> Path:
    """The path for the template ``name``, rejecting path traversal."""
    if not name or not _SAFE_NAME_RE.match(name):
        raise UnknownTemplateError(
            f"invalid template name: {name!r} (must be a plain word)"
        )
    return templates_home(home) / f"{name}.md"


def show_template(name: str, home: str | None = None) -> str:
    """Return the raw text of template ``name``.

    Raises :class:`UnknownTemplateError` listing the available templates when
    ``name`` is not in the store.
    """
    ensure_seeded(home)
    p = _template_path(name, home)
    if not p.exists():
        raise UnknownTemplateError(
            f"unknown template: {name!r}. Available: {', '.join(list_templates(home))}"
        )
    return p.read_text(encoding="utf-8")


def save_template(name: str, text: str, home: str | None = None) -> None:
    """Write ``text`` to the template named ``name`` (creating the store)."""
    ensure_seeded(home)
    p = _template_path(name, home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Auto-fill context (derived, never retyped)
# --------------------------------------------------------------------------- #

def _roadmap_now(project) -> str:
    """The project's ROADMAP.md "Now" items as a multi-line block.

    A missing roadmap, or a Now section with no open items, degrades to a clear
    marker ("(no roadmap)" / "(none)") — never a crash, never silence.
    """
    rm = roadmap.project_roadmap(project)
    if not rm.present:
        return "(no roadmap)"
    now = rm.milestone("Now")
    if now is None:
        return "(none)"
    lines = [f"- {i.text}" for i in now.items if not i.checked]
    return "\n".join(lines) if lines else "(none)"


def _card_summary(cards: list[dict]) -> tuple[str, str]:
    """Format the board's open cards and the subset awaiting review.

    Returns ``(open_block, awaiting_block)``. ``awaiting`` only includes cards
    in a review status; ``open`` includes every card. An empty board degrades
    to "(none)" so the template renders cleanly rather than leaving a hole.
    """
    open_lines: list[str] = []
    awaiting_lines: list[str] = []
    for c in cards:
        cid = c.get("id")
        title = c.get("title") or "(untitled)"
        status = c.get("status") or ""
        line = f"- {cid} ({status}) {title}" if cid else f"- {title}"
        open_lines.append(line)
        if status in kanban.REVIEW_STATUSES:
            awaiting_lines.append(line)
    open_block = "\n".join(open_lines) if open_lines else "(none)"
    awaiting_block = "\n".join(awaiting_lines) if awaiting_lines else "(none)"
    return open_block, awaiting_block


def gather_context(
    project,
    *,
    _run=None,
    _list_cards=None,
) -> dict:
    """The auto-fill context flightdeck can derive about ``project``.

    Every value degrades to a clear marker (never None, never a crash) so a
    partially-populated project still renders — an incomplete project should
    never block the operator from reaching the project's topic.

    ``_run`` is the injectable subprocess runner for git_state; ``_list_cards``
    is the injectable board reader (default ``kanban.list_cards``), both stubbed
    in tests so nothing here touches git, the network, or the live board.
    """
    if _list_cards is None:
        _list_cards = kanban.list_cards

    branch = git_state.current_branch(project.repo, _run=_run)
    head = git_state.head_sha(project.repo, _run=_run)
    short = (head[:7] + "…") if head else "(unknown)"

    cards: list[dict] = []
    if project.board is not None:
        cards = _list_cards(board=project.board)
    open_block, awaiting_block = _card_summary(cards)

    return {
        "project": project.name,
        "repo": project.repo,
        "branch": branch or "(unknown)",
        "head_sha": f"{short} ({head})" if head else "(unknown)",
        "verify": project.verify or "(none configured)",
        "roadmap_now": _roadmap_now(project),
        "open_cards": open_block,
        "awaiting_review": awaiting_block,
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

_SLOT_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


def _slot_value(name: str, context: dict, overrides: dict) -> str | None:
    """The value for a slot, applying --set override precedence.

    ``overrides`` always wins over the auto-fill ``context`` — the operator's
    explicit ``--set key=value`` is the highest authority. Either source being
    present satisfies the slot; both absent leaves it unfilled.
    """
    if name in overrides:
        return overrides[name]
    return context.get(name)


def render_template(
    text: str,
    context: dict,
    overrides: dict | None = None,
) -> str:
    """Fill every ``{{slot}}`` in ``text`` and return the rendered result.

    Raises :class:`UnfilledSlotError` naming any slot with no value — an
    operator-set value or an auto-filled context value — so a template with a
    hole is an error that lists what is expected, never a message sent with a
    literal ``{{slot}}`` in it.
    """
    overrides = overrides or {}
    slots = sorted({m.group(1).strip() for m in _SLOT_RE.finditer(text)})
    missing = [s for s in slots if _slot_value(s, context, overrides) is None]
    if missing:
        raise UnfilledSlotError(
            "template expects value(s) that are not filled: "
            + ", ".join(f"{{{s}}}" for s in missing)
            + " — pass them with --set <name>=<value>"
        )
    def _fill(m: re.Match) -> str:
        value = _slot_value(m.group(1).strip(), context, overrides)
        # Every slot was verified non-None above; '' is fine but None cannot
        # occur here. Belt-and-braces so a literal {{slot}} is never emitted.
        return value if value is not None else m.group(0)

    return _SLOT_RE.sub(_fill, text)


def slots_in(text: str) -> list[str]:
    """The distinct slot names a template references, sorted."""
    return sorted({m.group(1).strip() for m in _SLOT_RE.finditer(text)})
