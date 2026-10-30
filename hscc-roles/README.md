# hscc-roles

The role framework. Each role is a spec file (`roles/<name>.yaml`); a generator
builds it into a Hermes profile with a **layered SOUL** (shared base character +
role disposition + thin operational facts) and the full Hermes toolset **minus
cluster control** — only the orchestrator can change cluster shape.

## When you touch it

New role specs / SOUL layering / the orchestrator-provisioning layer. A role is
data: mint one with `create`, adjust it in `roles/<name>.yaml`, rebuild with
`generate`.

## CLI (`hscc.py`)
```
hscc.py generate            # build all profiles from specs → ~/.hermes/profiles/
hscc.py create <name> "…"   # author a new role on demand
hscc.py list                # roles + whether a profile exists
hscc.py validate            # validate every role spec (load + required fields)
hscc.py autonomy [on|off]   # the ~/.hscc/autonomy master flag
hscc.py orch <project|general>  # ensure a project's orchestrator profile exists
hscc.py orch-all [--registry P] # ensure an orchestrator for EVERY registry
                                # project + the `general` catch-all (idempotent, bulk)
hscc.py provision-check [--registry P] [--profile NAME] [--json]
                            # per-profile effective memory provider / char
                            # limit / compaction cap / summarization endpoint
```

## Per-project orchestrators (`orchestrators.py`)

**One project = one orchestrator profile + one long-lived session + one memory
+ one kanban board.** The boards already exist; `orchestrators.py` adds the
profile+session layer. The project list comes from the registry file —
`~/.flightdeck/registry.yaml`, the hscc-project home directory (never a
hardcoded list; override with `--registry PATH` or `HSCC_REGISTRY`).

Conventions (do not invent alternatives):
- orchestrator profile: `<project>-orch`   (e.g. `ecofire-app-orch`)
- long-lived session:   `<project>`         (`hermes -p <P>-orch chat --continue <P>`)
- board:                the project's existing board from the registry
- catch-all:            `general` → `general-orch` / `general` / `default`
                        (repo=None, not repo-scoped) — the DEFAULT when no
                        project is specified.

Two importable entry points (the HSCC API imports these):
- `orchestrators.resolve_orchestrator(project=None, path=None)` →
  `{"project","profile","session","board","repo-or-None"}`; raises
  `UnknownProjectError` for an unknown project.
- `orchestrators.ensure_orchestrator(project=None, base_identity=..., path=None)`
  → idempotent; re-running is a no-op and never clobbers an existing profile's
  memory or sessions. Built on the same `generator.generate_profile` machinery
  as the shipped role profiles (fresh EMPTY memory store — a per-project
  orchestrator NEVER inherits a shared role's cross-project memory). The
  profile's SOUL states its project, repo path, and board from the registry,
  and carries the kanban + delegation toolsets an orchestrator needs.

Provision one with `hscc.py orch <project>`; provision the whole registry at
once with `hscc.py orch-all` (bootstrap calls it during install). Verify the
result with `hscc.py provision-check`.

## Roster (`roles/`)

A full org (26 specs): orchestrator, architect, coder, **reviewer**, qa;
backend/frontend/devops/security/data engineers; ml-engineer, ml-researcher,
data-scientist; **ios-engineer**, **ecofire-bc-engineer**,
**ecofire-pontaje-engineer**; product-manager, technical-writer, ux-designer;
financial-analyst, business-analyst, market-researcher; content-writer,
social-media-manager, project-manager; worker. `ls roles/` is the census. New
roles are minted with `create` — the roster is data, not code.

`coder` blocks finished work for review; `reviewer` adversarially checks
diff + tests + spec and approves only when all three pass (powers the WS4
`auto_review` gate). Profiles are build artifacts (not tracked) — regenerate
with `generate`.

## Role SOUL drift (`role_soul_check.py`)

`generate` loops over **specs**, so a profile dir that was copy-pasted from
another one keeps the SOURCE's identity and nothing notices: the live
`ecofire-pontaje-engineer` ran its whole life under `## Role:
ecofire-bc-engineer` while its spec existed and
`test_every_hscc_owned_worker_profile_has_role_spec` was green — an existence
test cannot see identity drift. `role_soul_check.py` reports what an existence
test cannot: a `## Role:` header that disagrees with its profile dir name or
with `roles/<name>.yaml`, a missing/invalid spec, and any `## ` heading
`compose_soul()` cannot emit (a hand-patched block the next `generate` drops —
fold it into the spec's `identity` first).

It is a **reporter, not a pytest assertion**. Live profile state belongs where
a fault is surfaced to the operator: `doctor_role_soul.py` registers it as a
non-fatal WARNING in `hscc doctor`. The repair — `hscc roles generate`
re-stamps every generated profile and rewrites live profile state — is an
operator action the check deliberately never takes.

Orchestrator-family SOULs are excluded by design: `orchestrators.py` stamps a
per-project orch with the name of the SHARED spec it passes (`general-orch`
legitimately carries `## Role: orchestrator`), so judging it by dir name would
red-flag a layout that is correct by construction.

## How to test / Links

`tests/` — from this dir: `python -m pytest tests/ -q`.
Links: [registry + boards → hscc-project](../hscc-project/README.md) ·
[daemon escalation](../hscc_daemon/README.md)
