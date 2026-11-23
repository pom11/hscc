"""LEFT-4 coverage gates: dispatch <-> COMMAND_HELP and BUNDLED_SKILLS <-> disk.

Card ``t_c13de3cc`` (audit ``docs/audits/2026-11-23-agent-surface.md`` §11
LEFT-4). Two cheap gates, one module:

* **Gate 1** — every dispatched CLI verb is advertised in ``COMMAND_HELP``.
  This is checked at both levels the CLI actually has: top-level verbs parsed
  out of ``main()``'s own ``cmd``/``cmd_lower`` branches (plus the membership
  tables those branches dispatch through), and each group's leaf verbs read
  from that group's own dispatcher/registry. The audit's F3 is the second
  level: ``hscc help kanban`` listed 3 of the 7 verbs ``hscc kanban``
  dispatches. A top-level-only gate would never have caught it, so the group
  walk is the point of this test, not decoration.

  Exemptions are EXPLICIT and NAMED, never silent skips: ``unit-emit`` is the
  machine-only unit-file writer deliberately absent from every help surface
  (t_d8f13693), and any not-yet-merged sibling is named with its card id in
  ``GROUP_HELP_EXEMPTIONS`` (the audit's LEFT-4 hand-off rule: the gate ships
  green or the sibling is named TODO).

* **Gate 2** — ``BUNDLED_SKILLS`` and ``install/hscc-skills/`` agree in BOTH
  directions. The audit's F12 was exactly the two missing halves: a directory
  on disk with no list entry (``hscc-lessons``) is never installed, and a list
  entry with no directory is an install that fails or ships nothing.

Parses or imports only. Nothing here writes ``~/.hscc``, ``~/.hermes``, the
fleet, launchd or the network; the dispatcher registries imported are constants.

Leg choice: ``hscc_daemon/tests/``. Gate 1 imports ``hscc_daemon.hscc`` and the
group modules (this is that leg's package), and gate 2 needs only the repo
path to parse the hyphenated ``hscc-skills/hscc.py`` — the same
parse-never-import pattern ``test_skill_tool_coverage_t_cb167686.py`` uses.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HSCC_PY = REPO / "hscc_daemon" / "hscc.py"
CARDS_PY = REPO / "hscc_daemon" / "cards_live.py"
CLUSTER_PY = REPO / "hscc-cluster" / "hscc.py"
SKILLS_LIST = REPO / "hscc-skills" / "hscc.py"
SKILLS_DISK = REPO / "install" / "hscc-skills"

#: Top-level verbs that legitimately have no COMMAND_HELP entry. Every entry
#: must name a verb the dispatcher resolves and carry its reason (asserted).
TOP_LEVEL_EXEMPTIONS = {
    "unit-emit": (
        "internal machine-readable unit-file writer (t_d8f13693); deliberately "
        "absent from DAEMON_COMMANDS, COMMAND_HELP and every help surface"
    ),
}

#: Group/leaf pairs allowed to be missing from their group's COMMAND_HELP
#: entry. Empty today: F3 (t_46d03105) landed the kanban verbs, and this gate
#: found + fixed the remaining cluster/api gaps. Keep the mechanism so a future
#: not-yet-merged sibling is named here with its card id, never skipped
#: silently (audit LEFT-4 hand-off rule).
GROUP_HELP_EXEMPTIONS: dict[tuple[str, str], str] = {}

#: Skill directories that may sit on disk without a BUNDLED_SKILLS entry.
#: Empty on purpose: every current directory is a skill bootstrap must own.
SKILL_EXEMPTIONS: dict[str, str] = {}


# ── parsing helpers ────────────────────────────────────────────────────────

def _literal_strings(node: ast.AST) -> set[str]:
    """String constants in a literal (constant, tuple/list/set of constants)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        out: set[str] = set()
        for elt in node.elts:
            out |= _literal_strings(elt)
        return out
    return set()


def _variable_under_test(node: ast.AST) -> str | None:
    """The bare variable name a compare is about: ``cmd``, ``cmd_lower``,
    ``cmd.lower()``, ``argv[0]`` or ``sub``."""
    if isinstance(node, ast.Name):
        return node.id
    if (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "lower"
            and isinstance(node.func.value, ast.Name)
            and not node.args):
        return node.func.value.id
    if (isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == 0):
        return node.value.id
    return None


def _function(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.relative_to(REPO)} has no function {name!r} — "
                         "the dispatcher moved and this gate must move with it")


def _local_literal_sets(fn: ast.FunctionDef) -> dict[str, set[str]]:
    """``advanced_cmds = {"start-daemon", "advanced"}`` and friends."""
    out: dict[str, set[str]] = {}
    for node in ast.walk(fn):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            values = _literal_strings(node.value)
            if not values:
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    out[target.id] = values
    return out


def _branches_on(fn: ast.FunctionDef, variables: tuple[str, ...],
                 *, keep_help_tokens: bool = False) -> set[str]:
    """String constants a function compares ``variables`` against.

    Handles the dispatcher's real shapes: ``cmd == "x"`` / ``cmd_lower == "x"``
    (either side), ``cmd_lower in ("x", "y")``, ``argv[0] != "monitor"`` and
    ``sub in ("enable", "disable")``. Literal names bound in a local set
    (``advanced_cmds``) are resolved by the caller for ``main()``; for the
    group walk the branches are the registry.
    """
    out: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Compare):
            continue
        for op, comparator in zip(node.ops, node.comparators):
            if isinstance(op, (ast.Eq, ast.NotEq)):
                if _variable_under_test(node.left) in variables:
                    out |= _literal_strings(comparator)
                if _variable_under_test(comparator) in variables:
                    out |= _literal_strings(node.left)
            elif isinstance(op, ast.In):
                if _variable_under_test(node.left) in variables:
                    out |= _literal_strings(comparator)
    if not keep_help_tokens:
        out = {v for v in out if not v.startswith("-") and v != "help"}
    return out


def _top_level_dispatched_verbs() -> set[str]:
    """Every top-level verb ``main()`` resolves, from its own source."""
    import hscc_daemon.hscc as unified

    main = _function(HSCC_PY, "main")
    local_sets = _local_literal_sets(main)
    verbs = _branches_on(main, ("cmd", "cmd_lower"), keep_help_tokens=True)

    # `cmd.lower() in DAEMON_COMMANDS` / `in advanced_cmds`: the comparison's
    # right side is a TABLE, not a literal. Resolve both without duplicating
    # either list here.
    for node in ast.walk(main):
        if not isinstance(node, ast.Compare):
            continue
        for op, comparator in zip(node.ops, node.comparators):
            if not isinstance(op, ast.In):
                continue
            if _variable_under_test(node.left) not in ("cmd", "cmd_lower"):
                continue
            if isinstance(comparator, ast.Name):
                if comparator.id == "DAEMON_COMMANDS":
                    verbs |= set(unified.DAEMON_COMMANDS)
                else:
                    verbs |= local_sets.get(comparator.id, set())
            else:
                verbs |= _literal_strings(comparator)
    return verbs


def _group_dispatch_verbs() -> dict[str, set[str]]:
    """Every leaf verb each group dispatcher accepts, from its own registry."""
    import hscc_daemon.hscc as unified
    from hscc_daemon import (api_cli, autodown_cli, kanban_blocked,
                             kanban_cli, main_cli, merge_queue_cli)

    return {
        # Two tables: the dispatched set and the per-verb help keys. Union them
        # so a verb added to either surface is walked.
        "cluster": set(unified.CLUSTER_SUBCOMMANDS)
                   | set(unified._CLUSTER_SUBCOMMAND_HELP),
        "template": set(unified.TEMPLATE_SUBCOMMANDS),
        "node": set(unified._NODE_SUBCOMMAND_HELP),
        "api": set(api_cli.VALID_SUBCOMMANDS),
        "autodown": set(autodown_cli.VALID_SUBCOMMANDS),
        # kanban has two owners; blocked lives in kanban_blocked's help table.
        "kanban": set(kanban_cli.VALID_SUBCOMMANDS)
                  | set(kanban_blocked.SUBCOMMAND_HELP),
        "main": set(main_cli.VALID_SUBCOMMANDS),
        "merge-queue": set(merge_queue_cli.SUBCOMMAND_HELP),
        # No verb constant in these two: read the dispatch branches directly.
        "cards": _branches_on(_function(CARDS_PY, "cmd_cards"), ("argv",)),
        "orch": _branches_on(_function(CLUSTER_PY, "cmd_orch"), ("argv",)),
    }


def _mentions(text: str, verb: str) -> bool:
    """True when *verb* appears as a whole token (hyphens included)."""
    return re.search(rf"(?<![A-Za-z0-9_-]){re.escape(verb)}(?![A-Za-z0-9_-])",
                     text) is not None


def _bundled_skill_names() -> list[str]:
    tree = ast.parse(SKILLS_LIST.read_text(encoding="utf-8"),
                     filename=str(SKILLS_LIST))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(t, ast.Name) and t.id == "BUNDLED_SKILLS"
               for t in node.targets):
            assert isinstance(node.value, (ast.List, ast.Tuple)), (
                "BUNDLED_SKILLS is no longer a literal list — this gate must "
                "change shape with it rather than pass vacuously")
            return [e.value for e in node.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    raise AssertionError("BUNDLED_SKILLS not found in "
                         f"{SKILLS_LIST.relative_to(REPO)}")


# ── gate 1: dispatch -> COMMAND_HELP ───────────────────────────────────────

def test_every_dispatched_top_level_verb_is_in_command_help():
    import hscc_daemon.hscc as unified

    verbs = _top_level_dispatched_verbs()
    # Parse floor: the AST walker must see the four branch shapes it handles
    # and the two membership tables, not merely a non-empty set.
    assert {"help", "start", "cluster", "kanban", "merge-queue", "node",
            "heal-clear", "start-daemon", "unit-emit"} <= verbs, (
        f"dispatcher parse missed known verbs; parsed {sorted(verbs)}")
    assert set(TOP_LEVEL_EXEMPTIONS) <= verbs, (
        "an exemption names a verb the dispatcher no longer resolves — delete "
        f"the stale entry: {sorted(set(TOP_LEVEL_EXEMPTIONS) - verbs)}")

    missing = sorted(verbs - set(unified.COMMAND_HELP)
                     - set(TOP_LEVEL_EXEMPTIONS))
    assert not missing, (
        f"dispatched verbs with no COMMAND_HELP entry: {missing} — "
        "`hscc help <verb>` would exit 1 on a verb the CLI runs. Add the "
        "entry; do not add a silent exemption (name it with a reason).")


def test_every_group_subcommand_is_advertised_in_its_command_help():
    import hscc_daemon.hscc as unified

    groups = _group_dispatch_verbs()
    # Floors for the two AST-derived groups so a parse change cannot make the
    # gate vacuous.
    assert groups["cards"] == {"monitor"}, groups["cards"]
    assert {"inject", "waker"} <= groups["orch"], groups["orch"]

    problems: list[str] = []
    for group, verbs in sorted(groups.items()):
        assert verbs, f"no subcommands parsed for group {group!r}"
        assert group in unified.COMMAND_HELP, (
            f"group {group!r} dispatches subcommands but has no COMMAND_HELP")
        text = unified.COMMAND_HELP[group]
        for verb in sorted(verbs):
            if _mentions(text, verb):
                continue
            if (group, verb) in GROUP_HELP_EXEMPTIONS:
                continue
            problems.append(f"hscc {group} {verb}")

    for key in GROUP_HELP_EXEMPTIONS:
        group, verb = key
        # An exemption must name a real dispatched verb, or it is a hole with
        # a misleading comment. A sibling landing the verb turns the entry into
        # a no-op; that is deliberately NOT a failure, so this gate is green
        # whichever of the two PRs merges first.
        assert group in groups and verb in groups[group], (
            f"GROUP_HELP_EXEMPTIONS names a verb that is not dispatched: "
            f"hscc {group} {verb}")

    assert not problems, (
        "dispatched group verbs missing from COMMAND_HELP: "
        + ", ".join(problems)
        + " — `hscc help <group>` hides a verb the group runs (audit F3). "
        "Add it to the group's entry, or name an explicit TODO exemption with "
        "the sibling card id.")


def test_main_wakeups_leaf_verbs_are_advertised():
    """The four ``hscc main wakeups`` leaves are dispatched too (audit §1)."""
    import hscc_daemon.hscc as unified

    fn = _function(REPO / "hscc_daemon" / "main_cli.py", "cmd_wakeups")
    verbs = _branches_on(fn, ("sub",))
    assert {"list", "set", "enable", "disable"} <= verbs, verbs
    text = unified.COMMAND_HELP["main"]
    missing = sorted(v for v in verbs if not _mentions(text, v))
    assert not missing, (
        f"hscc main wakeups verbs missing from COMMAND_HELP['main']: {missing}")


# ── gate 2: BUNDLED_SKILLS <-> install/hscc-skills, both directions ────────

def test_every_bundled_skill_name_exists_on_disk():
    names = _bundled_skill_names()
    assert names, "BUNDLED_SKILLS parsed empty — the gate would pass vacuously"
    assert {"hscc", "hscc-cluster", "devops"} <= set(names), names

    missing_dirs = [n for n in names if not (SKILLS_DISK / n).is_dir()]
    assert not missing_dirs, (
        f"BUNDLED_SKILLS lists skills with no directory under "
        f"install/hscc-skills/: {sorted(set(missing_dirs))} — bootstrap "
        "promises to install a skill the repo does not carry")

    missing_skill = [n for n in names
                     if not any((SKILLS_DISK / n).rglob("SKILL.md"))]
    assert not missing_skill, (
        f"bundled skill directories with no SKILL.md anywhere beneath them: "
        f"{missing_skill} — bootstrap would install an empty skill")


def test_every_skill_directory_is_bundled():
    names = set(_bundled_skill_names())
    on_disk = sorted(p.name for p in SKILLS_DISK.iterdir()
                     if p.is_dir() and not p.name.startswith(".")
                     and p.name != "__pycache__")
    unlisted = sorted(set(on_disk) - names - set(SKILL_EXEMPTIONS))
    assert not unlisted, (
        f"directories under install/hscc-skills/ absent from BUNDLED_SKILLS: "
        f"{unlisted} — no bootstrap or install-skills run ever copies them "
        "(audit F12). Add the name to the list; do not leave the tree "
        "orphaned.")

    stale = sorted(set(SKILL_EXEMPTIONS) - set(on_disk))
    assert not stale, (
        f"skill exemptions name directories that no longer exist: {stale} — "
        "remove the stale exemption")


def test_bundled_skills_are_unique_and_directly_named():
    names = _bundled_skill_names()
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"BUNDLED_SKILLS has duplicate names: {dupes}"
    bad = sorted(n for n in names if "/" in n or n in ("", ".", ".."))
    assert not bad, f"BUNDLED_SKILLS entries are not plain directory names: {bad}"
