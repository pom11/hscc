"""Cross-validate the resolver MIRROR against the LIVE hermes resolver.

Round-2 review N3 corrected the handoff's "9/9 ALL-AGREE" claim: the reviewer's
independent 9-layout cross-validation disagreed on 3 layouts — the axes the
first cut's sample never crossed (equal-rank identical copies B2, BOM'd
frontmatter S1, skills.disabled S2). This test makes that cross-validation a
standing regression gate instead of a claim: every fabricated layout is judged
BOTH by skill_guard.resolution() (the mirror doctor/installer trust) AND by
the real ``agent.skill_commands.build_preloaded_skills_prompt`` subprocess,
and the verdicts must agree on the question that matters — does a spawn
asking for the bare name die (missing) or not.

Skipped when the hermes checkout / venv is absent (a deployed plugin dir
without a sibling hermes-agent still runs the rest of the suite).

Project-tier caveat (N2, stated for the PR body): the live resolver ALSO
scans project-local skill dirs (cwd/.hermes/skills, cwd/.agents/skills —
agent.skill_utils.get_project_skills_dirs) and prefers them. The mirror does
not model that tier. The subprocess runs with cwd=fabricated home (which has
no such dirs), so these layouts compare exactly the tier both sides scan
(profile skills + external dirs); no hscc-managed repo on this fleet carries
project skill dirs today, and if one later does, the mirror's error direction
is conservative (it can report a shadowed copy as colliding, never a
collision as ok).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import skill_guard

REPO = Path(__file__).resolve().parent.parent.parent
REAL_VENV_PY = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
REAL_HERMES_AGENT = Path.home() / ".hermes" / "hermes-agent"

needs_hermes = pytest.mark.skipif(
    not (REAL_VENV_PY.is_file() and REAL_HERMES_AGENT.is_dir()),
    reason="needs the real Hermes checkout + venv to consult the live resolver")

SKILL = "sdlc-review"

#: The probe: load the bare name through the same entry point the dispatcher
#: spawn uses (oneshot -> build_preloaded_skills_prompt) and report whether a
#: spawn can load it. HERMES_HOME + cwd are set by the caller.
#:
#: Do NOT test membership of the bare name in `missing`. A same-tier name
#: collision is not reported as the bare identifier: hermes keeps the bare name
#: OUT of `missing` and puts a labelled refusal in its place —
#: "Ambiguous skill name <id>: use one of <paths>" (agent/skill_commands.py
#: `ambiguous_skill_label`). `agent.skill_utils.AMBIGUOUS_SKILL_PREFIX` is
#: hermes' own "one spelling" for every same-tier refusal, so the probe imports
#: it from the live checkout instead of hard-coding the wording, and any THIRD
#: spelling comes back as `unrecognised` so the test fails loudly instead of
#: silently reading a refusal as a successful load. (The bare-name membership
#: test is exactly what made five layouts here fail as "fixture expectation
#: drifted from the live resolver" once hermes moved from bare-name-missing to
#: labelled refusal.)
_PROBE = '''
import json, sys
sys.path.insert(0, sys.argv[1])
from agent.skill_commands import build_preloaded_skills_prompt
from agent.skill_utils import AMBIGUOUS_SKILL_PREFIX as PREFIX
SKILL = "__SKILL__"
missing = sorted(build_preloaded_skills_prompt([SKILL])[2])
refusal = PREFIX + SKILL + ":"
print(json.dumps({
    "missing": missing,
    "refused": any(m == SKILL or m.startswith(refusal) for m in missing),
    "unrecognised": [m for m in missing
                     if m != SKILL and not m.startswith(refusal)],
}))
'''.replace("__SKILL__", SKILL)


def _mk(root: Path, rel: str, content: str):
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(content, encoding="utf-8")
    return d


def _real_verdict(home: Path) -> bool:
    """True when a spawn CAN load the bare name (live resolver says present).

    The refusal is read from the resolver's OWN vocabulary (see _PROBE): a
    layout can fail to load either because the name is absent (`missing` holds
    the bare identifier) or because it collides (`missing` holds an
    ``Ambiguous skill name`` label instead). Anything the probe could not
    classify aborts here rather than defaulting to "loads".
    """
    probe = home / "_probe.py"
    probe.write_text(_PROBE, encoding="utf-8")
    out = subprocess.run(
        [str(REAL_VENV_PY), str(probe), str(REAL_HERMES_AGENT)],
        capture_output=True, text=True, env={**os.environ, "HERMES_HOME": str(home)},
        cwd=str(home), timeout=120)
    assert out.returncode == 0, f"probe failed: {out.stderr[-800:]}"
    payload = json.loads(out.stdout.strip())
    assert not payload["unrecognised"], (
        "the live resolver reported this layout with a missing-spelling this "
        f"test cannot classify: {payload['unrecognised']!r}. Read "
        "agent.skill_utils.AMBIGUOUS_SKILL_PREFIX (the one spelling of every "
        "same-tier refusal) and teach _PROBE before trusting any verdict — "
        "defaulting an unknown shape to 'loads' is the blind spot that hid "
        "five refused layouts as fixture drift.")
    return not payload["refused"]


# ── fabricated layouts: (name, skills {rel: content}, config.yaml | None) ───
# The verdict axis: can a spawn load the bare name?
LAYOUTS = [
    # (label, skills tree, config text, spawn-loads?)
    ("clean single category copy", {"devops/sdlc-review": "cat\n"}, None, True),
    ("absent entirely", {}, None, False),
    ("flat vs category DIFFERENT content (June collision)",
     {"sdlc-review": "june\n", "devops/sdlc-review": "sept\n"}, None, False),
    ("flat + category byte-identical (ranks differ -> resolves)",
     {"sdlc-review": "SAME\n", "devops/sdlc-review": "SAME\n"}, None, True),
    ("two categories byte-identical (EQUAL rank -> refuses)  # B2",
     {"catA/sdlc-review": "SAME\n", "catB/sdlc-review": "SAME\n"}, None, False),
    ("two categories DIFFERENT content -> refuses",
     {"catA/sdlc-review": "a\n", "catB/sdlc-review": "b\n"}, None, False),
    ("BOM'd frontmatter renames a dir onto the name  # S1",
     {"catA/x": "\ufeff---\nname: sdlc-review\n---\nbody\n",
      "catB/sdlc-review": "other\n"}, None, False),
    ("frontmatter name beyond the old 40-line window  # S1",
     {"catA/renamed": "---\n"
      + "".join(f"pad{i}: v\n" for i in range(60))
      + "name: sdlc-review\n---\nbody\n",
      "catB/sdlc-review": "other\n"}, None, False),
    ("present but skills.disabled  # S2",
     {"devops/sdlc-review": "cat\n"},
     "skills:\n  disabled: [sdlc-review]\n", False),
    ("frontmatter name matches dir, single copy resolves",
     {"devops/whatever": "---\nname: sdlc-review\n---\nx\n"}, None, True),
    ("legacy flat <name>.md only", {}, None, True),  # filled below
]


@needs_hermes
@pytest.mark.parametrize("label,tree,cfg,spawn_loads", LAYOUTS,
                         ids=[l[0] for l in LAYOUTS])
def test_mirror_agrees_with_live_resolver(tmp_path, label, tree, cfg, spawn_loads):
    home = tmp_path / "home"
    skills = home / "skills"
    skills.mkdir(parents=True)
    for rel, content in tree.items():
        _mk(skills, rel, content)
    if label.startswith("legacy flat"):
        (skills / f"{SKILL}.md").write_text("legacy\n", encoding="utf-8")
    if cfg:
        (home / "config.yaml").write_text(cfg, encoding="utf-8")

    # 1) the live resolver (ground truth): does a spawn load the bare name?
    assert _real_verdict(home) is spawn_loads, (
        f"fixture expectation drifted from the live resolver for {label!r}")

    # 2) the mirror must answer the SAME question.
    res = skill_guard.resolution(
        skill_guard.profile_skill_roots(home), SKILL,
        disabled=skill_guard.disabled_skills_for_home(home))
    mirror_loads = res["state"] == "ok"
    assert mirror_loads is spawn_loads, (
        f"mirror {res['state']!r} vs live spawn verdict for {label!r}: "
        f"loads={spawn_loads}")
