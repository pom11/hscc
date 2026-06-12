# hscc-roles

The role framework. Each role is a spec file (`roles/<name>.yaml`); a generator
builds it into a Hermes profile with a **layered SOUL** (shared base character +
role disposition + thin operational facts) and the full Hermes toolset **minus
cluster control** — only the orchestrator can change cluster shape.

## CLI (`hscc.py`)
```
hscc.py generate            # build all profiles from specs → ~/.hermes/profiles/
hscc.py create <name> "…"   # author a new role on demand
hscc.py list                # roles + whether a profile exists
hscc.py autonomy [on|off]   # the ~/.hscc/autonomy master flag
```

## Roster (`roles/`)
A full org: orchestrator, architect, coder, **reviewer**, qa; backend/frontend/
devops/security/data engineers; ml-engineer, ml-researcher, data-scientist;
product-manager, technical-writer, ux-designer; financial-analyst,
business-analyst, market-researcher; content-writer, social-media-manager,
project-manager. New roles are minted with `create` — the roster is data, not code.

`coder` blocks finished work for review; `reviewer` adversarially checks
diff + tests + spec and approves only when all three pass (powers the WS4
`auto_review` gate). Profiles are build artifacts (not tracked) — regenerate
with `generate`.

Tests: `tests/` — `python -m pytest tests/ -q`.
