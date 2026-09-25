# Fix: memori_byodb capture never wrote conversation/facts (t_57e5b3f7)

- Task: t_57e5b3f7 (worktree replacement for scratch-kind t_b0e3d750)
- Date: 2026-09-25
- Branch: wt/t_57e5b3f7
- Verdict: **FIXED and verified by execution against a scratch DB.**

Parent audit t_3969ace6 (docs/audits/t_3969ace6_memori_capture.md) measured that
memori_byodb had captured nothing since 2026-06-13 (newest memori_conversation =
2026-06-13T02:50:29Z) despite the provider being active and capture invoked. That
audit identified the root cause as `local_augmentation.py` `process()` passing a
`list` of plain dicts to the SDK helper `_schedule_entity_writes`, which is typed
`memories: Memories` and does `memories.entity.facts` -> `AttributeError`.

This card implements and verifies the fix.

---

## Root cause (confirmed by reproduction)

Every augmentation raised `AttributeError: 'list' object has no attribute
'entity'` at `local_augmentation.py:91` (old) inside
`_schedule_entity_writes` (`_augmentation.py:227`, `facts_to_write =
memories.entity.facts`). Because `_schedule_entity_writes` ran before
`_schedule_conversation_writes`, the conversation/message write was never
reached.

**Second, latent bug (found during this card):** even after fixing the
AttributeError, the SDK `_schedule_*` helpers do not write conversation or
message rows at all — the SDK never calls `driver.conversation.create` /
`driver.conversation.message.create` in the augmentation path. And the payload's
`conversation_id` is a session *string*, not the numeric conversation row id the
helpers expect. So just constructing a `Memories` object (option 1) would still
have left `memori_conversation` and `memori_conversation_message` at 0.

## Fix

Rewrote the persistence block in `process()` (memori_byodb/local_augmentation.py)
to bypass the SDK `_schedule_*` helpers and write the rows directly via the
driver (proposed option 2). `_parse_response` output stays the source of facts.

After parsing facts:

1. `entity_id = driver.entity.create(ctx.payload.entity_id)` — idempotent by
   external_id.
2. `driver.conversation.create(session_id, 30)` — session string -> numeric
   conversation row id (reuses existing conversation for the session within the
   30-minute timeout; returns the numeric id).
3. `driver.conversation.message.create(conversation_id, role, type, content)`
   for each `ctx.payload.conversation_messages` entry.
4. `driver.entity_fact.create(entity_id, fact_texts, None, conversation_id)` —
   writes the fact rows and links each to the conversation via
   `memori_entity_fact_mention`.

No SDK `AdvancedAugmentation` import remains in `local_augmentation.py`, so the
module no longer depends on the SDK's `memori` package at all (it only ever used
the private `_schedule_*` helpers).

## Verification (by execution)

Harness (scratch dir outside the repo, so the SDK `memori` in site-packages was
imported rather than the repo's cloud `memori` plugin whose name collides):
local fake OpenAI-compatible `/chat/completions` server returning a 2-fact JSON
array + a fresh scratch SQLite DB. `MemoriBYODBClient.capture_turn(...)` was run
against it. Scratch DB rows after one capture:

- memori_conversation            = 1  (session-abc-123)
- memori_conversation_message    = 2  (user + assistant)
- memori_entity                  = 1
- memori_entity_fact             = 2  (the two extracted facts)
- memori_entity_fact_mention     = 2  (facts linked to the conversation)
- memori_process / _attribute    = 0  (no process attributes extracted)

Second capture for the same session reuses the conversation row (still 1),
appends its messages (2 -> 4), and does NOT duplicate existing facts
(`ON CONFLICT(entity_id, uniq)` bumps `num_times`, mention is
`INSERT OR IGNORE`). A new session creates a new conversation row.

No `AttributeError` in any run. Before the fix the same harness reproduced the
exact audit failure (`memori_conversation=0, memori_conversation_message=0,
memori_entity_fact=0`, entity=1 orphan).

### Regression tests

Added `memori_byodb/tests/test_local_augmentation.py` (5 hermetic tests): no
network (local LLM stubbed), no SDK `memori` import (name-collision-safe), no DB
touched (fake driver). Exercises `_parse_response` parsing / markdown-fence
stripping / invalid-fallback, and the new `process()` write path (entity +
conversation + messages + facts with conversation link) plus the no-messages
no-op.

Pass under both required interpreters:

```
~/.hermes/hermes-agent/venv/bin/python -m pytest -q memori_byodb/tests   -> 5 passed
/Users/desac/miniconda3/envs/p313/bin/python -m pytest -q memori_byodb/tests -> 5 passed
```

## Merge / deploy

- Commit `38d3e54` on `wt/t_57e5b3f7` -> merged to main + pushed.
- `python3 hscc-bootstrap/install_payload.py` run.
- Full suite (`scripts/run_tests.sh`) green under both the hermes venv and
  p313 interpreters.

## Constraints honored

- No live DB touched — all verification against `/tmp` scratch DB copies.
- No new test touches the live DB.
- No LAN/tailnet/infra addresses in the change (localhost `127.0.0.1` used only
  in throwaway harness code under /tmp, not committed).
- No gateway/daemon restart.
- No `~/.hermes/state.db` or `~/.config/...` touched.
