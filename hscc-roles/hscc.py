#!/usr/bin/env python3
"""HSCC Roles — author + generate role-specialized Hermes profiles.

Usage: hscc-roles <command> [args]

Commands:
  generate                 Build/refresh all role profiles from roles/*.yaml
  create <name> <desc...>  Author a new role spec from a description
  list                     List role specs and whether their profile exists
  validate                 Validate every role spec (load + required fields)
  autonomy [on|off]        Show or set the fleet autonomy flag
  orch <project|general>   Ensure a project's orchestrator profile exists
                           (idempotent) and print its resolved identity
  orch-all [--registry P]  Ensure an orchestrator for EVERY registry project
                           plus the `general` catch-all (idempotent, bulk)
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rolelib
import generator
import author
import autonomy
import orchestrators
import _theme
from _theme import escape, make_console, panel, status_panel, table


def _base_identity():
    with open(rolelib.BASE_IDENTITY_PATH) as f:
        return f.read()


def _usage(msg: str):
    """Print a usage/error line to stderr via the themed console (plain on a
    pipe). Human-facing only — never used on a machine (--json) path."""
    make_console(file=sys.stderr).print(f"[error]{escape(msg)}[/error]")


def cmd_generate():
    base = _base_identity()
    out = []
    failures = []
    for spec_file in rolelib.list_spec_files():
        try:
            spec = rolelib.load_spec(spec_file)
            changed = generator.generate_profile(spec, base)
            out.append({"role": spec["name"], "changed": changed})
        except Exception as e:
            failures.append({"file": os.path.basename(spec_file), "error": str(e)})
    result = {"generated": out}
    if failures:
        result["failures"] = failures
    # --json byte-identity: the machine payload stays RAW print(json.dumps),
    # never routed through a Console (no ANSI / no reformat) — bootstrap and
    # scripts capture + json.load() this.
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 1 if failures else 0


def cmd_create(argv):
    if len(argv) < 2:
        _usage("Usage: hscc-roles create <name> <description...>")
        return 1
    name = argv[0]
    desc = " ".join(argv[1:])
    path = author.create_role(name, desc)
    make_console().print(panel(
        "hscc-roles create",
        f"[ok]created[/ok] role [accent]{escape(name)}[/accent]\n"
        f"[label]spec:[/label] {escape(path)}\n"
        f"[dim]next: run `hscc-roles generate` to build the profile[/dim]",
    ))
    return 0


def cmd_list():
    rows = []
    for spec_file in rolelib.list_spec_files():
        spec = rolelib.load_spec(spec_file)
        pdir = os.path.join(rolelib.PROFILES_DIR, spec["name"])
        rows.append({"role": spec["name"],
                     "profile_exists": os.path.isdir(pdir)})
    tb = table("hscc-roles — role specs")
    tb.add_column("role", style="accent")
    tb.add_column("profile")
    for r in rows:
        tb.add_row(escape(r["role"]), "yes" if r["profile_exists"] else "no")
    make_console().print(tb)
    return 0


def cmd_validate():
    errs = []
    for spec_file in rolelib.list_spec_files():
        try:
            rolelib.load_spec(spec_file)
        except ValueError as e:
            errs.append(str(e))
    if errs:
        make_console().print(status_panel(
            "\n".join(escape(e) for e in errs), "error", title="validate"))
    else:
        make_console().print(status_panel(
            "all role specs valid", "ok", title="validate"))
    return 1 if errs else 0


def cmd_autonomy(argv):
    if argv:
        autonomy.set_state(argv[0])
    state = "on" if autonomy.is_on() else "off"
    make_console().print(panel(
        "autonomy",
        f"fleet autonomy is [accent]{state}[/accent]",
    ))
    return 0


def cmd_orch(argv):
    """Ensure + report a project's orchestrator profile (single project).

    Usage: hscc.py orch <project|general> [--registry PATH]
    Idempotent — re-running never clobbers an existing profile's memory/sessions.
    """
    registry = None
    rest = list(argv)
    if "--registry" in rest:
        i = rest.index("--registry")
        if i + 1 >= len(rest):
            _usage("Usage: hscc.py orch <project|general> [--registry PATH]")
            return 1
        registry = rest[i + 1]
        del rest[i:i + 2]
    if len(rest) != 1:
        _usage("Usage: hscc.py orch <project|general> [--registry PATH]")
        return 1
    project = rest[0] or None
    base = _base_identity()
    try:
        result = orchestrators.ensure_orchestrator(project, base_identity=base,
                                                    path=registry)
        # --json byte-identity: raw print(json.dumps), never themed.
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except orchestrators.OrchestratorError as e:
        make_console().print(status_panel(
            escape(str(e)), "error", title="orch"))
        return 1


def cmd_orch_all(argv):
    """Ensure an orchestrator for EVERY registry project + the `general` catch-all.

    Usage: hscc.py orch-all [--registry PATH]

    Idempotent — re-running never clobbers an existing profile's memory/sessions
    (``ensure_orchestrator`` guarantees this). One invocation covers the whole
    registry so bootstrap can provision the full per-project orchestrator fleet
    without enumerating projects itself.

    stdout is PURE JSON (callers may capture + ``json.load()`` it): per-profile
    results (profile/session/board/changed) plus any failures. Warnings for a
    missing/unreadable registry go to stderr, not stdout. A failure on one
    project never aborts the rest — every project is attempted, failures are
    collected, and the exit code is non-zero iff at least one failed.
    """
    registry = None
    rest = list(argv)
    if "--registry" in rest:
        i = rest.index("--registry")
        if i + 1 >= len(rest):
            _usage("Usage: hscc.py orch-all [--registry PATH]")
            return 1
        registry = rest[i + 1]
        del rest[i:i + 2]

    base = _base_identity()

    # Which projects to provision: every registry project plus `general`.
    # A missing/unreadable registry yields an empty list — we still ensure
    # `general` (the project-less default must work on a bare machine) and
    # warn rather than dying. Warnings go to stderr via the themed console
    # (plain on a pipe), never stdout — stdout stays pure JSON.
    try:
        projects = orchestrators.list_registry_projects(registry)
    except Exception as e:  # defensive: never abort the whole command on registry
        projects = []
        make_console(file=sys.stderr).print(
            f"[warn]{escape(f'warn: registry unreadable; ensuring only `general`: {e}')}[/warn]")
    if not projects:
        make_console(file=sys.stderr).print(
            "[warn]warn: no registry projects found; ensuring only `general`[/warn]")

    targets = projects + ["general"]
    ensured = []
    failures = []
    for project in targets:
        try:
            result = orchestrators.ensure_orchestrator(
                project, base_identity=base, path=registry)
            ensured.append({
                "project": result["project"],
                "profile": result["profile"],
                "session": result["session"],
                "board": result["board"],
                "changed": result["changed"],
            })
        except orchestrators.OrchestratorError as e:
            failures.append({"project": project, "error": str(e)})
        except Exception as e:  # non-orchestrator errors also isolated per project
            failures.append({"project": project, "error": f"{type(e).__name__}: {e}"})

    report = {"requested": targets, "ensured": ensured}
    if failures:
        report["failures"] = failures
    # --json byte-identity: raw print(json.dumps), never themed.
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if failures else 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        make_console().print(panel("hscc-roles — usage", str(__doc__)))
        return 0
    cmd = sys.argv[1]
    if cmd == "generate":
        return cmd_generate()
    if cmd == "create":
        return cmd_create(sys.argv[2:])
    if cmd == "list":
        return cmd_list()
    if cmd == "validate":
        return cmd_validate()
    if cmd == "autonomy":
        return cmd_autonomy(sys.argv[2:])
    if cmd == "orch":
        return cmd_orch(sys.argv[2:])
    if cmd == "orch-all":
        return cmd_orch_all(sys.argv[2:])
    make_console(file=sys.stderr).print(
        f"[error]Unknown command: {escape(cmd)}[/error]")
    make_console().print(panel("hscc-roles — usage", str(__doc__)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
