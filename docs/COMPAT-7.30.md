# COMPAT AUDIT: hermes-agent upstream v2026.7.30 (v0.19.1) vs HSCC fork+patches

## Verdict

**UPSTREAM 7.30 DOES NOT contain the kanban review-flow features.**

All HSCC patches 0001-0006 (review-flow additions) remain necessary.
None can be dropped. The pom11 fork must be kept or patches reapplied onto upstream.

The one exception: patch 0007 (remove holographic-memory-plugin) is a separate chore
not part of the review-flow feature set.

---

## Method

- Cloned `NousResearch/hermes-agent` tag `v2026.7.30` (commit `cc4cab2`, package version `0.19.1`)
  to `/tmp/hermes-7.30`.
- Ran HSCC's `apply_patches.py --check` against the clean checkout.
- Grepped upstream sources for every symbol listed in the task body.

---

## Per-patch determination

| Patch | Description | Applies? | Status |
|-------|------------|----------|--------|
| 0001 | fix: rewrite unknown create-assignee to default_assignee | YES (clean) | **Still needed** - upstream has no `_resolve_valid_assignee()` |
| 0002 | feat: pure review-pairing transform (_pair_review_tasks) | YES (clean) | **Still needed** - upstream has no `_pair_review_tasks()` |
| 0003 | feat: auto_review config policy reader (_review_policy) | FAILS (test file missing) | **Still needed** - upstream has no `_review_policy()` |
| 0004 | feat: wire review-pairing into decompose_task | FAILS (test file missing) | **Still needed** - upstream has no `_pair_review_tasks()` call |
| 0005 | refactor: use built-in review path, stop Phase-2 auto-pairing | FAILS (hunk offsets + test file) | **Still needed** - upstream does not have Phase-2 auto-pairing to remove |
| 0006 | feat: kanban_submit_review tool + running->review transition | FAILS (hunk offset at hermes_cli/kanban_db.py:4164) | **Still needed** - upstream has no `submit_review_task()` |
| 0007 | chore: remove obsolete holographic-memory-plugin | YES (clean) | Informational - unrelated to review flow |

Patches 0003-0005 fail because `tests/test_kanban_review_pairing.py` does not exist in
upstream 7.30 (it was introduced by our patches, not present in upstream). Patch 0006 fails
because the hunk context at `hermes_cli/kanban_db.py:4164` differs from the upstream's code
at that location (the upstream's `claim_task` function has different surrounding structure
than what the patch expects).

---

## Question 1: Are these UPSTREAM-NATIVE in v2026.7.30?

**NO** for all four items:

1. **`kanban_submit_review` tool** (tools/kanban_tools.py): NOT present. No `KANBAN_SUBMIT_REVIEW_SCHEMA`
   and no `_handle_submit_review` handler in upstream's `tools/kanban_tools.py`.

2. **`submit_review_task` function** (hermes_cli/kanban_db.py): NOT present. Grep for `submit_review`
   in the entire upstream repo returns zero matches.

3. **`auto_review` policy reader + review-pairing in decompose** (hermes_cli/kanban_decompose.py):
   NOT present. No `_review_policy()` function, no `_pair_review_tasks()` function, and
   `decompose_task()` does not call `_pair_review_tasks()`. The function `decompose_task()`
   exists at line 271 but the review-pairing body (patches 0002-0004) is absent.

4. **"rewrite unknown create-assignee to default_assignee" fix** (tools/kanban_tools.py):
   NOT present. No `_resolve_valid_assignee()` function in upstream's kanban_tools.py.
   The upstream does have `kanban.default_assignee` support in `decompose_task` (kanban_decompose.py),
   but the `kanban_create` call-side validation (patch 0001) is absent from the tools layer.

---

## Question 2: If YES, drop patches/fork?

Not applicable — all items are NO (not upstream-native). HSCC must either:
- Continue using the pom11 fork, or
- Reapply patches 0001-0006 onto a clean upstream 7.30 checkout (patches 0003-0006 need
  manual hunk adjustments as shown by the --check failures above).

---

## Question 3: Plugin lifecycle hooks

All three hooks exist in upstream 7.30 with the same kwargs:

| Hook | File:line | Kwargs |
|------|-----------|--------|
| `kanban_task_claimed` | hermes_cli/plugins.py:212 | `board`, `assignee`, `run_id` |
| `kanban_task_completed` | hermes_cli/plugins.py:213 | `summary` |
| `kanban_task_blocked` | hermes_cli/plugins.py:214 | `reason` |

Evidence — upstream sources:

```
# hermes_cli/plugins.py:197-214
#   - kanban_task_claimed   -> the DISPATCHER process (gateway-embedded
#   - kanban_task_completed -> the WORKER process, when it calls
#   - kanban_task_blocked   -> the WORKER process (worker-initiated block)
#
# kanban_task_completed adds: summary: str | None.
# kanban_task_blocked adds:   reason: str | None.
"kanban_task_claimed",
"kanban_task_completed",
"kanban_task_blocked",
```

Also registered at `hermes_cli/kanban_db.py:4192`, `hermes_cli/kanban_db.py:4889`,
and `hermes_cli/kanban_db.py:5562` / `hermes_cli/kanban_db.py:5673`.

`PluginContext.register_hook` exists at `hermes_cli/plugins.py:1177`:
```python
def register_hook(self, hook_name: str, callback: Callable) -> None:
```

---

## Question 4: kanban_db API surface

All six functions called by HSCC exist in upstream 7.30 with the same signatures:

| Function | Line | Signature |
|----------|------|-----------|
| `connect` | 2095 | `connect(db_path=None, *, board=None)` |
| `get_task` | 3255 | `get_task(conn, task_id)` |
| `list_comments` | 3540 | `list_comments(conn, task_id)` |
| `add_comment` | 3518 | `add_comment(conn, task_id, author, body)` |
| `unblock_task` | 5754 | `unblock_task(conn, task_id)` |
| `reclaim_task` | 4453 | `reclaim_task(conn, task_id, *, reason=None, signal_fn=None)` |

No signature or behavior changes detected between a 7.20 baseline and 7.30 for these APIs.

---

## Question 5: Other HSCC-affecting breaks between 7.20 and 7.30

- **`decompose_triage_task`**: Present at `hermes_cli/kanban_db.py:5911`. Signatures unchanged.
- **`decompose_task`**: Present at `hermes_cli/kanban_decompose.py:271`. Present and unchanged
  (the review-pairing additions from patches 0002-0004 are the only modifications).
- **`_resolve_default_assignee`**: Present at `hermes_cli/kanban_decompose.py:201`. Same function.
- **`create_task`**: Present at `hermes_cli/kanban_db.py:2820`. No signature changes.
- **`complete_task`**: Present at `hermes_cli/kanban_db.py:4689`. No signature changes.
- **`block_task`**: Present at `hermes_cli/kanban_db.py:5471`. No signature changes.
- **`claim_task`**: Present at `hermes_cli/kanban_db.py:4079`. No signature changes.
- **Plugin loader**: `hermes_cli/plugins.py` module structure intact; `register_hook` at line 1177
  with the same `(hook_name, callback)` signature.
- **Toolset registration**: `tools/kanban_tools.py` has the `registry.register()` pattern intact.
  The kanban tools module itself exists (`tools/kanban_tools.py`, 86KB).
- **CLI entrypoints**: `hermes_cli/kanban.py` present with all subcommands intact.

No breaking changes detected in plugin loader, toolset registration, CLI entrypoints, or
the kanban_db API surface.

---

## apply_patches.py --check output

```
Patch 0001: applies=true (clean)
Patch 0002: applies=true (clean)
Patch 0003: applies=false - "tests/test_kanban_review_pairing.py: No such file or directory"
Patch 0004: applies=false - "tests/test_kanban_review_pairing.py: No such file or directory"
Patch 0005: applies=false - "patch failed: hermes_cli/kanban_decompose.py:499" + test file missing
Patch 0006: applies=false - "patch failed: hermes_cli/kanban_db.py:4164" + hunk does not apply
Patch 0007: applies=true (clean)
```

---

## Conclusion

HSCC cannot drop the fork or patches 0001-0006. The kanban review-flow features
(`kanban_submit_review`, `submit_review_task`, `auto_review` policy, `_pair_review_tasks`,
and the create-assignee validation fix) do not exist in plain upstream v2026.7.30.

The kanban_db API, plugin lifecycle hooks, and toolset registration patterns are all
unchanged and compatible with HSCC's existing integrations.

The only upgrade path is to reapply patches 0001-0006 onto a clean upstream 7.30 checkout,
with manual hunk adjustment needed for patches 0003-0006 (the test file and hunk offsets
differ from the fork's base).