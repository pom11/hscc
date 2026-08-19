"""lint.py — flightdeck lint-cards: flag cards missing quality markers.

Presents `flightdeck lint-cards [board]` against :mod:`flightdeck.core.lint`
(the pure quality rules) and :mod:`flightdeck.core.kanban` (card reads).

Report-only: it never edits a card. It exits 1 when any card would run in a
repo's MAIN TREE (so a script can gate a dispatch), and 0 otherwise. Advisory
findings (missing VERIFY / acceptance / concrete references) are reported but
never make it exit non-zero.

For the "large module" check, referenced ``.py`` modules in each card body
are resolved to real files under a repo root and their line counts read;
module line counting is injectable (``args.module_line_counts``) so tests
drive the command without touching the filesystem.
"""

from __future__ import annotations

import argparse
import os
import sys

from ..core import kanban, lint


def _read_line_counts(body: str, repo_root: str) -> dict[str, int]:
    """Resolve every ``.py`` module the body references to a real file under
    ``repo_root`` and return {module: line_count}.

    An unresolvable module is omitted (its line count stays unknown), so it is
    never flagged as large — we refuse to call a module large without seeing it.
    I/O lives here; tests inject a stub in its place.
    """
    counts: dict[str, int] = {}
    for mod in lint.referenced_modules(body):
        path = os.path.join(repo_root, mod)
        try:
            with open(path, encoding="utf-8") as f:
                counts[mod] = sum(1 for _ in f)
        except (OSError, IOError):
            continue
    return counts


def cmd_lint(args: argparse.Namespace, cards: list[dict]) -> int:
    """Lint the given flightdeck cards. Read-only. Prints issues.

    Returns 0 when no CRITICAL finding exists, 1 when any card would run in a
    project's MAIN TREE (so it can gate a dispatch). ``args.module_line_counts``
    is an injectable ``body -> {module: line_count}`` callable defaulting to
    file-based counting against ``args.repo_root``; ``args.projects`` is the
    registered project list used for the main-tree check (defaults to empty,
    set by :func:`run` from the registry or injected directly in tests).
    """
    counts_fn = getattr(args, "module_line_counts", None)
    if counts_fn is None:
        repo_root = getattr(args, "repo_root", None) or os.getcwd()
        counts_fn = lambda body: _read_line_counts(body, repo_root)
    projects = getattr(args, "projects", None) or []

    advisory = 0
    for card in sorted(cards, key=lambda c: str(c.get("id") or "")):
        body = card.get("body") or ""
        issues = lint.lint_card(
            card,
            module_line_counts=counts_fn(body),
            module_line_threshold=getattr(
                args, "module_line_threshold", lint.DEFAULT_MODULE_LINE_THRESHOLD
            ),
        )
        if not issues:
            continue
        advisory += 1
        title = card.get("title") or "(untitled)"
        print(f"[{card.get('id') or '?'}] {title}")
        for issue in issues:
            print(f"    - {issue}")

    if advisory == 0:
        print("lint clean: every card has a VERIFY: line and an acceptance criterion.")

    # CRITICAL: a card that would run in a repo's MAIN TREE. This is the gate —
    # reported first and loudly, and any occurrence makes the command exit
    # non-zero so a script cannot dispatch a main-tree card silently.
    critical = kanban.main_tree_cards(cards, projects)
    if critical:
        word = "card" if len(critical) == 1 else "cards"
        print(
            f"\nCRITICAL: {len(critical)} {word} would run in a repo's MAIN TREE "
            f"(workspace must be under <repo>/.worktrees/, not the repo root):"
        )
        for v in critical:
            proj = v.get("project") or "?"
            print(
                f"    [{v.get('id')}] {v.get('title')} — "
                f"workspace {v.get('path')} (project {proj})"
            )
        return 1

    if advisory:
        print(f"\n{advisory} card(s) have advisory findings (non-fatal).")
    return 0


def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "lint-cards",
        help="flag cards missing VERIFY / acceptance / concrete references; "
        "exit non-zero if any card would run in a repo MAIN TREE (read-only)",
        epilog="example: flightdeck lint-cards",
    )
    p.add_argument("board", nargs="?", default=None, help="board slug (default: all boards)")
    p.add_argument(
        "--repo-root",
        default=None,
        help="root to resolve referenced .py modules against (default: cwd)",
    )
    p.set_defaults(func=cmd_lint)


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py: read cards for the board and lint them.

    Attaches the injectable line-count source, repo root and project list to
    ``args`` so ``cmd_lint`` (and tests calling it directly) share one path.
    """
    args.repo_root = getattr(args, "repo_root", None) or os.getcwd()
    args.module_line_counts = getattr(args, "module_line_counts", None)
    if getattr(args, "projects", None) is None:
        from ..core import registry

        args.projects = registry.load_registry(registry_path)

    try:
        cards = kanban.list_cards(board=getattr(args, "board", None))
    except kanban.KanbanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return cmd_lint(args, cards)
