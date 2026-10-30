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


def _worker_soul(role_header, *, extra_headings=()):
    """A worker SOUL shaped like compose_soul()'s output."""
    blocks = ["# base", "## Shared value one", "body", "## Shared value two",
              "body", f"## Role: {role_header}", "role identity text",
              WORKER_OPS.format(name=role_header)]
    for h in extra_headings:
        blocks.append(f"## {h}\n\nhand-patched body\n")
    return "\n\n".join(blocks) + "\n"


def _world(tmp_path, *, profiles, specs, base=BASE_MD):
    """Build tmp profiles/ + roles/ + base-identity.md; return the three paths.

    ``profiles`` maps dir name -> SOUL text (or None for a dir with no SOUL.md);
    ``specs`` maps spec stem -> the spec's ``name:`` field (identity text is a
    stub, which the heading sweep only needs for attribution).
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
                            "identity": "spec identity text.\n",
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
        "role identity text",
        "role identity text\n\n## Role: someone-else\n\nbogus second identity")
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
        "role identity text",
        "role identity text\n\n## Role: a-engineer\n\nduplicated block")
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
            .replace("role identity text",
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
    assert "hscc roles generate" in res["fix"]
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
# Derivation, not re-typing: the emittable set follows the modules that own it.
# --------------------------------------------------------------------------- #

def test_base_headings_read_from_the_file_not_a_hardcoded_list(tmp_path):
    """Add a heading to base-identity.md and the check must accept it without an
    edit here — the rule that keeps doctor and the generator from disagreeing."""
    p = tmp_path / "base3.md"
    p.write_text(BASE_MD + "\n## A brand new shared value\n\nbody\n",
                 encoding="utf-8")
    assert "A brand new shared value" in rsc.base_headings(str(p))
    proot = tmp_path / "profiles" / "a-engineer"
    proot.mkdir(parents=True)
    (proot / "SOUL.md").write_text(
        _worker_soul("a-engineer").replace(
            "# base", "# base\n\n## A brand new shared value\n\nbody"),
        encoding="utf-8")
    rroot = tmp_path / "roles3"
    rroot.mkdir()
    (rroot / "a-engineer.yaml").write_text(yaml.safe_dump(
        {"name": "a-engineer", "identity": "x.\n", "routing_description": "y."}),
        encoding="utf-8")
    res = rsc.check(str(tmp_path / "profiles"), roles_dir=str(rroot),
                    base_path=str(p))
    assert res["ok"] is True, res["detail"]


def test_ops_heading_set_comes_from_generator(tmp_path):
    """ops_headings() reads generator's three blocks; it must contain the ops
    heading the real SOULs carry (else every worker would be flagged)."""
    assert "Operational" in rsc.ops_headings()


def test_headings_helper_ignores_indented_fence_content():
    """An indented `## ` inside a fenced code block is content, not a heading."""
    text = "## Real\n\ntext\n\n    ## Fenced\n"
    assert rsc._headings(text) == ["Real"]
