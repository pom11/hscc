# hscc-skills

Idempotent installer for HSCC's bundled skills + templates into `~/.hermes/skills/`
and `~/.hermes/templates/`. The skill **sources** are vendored in
[`../install/hscc-skills/`](../install/README.md) (the source of truth);
`hscc.py` copies the ones in `BUNDLED_SKILLS` into the live Hermes dirs. A file
that already matches the repo is left alone; an hscc-**owned** skill whose
installed copy differs is snapshotted to `<home>/skills-backups/<name>.bak-<stamp>`
and then overwritten, so an existing copy is never a reason to skip (t_45fa38e2).
Names outside `BUNDLED_SKILLS` are never written, moved or removed.

The cluster-control skills (`hscc`, `hscc-cluster`, `hscc-model-onboard`) are
**main-profile only** (R4, t_b52e70ba). They install to
`<root>/profiles/<main_orch.main_name()>/skills/`; copies already sitting in the
root home or any other profile are MOVED (never deleted) to that home's
`skills-backups/removed-<stamp>/`. The generic/work skills keep installing at
`HERMES_HOME/skills/`.

The vendored tree and that list are the **same set**: `install/hscc-skills/` holds
13 skill dirs and `BUNDLED_SKILLS` names all 13, so every vendored skill is installed
and updated. The full vendored inventory is in
[`install/README.md`](../install/README.md); `BUNDLED_SKILLS` stays the only authority
for what this installer touches, and
`hscc_daemon/tests/test_surface_coverage_gates_t_c13de3cc.py` gates that the two
directions agree (a directory nobody installs and a name with no directory both fail).

## CLI (`hscc.py`)
```
hscc.py install            # skills + templates (full)
hscc.py install-skills     # skills only
hscc.py status             # installed / missing / out-of-date
hscc.py uninstall          # reverse install
```

It locates the source by walking up from `__file__` to `<repo>/install/hscc-skills`,
so it works from both the repo and the installed runtime path.
