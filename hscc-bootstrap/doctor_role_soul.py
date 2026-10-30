"""Doctor coverage for role-SOUL identity drift (t_c823ebc9).

One check, answering the question the spec-existence test cannot: does each
live HSCC profile SOUL actually carry ITS OWN role identity, or the identity of
whichever profile it was copied from? Measured instance: `ecofire-pontaje-engineer`
ran with `## Role: ecofire-bc-engineer` for its whole life while its spec existed
and the invariant test was green.

This file is a BRIDGE, not a second implementation. The check logic — the
profile sweep, the emittable-heading derivation from base-identity.md and
generator's three ops blocks — lives in ``hscc-roles/role_soul_check.py``, the
module that owns ``rolelib.PROFILES_DIR``, ``rolelib.ROLES_DIR`` and
``compose_soul``. Re-deriving any of that here would let doctor and the
generator disagree about what a valid SOUL is, which is exactly the drift class
this check hunts.

Why the bridge exists at all: each HSCC plugin deploys standalone into
``~/.hermes/plugins`` and the hyphenated dirs are not importable package names,
so ``hscc-bootstrap`` cannot ``import role_soul_check``. The resolution below is
the same sibling-dir pattern ``doctor_node_recovery.py`` uses for the
``hscc-cluster`` modules — APPENDED to sys.path, never prepended, so bootstrap's
own ``_theme``/``doctor`` stay authoritative.

Honest signal: when the sibling plugin is absent from the layout the check
comes back as an ERROR naming what is missing — never as an all-clear and never
as a silent skip. A doctor run that quietly checks nothing is the false
green this rule exists to prevent.

READ-ONLY: the check reads SOUL.md files and nothing else. The repair
(``hscc.py generate``) rewrites live profile state and is an operator action.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

#: Name used when the hscc-roles sibling is NOT importable (the only case where
#: this literal is ever printed). When the sibling IS importable the name comes
#: from role_soul_check.CHECK_NAME — see :func:`check_name`. The one-line test
#: in test_doctor_role_soul.py pins the two equal so this fallback cannot drift.
CHECK_NAME = "role SOUL identity (## Role: header vs profile dir)"


def check_name() -> str:
    """The check's display name, single-sourced from role_soul_check when the
    sibling plugin is importable, else the fallback literal above.

    The name matters: doctor's checklist is keyed by it (tests and any
    downstream parser look checks up by name), so a name that drifts between the
    bridge and the check module would silently break lookups.
    """
    rsc = _module()
    return rsc.CHECK_NAME if rsc is not None else CHECK_NAME


def _roles_dir() -> str:
    """The sibling hscc-roles dir, APPENDED to sys.path when needed.

    Appended, never prepended: hscc-bootstrap has its own ``_theme.py`` and so
    does hscc-roles, and a prepend would let a later ``import _theme`` on the
    roles leg silently pick up the roles plugin's copy. Appending keeps every
    same-named bootstrap module authoritative while still resolving
    ``role_soul_check`` / ``rolelib`` / ``generator``, which no bootstrap module
    shadows.
    """
    roles_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             os.pardir, "hscc-roles")
    roles_dir = os.path.abspath(roles_dir)
    if roles_dir not in sys.path:
        sys.path.append(roles_dir)
    return roles_dir


def _module():
    """The check module (role_soul_check), or None when the layout lacks it."""
    _roles_dir()
    try:
        import role_soul_check  # noqa: PLC0415 - path set above
        return role_soul_check
    except ImportError:
        return None


def check(profiles_dir: Optional[str] = None, *, roles_dir: Optional[str] = None,
          base_path: Optional[str] = None) -> Dict[str, Any]:
    """The doctor check as a plain dict (Check-compatible fields).

    Non-fatal: a wrong role identity is a loud WARNING to fix, never a reason to
    hard-stop an install — same posture as the "models served" check. The
    per-profile detail lines name profile, actual header and expected header.
    """
    rsc = _module()
    if rsc is None:
        return {"name": CHECK_NAME, "ok": False, "fatal": False, "rows": [],
                "scanned": 0,
                "detail": ("hscc-roles plugin unavailable next to hscc-bootstrap "
                           "— cannot verify role SOUL identity for any profile"),
                "fix": "Deploy the hscc-roles plugin beside hscc-bootstrap "
                       "(both land under ~/.hermes/plugins), then re-run."}
    return rsc.check(profiles_dir, roles_dir=roles_dir, base_path=base_path)


def main(argv: Optional[List[str]] = None) -> int:
    """Standalone probe: `python3 doctor_role_soul.py [--profiles-dir DIR] [--json]`."""
    import argparse
    import json
    ap = argparse.ArgumentParser(
        prog="doctor_role_soul.py",
        description="Read-only doctor view of role_soul_check: every HSCC "
                    "worker-signature profile SOUL's '## Role:' header vs its dir "
                    "name and spec, plus any heading compose_soul() cannot emit.")
    ap.add_argument("--profiles-dir", default=None,
                    help="profiles root (default: hscc-roles' rolelib.PROFILES_DIR)")
    ap.add_argument("--roles-dir", default=None,
                    help="role spec dir (default: hscc-roles' rolelib.ROLES_DIR)")
    ap.add_argument("--base-identity", default=None,
                    help="base identity markdown (default: rolelib.BASE_IDENTITY_PATH)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    res = check(args.profiles_dir, roles_dir=args.roles_dir,
                base_path=args.base_identity)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        mark = "ok" if res["ok"] else "XX"
        print(f"  {mark} {res['name']}: {res['detail']}")
        if res["fix"]:
            print(f"      -> {res['fix']}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
