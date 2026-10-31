"""role_soul_check tests — the doctor's role-SOUL identity audit (t_c823ebc9).

Hermetic by construction, mirroring test_provision_check.py's isolation style:
every fixture builds TEMP profile dirs and temp spec/base-identity files and
passes them explicitly. Nothing here reads ``~/.hermes/profiles`` — the check's
default target is the live profiles root, so a fixture that relied on the
default would be the exact environment-dependent test this card exists to avoid.

The anti-vacuity load-bearer is the fixture whose ``## Role:`` header DISAGREES
with its dir name (the real ecofire-pontaje-engineer defect): if the check were
wired to nothing, or its comparison inverted, that case fails. Several tests
therefore assert BOTH directions — a matching SOUL must not be flagged, a
mismatched one must be.

Population rule pinned here (why orchestrator-family SOULs are excluded):
``orchestrators.py`` stamps a per-project orch with the spec name of the SHARED
spec it passes (``general-orch`` legitimately carries ``## Role: orchestrator``),
so a dir-name comparison would red-flag a layout that is correct by design.
"""

import json
import os

import pytest
import yaml

import role_soul_check as rsc
import rolelib

WORKER_OPS = "## Operational\n\nYou run as the **{name}** role on a worker GPU " \
             "node of the cluster, executing a single kanban task in your own " \
             "git worktree.\n"

BASE_MD = "## Shared value one\n\nbody\n\n## Shared value two\n\nbody\n"

#: The stub spec's `identity:` text. Kept in ONE place so the fixture SOUL can
#: carry the same words the spec emits: t_5b362400 added a CONTENT pass, so a
#: SOUL whose prose does not match its spec is now (correctly) reported as a
#: loss. A fixture that "passed" only because the check looked at headings
#: would be the vacuous test this file exists to prevent.
SPEC_IDENTITY = "spec identity text.\n"


def _worker_soul(role_header, *, extra_headings=(), base_md=BASE_MD,
                 identity=SPEC_IDENTITY):
    """A worker SOUL for ``role_header`` + ``identity`` over ``base_md``.

    DERIVED from the generator's own ``compose_soul`` rather than re-typed: with
    a content pass in play, a hand-typed facsimile of the ops block is a
    phantom "loss" (and a phantom "move") that has nothing to do with what is
    being tested, and re-typing the block is exactly the doctor-vs-generator
    disagreement this repo keeps having to fix. Tests that need a defect pass
    ``identity``/``extra_headings`` or edit the returned text.
    """
    import generator
    soul = generator.compose_soul({"name": role_header, "identity": identity},
                                  base_md)
    for h in extra_headings:
        soul += f"\n## {h}\n\nhand-patched body\n"
    return soul


def _world(tmp_path, *, profiles, specs, base=BASE_MD):
    """Build tmp profiles/ + roles/ + base-identity.md; return the three paths.

    ``profiles`` maps dir name -> SOUL text (or None for a dir with no SOUL.md);
    ``specs`` maps spec stem -> the spec's ``name:`` field (identity text is
    :data:`SPEC_IDENTITY`, which is what the fixture SOULs carry).
    """
    proot = tmp_path / "profiles"
    rroot = tmp_path / "roles"
    proot.mkdir(parents=True, exist_ok=True)
    rroot.mkdir(parents=True, exist_ok=True)
    for name, soul in profiles.items():
        pdir = proot / name
        pdir.mkdir(parents=True, exist_ok=True)
        if soul is not None:
            (pdir / "SOUL.md").write_text(soul, encoding="utf-8")
    for stem, spec_name in specs.items():
        (rroot / f"{stem}.yaml").write_text(
            yaml.safe_dump({"name": spec_name,
                            "identity": SPEC_IDENTITY,
                            "routing_description": "routing."}),
            encoding="utf-8")
    base_path = tmp_path / "base-identity.md"
    base_path.write_text(base, encoding="utf-8")
    return str(proot), str(rroot), str(base_path)


def _row(res, name):
    rows = [r for r in res["rows"] if r["profile"] == name]
    assert rows, f"no row for {name}; rows={[r['profile'] for r in res['rows']]}"
    return rows[0]


# --------------------------------------------------------------------------- #
# The defect this card exists for: header != dir name.
# --------------------------------------------------------------------------- #

def test_mismatched_header_is_flagged_with_actual_and_expected(tmp_path):
    """THE regression. A copy-pasted profile whose SOUL still reads the SOURCE's
    `## Role:` must be reported, naming the profile, the actual header and the
    expected one — the card's exact rendering requirement. If the check cannot
    see this, it cannot pass vacuously."""
    proot, rroot, base = _world(
        tmp_path,
        profiles={"pontaje-engineer": _worker_soul("bc-engineer")},
        specs={"pontaje-engineer": "pontaje-engineer"})
    res = rsc.check(proot, roles_dir=rroot, base_path=base)

    assert res["ok"] is False
    assert res["fatal"] is False, "a wrong identity is a WARNING, never fatal"
    row = _row(res, "pontaje-engineer")
    assert row["role_header"] == "bc-engineer"
    assert row["expected"] == "pontaje-engineer"
    assert row["has_spec"] is True
    joined = "\n".join(row["problems"])
    assert "bc-engineer" in joined
    assert "pontaje-engineer" in joined
    # The doctor's rendered detail must carry it too (the operator sees detail).
    assert "pontaje-engineer" in res["detail"]
    assert "bc-engineer" in res["detail"]


def test_matching_header_is_not_flagged(tmp_path):
    """Same world, header agrees -> clean. Without this pair the first test
    could pass on an always-flag implementation."""
    proot, rroot, base = _world(
        tmp_path,
        profiles={"pontaje-engineer": _worker_soul("pontaje-engineer")},
        specs={"pontaje-engineer": "pontaje-engineer"})
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    assert res["ok"] is True
    assert res["fix"] == ""
    assert _row(res, "pontaje-engineer")["problems"] == []


def test_mismatch_line_renders_the_heading_literally(tmp_path):
    """The report is the operator's diff against the FILE, so it must quote the
    heading as it physically reads — `## Role: bc-engineer`, not `##
    bc-engineer`. A truncated rendering sends the operator hunting for a heading
    that is not in the file, and hides which heading the file DOES carry."""
    proot, rroot, base = _world(
        tmp_path,
        profiles={"pontaje-engineer": _worker_soul("bc-engineer")},
        specs={"pontaje-engineer": "pontaje-engineer"})
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    joined = "\n".join(_row(res, "pontaje-engineer")["problems"])
    assert "'## Role: bc-engineer'" in joined, joined
    # And the truncation this replaces must not come back: a bare `## bc-...`
    # with no `Role:` between the sigil and the name is the bug.
    assert "'## bc-engineer'" not in joined


def test_duplicate_role_heading_is_flagged(tmp_path):
    """compose_soul() emits exactly ONE `## Role:` heading. A second one is
    hand-patched and generate drops it, so it must be reported — the sweep may
    skip the heading it already judged, not every `Role:`-prefixed heading."""
    soul = _worker_soul("a-engineer").replace(
        "spec identity text",
        "spec identity text\n\n## Role: someone-else\n\nbogus second identity")
    proot, rroot, base = _world(tmp_path, profiles={"a-engineer": soul},
                                specs={"a-engineer": "a-engineer"})
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    assert res["ok"] is False
    joined = "\n".join(_row(res, "a-engineer")["problems"])
    assert "second '## Role: someone-else'" in joined, joined
    assert "drops it" in joined


def test_identical_duplicate_role_heading_is_flagged(tmp_path):
    """The exemption is a COUNT of one, not a string match. Pasting the SAME
    `## Role:` block twice is the plainest copy-paste artifact there is — the
    exact fault class this card exists for — and a
    `if h == emitted: continue` would excuse both occurrences."""
    soul = _worker_soul("a-engineer").replace(
        "spec identity text",
        "spec identity text\n\n## Role: a-engineer\n\nduplicated block")
    proot, rroot, base = _world(tmp_path, profiles={"a-engineer": soul},
                                specs={"a-engineer": "a-engineer"})
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    assert res["ok"] is False, "the header matches the dir, so only the duplicate can be the fault"
    joined = "\n".join(_row(res, "a-engineer")["problems"])
    assert "second '## Role: a-engineer'" in joined, joined


def test_clean_world_reports_counts_not_silence(tmp_path):
    """A healthy fleet prints an explicit count, so a clean result is EVIDENCE
    rather than absence of output (the same rule flightdeck's doctor follows)."""
    proot, rroot, base = _world(
        tmp_path,
        profiles={"a-engineer": _worker_soul("a-engineer"),
                  "b-engineer": _worker_soul("b-engineer")},
        specs={"a-engineer": "a-engineer", "b-engineer": "b-engineer"})
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    assert res["scanned"] == 2
    assert res["ok"] is True
    assert "2/2" in res["detail"]


# --------------------------------------------------------------------------- #
# Hand-patched block: a heading compose_soul() cannot emit.
# --------------------------------------------------------------------------- #

def test_hand_patched_heading_is_flagged_as_regeneration_loss(tmp_path):
    """The live SOUL carried `## Project override: ecofire-pontaje`, which
    compose_soul() cannot produce. Must be flagged, and must say the next
    generate DROPS it — the operator has to know folding it into the spec comes
    first."""
    proot, rroot, base = _world(
        tmp_path,
        profiles={"pontaje-engineer": _worker_soul(
            "pontaje-engineer", extra_headings=["Project override: pontaje"])},
        specs={"pontaje-engineer": "pontaje-engineer"})
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    assert res["ok"] is False
    joined = "\n".join(_row(res, "pontaje-engineer")["problems"])
    assert "Project override: pontaje" in joined
    assert "compose_soul" in joined
    assert "drops it" in joined


def test_base_and_ops_headings_are_never_flagged(tmp_path):
    """Base-identity headings and the ops heading ARE emitted by compose_soul,
    so a healthy SOUL must not be red-flagged for them. Guards against an
    emittable-set bug that would flag every profile in the fleet."""
    proot, rroot, base = _world(
        tmp_path,
        profiles={"a-engineer": _worker_soul("a-engineer")},
        specs={"a-engineer": "a-engineer"})
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    assert res["ok"] is True, res["detail"]


def test_spec_identity_heading_is_not_flagged(tmp_path):
    """A spec is a generator INPUT: a heading inside the spec's own identity is
    emittable by definition and must not be called hand-patched."""
    proot = tmp_path / "profiles"
    rroot = tmp_path / "roles"
    (proot / "a-engineer").mkdir(parents=True)
    rroot.mkdir()
    (rroot / "a-engineer.yaml").write_text(yaml.safe_dump({
        "name": "a-engineer",
        "identity": "You do the thing.\n\n## Project scope\n\nown it\n",
        "routing_description": "routing."}), encoding="utf-8")
    soul = (_worker_soul("a-engineer")
            .replace("spec identity text",
                     "You do the thing.\n\n## Project scope\n\nown it"))
    (proot / "a-engineer" / "SOUL.md").write_text(soul, encoding="utf-8")
    res = rsc.check(str(proot), roles_dir=str(rroot),
                    base_path=_write_base(tmp_path))
    assert res["ok"] is True, res["detail"]


def _write_base(tmp_path):
    p = tmp_path / "base2.md"
    p.write_text(BASE_MD, encoding="utf-8")
    return str(p)


# --------------------------------------------------------------------------- #
# The third side of the comparison: header vs roles/<name>.yaml.
# --------------------------------------------------------------------------- #

def test_spec_name_disagreeing_with_dir_name_is_flagged(tmp_path):
    """The card requires the header compared against `roles/<name>.yaml` too.
    compose_soul stamps `## Role: <spec name>`, so a spec whose name != its
    filename / the dir name means generate would stamp the wrong identity here
    (the t_8d22ec81 rule: spec name == profile dir name)."""
    proot, rroot, base = _world(
        tmp_path,
        profiles={"pontaje-engineer": _worker_soul("pontaje-engineer")},
        specs={"pontaje-engineer": "bc-engineer"})  # spec name != dir name
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    assert res["ok"] is False
    joined = "\n".join(_row(res, "pontaje-engineer")["problems"])
    assert "bc-engineer" in joined
    assert "spec name" in joined.lower() or "name" in joined


def test_missing_spec_is_reported_as_unrepairable(tmp_path):
    """A worker SOUL with no spec is the OTHER fault (the one the existence test
    catches) and must be reported with the fix that applies: generate SKIPS it."""
    proot, rroot, base = _world(
        tmp_path,
        profiles={"handmade-engineer": _worker_soul("handmade-engineer")},
        specs={})
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    assert res["ok"] is False
    joined = "\n".join(_row(res, "handmade-engineer")["problems"])
    assert "no readable" in joined or "no roles/" in joined
    assert "skips" in joined


def test_unreadable_spec_is_honest_cannot_verify_not_a_pass(tmp_path):
    """A spec file that fails validation must NOT become a silent pass and must
    NOT be guessed at (guessing would flag the spec's own headings)."""
    proot = tmp_path / "profiles"
    rroot = tmp_path / "roles"
    (proot / "a-engineer").mkdir(parents=True)
    rroot.mkdir()
    (rroot / "a-engineer.yaml").write_text("identity: no name field\n",
                                           encoding="utf-8")
    (proot / "a-engineer" / "SOUL.md").write_text(
        _worker_soul("a-engineer"), encoding="utf-8")
    res = rsc.check(str(proot), roles_dir=str(rroot), base_path=_write_base(tmp_path))
    assert res["ok"] is False
    assert "unreadable or invalid" in "\n".join(_row(res, "a-engineer")["problems"])


def test_missing_role_header_entirely_is_flagged(tmp_path):
    """Worker signature present, no `## Role:` heading at all — expected header
    is still named so the operator knows what to write."""
    soul = "# base\n\n## Shared value one\n\nbody\n\n" + WORKER_OPS.format(
        name="a-engineer")
    proot, rroot, base = _world(tmp_path, profiles={"a-engineer": soul},
                                specs={"a-engineer": "a-engineer"})
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    assert res["ok"] is False
    joined = "\n".join(_row(res, "a-engineer")["problems"])
    assert "no '## Role:' header" in joined
    assert "## Role: a-engineer" in joined


# --------------------------------------------------------------------------- #
# Population: who is judged, who is not.
# --------------------------------------------------------------------------- #

def test_orchestrator_family_soul_is_not_judged(tmp_path):
    """general-orch legitimately carries `## Role: orchestrator` (stamped with
    the SHARED spec's name by orchestrators.py). Judging it by dir name would
    red-flag a correct-by-design layout, so it is excluded and counted."""
    orch_soul = ("# base\n\n## Shared value one\n\nbody\n\n"
                 "## Role: orchestrator\n\nYou orchestrate.\n\n"
                 "## Operational\n\nYou run as the **general-orch** on the "
                 "gateway node.\n")
    proot, rroot, base = _world(
        tmp_path,
        profiles={"general-orch": orch_soul,
                  "a-engineer": _worker_soul("a-engineer")},
        specs={"a-engineer": "a-engineer", "orchestrator": "orchestrator"})
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    assert res["ok"] is True, res["detail"]
    assert res["scanned"] == 1, "only the worker-signature SOUL is judged"
    assert res["nonworker"] == 1
    assert "orchestrator-family" in res["detail"]


def test_dir_without_soul_and_non_profile_dir_are_skipped(tmp_path):
    """A dir with no SOUL.md, and a plain file under the profiles root, are not
    HSCC identities — skipped, not flagged, and not counted."""
    proot, rroot, base = _world(
        tmp_path,
        profiles={"no-soul-engineer": None,
                  "a-engineer": _worker_soul("a-engineer")},
        specs={"a-engineer": "a-engineer", "no-soul-engineer": "no-soul-engineer"})
    (tmp_path / "profiles" / "stray.txt").write_text("x", encoding="utf-8")
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    assert res["scanned"] == 1
    assert res["ok"] is True
    assert [r["profile"] for r in res["rows"]] == ["a-engineer"]


def test_unreadable_soul_is_an_error_row_not_a_pass(tmp_path, monkeypatch):
    """A SOUL that cannot be read is a red line naming the profile — never a
    silent skip, which would shrink the audited population unnoticed."""
    proot, rroot, base = _world(
        tmp_path,
        profiles={"a-engineer": _worker_soul("a-engineer")},
        specs={"a-engineer": "a-engineer"})
    real_read = open

    def boom(path, *a, **kw):
        if str(path).endswith("SOUL.md"):
            raise OSError("permission denied")
        return real_read(path, *a, **kw)

    monkeypatch.setattr("builtins.open", boom)
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    assert res["ok"] is False
    assert "unreadable" in "\n".join(_row(res, "a-engineer")["problems"])


# --------------------------------------------------------------------------- #
# Vacuity guard: absent PROFILES_DIR must not crash and must not read healthy.
# --------------------------------------------------------------------------- #

def test_absent_profiles_dir_is_vacuous_not_a_crash(tmp_path):
    """Same vacuity guard the invariant test uses (it returns early when
    PROFILES_DIR is missing). The doctor must not crash, and must SAY nothing
    was compared — 'vacuous' plus scanned=0, so it can never be read as health."""
    missing = str(tmp_path / "does-not-exist")
    res = rsc.check(missing)
    assert res["ok"] is True
    assert res["scanned"] == 0
    assert "vacuous" in res["detail"]
    assert missing in res["detail"]


def test_check_never_raises_when_rolelib_defaults_are_used(tmp_path, monkeypatch):
    """The bridge calls check() with no args, i.e. against rolelib's defaults.
    Point those at a non-existent root and confirm it still returns a
    Check-shaped dict instead of raising (a doctor that crashes on a profile-scoped
    HERMES_HOME is a doctor that gets disabled)."""
    monkeypatch.setattr(rolelib, "PROFILES_DIR", str(tmp_path / "absent"))
    res = rsc.check()
    assert set(res) >= {"name", "ok", "detail", "fix", "fatal"}
    assert res["ok"] is True


# --------------------------------------------------------------------------- #
# Rendering contract: the operator must be able to act on the detail.
# --------------------------------------------------------------------------- #

def test_detail_names_every_offending_profile_and_lists_per_problem(tmp_path):
    """Two offenders -> both named, each on its own red-marked line. A summary
    alone would hide WHICH profile to fix."""
    proot, rroot, base = _world(
        tmp_path,
        profiles={"a-engineer": _worker_soul("wrong-a"),
                  "b-engineer": _worker_soul(
                      "b-engineer", extra_headings=["Project override: b"])},
        specs={"a-engineer": "a-engineer", "b-engineer": "b-engineer"})
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    assert res["ok"] is False
    lines = res["detail"].splitlines()
    assert any("a-engineer" in ln and "wrong-a" in ln for ln in lines)
    assert any("Project override: b" in ln for ln in lines)
    assert sum(1 for ln in lines if "[XX]" in ln) == 2
    assert "2 need attention" in lines[0]


def test_fix_names_the_command_and_the_operator_action_rule(tmp_path):
    """The fix line must name the repair AND the constraint: generate re-stamps
    every profile (an operator action) and hand-patched blocks must be folded
    into the spec FIRST."""
    proot, rroot, base = _world(
        tmp_path,
        profiles={"a-engineer": _worker_soul("wrong-a")},
        specs={"a-engineer": "a-engineer"})
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    assert "hscc.py generate" in res["fix"]
    assert "OPERATOR" in res["fix"]
    assert "FIRST" in res["fix"]


# --------------------------------------------------------------------------- #
# CLI: --json stays pure JSON; exit code follows the verdict.
# --------------------------------------------------------------------------- #

def test_cli_json_is_parsable_and_exit_code_follows(tmp_path, capsys):
    proot, rroot, base = _world(
        tmp_path,
        profiles={"a-engineer": _worker_soul("wrong-a")},
        specs={"a-engineer": "a-engineer"})
    rc = rsc.main(["--profiles-dir", proot, "--roles-dir", rroot,
                   "--base-identity", base, "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == rsc.CHECK_NAME
    assert payload["ok"] is False


def test_cli_clean_world_exits_zero(tmp_path, capsys):
    proot, rroot, base = _world(
        tmp_path,
        profiles={"a-engineer": _worker_soul("a-engineer")},
        specs={"a-engineer": "a-engineer"})
    rc = rsc.main(["--profiles-dir", proot, "--roles-dir", rroot,
                   "--base-identity", base, "--json"])
    assert rc == 0
    json.loads(capsys.readouterr().out)


# --------------------------------------------------------------------------- #
# The content pass (t_5b362400): the loss list a heading sweep cannot see.
# --------------------------------------------------------------------------- #
#
# The headline gap: a hand-patched PARAGRAPH under an EMITTABLE heading leaves
# the heading sweep silent, so every test below drives CONTENT, not headings.

def test_paragraph_under_an_emittable_heading_is_reported(tmp_path):
    """THE reason this pass exists. The heading is legal (`## Role:` is emitted),
    so the heading sweep returns a clean row while the sentence under it is
    exactly what the next `generate` deletes — the measurement t_0f2ee405 had to
    make with an ad-hoc script. Must be a problem line naming the line number and
    quoting the line, and must be a WARNING (ok False, fatal False)."""
    live_identity = ("spec identity text\n\n"
                     "Read docs/superpowers/specs/2026-10-26-module-design.md "
                     "before planning new work.")
    soul = _worker_soul("a-engineer", identity=live_identity)
    proot, rroot, base = _world(  # spec identity is only SPEC_IDENTITY
        tmp_path, profiles={"a-engineer": soul},
        specs={"a-engineer": "a-engineer"})
    res = rsc.check(proot, roles_dir=rroot, base_path=base)

    row = _row(res, "a-engineer")
    assert res["ok"] is False
    assert res["fatal"] is False, "a content loss is a WARNING, never fatal"
    assert row["content"]["compared"] is True
    assert row["content"]["lost_count"] == 1
    lost = row["content"]["lost"][0]
    assert "superpowers" in lost["text"]
    joined = "\n".join(row["problems"])
    assert f"SOUL line {lost['line']}" in joined, joined
    assert "cannot be reproduced by compose_soul()" in joined
    assert "deletes it" in joined
    # The operator must see it in the rendered detail, not only in --json.
    assert "superpowers" in res["detail"]
    assert res["lost_lines"] == 1


def test_a_duplicated_line_does_not_survive_by_coincidence(tmp_path):
    """TRAP (3a): a live line whose text matches identical text ELSEWHERE must
    NOT count as surviving — the positional opcode test, not a set difference.
    Fixture: a bullet generate already writes under a shared value, hand-patched
    a SECOND time under the role section. The positional pass flags the extra
    copy (a set difference would flag NOTHING, because the text is present);
    it is not a loss, because the first copy survives, but it is printed."""
    base_md = BASE_MD + "\n- keep every commit small\n"
    soul_lines = _worker_soul("a-engineer", base_md=base_md).splitlines()
    for i, ln in enumerate(soul_lines):
        if ln.startswith("spec identity text"):
            soul_lines.insert(i + 1, "- keep every commit small")
            break
    soul = "\n".join(soul_lines)
    proot, rroot, base = _world(
        tmp_path, profiles={"a-engineer": soul},
        specs={"a-engineer": "a-engineer"}, base=base_md)
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    row = _row(res, "a-engineer")
    c = row["content"]

    # The set-difference answer would be "nothing flagged at all": the bullet's
    # text IS present in the composed document. Prove that first.
    assert "- keep every commit small" in base_md
    # The positional pass disagrees: exactly one copy survives, the extra one
    # sits in a non-equal opcode and is reported (in `covered`, since losing a
    # duplicate loses no novel content — but it IS said, line number included).
    assert c["lost_count"] == 0, c
    assert c["covered_count"] == 1, c
    assert any("keep every commit small" in n for n in row["notes"]), row["notes"]


def test_copied_identity_lines_are_not_reported_as_losses(tmp_path):
    """TRAP (3b, first half): the copy-pasted `## Role:` identity is what
    `generate` EXISTS to replace, so its disappearance is the repair, not a
    loss. It must land in `identity`, be said in a note, and never become a
    problem line — or every drifted profile reports a wall of noise."""
    proot, rroot, base = _world(
        tmp_path,
        profiles={"pontaje-engineer": _worker_soul("bc-engineer")},
        specs={"pontaje-engineer": "pontaje-engineer"})
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    row = _row(res, "pontaje-engineer")

    # The header mismatch is still exactly one problem line...
    assert len([p for p in row["problems"] if "SOUL header" in p]) == 1
    # ...and the lines that carry it are classified, not listed as losses.
    assert row["content"]["identity_count"] >= 1
    assert row["content"]["lost_count"] == 0, row["content"]
    assert not any("cannot be reproduced" in p for p in row["problems"])
    assert any("identity" in n and "repair" in n for n in row["notes"])


def test_identity_bucket_does_not_swallow_novel_content(tmp_path):
    """The identity exemption requires a header mismatch AND no other missing
    word. A hand-patched paragraph that merely happens to mention the copied
    role name is still a genuine loss."""
    live = ("spec identity text\n\nAlso ask the bc-engineer role for the "
            "retirement runbook in docs/legacy/runbook.md.")
    proot, rroot, base = _world(
        tmp_path, profiles={"pontaje-engineer": _worker_soul(
            "bc-engineer", identity=live)},
        specs={"pontaje-engineer": "pontaje-engineer"})
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    row = _row(res, "pontaje-engineer")
    assert row["content"]["lost_count"] == 1
    assert "runbook" in row["content"]["lost"][0]["text"], row["content"]["lost"]


def test_shared_sandbox_inventory_line_is_intended_removal(tmp_path):
    """TRAP (3b, second half): the named shared/prod sandboxes t_8d22ec81 ruled
    must NOT come back. A live line whose missing tokens are ALL sandbox-shaped
    identifiers is intended removal — a note, never a problem. The rule is a
    SHAPE (an identifier carrying an underscore), never a name list, so this
    file does not name the operator's real sandboxes either."""
    live = ("spec identity text.\n\n"
            "POM_FIXTURE_ONE, POM_FIXTURE_TWO, FIXTURE_LIVE\n")
    proot, rroot, base = _world(
        tmp_path, profiles={"a-engineer": _worker_soul("a-engineer",
                                                       identity=live)},
        specs={"a-engineer": "a-engineer"})
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    row = _row(res, "a-engineer")
    assert row["content"]["inventory_count"] == 1, row["content"]
    assert row["content"]["lost_count"] == 0
    assert not any("cannot be reproduced" in p for p in row["problems"])
    assert any("sandbox" in n for n in row["notes"])
    # Masked in every surface, counted in --json.
    blob = json.dumps(res)
    assert "POM_FIXTURE_ONE" not in blob and "<sandbox>" in blob


def test_inventory_line_carrying_novel_prose_is_still_a_loss(tmp_path):
    """`all tokens are inventory-shaped`, not `any`: the real pontaje line
    carried the sandbox inventory AND the doc pointer. Classifying it as
    inventory because one token matched would hide the one genuine loss in the
    fleet — the same fail-open shape as t_0f2ee405's surviving mutant M3."""
    live = ("spec identity text\n\n"
            "POM_FIXTURE_ONE or POM_FIXTURE_TWO). Spec + plan in "
            "docs/superpowers/specs/2026-10-26-module-design.md.")
    proot, rroot, base = _world(
        tmp_path, profiles={"a-engineer": _worker_soul("a-engineer",
                                                       identity=live)},
        specs={"a-engineer": "a-engineer"})
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    row = _row(res, "a-engineer")
    assert row["content"]["inventory_count"] == 0
    assert row["content"]["lost_count"] == 1
    assert "superpowers" in row["content"]["lost"][0]["text"]


def test_rewrapped_line_is_counted_as_covered_not_lost(tmp_path):
    """A line that does not survive byte-for-byte but whose every word still
    appears in the composed text lost no NOVEL content (a re-wrap or reorder),
    so it is not a problem — but it is still printed with its line number,
    because the classification is the operator's to audit."""
    proot, rroot, base = _world(
        tmp_path, profiles={"a-engineer": _worker_soul(
            "a-engineer", identity="alpha beta gamma delta epsilon zeta\n")},
        specs={"a-engineer": "a-engineer"})
    (tmp_path / "roles" / "a-engineer.yaml").write_text(
        yaml.safe_dump({"name": "a-engineer",
                        "identity": "alpha beta gamma delta epsilon zeta\n",
                        "routing_description": "r."}), encoding="utf-8")
    (tmp_path / "profiles" / "a-engineer" / "SOUL.md").write_text(
        _worker_soul("a-engineer",
                     identity="alpha beta gamma\ndelta epsilon zeta\n"),
        encoding="utf-8")
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    row = _row(res, "a-engineer")
    assert row["content"]["lost_count"] == 0, row["content"]
    assert row["content"]["covered_count"] == 2
    assert res["ok"] is True, res["detail"]
    # Printed, not summarised away: each line shows with its line number.
    assert any("re-wrap" in n for n in row["notes"])
    assert any("L11:" in n and "alpha beta gamma" in n for n in row["notes"]), \
        row["notes"]
    assert "alpha beta gamma" in res["detail"]


def test_excluded_lines_are_printed_and_capped(tmp_path):
    """Two properties of the excluded-line rendering: the cap bounds what one
    row can print (a genuinely re-written block must not bury every other row),
    and the full list always survives into --json. The exact split between
    buckets for a duplicated block is difflib's anchor choice, so the invariant
    asserted is over ALL printed lines, not one bucket."""
    many = "\n".join(f"wordalpha{i} wordbeta wordgamma worddelta"
                     for i in range(rsc._NOTE_LINE_CAP + 3))
    proot, rroot, base = _world(
        tmp_path,
        # The live SOUL carries the block TWICE; the spec writes it once. The
        # extra copy is exactly what a set difference would have skipped.
        profiles={"a-engineer": _worker_soul(
            "a-engineer", identity=SPEC_IDENTITY + "\n" + many + "\n" + many)},
        specs={"a-engineer": "a-engineer"})
    (tmp_path / "roles" / "a-engineer.yaml").write_text(
        yaml.safe_dump({"name": "a-engineer",
                        "identity": SPEC_IDENTITY + "\n" + many + "\n",
                        "routing_description": "r."}), encoding="utf-8")
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    row = _row(res, "a-engineer")
    c = row["content"]
    flagged = (c["lost_count"] + c["covered_count"] + c["identity_count"]
               + c["inventory_count"])
    assert flagged >= rsc._NOTE_LINE_CAP + 3, c  # nothing silently dropped
    shown = [n for n in row["notes"] if "wordalpha" in n]
    assert shown, row["notes"]                      # printed, not hidden
    assert len(shown) <= rsc._NOTE_LINE_CAP, row["notes"]   # bounded
    assert any("full list in `--json`" in n for n in row["notes"])
    # The cap is a RENDERING cap: every flagged line is in --json.
    blob = json.dumps(c)
    for i in range(rsc._NOTE_LINE_CAP + 3):
        assert f"wordalpha{i}" in blob


def test_sandbox_names_are_masked_in_every_surface(tmp_path):
    """The check reads the operator's live SOUL, and the line it must quote is
    the one carrying the named shared/prod sandboxes. A verbatim rendering turns
    a `doctor --json` paste into the leak t_0f2ee405 recorded — so the identifier
    shape is masked in the row AND in the rendered detail AND in --json."""
    live = ("spec identity text\n\n"
            "Publish only to POM_FIXTURE_ONE — never POM_FIXTURE_TWO, and read "
            "docs/legacy/runbook.md first.")
    proot, rroot, base = _world(
        tmp_path, profiles={"a-engineer": _worker_soul("a-engineer",
                                                       identity=live)},
        specs={"a-engineer": "a-engineer"})
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    blob = json.dumps(res)
    assert "POM_FIXTURE_ONE" not in blob, "the loss list leaked a sandbox name"
    assert "POM_FIXTURE_TWO" not in blob
    assert "<sandbox>" in blob, "masking must be visible, not a silent drop"
    # The operator keeps everything needed to FIND the line.
    assert "docs/legacy/runbook.md" in blob
    assert "runbook" in res["detail"]


def test_content_pass_reports_without_any_heading_defect(tmp_path):
    """Additivity, the other direction: with ZERO heading problems the content
    pass still flips the verdict and still says how many lines are at risk in
    the summary — so a heading-only sweep can never read as 'content checked'."""
    proot, rroot, base = _world(
        tmp_path, profiles={"a-engineer": _worker_soul(
            "a-engineer", identity="spec identity text\n\n"
                                   "Own the widgets ledger in reports/ledger.md.")},
        specs={"a-engineer": "a-engineer"})
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    assert not any("heading" in p for p in _row(res, "a-engineer")["problems"])
    assert res["ok"] is False
    assert "1 live line(s) across 1 profile(s) cannot be reproduced" in \
        res["detail"].splitlines()[0]


def test_clean_soul_loses_nothing_and_says_so(tmp_path):
    """A SOUL byte-identical to what compose_soul writes must report zero in
    every bucket AND print the positive 'no live content would be lost' clause —
    the pair that keeps the loss count from being vacuously zero."""
    proot, rroot, base = _world(
        tmp_path, profiles={"a-engineer": _worker_soul("a-engineer")},
        specs={"a-engineer": "a-engineer"})
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    row = _row(res, "a-engineer")
    assert res["ok"] is True, res["detail"]
    assert res["lost_lines"] == 0
    c = row["content"]
    assert (c["compared"], c["lost_count"], c["covered_count"],
            c["identity_count"], c["inventory_count"]) == (True, 0, 0, 0, 0)
    assert row["notes"] == []
    assert "no live content would be lost by generate" in res["detail"]


def test_uncomposable_soul_says_the_content_pass_skipped(tmp_path):
    """Honest signal: if the base identity cannot be read there is nothing to
    diff against. The row must say the pass was SKIPPED (compared False + the
    reason) and the summary must not let the reader assume every profile's
    content was compared — never a silent pass."""
    proot, rroot, base = _world(
        tmp_path, profiles={"a-engineer": _worker_soul("a-engineer")},
        specs={"a-engineer": "a-engineer"})
    res = rsc.scan(proot, roles_dir=rroot, base_path=base, base_text=None)
    row = _row(res, "a-engineer")
    assert row["content"]["compared"] is False
    assert "unreadable" in row["content"]["reason"]
    assert any("SKIPPED" in n for n in row["notes"])


def test_spec_name_mismatch_defers_the_content_pass(tmp_path):
    """generate would stamp a different identity here, so every identity line
    differs by construction and the diff would be a wall of noise on top of the
    problem already reported. Deferred with a reason, never judged clean."""
    proot, rroot, base = _world(
        tmp_path, profiles={"pontaje-engineer": _worker_soul("pontaje-engineer")},
        specs={"pontaje-engineer": "bc-engineer"})
    res = rsc.check(proot, roles_dir=rroot, base_path=base)
    row = _row(res, "pontaje-engineer")
    assert row["content"]["compared"] is False
    # A deferred row must not even CARRY a loss count: `lost_count: 0` reads as
    # "checked, nothing lost", which is the false green this card is about.
    assert "lost_count" not in row["content"], row["content"]
    assert "fix that first" in row["content"]["reason"]
    assert any("SKIPPED" in n for n in row["notes"])


def test_content_diff_is_pure_and_positional():
    """Unit pin of the classifier itself: identical input -> zero everywhere,
    and a line appended at the END is the only thing reported."""
    a = "## Role: x\n\none two three\n\n## Operational\n\nops line\n"
    assert rsc.content_diff(a, a)["lost_count"] == 0
    b = a + "\nan extra sentence about the ledger\n"
    d = rsc.content_diff(b, a)
    assert d["lost_count"] == 1
    assert d["lost"][0]["line"] == len(b.splitlines())
    assert d["live_lines"] == len(b.splitlines())


# Recipe, not a random draw: a long boilerplate-heavy document (so difflib's
# popular-element heuristic ARMS — it only applies at len(b) >= 200) plus a line
# repeated 29 times, and ONE duplicate of that popular line inserted into the
# live text. Deterministic, so the assertions below are stable across runs.
_AJ_BOILER = [f"shared value number {i}" for i in range(6)]
_AJ_UNIQ = [f"unique prose line {i}" for i in range(10)]
_AJ_N, _AJ_K = 200, 32


def _autojunk_fixture():
    composed = "\n\n".join(_AJ_BOILER[i % 6] if (i % 7) else _AJ_UNIQ[i % 10]
                           for i in range(_AJ_N))
    lines = composed.splitlines()
    popular = lines[_AJ_K]
    live = "\n\n".join(lines[:_AJ_K] + [popular] + lines[_AJ_K:])
    return composed, live, popular


def test_autojunk_off_is_what_keeps_the_report_a_list_not_a_wall():
    """`autojunk=False` is load-bearing, and NOT for the reason one would guess.

    Measured over ~6000 boilerplate-heavy documents: the default heuristic never
    marked a genuinely novel line as equal (0 hits), so it does not hide losses.
    What it does is flag the ENTIRE document when a popular line is duplicated —
    784 of this fixture's 799 live lines instead of the 1 line that is actually
    the patch. That is precisely the untriageable wall the classification exists
    to prevent, and it also loses WHICH line the operator should fold into the
    spec. So the flag is pinned on the reported line number, not on a loss count.
    """
    composed, live, popular = _autojunk_fixture()
    assert len(live.splitlines()) >= 200, "the heuristic must be armed"
    assert live.splitlines().count(popular) > max(len(live.splitlines()) // 100, 1)

    d = rsc.content_diff(live, composed)
    assert d["lost_count"] == 0, d["lost"]
    assert (d["lost_count"] + d["covered_count"] + d["identity_count"]
            + d["inventory_count"]) == 1, (d["covered"], d["lost"])
    assert d["covered"][0]["text"] == popular

    # The mutant this pins: the same call with difflib's default left in place.
    import difflib
    cl, ll = composed.splitlines(), live.splitlines()
    sm = difflib.SequenceMatcher(None, cl, ll)   # autojunk defaults to True
    alive = [False] * len(ll)
    for tag, _a, _c, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for j in range(j1, j2):
                alive[j] = True
    default_flagged = sum(1 for x in alive if not x)
    assert default_flagged > 100, (
        "fixture no longer distinguishes the two settings — the pin below would "
        "pass on an implementation that dropped autojunk=False")


# --------------------------------------------------------------------------- #
# Derivation, not re-typing: the emittable set follows the modules that own it.
# --------------------------------------------------------------------------- #

def test_base_headings_read_from_the_file_not_a_hardcoded_list(tmp_path):
    """Add a heading to base-identity.md and the check must accept it without an
    edit here — the rule that keeps doctor and the generator from disagreeing.

    The fixture SOUL is built from the SAME extended base text the check reads,
    so a clean row here is clean for the content pass too, not just for the
    heading sweep."""
    extended = BASE_MD + "\n## A brand new shared value\n\nbody\n"
    p = tmp_path / "base3.md"
    p.write_text(extended, encoding="utf-8")
    assert "A brand new shared value" in rsc.base_headings(str(p))
    proot = tmp_path / "profiles" / "a-engineer"
    proot.mkdir(parents=True)
    (proot / "SOUL.md").write_text(
        _worker_soul("a-engineer", base_md=extended), encoding="utf-8")
    rroot = tmp_path / "roles3"
    rroot.mkdir()
    (rroot / "a-engineer.yaml").write_text(yaml.safe_dump(
        {"name": "a-engineer", "identity": SPEC_IDENTITY,
         "routing_description": "y."}), encoding="utf-8")
    res = rsc.check(str(tmp_path / "profiles"), roles_dir=str(rroot),
                    base_path=str(p))
    assert res["ok"] is True, res["detail"]
    assert res["lost_lines"] == 0


def test_ops_heading_set_comes_from_generator(tmp_path):
    """ops_headings() reads generator's three blocks; it must contain the ops
    heading the real SOULs carry (else every worker would be flagged)."""
    assert "Operational" in rsc.ops_headings()


def test_headings_helper_ignores_indented_fence_content():
    """An indented `## ` inside a fenced code block is content, not a heading."""
    text = "## Real\n\ntext\n\n    ## Fenced\n"
    assert rsc._headings(text) == ["Real"]
