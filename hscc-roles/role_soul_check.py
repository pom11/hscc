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
produce. Nothing warns that the next ``hscc.py generate`` overwrites it.

This module reports both, and it is a REPORTER, not a pytest assertion. The
card's ruling is explicit: a live-state invariant belongs where a fault is
surfaced to the operator (the bootstrap doctor, ``hscc-bootstrap/doctor.py``),
never as a test that is red on the operator machine and vacates in CI. Nothing
here writes anything; the repair is
an operator action (``hscc.py generate``), and re-stamping every live profile
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

The content pass (why a heading sweep is not enough — t_5b362400)
-----------------------------------------------------------------
A heading sweep cannot see a hand-patched *paragraph under an emittable
heading*: the heading is legal, so the row comes back clean while the sentence
under it is exactly what the next ``generate`` deletes. t_0f2ee405 measured that
gap with an ad-hoc whole-document diff and found one genuine loss in the fleet
that the heading sweep never mentioned. This module now does that diff
permanently: it composes what ``generate`` WOULD write (``compose_soul`` of the
profile's own tracked spec + base-identity.md) and positionally diffs the live
SOUL against it, so the operator gets the loss list, not just the bad headings.

Two measurement rules the ad-hoc script had to learn the hard way, pinned here
and in the tests:

* POSITIONAL, not a set difference. A live line counts as surviving only if it
  falls inside an ``equal`` opcode of ``difflib.SequenceMatcher(composed, live)``
  — otherwise a hand-patched line "survives" by coincidentally matching identical
  text elsewhere in the document (a duplicated bullet, a boilerplate sentence).
  ``autojunk=False`` because the default popular-element heuristic makes the
  verdict depend on how long the document happens to be and how often a line
  repeats, not on what differs: measured over 3000 boilerplate-heavy documents it
  changed the verdict on 2660 of them, always by flagging MORE lines (never by
  hiding one). So this choice is about stability and false alarms, not about
  catch — and it is pinned by a test rather than left as a preference.
* NOT every differing line is a loss, or the report is a wall nobody triages.
  Each non-equal line is classified: ``identity`` carries the role identity that
  ``generate`` exists to replace (the copy-pasted header's name — deleting it IS
  the repair); ``inventory`` carries only sandbox-shaped identifiers the composed
  text does not, i.e. the named shared/prod sandboxes t_8d22ec81 ruled must NOT
  come back (removal is intended); ``covered`` does not survive byte-for-byte but
  every distinctive word on it still appears in the composed text (a rewrap, a
  reorder or a duplicate lost no novel content). Only ``lost`` — content that
  appears nowhere in what ``generate`` would write — is reported as a problem.
  The classification is a judgement, so the lines it EXCLUDES are still printed
  with their line numbers (bounded per row, full lists in ``--json``): the
  operator gets to audit the call rather than trust it. ``## `` heading lines are
  excluded from the content pass: the heading sweep above already owns them, and
  double-reporting one defect under two headings makes the loss list look bigger
  than the repair.

Known limit, stated rather than hidden: a hand-patched sentence built ENTIRELY
from words the composed text already uses elsewhere lands in ``covered``, so the
loss list can under-report that one shape. It is the price of not drowning the
operator in rewraps, and printing every covered line is what keeps it reviewable
instead of invisible.

The shape rule for the sandbox inventory is deliberately a SHAPE (an identifier
carrying an underscore) and never a name list: the leak class t_0f2ee405's first
audit draft committed was an audit doc that spelled the sandboxes out to explain
why they must not be spelled out. This file must not repeat that mistake, so it
matches the shape, masks it in every rendered surface, and the tests use
fixture-shaped names only.

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

import difflib
import os
import re
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


# ── the content pass (t_5b362400) ──────────────────────────────────────────
#
# Identifier-shaped token: the shape of a BC/al sandboxes inventory entry
# (`POM_…`, `…_LIVE`). Deliberately a SHAPE and never a name list — a rule that
# names the operator's shared/prod sandboxes would itself be the leak
# t_0f2ee405's first audit draft committed.
_INVENTORY_SHAPE = re.compile(r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+")

#: Sentinel: "read base-identity.md yourself" — distinguishes *unset* from an
#: explicit ``None`` (= base identity could not be read, which must stay
#: honest-cannot-verify instead of re-reading the default path).
_UNSET = object()

#: Per-bucket cap on how many excluded lines a row prints. The classification is
#: the operator's to audit, so lines are shown, not summarised away — but a
#: block that was genuinely re-written would otherwise bury every other row.
#: Full lists are always in ``--json``.
_NOTE_LINE_CAP = 6

#: Words worth 4+ chars. Below that a line is markdown punctuation and carries
#: no information to lose.
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.{}/+*\-']{3,}")


def _tokens(line: str) -> List[str]:
    """Distinctive words of a line, lowercased, for token-coverage tests.

    Ends are unquoted first (``customization.`` -> ``customization``): a live
    line and the composed text differ in where the line BREAKS, so a trailing
    period on a re-wrapped line is a formatting artifact, not a missing word.
    """
    return [t.strip("._*/{}\"'`()") for t in _TOKEN.findall(line)
            if t.strip("._*/{}\"'`()")]


def _is_inventory_only(tokens: List[str]) -> bool:
    """True when EVERY token is identifier-shaped, i.e. a sandbox inventory.

    All, not any: a line that carries inventory tokens AND novel prose tokens
    (the pontaje live line does exactly this) is a genuine loss too, and
    classifying it as inventory because one token matched is the fail-open this
    fleet already got burned by (t_0f2ee405's mutation M3 survived on a
    words-present/constraint-gone assertion).
    """
    return bool(tokens) and all(
        _INVENTORY_SHAPE.fullmatch(t) and "_" in t for t in tokens)


def mask_inventory(line: str) -> str:
    """Redact identifier-shaped tokens before a line is rendered or stored.

    The check reads the operator's live SOUL, and the line it must quote is the
    one carrying the named shared/prod sandboxes. Rendering them verbatim turns
    a `doctor --json` paste into a public leak — the exact accident class
    t_0f2ee405 recorded, one step earlier. Line number, position in the line and
    the rest of the sentence survive, which is all the operator needs to find
    and fold the line; the shape rule is the same one the audit doc obeys.
    """
    return _INVENTORY_SHAPE.sub("<sandbox>", line)


def content_diff(live_text: str, composed_text: str, *,
                 wrong_header: Optional[str] = None) -> Dict[str, Any]:
    """Classify every live line `compose_soul()` cannot reproduce, byte-for-byte.

    POSITIONAL by construction: a live line survives only if it sits inside an
    ``equal`` opcode of ``SequenceMatcher(composed_lines, live_lines)`` — never a
    set difference, which lets a hand-patched line "survive" by coincidentally
    matching identical text elsewhere in the document (a duplicated bullet, a
    boilerplate sentence). ``autojunk=False`` for the same reason: the default
    popular-element heuristic can mark repeated lines as junk and manufacture
    ``equal`` runs that do not exist, which would be a fail-open in the one test
    this whole pass depends on.

    ``## `` heading lines and blank lines are not judged here — the heading
    sweep above owns headings, and double-reporting one defect under two
    headings inflates the loss list beyond the repair.

    Buckets (only ``lost`` is a problem):
      * ``identity``  — every token the composed text lacks is the copy-pasted
        ``## Role:`` name this profile's mismatch already reports. Deleting them
        IS the repair (t_0f2ee405's lines 121/123/127), so they are not losses.
        Requires a header mismatch AND no other missing token, so a hand-patched
        paragraph that merely mentions another role is still reported.
      * ``inventory`` — every missing token is identifier-shaped: the named
        shared/prod sandboxes t_8d22ec81 ruled must NOT come back. Their removal
        is intended, so a blocklist of names is not needed here either.
      * ``covered``   — nothing missing: every token of the line also appears in
        the composed text, i.e. a re-wrap, a reorder, or a duplicate of a line
        generate already writes. No NOVEL content is at stake, so it is not a
        loss — but each such line is still printed (with its line number) by
        :func:`check`, because a set difference would have skipped it entirely
        and the operator is the one who gets to judge the classification.
      * ``lost``      — content that appears nowhere in what generate writes.

    Known limit, stated rather than hidden: a hand-patched sentence built
    ENTIRELY from words the composed text already uses elsewhere lands in
    ``covered``, so the loss list can under-report that one shape. It is the
    price of not drowning the operator in re-wraps, and printing every covered
    line is what keeps it reviewable instead of invisible.
    """
    composed_lines = composed_text.splitlines()
    live_lines = live_text.splitlines()
    sm = difflib.SequenceMatcher(None, composed_lines, live_lines,
                                 autojunk=False)
    survives = [False] * len(live_lines)
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for j in range(j1, j2):
                survives[j] = True

    composed_tokens = {t for ln in composed_lines for t in _tokens(ln)}
    wanted = {wrong_header.lower()} if wrong_header else set()
    out: Dict[str, Any] = {
        "compared": True, "live_lines": len(live_lines),
        "lost": [], "covered": [], "identity": [], "inventory": [],
    }
    for i, line in enumerate(live_lines):
        if survives[i] or not line.strip() or line.startswith("## "):
            continue
        missing = [t for t in _tokens(line) if t not in composed_tokens]
        if not missing:
            out["covered"].append({"line": i + 1, "text": mask_inventory(line)})
        elif wanted and all(t in wanted for t in missing):
            out["identity"].append({"line": i + 1,
                                    "text": mask_inventory(line)})
        elif _is_inventory_only(missing):
            out["inventory"].append({"line": i + 1,
                                     "text": mask_inventory(line)})
        else:
            out["lost"].append({"line": i + 1, "text": mask_inventory(line),
                                "missing": [mask_inventory(t) for t in missing]})
    for key in ("lost", "covered", "identity", "inventory"):
        out[key + "_count"] = len(out[key])
    return out


def _compose_expected(name: str, spec: Dict[str, Any], base_text: str,
                      compose: Optional[Callable[..., str]] = None
                      ) -> Optional[str]:
    """What `hscc.py generate` WOULD write for this profile, or None.

    The same call generate makes — `generator.compose_soul(spec, base)` with the
    base identity read the way ``hscc.py cmd_generate`` reads it — so the two can
    never disagree about the target. None means "cannot compose", which callers
    render as an honest cannot-verify note, never as a clean row.
    """
    if compose is None:
        import generator
        compose = generator.compose_soul
    try:
        return compose({"name": name, "identity": spec["identity"]}, base_text)
    except Exception:
        return None


def _read_base_text(base_path: Optional[str] = None) -> Optional[str]:
    """base-identity.md as ``hscc.py cmd_generate`` reads it, or None."""
    path = base_path if base_path is not None else rolelib.BASE_IDENTITY_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


# ── one profile ────────────────────────────────────────────────────────────

def inspect_profile(name: str, soul_text: str, *, base: List[str],
                    ops: List[str], known_spec_names,
                    spec_loader: Optional[Callable[..., Optional[Dict[str, Any]]]] = None,
                    roles_dir: Optional[str] = None,
                    base_text: Any = _UNSET,
                    compose: Optional[Callable[..., str]] = None) -> Dict[str, Any]:
    """Verdict for ONE profile SOUL. Never raises on odd content.

    ``known_spec_names`` is every spec name/filename stem in the tree, so the
    only thing missing here is a profile that has NO spec at all — the one fault
    ``hscc.py generate`` cannot repair. ``spec_loader(name, roles_dir)``
    defaults to :func:`_load_spec_quietly`.

    ``base_text`` / ``compose`` are the content pass's seams (t_5b362400): the
    base identity to compose against and the composition function itself. Both
    default to what ``hscc.py generate`` uses, and both are injected in tests so
    a suite can drive the content pass without importing the generator.
    """
    heads = _headings(soul_text)
    actual = [h for h in heads if h.startswith(_ROLE_PREFIX)]
    header = actual[0][len(_ROLE_PREFIX):].strip() if actual else None
    problems: List[str] = []
    notes: List[str] = []
    row: Dict[str, Any] = {
        "profile": name,
        "worker": WORKER_MARKER in soul_text,
        "role_header": header,
        "expected": name,
        "spec_name": None,
        "has_spec": False,
        "problems": problems,
        "notes": notes,
        # The content pass's verdict (t_5b362400). `compared: False` + a reason
        # is the honest shape: a row that did not diff its SOUL says so instead
        # of reading as "nothing lost".
        "content": {"compared": False, "reason": "no spec: nothing to compose"},
    }

    if header is None:
        problems.append(
            f"{name}: SOUL.md carries the worker signature but has no "
            f"'## Role:' header at all — expected '## Role: {name}'")
    elif header != name:
        # Render the heading LITERALLY (`## Role: x`, not `## x`): this line is
        # the operator's diff against what is physically in the file, and a
        # truncated rendering sends them hunting for a heading that does not
        # exist there.
        problems.append(
            f"{name}: SOUL header '## {actual[0]}' != profile dir "
            f"name — expected '## Role: {name}' (copy-pasted profile: this "
            "worker has run under the wrong role identity)")

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
                f"{name}: no roles/{name}.yaml spec — `hscc.py generate` "
                "skips this profile, so nothing can repair its SOUL until a "
                "spec exists (add roles/<name>.yaml, then generate)")
            row["content"]["reason"] = "no spec: there is nothing to compose"
        else:
            problems.append(
                f"{name}: roles/{name}.yaml unreadable or invalid — heading "
                "sweep skipped (cannot attribute '## ' headings without the "
                "spec)")
            row["content"]["reason"] = ("spec unreadable/invalid: the composed "
                                        "text cannot be attributed")
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
    # compose_soul emits exactly ONE `## Role:` heading, and it is the one read
    # as `header` above. The sweep may therefore exempt that heading exactly
    # ONCE and must flag every other `Role:`-prefixed heading: a duplicate is a
    # hand-patched section that generate drops, and the previous blanket
    # `startswith(_ROLE_PREFIX): continue` let it pass silently on the claim
    # that it had "already been reported" — true only of the first. The budget
    # is a COUNT, not a string comparison, so two *identical* `## Role: x`
    # headings — the plainest paste-the-block-twice artifact, and this card is
    # about copy-paste — cannot both be excused as "the" emitted header.
    emitted = actual[0] if actual else None
    role_budget = 1 if actual else 0
    for h in heads:
        if h in emittable:
            continue
        if h.startswith(_ROLE_PREFIX):
            if h == emitted and role_budget:
                role_budget -= 1
                continue  # the single emitted role header, judged above
            problems.append(
                f"{name}: SOUL has a second '## {h}' heading — compose_soul() "
                "emits exactly one '## Role:' header, so this one is "
                "hand-patched and the next `hscc.py generate` drops it")
            continue
        problems.append(
            f"{name}: SOUL heading '## {h}' cannot be emitted by compose_soul() "
            "— hand-patched block; the next `hscc.py generate` drops it "
            "(fold it into the spec identity first)")

    # ── content pass ────────────────────────────────────────────────────────
    # The heading sweep above is a HEADING sweep: a hand-patched paragraph under
    # an EMITTABLE heading is invisible to it, which is the gap t_0f2ee405 had to
    # measure with an ad-hoc script. Compose what generate WOULD write and diff
    # the live SOUL against it positionally, so the operator gets the loss list.
    if spec_name and spec_name != name:
        # generate would stamp `## Role: <spec_name>` here, so every identity and
        # ops line differs by construction and the diff would be a wall of noise
        # on top of the problem line above. Deferred, and said so — never judged
        # as "nothing lost".
        row["content"] = {
            "compared": False,
            "reason": f"spec name '{spec_name}' != dir name — fix that first",
        }
        notes.append(f"{name}: content pass SKIPPED — generate would rewrite "
                     "this profile's identity lines anyway (spec name != dir "
                     "name); only the heading sweep ran")
        return row
    base_txt = _read_base_text() if base_text is _UNSET else base_text
    expected = _compose_expected(name, spec, base_txt, compose) \
        if base_txt is not None else None
    if expected is None:
        row["content"] = {
            "compared": False,
            "reason": ("cannot compose the expected SOUL (base identity "
                       "unreadable)" if base_txt is None
                       else "compose_soul() failed for this spec"),
        }
        notes.append(f"{name}: content pass SKIPPED — "
                     f"{row['content']['reason']}; only the heading sweep ran")
    else:
        diff = content_diff(soul_text, expected,
                            wrong_header=(header if header and header != name
                                          else None))
        row["content"] = diff
        for entry in diff["lost"]:
            problems.append(
                f"{name}: SOUL line {entry['line']} cannot be reproduced by "
                f"compose_soul() — the next `hscc.py generate` deletes it: "
                f"{entry['text']!r}")
        # Every line the POSITIONAL diff flagged gets said out loud, even the
        # ones classified as no-loss: a set difference would have skipped them
        # all, and the classification is a judgement the operator must be able
        # to audit. `_NOTE_LINE_CAP` bounds how many a single row may print —
        # one budget for the row, so a genuinely re-written block cannot bury
        # every other profile's line. `--json` always carries the full lists.
        budget = _NOTE_LINE_CAP
        overflow = 0
        for label, key in (("carry the copied identity that generate replaces "
                            "(the repair, not a loss)", "identity"),
                           ("name shared/prod sandboxes the composed text "
                            "deliberately does not carry (t_8d22ec81: "
                            "constraint, not inventory)", "inventory"),
                           ("reuse only words generate already writes "
                            "(a re-wrap, reorder or duplicate)", "covered")):
            entries = diff[key]
            if not entries:
                continue
            notes.append(f"{name}: {len(entries)} line(s) {label}:")
            for e in entries[:budget]:
                notes.append(f"{name}   L{e['line']}: {e['text']}")
            shown = min(len(entries), budget)
            budget -= shown
            overflow += len(entries) - shown
        if overflow:
            notes.append(f"{name}   … and {overflow} more line(s) not shown "
                         "(full list in `--json`)")
    return row


# ── the whole profiles dir ─────────────────────────────────────────────────

def scan(profiles_dir: Optional[str] = None, *, roles_dir: Optional[str] = None,
         base_path: Optional[str] = None, base: Optional[List[str]] = None,
         ops: Optional[List[str]] = None,
         known_spec_names: Optional[List[str]] = None,
         spec_loader=None, base_text: Any = _UNSET,
         compose: Optional[Callable[..., str]] = None) -> Dict[str, Any]:
    """Audit every profile dir under ``profiles_dir`` (default rolelib's).

    Returns the rows + counts + a vacuity flag. Never raises when the dir is
    absent: the worker env can point HERMES_HOME at a profile dir, in which case
    there is genuinely nothing to scan and the check says so out loud.

    ``base_text`` / ``compose`` are the content pass's seams, passed straight to
    :func:`inspect_profile`; ``base_text`` defaults to the file at ``base_path``.
    """
    root = profiles_dir if profiles_dir is not None else rolelib.PROFILES_DIR
    base = base_headings(base_path) if base is None else list(base)
    ops = ops_headings() if ops is None else list(ops)
    known = (spec_names(roles_dir) if known_spec_names is None
             else list(known_spec_names))
    if base_text is _UNSET:
        # Only an UNSET seam re-reads the file; an explicit None means "the base
        # identity could not be read" and must stay an honest cannot-verify row,
        # never silently re-read here into a fake `compared: True`.
        base_text = _read_base_text(base_path)

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
            # The content pass needs the SAME read the heading sweep used, so a
            # file that cannot be read cannot be diffed either — the row says
            # which half ran instead of reading as an all-clear.
            rows.append({"profile": name, "worker": False, "role_header": None,
                         "expected": name, "spec_name": None, "has_spec": False,
                         "problems": [f"{name}: SOUL.md unreadable ({exc}) — "
                                      "cannot verify its identity"],
                         "notes": [f"{name}: heading sweep and content pass both "
                                   "SKIPPED — SOUL.md unreadable"],
                         "content": {"compared": False,
                                     "reason": "SOUL.md unreadable"}})
            scanned += 1
            continue
        if WORKER_MARKER in text:
            rows.append(inspect_profile(name, text, base=base, ops=ops,
                                        known_spec_names=known,
                                        spec_loader=spec_loader,
                                        roles_dir=roles_dir,
                                        base_text=base_text,
                                        compose=compose))
            scanned += 1
        elif "## Role:" in text:
            nonworker += 1  # orchestrator-family: stamped with its spec's name
    return {"scanned": scanned, "worker_scanned": scanned,
            "nonworker": nonworker, "rows": rows, "vacuous": False,
            "detail": ""}


def check(profiles_dir: Optional[str] = None, *, roles_dir: Optional[str] = None,
          base_path: Optional[str] = None,
          compose: Optional[Callable[..., str]] = None) -> Dict[str, Any]:
    """The doctor check as a plain dict (Check-compatible fields).

    Non-fatal: a mislabelled role identity is a loud WARNING to fix, never a
    reason to hard-stop an install — same posture as "models served" and the
    node-recovery check. Every red line names the profile, the actual header and
    the expected one, per the card.

    ``compose`` is the content pass's seam (see :func:`scan`); the doctor and the
    CLI leave it unset and get the generator's own ``compose_soul``.
    """
    res = scan(profiles_dir, roles_dir=roles_dir, base_path=base_path,
               compose=compose)
    rows = res["rows"]
    bad = [r for r in rows if r["problems"]]

    if res["vacuous"]:
        # Vacuous, not healthy: the reason and the zero are both printed, so a
        # scan that looked at nothing can never read as an all-clear.
        return {"name": CHECK_NAME, "ok": True, "fatal": False, "rows": [],
                "scanned": 0, "nonworker": 0,
                "detail": f"{res['detail']} (vacuous: nothing to compare)",
                "fix": ""}

    # The content pass's own headline, so a heading-only sweep can never read as
    # "content checked, nothing lost": N is the number of lines generate WOULD
    # delete, counted across the judged rows.
    lost_rows = [r for r in rows if r.get("content", {}).get("lost_count")]
    lost_lines = sum(r["content"]["lost_count"] for r in lost_rows)
    skipped = [r for r in rows
               if r.get("worker") and not r.get("content", {}).get("compared")]

    summary = (f"{res['scanned'] - len(bad)}/{res['scanned']} worker-signature "
               f"SOUL(s) match their dir name and spec")
    if res["nonworker"]:
        summary += (f" ({res['nonworker']} orchestrator-family SOUL(s) not "
                    "judged — generated from a shared spec)")
    if lost_lines:
        summary += (f"; {lost_lines} live line(s) across {len(lost_rows)} "
                    "profile(s) cannot be reproduced by generate")
    else:
        summary += "; no live content would be lost by generate"
    if skipped:
        # Honest about coverage: a row whose content pass could not run must not
        # leave the reader assuming the content of every judged profile was
        # compared. The row itself carries the reason.
        summary += (f" ({len(skipped)} profile(s) could not be content-compared "
                    "— see their rows)")
    if bad:
        summary += f"; {len(bad)} need attention"

    lines = [summary]
    for r in bad:
        for i, p in enumerate(r["problems"]):
            lines.append(("      [XX] " if i == 0 else "           ") + p)
        # Informational lines: what the diff EXCLUDED and why. Kept out of
        # `problems` so they never flip a verdict, printed so the operator can
        # audit the classification instead of trusting it.
        for n in r.get("notes", []):
            lines.append("      [!!] " + n)
    for r in rows:
        if r["problems"]:
            continue
        for n in r.get("notes", []):
            lines.append("      [!!] " + n)
    fix = ("`hscc.py generate` rewrites every SOUL from its spec and repairs "
           "the header. It re-stamps ALL generated profiles and rewrites live "
           "profile state, so it is an OPERATOR action — and every hand-appended "
           "heading or line named above must be folded into roles/<name>.yaml's "
           "identity FIRST, or generate silently drops it.")
    return {"name": CHECK_NAME, "ok": not bad, "fatal": False,
            "rows": rows, "scanned": res["scanned"],
            "nonworker": res["nonworker"],
            "lost_lines": lost_lines,
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
