"""incident.py — `flightdeck incident` : append a dated lesson to docs/INCIDENTS.md.

Hard-won lessons die when they live only in one operator's memory. This command
records an incident as a dated, structured entry in the TARGET project's
``docs/INCIDENTS.md``, so the lesson lives in the repo beside the code it warns
about — visible in code review, searchable, never lost to a restart or a
handover.

    flightdeck incident "<what happened>" [--project P] [--fix "..."] \\
        [--cause "..."] [--lesson "..."] [--apply]

The entry is rendered to stdout ALWAYS. ``--apply`` is required to actually
write it; without it nothing on disk changes (the same dry-run-by-default
contract as reconcile / roadmap).

The file is a NEWEST-FIRST log: a new entry is inserted at the TOP, right after
the header, so the most recent lesson greets the next reader while older
entries stay exactly where they are. Existing entries are ALWAYS preserved
byte-for-byte — this command only ever inserts a new block; it never rewrites
or reformats a prior entry. If the file does not exist it is created with a
header.

Entry field contract (every entry carries all five):

    ## YYYY-MM-DD — <one-line symptom>
    **Project:** <project name>
    **Symptom:** what was observed, with the concrete evidence
    **Cause:** why it happened
    **Fix:** what actually resolved it
    **Lesson:** the general rule, stated so it applies next time

The first positional argument is the symptom: its first line becomes the
``##`` heading and the full text fills the **Symptom** field. ``--fix``,
``--cause`` and ``--lesson`` supply the remaining fields (each optional,
rendered empty when omitted). ``--project`` selects which registered project's
repo is written to; it defaults to ``flightdeck`` (this repo). An unknown
project is an error that lists the known ones.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date

from ..core import registry

# The entry each incident writes to under the project's repo.
_INCIDENTS_RELPATH = os.path.join("docs", "INCIDENTS.md")

# The project written to when --project is not given. flightdeck records its own
# lessons here too.
_DEFAULT_PROJECT = "flightdeck"

# File header, written ONLY when the file does not exist yet. Existing files are
# never rewritten, so this never clobbers a hand-written header.
_HEADER = (
    "# Incidents\n"
    "\n"
    "Lessons from real incidents, newest first. Append a lesson with "
    "`flightdeck incident \"<what happened>\" --fix \"...\" --apply`.\n"
    "\n"
)


def _today() -> str:
    """Today's date as ``YYYY-MM-DD`` (injectable in tests)."""
    return date.today().isoformat()


def _render_entry(incident: dict) -> str:
    """Render one entry as the canonical multi-line block (no trailing blank)."""
    return "\n".join(
        [
            f"## {incident['date']} — {incident['heading']}",
            f"**Project:** {incident['project']}",
            f"**Symptom:** {incident['symptom']}",
            f"**Cause:** {incident['cause']}",
            f"**Fix:** {incident['fix']}",
            f"**Lesson:** {incident['lesson']}",
        ]
    )


def _file_with_entry(text: str, entry: str) -> str:
    """Insert ``entry`` at the TOP of an existing incidents file, newest-first.

    The first ``## `` line starts the first existing entry; everything up to it
    (the header block) stays untouched, the new entry goes right after it, and
    a single blank line separates the new entry from the pre-existing ones.
    Existing lines are only ever copied through — never modified — so they are
    preserved byte-for-byte. A file with no ``## `` block yet just gets the
    entry appended after whatever header it already has.
    """
    lines = text.split("\n")
    first_entry = next(
        (i for i, line in enumerate(lines) if line.startswith("## ")),
        len(lines),
    )
    new_lines = lines[:first_entry] + entry.split("\n") + [""] + lines[first_entry:]
    return "\n".join(new_lines)


def _new_file(entry: str) -> str:
    """The full contents for a brand-new incidents file: header + one entry."""
    return _HEADER + entry + "\n"


# --------------------------------------------------------------------------- #
# injectable file access (tests stub these; production uses the real fs)
# --------------------------------------------------------------------------- #


def _read_file(path: str):
    """The file's text, or None when absent/unreadable (the \"no file\" marker)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, IOError, UnicodeDecodeError):
        return None


def _write_file(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# --------------------------------------------------------------------------- #
# presentation / dispatch
# --------------------------------------------------------------------------- #


def _first_line(text: str) -> str:
    """The symptom's first line, trimmed — used as the one-line heading."""
    first = text.splitlines()[0].strip() if text else ""
    return first


def _resolve_project(projects: list[registry.Project], name: str | None):
    """Resolve ``--project`` (default ``flightdeck``) or error with the knowns.

    Returns the Project, or None after printing an error that lists the known
    projects — an unknown project is never silently guessed, matching the
    ``ask``/``roadmap`` contract.
    """
    if name is None:
        name = _DEFAULT_PROJECT
    for proj in projects:
        if proj.name == name:
            return proj
    known = ", ".join(sorted(p.name for p in projects)) or "(none registered)"
    print(
        f"error: no project named {name!r} in the registry. Known projects: {known}",
        file=sys.stderr,
    )
    return None


def cmd_incident(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    project = _resolve_project(projects, getattr(args, "project", None))
    if project is None:
        return 2

    incident = {
        "date": _today(),
        "heading": _first_line(args.symptom),
        "project": project.name,
        "symptom": (args.symptom or "").strip(),
        "cause": (getattr(args, "cause", None) or "").strip(),
        "fix": (getattr(args, "fix", None) or "").strip(),
        "lesson": (getattr(args, "lesson", None) or "").strip(),
    }
    entry = _render_entry(incident)
    path = os.path.join(project.repo, _INCIDENTS_RELPATH)

    existing = _read_file(path)
    if existing is None:
        contents = _new_file(entry)
        state = "create"
    else:
        contents = _file_with_entry(existing, entry)
        state = "append"

    # The rendered entry is ALWAYS shown, so the operator reviews exactly what
    # --apply would write, even before choosing to write it.
    print(entry)
    if not args.apply:
        print(
            f"\ndry-run: would {state} {path}; pass --apply to write.",
            file=sys.stderr,
        )
        return 0

    _write_file(path, contents)
    print(f"wrote {state} {path}", file=sys.stderr)
    return 0


def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "incident",
        help="append a dated lesson to docs/INCIDENTS.md (newest first)",
        epilog='example: flightdeck incident "topic not mapped" --fix "ran topics bind" --apply',
    )
    p.add_argument(
        "symptom",
        help="what happened: first line becomes the heading, full text the Symptom field",
    )
    p.add_argument(
        "--project",
        default=None,
        help=f"project in the registry whose repo to write (default: {_DEFAULT_PROJECT})",
    )
    p.add_argument("--fix", default=None, help="what actually resolved it")
    p.add_argument("--cause", default=None, help="why it happened")
    p.add_argument(
        "--lesson",
        default=None,
        help="the general rule, stated so it applies next time",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="write the entry (dry-run by default; never rewrites existing entries)",
    )
    p.set_defaults(func=cmd_incident)
    return p


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py: record an incident for the resolved project's repo.

    File access goes through the module-level ``_read_file`` / ``_write_file``,
    which tests can monkeypatch — or, more simply, point the project's ``repo``
    at a tmp_path so nothing here touches a live repo.
    """
    args.registry = registry_path
    projects = registry.load_registry(registry_path)
    return cmd_incident(args, projects)
