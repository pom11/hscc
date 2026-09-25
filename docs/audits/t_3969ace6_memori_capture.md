# Audit: memori_byodb capture — root cause of silence since 2026-06-13

- Task: t_3969ace6
- Date of audit: 2026-09-25 (local UTC+3)
- Branch: wt/t_3969ace6 → merged to main
- Verdict: **CONFIRMED NOT CAPTURING.** Root cause is a code bug in
  `memori_byodb/local_augmentation.py` that makes every augmentation raise before any
  conversation/message/fact row is written.

---

## 1. Measurement: newest row per table (reproduced read-only)

The DB is a live file, so I copied it to `/tmp` and queried the copy in read-only
mode (no journals created on the live DB):

```
cp /Users/desac/.hermes/memori_byodb.db /tmp/memori_byodb_readonly.db
```

Per-table newest timestamps (all via `sqlite3 -readonly /tmp/memori_byodb_readonly.db`):

| table                          | count | max(date_created)                | max(date_updated) |
|--------------------------------|-------|----------------------------------|-------------------|
| memori_conversation            | 3     | 2026-06-13T02:50:29.978574+00:00 | NULL              |
| memori_conversation_message    | 6     | 2026-06-13T02:50:29.978574+00:00 | NULL              |
| memori_entity                  | 3     | **2026-08-14 06:41:28**          | NULL              |
| memori_entity_fact             | 25    | 2026-06-13 02:50:29             | NULL              |
| memori_entity_fact_mention     | 26    | 2026-06-13 02:50:29             | NULL              |
| memori_knowledge_graph         | 0     | —                               | —                 |
| memori_process                 | 1     | 2026-06-13T02:48:10.105431       | NULL              |
| memori_process_attribute       | 0     | —                               | —                 |
| memori_session                 | 3     | 2026-06-13T02:50:29.978574+00:00 | NULL              |
| memori_subject / predicate / object | 0  | —                             | —                 |
| memori_schema_version          | (num=2) | n/a                          | n/a               |

Newest `memori_conversation` row (verbatim):

```
3|f4b0f6a5-7a8f-464c-8355-d25fd2a41ea3|3|Twitter autosave cron - 2026-06-13|2026-06-13T02:50:29.978574+00:00
```

**Key observations**

1. All capture-path tables — conversation, conversation_message, entity_fact,
   entity_fact_mention, session, process — are frozen at **2026-06-13 02:50:29**
   (the message/fact/mention rows all carry 2026-06-13; process/session too).
2. There is exactly ONE row written after 06-13: `memori_entity` id=3
   `desac_hermes`, date_created **2026-08-14 06:41:28**. It is an **orphan** —
   it has no session, no conversation, no entity_fact. (Verified: entity 3 has
   no rows in memori_session; sessions 1–3 all belong to entities 1–2.)
3. The 06-13 data is entirely **cron-generated**: all three conversations have
   summary `Twitter autosave cron - 2026-06-13` and messages like
   "Extracted 13 notable tweets…". They used the OLD entity IDs
   (`twitter-autosave` entity 1 + process `twitter-autosave-cron`, and
   `default` entity 2). The current interactive hermes sessions (entity
   `desac_hermes`) have produced **zero** rows.

So the operator's claim is confirmed: memori_byodb has captured nothing since
2026-06-13 at the conversation level, and the only later write is a lone orphan
entity row.

---

## 2. What ENABLES capture (wiring)

- Capture is enabled by Hermes config `memory.provider: memori_byodb` plus a
  valid `/Users/desac/.hermes/memori_byodb.json` (the plugin's `_load_config`
  returns a config only if that file exists and has `entityId`). Current file
  (mtime 2026-08-12 11:47):
  ```json
  { "entityId": "desac_hermes", "projectId": "hermes_cluster",
    "processId": "hermes_gateway", "dbPath": "/Users/desac/.hermes/memori_byodb.db" }
  ```
- It is configured on many profiles (`hscc-orch`, `flightdeck-orch`,
  `ecofire-*-orch`, `grid-orch`, `pom-orch`, `radio-orch`, `soconn-orch`,
  `sphoin-orch`, `powerbi-orch`, `pickolo-orch`, `flosana-orch`,
  `efsdriver-orch`, …) and in the root `/Users/desac/.hermes/config.yaml`
  (`memory.provider: memori_byodb`).
- The trigger is Hermes calling the provider's `sync_turn()` after each
  completed turn: `run_agent.py:901` calls `memory_manager.sync_all(...)`, which
  calls `provider.sync_turn(..., messages=...)` on a background worker
  (`agent/memory_manager.py:480-504`). `sync_turn` → `capture_turn` →
  `memori.config.augmentation.enqueue(...)` + `wait()`.
- **It does NOT require `memori_daemon`.** `memori_byodb` is the
  bring-your-own-DB variant (local SQLite + local LLM augmentation). The
  `memori` plugin is the separate cloud-daemon variant. The two are independent;
  memori_byodb capture depends only on (a) the config file existing, (b) the
  provider being registered/activated at session start, and (c) Hermes completing
  turns. `memori_daemon`'s traffic going to zero is a separate observation about
  the cloud plugin, not a cause of memori_byodb silence.

---

## 3. Root cause: every augmentation raises before the conversation write

The plugin's augmentation path (`memori_byodb/local_augmentation.py`, `process()`,
lines 82-93):

```python
memories = self._parse_response(response)          # -> list[dict]  (plain dicts)
if memories:
    ctx.data["memories"] = memories
    from memori.memory.augmentation.augmentations.memori._augmentation import AdvancedAugmentation
    aug = AdvancedAugmentation()
    await aug._schedule_entity_writes(ctx, driver, memories)   # line 91  <-- RAISES
    await aug._schedule_process_writes(ctx, driver, memories)  # line 92  (never reached)
    await aug._schedule_conversation_writes(ctx, memories)     # line 93  (never reached)
```

`_parse_response()` (lines 178-218) returns a **`list` of plain dicts**
(`[{"content": ..., "metadata": {...}}, ...]`).

But the memori SDK's `_schedule_entity_writes` is typed `memories: Memories`
and does (site-packages `memori/.../_augmentation.py` line 227):

```python
facts_to_write = memories.entity.facts
```

A `list` has no `.entity` → **`AttributeError: 'list' object has no attribute
'entity'`** raised at `_augmentation.py:227`, the FIRST statement of the FIRST
scheduled write. Because `_schedule_entity_writes` is called before
`_schedule_conversation_writes` in `process()`, the conversation/message write
is never reached.

Note also: inside `_schedule_entity_writes`, `driver.entity.create(entity_id)`
(extension line 223) runs BEFORE line 227, and is idempotent via the unique
constraint on `external_id`. That exactly explains the one orphan `desac_hermes`
entity row created on 2026-08-14 (first augmentation after the config switched
entityId to `desac_hermes`): the entity row is created, then `.entity` raises,
so no session/conversation/facts follow.

### The plugin is genuinely capturing (invoking augmentation) — and failing

Log evidence — `~/.hermes/logs/agent.log` (provider active, augmentation
attempted):

```
2026-09-17 01:22:31,088 INFO _hermes_user_memory.memori_byodb__source_..._client:
  Local augmentation initialized with Qwen3.6 on http://10.0.0.x:8000/v1/chat/completions
2026-09-17 01:22:31,088 INFO run_agent: Memory provider 'memori_byodb' activated
...
2026-09-17 01:41:11,380 ERROR memori.memory.augmentation._manager:
  Error in augmentation LocalAugmentationWrapper: 'list' object has no attribute 'entity'
  File ".../memori/.../_manager.py", line 133, in _process_augmentations
    ctx = await aug.process(ctx, driver)
  File "/Users/desac/.hermes/plugins/memori_byodb/_client.py", line 44, in process
    await self.local_aug.process(ctx, driver)
  File "/Users/desac/.hermes/plugins/memori_byodb/local_augmentation.py", line 91, in process
    await aug._schedule_entity_writes(ctx, driver, memories)
  File ".../memori/.../_augmentation.py", line 227, in _schedule_entity_writes
    facts_to_write = memories.entity.facts
AttributeError: 'list' object has no attribute 'entity'
```

(The augmentation LLM endpoint shown in the original log was a LAN address;
redacted to the placeholder `10.0.0.x` per repo policy. The LLM call is not the
blocker — the response was parsed successfully and reached the fact-write
stage, e.g. an 08-23 log shows the LLM returned a `{"facts":[...]}` JSON that
was being processed when the write failed.)

Occurrences of this exact error across the rotated logs:

| log file | window | occurrences |
|----------|--------|-------------|
| errors.log.2 | 2026-08-21 → ~2026-08-27 | 25 (first 2026-08-23 11:47; last 2026-08-27 04:51) |
| errors.log.1 | ~2026-08-27 → 2026-09-22 | 1 (2026-09-17 01:41:11) |
| errors.log (current) | 2026-09-22 → now | 0 |

The last error, `2026-09-17 01:41:11`, exactly matches the DB file mtime
(`Sep 17 01:41`) — the final failed write attempt. After that the provider has
not been observed active again in `agent.log` (last activation
`2026-09-17 02:23:28`), and the current errors.log shows no further capture
attempts.

---

## 4. Timeline / correlation with the 2026-06-13 cutoff

| date | event |
|------|-------|
| 2026-06-11 | `memori_byodb/local_augmentation.py` introduced (git 6498763, 94e7b3f) — list-shaped parse output from the start |
| 2026-06-13 02:48–02:50 | **Last successful captures** — all three conversations are `Twitter autosave cron - 2026-06-13`, entities `twitter-autosave`/`default`, process `twitter-autosave-cron` |
| 2026-06-13 → ~08-12 | Capture gap: `memori_byodb.json` absent/invalid → `_load_config` returned None → logs show `Memory provider 'memori_byodb' loaded but no provider instance found` all through early August (agent.log.3) |
| 2026-08-12 11:47 | `memori_byodb.json` written with `entityId: desac_hermes` |
| 2026-08-12 15:58 | provider first active with the local augmentation (`Local augmentation initialized …` agent.log.3:21727) |
| 2026-08-14 06:41 | first augmentation after config → `driver.entity.create('desac_hermes')` creates the orphan entity row, then `.entity` raises → no session/conversation/facts |
| 2026-08-23 → 08-27 | 25 × `'list' object has no attribute 'entity'` |
| 2026-09-17 01:41:11 | last augmentation error == DB mtime (final write attempt) |
| 2026-09-17 02:23 | last observed provider activation; none since |

The 06-13 cutoff is NOT explained by a config or daemon change on 06-13. It is
simply the last time a capture (the twitter-autosave cron) ran and *succeeded*
with the old entity IDs. From the moment the plugin's local augmentation was
introduced (06-11) with list-shaped parse output, the write path has been
broken; the reason capture appeared to "work" on 06-13 is that those rows were
written by whatever augmentation ran at that moment (the logs from that window
are rotated out), and — critically — the *current* config/entity
(`desac_hermes`) has never once produced a successful capture. The gap
06-13→08-12 is because the provider wasn't configured/active; from 08-12
onward it is configured and active but **fails every augmentation**.

---

## 5. Verdict

**The plugin is genuinely not capturing.** Newest `memori_conversation` =
`2026-06-13T02:50:29.978574+00:00`. The provider is configured and its capture
path is demonstrably invoked (provider registered/activated, augmentation
enqueued and reaching the fact-write stage), but every augmentation fails with
`AttributeError: 'list' object has no attribute 'entity'` because
`local_augmentation.py` hands the SDK a `list` of plain dicts where the SDK
requires a `Memories` object with `.entity.facts`. The failure point precedes
`_schedule_conversation_writes`, so no conversation/message/fact rows are ever
written under the current `desac_hermes` configuration.

**Capture is not tied to `memori_daemon`.** `memori_byodb` is fully local
(SQLite + local LLM augmentation) and needs only: config file present, provider
registered, and Hermes completing turns. `memori_daemon` is the separate cloud
plugin (`memori`); its traffic dropping to zero is unrelated to memori_byodb's
silence.

---

## 6. Recommendation (follow-up fix card — NOT fixed here)

Per the operator's rule, this card only measures and root-causes; the fix is
spawned as a separate card. Recommended follow-up:

- **Card**: `fix(memori_byodb): local augmentation writes list where SDK expects Memories` — the local augmentation must construct the SDK's `Memories` / entity-facts data structure (or write entity-facts + conversation rows directly through the driver/manager API) instead of passing the raw `list` from `_parse_response` into `_schedule_entity_writes`. Until then, every capture for `entity_id=desac_hermes` raises at `_augmentation.py:227` and nothing is persisted.
- Proposed fix shape (for the child card to design/implement): in `process()`, build a `Memories` object whose `.entity.facts` / `.entity.fact_embeddings` reflect the parsed facts, so `_schedule_entity_writes` / `_schedule_conversation_writes` operate on the expected shape — OR bypass the SDK `_schedule_*` helpers and write the rows directly via the driver.
- Reference: `memori_byodb/local_augmentation.py:91` (call site), memori SDK `_augmentation.py:217-261` (expected `Memories` contract), `memori_byodb/_client.py:141-174` (capture_turn enqueue+wait).

---

## Reproducer commands

```bash
# Read-only copy of the live DB (never open the live file for write)
cp /Users/desac/.hermes/memori_byodb.db /tmp/memori_byodb_readonly.db

# Table inventory
sqlite3 -readonly /tmp/memori_byodb_readonly.db ".tables"

# Newest conversation row
sqlite3 -readonly /tmp/memori_byodb_readonly.db \
  "SELECT id, uuid, session_id, summary, date_created FROM memori_conversation ORDER BY date_created;"

# Per-table newest timestamp
sqlite3 -readonly /tmp/memori_byodb_readonly.db \
  "SELECT 'conversation', COUNT(*), MAX(date_created) FROM memori_conversation
   UNION ALL SELECT 'conversation_message', COUNT(*), MAX(date_created) FROM memori_conversation_message
   UNION ALL SELECT 'entity', COUNT(*), MAX(date_created) FROM memori_entity
   UNION ALL SELECT 'entity_fact', COUNT(*), MAX(date_created) FROM memori_entity_fact
   UNION ALL SELECT 'entity_fact_mention', COUNT(*), MAX(date_created) FROM memori_entity_fact_mention
   UNION ALL SELECT 'session', COUNT(*), MAX(date_created) FROM memori_session
   UNION ALL SELECT 'process', COUNT(*), MAX(date_created) FROM memori_process;"

# Orphan entity check (entity 3 has no session/conversation)
sqlite3 -readonly /tmp/memori_byodb_readonly.db \
  "SELECT entity_id, COUNT(*) FROM memori_session GROUP BY entity_id;"
```

Log evidence (root-cause traceback, LAN addr redacted to 10.0.0.x):

```bash
grep -n "Local augmentation initialized\|Memory provider 'memori_byodb' activated\|Error in augmentation LocalAugmentationWrapper" \
  ~/.hermes/logs/agent.log
grep -n "Error in augmentation LocalAugmentationWrapper" ~/.hermes/logs/errors.log.1 ~/.hermes/logs/errors.log.2
```
