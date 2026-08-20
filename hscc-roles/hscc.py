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


def _base_identity():
    with open(rolelib.BASE_IDENTITY_PATH) as f:
        return f.read()


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
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 1 if failures else 0


def cmd_create(argv):
    if len(argv) < 2:
        print("Usage: hscc-roles create <name> <description...>")
        return 1
    name = argv[0]
    desc = " ".join(argv[1:])
    path = author.create_role(name, desc)
    print({"created": name, "spec": path,
           "next": "run `hscc-roles generate` to build the profile"})
    return 0


def cmd_list():
    rows = []
    for spec_file in rolelib.list_spec_files():
        spec = rolelib.load_spec(spec_file)
        pdir = os.path.join(rolelib.PROFILES_DIR, spec["name"])
        rows.append({"role": spec["name"],
                     "profile_exists": os.path.isdir(pdir)})
    print({"roles": rows})
    return 0


def cmd_validate():
    errs = []
    for spec_file in rolelib.list_spec_files():
        try:
            rolelib.load_spec(spec_file)
        except ValueError as e:
            errs.append(str(e))
    print({"ok": not errs, "errors": errs})
    return 1 if errs else 0


def cmd_autonomy(argv):
    if argv:
        autonomy.set_state(argv[0])
    print({"autonomy": "on" if autonomy.is_on() else "off"})
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
            print("Usage: hscc.py orch <project|general> [--registry PATH]")
            return 1
        registry = rest[i + 1]
        del rest[i:i + 2]
    if len(rest) != 1:
        print("Usage: hscc.py orch <project|general> [--registry PATH]")
        return 1
    project = rest[0] or None
    base = _base_identity()
    try:
        result = orchestrators.ensure_orchestrator(project, base_identity=base,
                                                    path=registry)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except orchestrators.OrchestratorError as e:
        print({"error": str(e)})
        return 1


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
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
    print(f"Unknown command: {cmd}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
