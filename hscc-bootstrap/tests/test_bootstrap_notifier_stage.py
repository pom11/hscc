"""The `Install: snap firmware-notifier mask` stage (card t_61850b24).

Static-only by necessity, and that posture is the point: a *real* run of
bootstrap.sh resolves the real Hermes venv and a real ``sparkrun`` on PATH, so
executing the apply path here would mask the operator's live fleet. A sandboxed
HOME does not contain a remote node. So the wiring is asserted against the
script text — the same posture test_bootstrap_node_recovery_stage.py takes for
its sibling stage — while the mask logic itself is covered dynamically (and
bash-backed) in test_install_notifier_mask.py.

Pinned here, because each of these is a way the stage could be silently wrong:

  * the stage exists and is terminated by the next header (a botched insertion
    can splice it into a neighbouring stage's ``if`` chain);
  * it delegates to the deployed module — bootstrap must not grow a second
    implementation, and must never hand-roll ssh (sparkrun owns the fleet);
  * hosts travel as one quoted argv value, never interpolated shell;
  * it is non-fatal but never silent, and a failure keeps printing per-node
    detail instead of replacing it with a dump;
  * both opt-outs are honoured ($SKIP_NODE_RECOVERY and the env guard, which is
    what keeps a sandboxed harness off the live fleet);
  * dry-run PLANS and executes no interpreter at all.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO / "hscc-bootstrap" / "bootstrap.sh"
SRC = BOOTSTRAP.read_text()

STAGE = "Install: snap firmware-notifier mask"


def _stage_body():
    m = re.search(rf'hdr "{re.escape(STAGE)}.*?\n(hdr "Install: hermes)',
                  SRC, re.S)
    assert m, f"the notifier-mask stage is absent or not terminated by the expected header"
    return m.group(0)


def test_stage_exists_and_flag_is_advertised():
    _stage_body()
    assert "--skip-node-recovery) SKIP_NODE_RECOVERY=true ;;" in SRC
    help_line = [ln for ln in SRC.splitlines() if "--help|-h)" in ln]
    assert help_line and "--skip-node-recovery" in help_line[0], \
        "the flag must be advertised in bootstrap --help"


def test_stage_delegates_to_the_deployed_module():
    """One implementation. bootstrap calls install_notifier_mask.py; it does not
    re-implement masking, and it never ssh's (sparkrun owns the fleet)."""
    body = _stage_body()
    assert "install_notifier_mask.py" in body
    assert "systemctl" not in body, \
        "the stage must not compose systemctl itself — the module owns the bus-correct call"
    assert "ssh " not in body, "bootstrap must not hand-roll ssh"


def test_stage_passes_hosts_as_argv_not_interpolated_shell():
    body = _stage_body()
    assert re.search(r'--hosts "\$HOSTS_CSV"', body), \
        "hosts must pass as one quoted argv value"
    assert "eval" not in body


def test_stage_honours_both_opt_outs():
    body = _stage_body()
    assert "$SKIP_NODE_RECOVERY" in body
    assert "HSCC_BOOTSTRAP_SKIP_NODE_RECOVERY" in body, \
        "the env guard is what keeps a sandboxed harness off the live fleet"


def test_dry_run_branch_only_plans():
    body = _stage_body()
    dry = re.search(r"elif \$DRY_RUN; then\n(.*?)\nelif", body, re.S)
    assert dry, "no dry-run branch in the stage"
    assert "plan " in dry.group(1)
    assert "$PYBIN" not in dry.group(1), \
        "dry-run must not EXECUTE the module at all (no interpreter call)"
    assert "NOTHING" in dry.group(1), \
        "the dry-run plan line must say plainly that it writes nothing"
    assert "snap itself untouched" in dry.group(1), \
        "the plan must state the snap is out of scope, so an operator reading " \
        "--dry-run knows the blast radius before anything runs"


def test_missing_module_is_reported_not_treated_as_success():
    body = _stage_body()
    assert "install_notifier_mask.py missing" in body, \
        "a missing module must warn, not silently pass the stage green"


def test_stage_is_non_fatal_but_never_silent():
    """The failure mode that matters is a false green: an unmasked node must
    warn and must still print its per-node line."""
    body = _stage_body()
    assert "warn" in body
    assert "NOT masked on every node" in body
    assert "die " not in body, "a per-node fault must not abort the install"
    assert "OK\\|*" in body and "WARN\\|*" in body, \
        "per-node OK|/WARN| lines must be rendered on both exit paths"


def test_opt_out_key_matches_the_module_and_the_siblings():
    """bootstrap.sh comments, the module and the doctor all name the same
    subkey; a typo here means an operator's opt-out silently does nothing."""
    body = _stage_body()
    assert "node_recovery.firmware_notifier_mask=false" in body
    import install_notifier_mask as nm
    assert nm.MASK_SUBKEY == "firmware_notifier_mask"


def test_stage_order_after_watchdog_before_patches():
    """Placement is part of the contract: both node-recovery stages run before
    the patch/plugin stages, so a fresh install reports a masked fleet."""
    i_watchdog = SRC.index('hdr "Install: node hardware watchdog')
    i_notifier = SRC.index(f'hdr "{STAGE}')
    i_patches = SRC.index('hdr "Install: hermes/sparkrun patches"')
    assert i_watchdog < i_notifier < i_patches
