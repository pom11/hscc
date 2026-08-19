"""ask.py — prompt templates with auto-filled project context.

One module, ONE top-level subcommand ``ask`` (auto-discovered by cli.py), which
mirrors message.py in the house convention: the operations live under it.

    flightdeck ask <project> <template> [--set key=value ...] [--dry-run]
    flightdeck ask template list
    flightdeck ask template show <name>
    flightdeck ask template edit <name>

``ask`` renders a stored template, fills it with context flightdeck ALREADY
knows about the project (never retyped — project name, repo, branch, HEAD sha,
ROADMAP "Now" items, open + awaiting-review cards, verify command), applies any
``--set`` overrides, and sends the result to that project's topic. All render
and context logic lives in :mod:`flightdeck.core.templates`; the send reuses
:mod:`flightdeck.core.telegram` (the same sender ``message send`` uses — no
second sender is written).

Behavior contract (docs/FEATURES-2.md "P0 — prompt templates"):
- ``ask`` is the interactive path: sending is the default, ``--dry-run``
  prints the rendered text and sends NOTHING.
- An unfilled slot is an ERROR listing what the template expects, and sends
  NOTHING — a message containing a literal ``{{slot}}`` is never sent.
- Unknown template lists the available ones.
- A project with no topic gives the actionable error, never a crash.
- ``template edit`` opens $EDITOR on a template (user-editable store).
- The single-writer Telegram session ("database is locked") surfaces as a clear
  message with a retry hint, never a traceback.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from ..core import registry, telegram, templates
from ..core.telegram import TelegramError, TopicLockedError


def _resolve_topic(projects: list[registry.Project], project_name: str):
    """Return ``(topic_id, None)`` or ``(None, error_string)`` for a project.

    Same contract as commands/message.py so the actionable "no topic" error is
    identical across both commands — an operator never handles a raw topic id.
    """
    for proj in projects:
        if proj.name == project_name:
            if proj.topic is None:
                return (
                    None,
                    f"project {project_name} has no topic; "
                    f"run: flightdeck project repair {project_name}",
                )
            return proj.topic, None
    return None, f"unknown project: {project_name!r} (check `flightdeck projects list`)"


def _get_project(projects: list[registry.Project], project_name: str):
    """Return the Project row, or None if absent."""
    for proj in projects:
        if proj.name == project_name:
            return proj
    return None


def _parse_overrides(pairs: list[str] | None) -> dict:
    """Turn ``--set key=value ...`` into ``{key: value}``.

    A pair without ``=`` is a set-to-empty (the operator explicitly supplied the
    key), matching the ``--set <name>=<value>`` contract. Malformed pairs are
    rejected loudly rather than guessed at.
    """
    overrides: dict = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"--set expects KEY=VALUE, got {pair!r}")
        key, _, value = pair.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"--set expects a non-empty KEY, got {pair!r}")
        overrides[key] = value
    return overrides


def _locked_message(exc: TopicLockedError) -> str:
    return (
        f"error: {exc}\n"
        "hint: another process is probably holding the ~/.hermes-tg session; "
        "wait a moment and retry."
    )


# --------------------------------------------------------------------------- #
# ask — the interactive path (send by default, --dry-run sends nothing)
# --------------------------------------------------------------------------- #

def cmd_ask(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    proj = _get_project(projects, args.project)
    if proj is None:
        print(
            f"error: unknown project: {args.project!r} "
            "(check `flightdeck projects list`)",
            file=sys.stderr,
        )
        return 2
    if proj.topic is None:
        print(
            f"error: project {args.project} has no topic; "
            f"run: flightdeck project repair {args.project}",
            file=sys.stderr,
        )
        return 2

    # The template must exist before we do any rendering — an unknown template
    # lists the available ones, never a partial run.
    try:
        text = templates.show_template(args.template, home=args.templates_home)
    except templates.UnknownTemplateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Parse --set overrides; a malformed key/value is a caller error, not a send
    # that goes out with the wrong text.
    try:
        overrides = _parse_overrides(args.set)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Auto-fill context from the registry + repo. An unfilled slot here raises
    # and sends NOTHING — a literal {{slot}} is never posted.
    context = templates.gather_context(
        proj, _run=args.run, _list_cards=args.list_cards
    )
    try:
        rendered = templates.render_template(text, context, overrides=overrides)
    except templates.UnfilledSlotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(rendered)
        print("\n[dry-run] rendered above; nothing was sent.")
        return 0

    try:
        telegram.send_message(proj.topic, rendered, _client=args.client)
    except TopicLockedError as exc:
        print(_locked_message(exc), file=sys.stderr)
        return 3
    except TelegramError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"sent {args.template!r} template to {args.project} (topic {proj.topic}).")
    return 0


# --------------------------------------------------------------------------- #
# template — manage the user-editable store
# --------------------------------------------------------------------------- #

def cmd_template_list(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    names = templates.list_templates(home=args.templates_home)
    if args.json:
        import json

        print(json.dumps(names))
        return 0
    if not names:
        print("No templates available.")
        return 0
    for n in names:
        print(n)
    return 0


def cmd_template_show(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    try:
        body = templates.show_template(args.name, home=args.templates_home)
    except templates.UnknownTemplateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(body)
    return 0


def _default_editor() -> str:
    return os.environ.get("EDITOR") or "vi"


def cmd_template_edit(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    try:
        path = templates._template_path(args.name, home=args.templates_home)
    except templates.UnknownTemplateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Editing an unknown template is forbidden (list the available ones) unless
    # the user explicitly wants to CREATE it — creating via edit is out of scope
    # for this card, so an unknown name is an error.
    if not path.exists():
        try:
            available = templates.list_templates(home=args.templates_home)
        except Exception:  # pragma: no cover - defensive
            available = []
        print(
            f"error: unknown template: {args.name!r}. Available: {', '.join(available)}",
            file=sys.stderr,
        )
        return 2
    editor = args.editor or _default_editor()
    rc = subprocess.call([editor, str(path)])
    if rc != 0:
        print(f"editor exited with status {rc}.", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------- #
# subparser + entry point
# --------------------------------------------------------------------------- #

def build_subparser(sub: argparse._SubParsersAction) -> None:
    """Build the single top-level ``ask`` parser.

    The render form (``ask <project> <template>``) and the template-manager
    form (``ask template list|show|edit``) both live under this one parser.
    argparse cannot host sibling positionals and a subparser at the same level
    (the positionals greedily swallow ``template list``), so the grammar is
    spelled as ONE variadic positional ``parts`` and disambiguated in
    :func:`run`. The ``--set`` / ``--dry-run`` / ``--editor`` flags are
    parser-wide so they parse cleanly next to the variadic token list.
    """
    p = sub.add_parser(
        "ask",
        help=(
            "render a prompt template filled with project context and send it; "
            "or 'ask template list|show|edit' to manage the template store"
        ),
        epilog="example: flightdeck ask flightdeck standup --set focus=q3 --dry-run",
    )
    p.add_argument(
        "parts",
        nargs="*",
        metavar="ARG",
        help=(
            "<project> <template> to render+send, or "
            "'template list|show <name>|edit <name>' to manage the store"
        ),
    )
    p.add_argument(
        "--set",
        action="append",
        metavar="KEY=VALUE",
        default=None,
        help="override/prefill a template slot (repeatable; render form only)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the rendered text and send NOTHING (render form only)",
    )
    p.add_argument(
        "--editor",
        default=None,
        help="editor to use (default: $EDITOR or vi; 'template edit' only)",
    )


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py: run an ask subcommand with shared injectables.

    Attaches the injectable client / git runner / board reader / templates home
    so core calls are stubbable in tests, then disambiguates the variadic
    ``parts`` into either a template-manager call or a render call, loading
    projects from the registry once if the render path needs them.
    """
    args.registry = registry_path
    args.client = getattr(args, "client", None)
    args.run = getattr(args, "run", None)
    args.list_cards = getattr(args, "list_cards", None)
    args.templates_home = getattr(args, "templates_home", None)

    parts = list(getattr(args, "parts", None) or [])

    # --- template manager: `ask template <verb> ...` ------------------------ #
    if parts and parts[0] == "template":
        if len(parts) < 2:
            print(
                "ask: 'template' needs a verb: list | show <name> | edit <name>.",
                file=sys.stderr,
            )
            return 2
        verb = parts[1]
        if verb == "list" and len(parts) == 2:
            return cmd_template_list(args, [])
        if verb in ("show", "edit") and len(parts) != 3:
            print(
                f"ask: 'template {verb}' needs exactly one <name>. Try: template {verb} <name>.",
                file=sys.stderr,
            )
            return 2
        if verb == "show":
            args.name = parts[2]
            return cmd_template_show(args, [])
        if verb == "edit":
            args.name = parts[2]
            return cmd_template_edit(args, [])
        print(
            f"ask: unknown 'template' verb {verb!r}. "
            "Try: template list | show <name> | edit <name>.",
            file=sys.stderr,
        )
        return 2

    # --- render form: `ask <project> <template>` ----------------------------- #
    if len(parts) != 2:
        # Absorb trailing bare `key=value` tokens into --set so the operator can
        # write `ask <project> <template> goal=x verify=cmd` without repeating
        # --set. `=`, when present, is the marker: a token with one is an
        # override, not a stray positional.
        if len(parts) > 2 and all("=" in t for t in parts[2:]):
            args.set = (args.set or []) + parts[2:]
            parts = parts[:2]
        else:
            print(
                "ask: expected '<project> <template>', or 'template list|show|edit'. "
                "Run `flightdeck ask --help` for usage.",
                file=sys.stderr,
            )
            return 2
    args.project = parts[0]
    args.template = parts[1]
    projects = registry.load_registry(registry_path)
    return cmd_ask(args, projects)
