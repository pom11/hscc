"""Bootstrap stage ORDER for the guarded review-skill fan-out (B1, round-2).

The reviewer measured that a fresh `hscc bootstrap` shipped UNREVIEWABLE
orchestrators: the guarded fan-out ran BEFORE `Install: role profiles` and
`Install: per-project orchestrators`, which CREATE the `*-orch` profiles with
empty skills trees — stage 2 saw zero reviewer-capable profiles, installed
only the root home, and every fresh orchestrator died on its first review-lane
spawn with the card's error string ('Unknown skill(s): sdlc-review').

These tests pin the ORDER statically (bootstrap.sh is bash; same approach as
test_orch_all_wiring.py) — the fan-out must run AFTER both provisioning
stages, stay warn-not-die, and keep its --skip-skills / --dry-run behaviour.
The behavioural half lives where the bug was observable:
test_bootstrap_isolated asserts every *-orch the real installer just created
resolves sdlc-review, and hscc-roles/tests/test_orchestrators.py asserts
ensure_orchestrator seeds a profile created by `orch <new-project>`.
"""
import os

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BOOT_SH = os.path.join(_PLUGIN_DIR, "bootstrap.sh")


def _read_bootstrap():
    with open(_BOOT_SH, encoding="utf-8") as f:
        return f.read()


def test_guarded_fanout_runs_after_both_provisioning_stages():
    text = _read_bootstrap()
    fan_pos = text.index("install_review_skill.py\" --skill all")
    gen_pos = text.index('hscc.py" generate')
    orch_pos = text.index('hscc.py" orch-all')
    assert fan_pos > gen_pos, "review-skill fan-out must run AFTER role profiles"
    assert fan_pos > orch_pos, "review-skill fan-out must run AFTER orch-all"


def test_guarded_fanout_warns_and_respects_skip_skills():
    text = _read_bootstrap()
    fan_pos = text.index("install_review_skill.py\" --skill all")
    seg_start = text.rindex('hdr "Install: review skills', 0, fan_pos)
    seg_end = text.index("fi", fan_pos) + 2
    seg = text[seg_start:seg_end]
    assert "die" not in seg              # warn-not-die (operator data to adjudicate)
    assert "$SKIP_SKILLS" in seg         # --skip-skills honoured
    assert "$DRY_RUN" in seg             # --dry-run plans without writing
    assert "install_review_skill.py" in seg


def test_no_duplicate_fanout_stage_before_provisioning():
    """A SECOND copy of the stage re-introduced above provisioning would
    silently resurrect B1 (an early stage reads green for a home where the
    profiles do not exist yet). Exactly one invocation, after both stages."""
    text = _read_bootstrap()
    assert text.count("install_review_skill.py\" --skill all") == 1
