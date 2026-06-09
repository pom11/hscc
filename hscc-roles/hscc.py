#!/usr/bin/env python3
"""HSCC Roles — author + generate role-specialized Hermes profiles.

Usage: hscc-roles <command> [args]

Commands:
  generate                 Build/refresh all role profiles from roles/*.yaml
  create <name> <desc...>  Author a new role spec from a description
  list                     List role specs and whether their profile exists
  validate                 Validate every role spec (load + required fields)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rolelib
import generator
import author


def _base_identity():
    with open(rolelib.BASE_IDENTITY_PATH) as f:
        return f.read()


def cmd_generate():
    base = _base_identity()
    out = []
    for spec_file in rolelib.list_spec_files():
        spec = rolelib.load_spec(spec_file)
        changed = generator.generate_profile(spec, base)
        out.append({"role": spec["name"], "changed": changed})
    print({"generated": out})
    return 0


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
    print(f"Unknown command: {cmd}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
