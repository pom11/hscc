# hscc-skills

Idempotent installer for HSCC's bundled skills + templates into `~/.hermes/skills/`
and `~/.hermes/templates/`. The skill **sources** are vendored in
[`../install/hscc-skills/`](../install/README.md) (the source of truth);
`hscc.py` copies the ones in `BUNDLED_SKILLS` into the live Hermes dirs. A file
that already matches the repo is left alone; an hscc-**owned** skill whose
installed copy differs is snapshotted to `<home>/skills-backups/<name>.bak-<stamp>`
and then overwritten, so an existing copy is never a reason to skip (t_45fa38e2).
Names outside `BUNDLED_SKILLS` are never written, moved or removed.

## CLI (`hscc.py`)
```
hscc.py install            # skills + templates (full)
hscc.py install-skills     # skills only
hscc.py status             # installed / missing / out-of-date
hscc.py uninstall          # reverse install
```

It locates the source by walking up from `__file__` to `<repo>/install/hscc-skills`,
so it works from both the repo and the installed runtime path.
