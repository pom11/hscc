# hscc-skills

Idempotent installer for HSCC's bundled skills + templates into `~/.hermes/skills/`
and `~/.hermes/templates/`. The skill **sources** are vendored in
[`../install/hscc-skills/`](../install/README.md) (the source of truth);
`hscc.py` copies the ones in `BUNDLED_SKILLS` into the live Hermes dirs,
hash-skipping already-installed files.

## CLI (`hscc.py`)
```
hscc.py install            # skills + templates (full)
hscc.py install-skills     # skills only
hscc.py status             # installed / missing / out-of-date
hscc.py uninstall          # reverse install
```

It locates the source by walking up from `__file__` to `<repo>/install/hscc-skills`,
so it works from both the repo and the installed runtime path.
