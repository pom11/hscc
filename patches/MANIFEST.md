# HSCC upstream patch set

HSCC runs **official** hermes + sparkrun. The local edits we depend on are
captured here as `git format-patch` artifacts and reapplied onto a fresh
upstream checkout by `apply_patches.py`. This replaces maintaining long-lived
forks (the "painful updates" problem): pull the upstream release, reapply the
small curated delta, done.

## hermes/ — applies onto NousResearch/hermes-agent

The kanban review-flow feature (powers HSCC's WS4 autonomous review gate).
Isolated to 6 files: `tools/kanban_tools.py`, `hermes_cli/kanban_db.py`,
`hermes_cli/kanban_decompose.py`, `agent/prompt_builder.py`, + 2 tests.

| Patch | What |
|-------|------|
| 0001 | fix(kanban): rewrite unknown create-assignee to default_assignee |
| 0002 | feat(kanban): pure review-pairing transform for decompose |
| 0003 | feat(kanban): auto_review config policy reader |
| 0004 | feat(kanban): wire review-pairing into decompose_task (policy-gated) |
| 0005 | refactor(kanban): use built-in review path; stop Phase-2 auto-pairing |
| 0006 | feat(kanban): kanban_submit_review tool + running->review transition |

Excluded from the curated set (not HSCC-essential): the holographic-memory
plugin removal and the Jun-3 autostash recovery commit — these were local
cleanup, not features HSCC needs to carry forward.

## sparkrun/ — applies onto spark-arena/sparkrun

**Empty — all sparkrun patches landed upstream.**

- `0001` (restart policy → `unless-stopped`) merged upstream as `9e4513f`.
- `0002` (OpenClaw 2026.5.19 compat) merged upstream as `37a7bdb`.

The patch directory exists but contains zero `.patch` files. `apply_patches.py`
returns `ok: true` for empty sets so bootstrap stays quiet.

## Recipes (not patches)

Recipe edits stay as the `~/.sparkrun-local/recipes/local-fixed/` overlay (the
sanctioned no-edit-official pattern) — sparkrun reads local recipes by path, so
they need no upstream patching.

## Updating

```
# dry-run: check the patches still apply onto current upstream
hscc-bootstrap/apply_patches.py --check

# apply onto a target checkout
hscc-bootstrap/apply_patches.py --target ~/.hermes/hermes-agent --set hermes
hscc-bootstrap/apply_patches.py --target ~/sparkrun --set sparkrun
```

When a patch fails to apply (upstream moved the code), the script reports the
conflicting patch + file so it can be regenerated from the rebased branch.