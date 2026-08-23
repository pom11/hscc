"""Bootstrap wiring for per-project orchestrators (C6).

bootstrap.sh is bash, so we test its wiring statically here: read the script
and assert that the `orch-all` provisioning step is present, sits immediately
after the role-generation step, and lives INSIDE the same `--skip-roles` guard
(so `--skip-roles` skips both).

The step must be non-fatal (never `die` on it) and run after the plugin payload
is copied into $PLUGINS (which is where hscc-roles/hscc.py lives at runtime).
"""
import os
import re

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BOOT_SH = os.path.join(_PLUGIN_DIR, "bootstrap.sh")


def _read_bootstrap():
    with open(_BOOT_SH, encoding="utf-8") as f:
        return f.read()


def test_orch_all_step_present_and_after_role_generation():
    text = _read_bootstrap()
    # The real invocation line (not the explanatory comment above it) targets
    # the deployed plugin's hscc.py with the `orch-all` verb.
    assert 'hscc-roles/hscc.py" orch-all >/dev/null' in text
    # It must appear AFTER the role-generation step (which is its prerequisite
    # sibling), not before it.
    gen_pos = text.index('hscc.py" generate')
    orch_pos = text.index('hscc.py" orch-all')
    assert orch_pos > gen_pos


def test_orch_all_inside_skip_roles_guard():
    text = _read_bootstrap()
    # The guard uses `if $SKIP_ROLES; ... else ... fi`. Locate the orch-all
    # command's enclosing guard block: the `else` that follows the `if
    # $SKIP_ROLES` open (scanning backward from the command), up to the `fi`
    # that closes it (scanning forward). The command must sit between them.
    orch_pos = text.index('hscc.py" orch-all')
    guard_else = text.rindex("if $SKIP_ROLES; then warn \"skipped\"; else",
                             text.index("Install: role profiles"), orch_pos)
    guard_end = text.index("fi", orch_pos) + len("fi")
    segment = text[guard_else:guard_end]
    assert "if $SKIP_ROLES;" in segment
    # `--skip-roles` skips BOTH provisioning stages together (the orch-all
    # command is inside its own $SKIP_ROLES guard block).
    assert 'hscc.py" orch-all' in segment


def test_orch_all_never_dies():
    """The step is non-foundational — it must warn, never hard-stop bootstrap."""
    text = _read_bootstrap()
    orch_pos = text.index('hscc.py" orch-all')
    # Scan forward to the `fi` that closes the orch-all guard block.
    seg_end = text.index("fi", orch_pos) + len("fi")
    tail = text[orch_pos:seg_end]
    assert "die" not in tail            # never a hard stop
    assert '&& ok "' in tail            # success reporting
    assert '|| warn "' in tail          # failure reporting (warn, not die)


def test_orch_all_registered_in_hscc_cli():
    """The command is real — hscc.py must dispatch `orch-all`."""
    hscc_py = os.path.join(_PLUGIN_DIR, "..", "hscc-roles", "hscc.py")
    with open(hscc_py, encoding="utf-8") as f:
        cli_text = f.read()
    # dispatched in main() and advertised in the module docstring
    assert 'if cmd == "orch-all"' in cli_text
    assert "orch-all" in cli_text.split('"""')[1]  # docstring mentions it
