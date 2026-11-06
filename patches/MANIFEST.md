# HSCC upstream patch set

HSCC runs **official** hermes + sparkrun. The local edits we depend on are
captured here as `git format-patch` artifacts and reapplied onto a fresh
upstream checkout by `apply_patches.py`. This replaces maintaining long-lived
forks (the "painful updates" problem): pull the upstream release, reapply the
small curated delta, done.

## hermes/ — applies onto NousResearch/hermes-agent

**Re-based onto v0.23.0.** The kanban review-flow feature that powered
HSCC's WS4 autonomous-review gate was absorbed upstream — upstream now ships
`kanban_request_review` / `request_review` / `claim_review_task` /
`request_changes` + KANBAN_GUIDANCE coverage (strict superset of the fork's
`kanban_submit_review`), so the old 0006 patch is **dropped** (documented
below). What remains carried is the unabsorbed delta:

| Patch | What |
|-------|------|
| 0001 | fix(kanban): rewrite unknown create-assignee to default_assignee (`tools/kanban_tools.py`) |
| 0002 | feat(kanban): pure review-pairing transform for decompose (`hermes_cli/kanban_decompose.py` + `tests/test_kanban_review_pairing.py`) |
| 0003 | feat(kanban): auto_review config policy reader (`hermes_cli/kanban_decompose.py`) |
| 0009 | fix(kanban): re-read dispatch caps live every tick, not at boot (`gateway/kanban_watchers_dispatcher.py`) |
| 0010 | test(kanban): prove caps resolve live each tick (`tests/gateway/test_kanban_caps_live_reload.py`) |
| 0011 | fix(kanban): reset started_at on reclaim and set fresh on each claim (`hermes_cli/kanban_db.py`) |
| 0012 | feat(kanban): configurable out-of-tree worktree root for compiler-root repos (`hermes_cli/kanban_db_workspace.py` + `tests/hermes_cli/test_kanban_worktree_isolation.py`) |

Notes on the re-base:

- **0004/0005 dropped**: these wired review-pairing into decompose then removed
  that wiring (net-zero churn). Under the acceptance gate each patch must apply
  independently to pristine, which a removal-only patch cannot. The net intent
  (helpers present, auto-pairing OFF) is preserved by 0002+0003 alone.
- **0006 dropped (absorbed)**: upstream v0.23.0 already implements the
  review flow natively as `kanban_request_review` (see above).
- **0007 dropped (absorbed)**: v0.22.0 moved the holographic memory provider out
  of core into the external plugin catalog, so `plugins/memory/holographic/` no
  longer exists upstream and the removal patch is obsolete.
- **0008 dropped (absorbed)**: v0.23.0's `_board_path` honours an explicit
  `board=` outside fenced workers via `_explicit_board_slug` /
  `_explicit_board_intent_pinned`, so the board-precedence fix is upstream; the
  remaining delta is only the worker/subagent fence, which we accept.
- **c873decc80 absorbed**: the carried `deterministic session-end flush`
  (c873decc80) touched both `cli.py` and `kanban_db.py`. Upstream already
  finalizes the single-query worker session deterministically and flushes the
  session-store before the kanban `os._exit(0)` signal path, and its board
  precedence now matches 0008's intent (dropped above), so no part remains.
- Spread across 5 modules + tests; the decompose review-pairing helpers
  (0002/0003) are dormant — `_apply_fanout` does NOT call them; review is
  handled by the native built-in review-status path.

Excluded from the curated set (not HSCC-essential): the holographic-memory
plugin removal (0007) is dropped because upstream removed the plugin from core;
the Jun-3 autostash recovery commit was local cleanup, not a feature HSCC needs
to carry forward.

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