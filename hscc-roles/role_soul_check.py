"""role_soul_check — read-only audit of live profile SOULs against the specs.

Why this exists (t_c823ebc9)
----------------------------
``test_every_hscc_owned_worker_profile_has_role_spec`` proves a profile has a
``roles/<name>.yaml``. It cannot see the defect that motivated it: a profile
copied from another one keeps the SOURCE's ``## Role:`` header, so
``~/.hermes/profiles/ecofire-pontaje-engineer/SOUL.md`` ran for its whole life
under ``## Role: ecofire-bc-engineer`` while its spec existed and its test was
green. Identity drift is invisible to an existence test.

The same class of drift is the hand-appended block: a live SOUL carried
``## Project override: ecofire-pontaje``, a heading ``compose_soul()`` cannot
produce. Nothing warns that the next ``hscc roles generate`` overwrites it.

This module reports both, and it is a REPORTER, not a pytest assertion. The
card's ruling is explicit: a live-state invariant belongs where a fault is
surfaced to the operator (``hscc doctor``), never as a test that is red on the
operator machine and vacates in CI. Nothing here writes anything; the repair is
an operator action (``hscc roles generate``), and re-stamping every live profile
is not something a check may do on the operator's behalf.

The population (why worker-signature, not "every dir")
------------------------------------------------------
The card's measured sweep counted the profiles whose SOUL carries the worker
signature, so that is the population: a worker is who actually runs under a
role identity. Orchestrator-family SOULs (the cluster-wide ``orchestrator`` and
any ``<project>-orch``) are deliberately EXCLUDED — ``orchestrators.py`` mints
those dynamically and stamps them with the spec name of whatever shared spec it
passes (e.g. ``general-orch`` carries ``## Role: orchestrator``), so a dir-name
comparison would red-flag a layout that is correct by construction. A dir with
no worker signature is reported in the scanned count's summary, not judged.

What "emittable" means (derived, never re-typed)
------------------------------------------------
``compose_soul()`` is the ONLY writer of a role SOUL. Its output is:

    base-identity headings (from rolelib.BASE_IDENTITY_PATH)
    + "## Role: <spec name>"
    + the spec's identity text (which may itself carry "## " headings — a spec
      is a generator INPUT, so anything it emits is emittable by definition)
    + one of generator._WORKER_OPS / _ORCH_OPS / _PROJECT_ORCH_OPS

So a heading is emittable when it is a base heading, a spec-identity heading of
THIS profile's own spec, "## Role: <dir name>", or the ops heading. Anything
else was hand-patched and a regeneration drops it. The base set and the ops set
are READ from the modules that own them — if base-identity.md gains a section,
the check follows without an edit here.

Honest signal (the fleet rule this file obeys)
----------------------------------------------
A profile whose SOUL cannot be read is a row naming the profile and the reason,
never a silent pass. An unreadable/invalid spec is an honest "cannot verify"
row, never a guessed verdict. An absent PROFILES_DIR is reported as a VACUOUS
scan (ok, scanned=0, the reason named) — the same vacuity guard the invariant
test uses — never a crash, and never a silent skip presented as health.

Address discipline: no real host appears here; fixtures use the documented
placeholders (10.0.0.x) and profile names are fixture names.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

import yaml

import rolelib

CHECK_NAME = "role SOUL identity (## Role: header vs profile dir)"

#: The marker that marks a SOUL as an HSCC-generated WORKER soul. Deliberately
#: the SAME string the invariant test uses (test_generator.py's worker_marker) —
#: the two surfaces must agree on what "HSCC-owned worker profile" means, or the
#: doctor audits a population the test does not and the operator gets two
#: different answers about the same fleet.
WORKER_MARKER = "your own git worktree"

_ROLE_PREFIX = "Role:"


# ── the emittable-heading set, derived from the modules that own it ────────

def _headings(text: str) -> List[str]:
    """Every level-2 heading in ``text``, stripped, in document order.

    Exact ``## `` prefix (not lstrip): an indented ``## `` sits inside a fenced
    code block and is markdown content, not a section heading.
    """
    return [ln[3:].strip() for ln in text.splitlines() if ln.startswith("## ")]


def base_headings(base_path: Optional[str] = None) -> List[str]:
    """`## ` headings inside base-identity.md, as compose_soul embeds them.

    Read per call (not import-time) so a caller — or a test — can point the
    check at a different base identity and get that file's set. A missing or
    unreadable base identity yields []; the header comparison still works,
    which is the defect that motivated this card.
    """
    path = base_path if base_path is not None else rolelib.BASE_IDENTITY_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            return _headings(fh.read())
    except (OSError, UnicodeDecodeError):
        return []


def ops_headings() -> List[str]:
    """The `## ` headings of the three operational blocks generator can pick.

    Read from generator itself rather than hardcoded: if a fourth ops block or a
    renamed ops heading lands there, this check follows automatically instead of
    flagging a legitimate heading as hand-patched.
    """
    import generator
    out: List[str] = []
    for block in (generator._WORKER_OPS, generator._ORCH_OPS,
                  generator._PROJECT_ORCH_OPS):
        for h in _headings(block.format(name="x")):
            if h not in out:
                out.append(h)
    return out


def spec_identity_headings(spec: Dict[str, Any]) -> List[str]:
    """Headings the spec's own identity text emits (a spec is an input, so
    whatever it contains is emittable — the check must not flag spec-authored
    sections as hand-patched)."""
    return _headings(str(spec.get("identity") or ""))


def spec_names(roles_dir: Optional[str] = None) -> List[str]:
    """Every spec's own ``name:`` field plus its filename stem.

    Both, so a spec whose name disagrees with its filename is still ATTRIBUTED
    (it loads, so its identity headings are known) and then reported as the
    mismatch it is. A filename-only set would instead blame it for its own
    headings, and a name-only set would never notice the mismatch at all.
    """
    root = roles_dir if roles_dir is not None else rolelib.ROLES_DIR
    out: List[str] = []
    if not os.path.isdir(root):
        return out
    for fname in sorted(os.listdir(root)):
        if not fname.endswith(".yaml"):
            continue
        stem = os.path.splitext(fname)[0]
        if stem not in out:
            out.append(stem)
        try:
            with open(os.path.join(root, fname), encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except Exception:
            continue
        nm = str((data.get("name") if isinstance(data, dict) else "") or "").strip()
        if nm and nm not in out:
            out.append(nm)
    return out


def _load_spec_quietly(name: str, roles_dir: Optional[str] = None
                       ) -> Optional[Dict[str, Any]]:
    """rolelib.load_spec for one spec, or None on ANY failure.

    None means "this spec cannot be attributed", which callers render as an
    honest cannot-verify row — never as a pass and never as a guessed verdict.
    """
    root = roles_dir if roles_dir is not None else rolelib.ROLES_DIR
    try:
        return rolelib.load_spec(os.path.join(root, f"{name}.yaml"))
    except Exception:
        return None


# ── one profile ────────────────────────────────────────────────────────────

def inspect_profile(name: str, soul_text: str, *, base: List[str],
                    ops: List[str], known_spec_names,
                    spec_loader: Optional[Callable[..., Optional[Dict[str, Any]]]] = None,
                    roles_dir: Optional[str] = None) -> Dict[str, Any]:
    """Verdict for ONE profile SOUL. Never raises on odd content.

    ``known_spec_names`` is every spec name/filename stem in the tree, so the
    only thing missing here is a profile that has NO spec at all — the one fault
    ``hscc roles generate`` cannot repair. ``spec_loader(name, roles_dir)``
    defaults to :func:`_load_spec_quietly`.
    """
    heads = _headings(soul_text)
    actual = [h for h in heads if h.startswith(_ROLE_PREFIX)]
    header = actual[0][len(_ROLE_PREFIX):].strip() if actual else None
    problems: List[str] = []
    row: Dict[str, Any] = {
        "profile": name,
        "worker": WORKER_MARKER in soul_text,
        "role_header": header,
        "expected": name,
        "spec_name": None,
        "has_spec": False,
        "problems": problems,
        "notes": [],
    }

    if header is None:
        problems.append(
            f"{name}: SOUL.md carries the worker signature but has no "
            f"'## Role:' header at all — expected '## Role: {name}'")
    elif header != name:
        problems.append(
            f"{name}: SOUL header '## {header}' != profile dir name — expected "
            f"'## Role: {name}' (copy-pasted profile: this worker has run under "
            "the wrong role identity)")

    loader = spec_loader or _load_spec_quietly
    spec = loader(name, roles_dir)
    if spec is None:
        # Two different faults, two different fixes, so they must not share a
        # rendering: a profile with NO spec is skipped by generate outright (the
        # fault the invariant test catches); a spec that fails validation is a
        # broken input that must be repaired before generate can help. Guessing
        # the heading sweep in the second case would blame the spec for its own
        # identity headings, so the sweep is skipped and said so.
        if name not in set(known_spec_names):
            problems.append(
                f"{name}: no roles/{name}.yaml spec — `hscc roles generate` "
                "skips this profile, so nothing can repair its SOUL until a "
                "spec exists (add roles/<name>.yaml, then generate)")
        else:
            problems.append(
                f"{name}: roles/{name}.yaml unreadable or invalid — heading "
                "sweep skipped (cannot attribute '## ' headings without the "
                "spec)")
        return row

    row["has_spec"] = True
    spec_name = str(spec.get("name") or "").strip()
    row["spec_name"] = spec_name or None

    # The third side the card asks for: header vs the SPEC's own name. compose_soul
    # stamps `## Role: <spec name>`, so a SOUL whose header matches its dir can
    # still disagree with the spec if the two were minted apart — and a spec whose
    # name != the dir name breaks generate for this profile outright.
    if spec_name and spec_name != name:
        problems.append(
            f"{name}: roles/{name}.yaml declares name '{spec_name}' != profile "
            f"dir name — generate would stamp '## Role: {spec_name}' here "
            "(t_8d22ec81 rule: spec name == profile dir name)")

    emittable = set(base) | set(ops) | set(spec_identity_headings(spec))
    allowed_headers = {f"Role: {name}"}
    if header is not None:
        allowed_headers.add(f"Role: {header}")
    for h in heads:
        if h in emittable or h in allowed_headers:
            continue
        if h.startswith(_ROLE_PREFIX):
            continue  # already reported as the header mismatch above
        problems.append(
            f"{name}: SOUL heading '## {h}' cannot be emitted by compose_soul() "
            "— hand-patched block; the next `hscc roles generate` drops it "
            "(fold it into the spec identity first)")
    return row


# ── the whole profiles dir ─────────────────────────────────────────────────

def scan(profiles_dir: Optional[str] = None, *, roles_dir: Optional[str] = None,
         base_path: Optional[str] = None, base: Optional[List[str]] = None,
         ops: Optional[List[str]] = None,
         known_spec_names: Optional[List[str]] = None,
         spec_loader=None) -> Dict[str, Any]:
    """Audit every profile dir under ``profiles_dir`` (default rolelib's).

    Returns the rows + counts + a vacuity flag. Never raises when the dir is
    absent: the worker env can point HERMES_HOME at a profile dir, in which case
    there is genuinely nothing to scan and the check says so out loud.
    """
    root = profiles_dir if profiles_dir is not None else rolelib.PROFILES_DIR
    base = base_headings(base_path) if base is None else list(base)
    ops = ops_headings() if ops is None else list(ops)
    known = (spec_names(roles_dir) if known_spec_names is None
             else list(known_spec_names))

    if not os.path.isdir(root):
        return {"scanned": 0, "worker_scanned": 0, "nonworker": 0,
                "rows": [], "vacuous": True,
                "detail": f"no profiles dir at {root} — nothing scanned"}

    rows: List[Dict[str, Any]] = []
    scanned = 0          # dirs actually judged (worker-signature or role-less)
    nonworker = 0        # HSCC SOULs excluded from judgement (orchestrator family)
    for name in sorted(os.listdir(root)):
        pdir = os.path.join(root, name)
        if not os.path.isdir(pdir):
            continue
        soul = os.path.join(pdir, "SOUL.md")
        if not os.path.isfile(soul):
            continue  # no SOUL at all — not an HSCC-generated identity
        try:
            with open(soul, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            rows.append({"profile": name, "worker": False, "role_header": None,
                         "expected": name, "spec_name": None, "has_spec": False,
                         "problems": [f"{name}: SOUL.md unreadable ({exc}) — "
                                      "cannot verify its identity"],
                         "notes": []})
            scanned += 1
            continue
        if WORKER_MARKER in text:
            rows.append(inspect_profile(name, text, base=base, ops=ops,
                                        known_spec_names=known,
                                        spec_loader=spec_loader,
                                        roles_dir=roles_dir))
            scanned += 1
        elif "## Role:" in text:
            nonworker += 1  # orchestrator-family: stamped with its spec's name
    return {"scanned": scanned, "worker_scanned": scanned,
            "nonworker": nonworker, "rows": rows, "vacuous": False,
            "detail": ""}


def check(profiles_dir: Optional[str] = None, *, roles_dir: Optional[str] = None,
          base_path: Optional[str] = None) -> Dict[str, Any]:
    """The doctor check as a plain dict (Check-compatible fields).

    Non-fatal: a mislabelled role identity is a loud WARNING to fix, never a
    reason to hard-stop an install — same posture as "models served" and the
    node-recovery check. Every red line names the profile, the actual header and
    the expected one, per the card.
    """
    res = scan(profiles_dir, roles_dir=roles_dir, base_path=base_path)
    rows = res["rows"]
    bad = [r for r in rows if r["problems"]]

    if res["vacuous"]:
        # Vacuous, not healthy: the reason and the zero are both printed, so a
        # scan that looked at nothing can never read as an all-clear.
        return {"name": CHECK_NAME, "ok": True, "fatal": False, "rows": [],
                "scanned": 0, "nonworker": 0,
                "detail": f"{res['detail']} (vacuous: nothing to compare)",
                "fix": ""}

    summary = (f"{res['scanned'] - len(bad)}/{res['scanned']} worker-signature "
               f"SOUL(s) match their dir name and spec")
    if res["nonworker"]:
        summary += (f" ({res['nonworker']} orchestrator-family SOUL(s) not "
                    "judged — generated from a shared spec)")
    if bad:
        summary += f"; {len(bad)} need attention"

    lines = [summary]
    for r in bad:
        for i, p in enumerate(r["problems"]):
            lines.append(("      [XX] " if i == 0 else "           ") + p)
    fix = ("`hscc roles generate` rewrites every SOUL from its spec and repairs "
           "the header. It re-stamps ALL generated profiles and rewrites live "
           "profile state, so it is an OPERATOR action — and any hand-appended "
           "heading named above must be folded into roles/<name>.yaml's identity "
           "FIRST, or generate silently drops it.")
    return {"name": CHECK_NAME, "ok": not bad, "fatal": False,
            "rows": rows, "scanned": res["scanned"],
            "nonworker": res["nonworker"],
            "detail": "\n".join(lines), "fix": fix if bad else ""}


def main(argv=None) -> int:
    """Standalone probe: `python3 role_soul_check.py [--profiles-dir DIR] [--json]`."""
    import argparse
    import json
    ap = argparse.ArgumentParser(
        prog="role_soul_check.py",
        description="Read-only check: every HSCC worker-signature profile SOUL's "
                    "'## Role:' header matches its profile dir name and its role "
                    "spec, and carries no heading compose_soul() cannot emit.")
    ap.add_argument("--profiles-dir", default=None,
                    help="profiles root (default: rolelib.PROFILES_DIR)")
    ap.add_argument("--roles-dir", default=None,
                    help="role spec dir (default: rolelib.ROLES_DIR)")
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
