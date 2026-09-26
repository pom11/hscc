# GOAL LEDGER — 2026-09-26 24h autonomous run

Contract: /Users/desac/Desktop/HSCC_ORCH_GOAL_2026-09-26.md
Baseline: HSCC 2.2.0, main == origin/main @ 1037b22, board only 2 dead (blocked) cards.

## Phase 0 — Self-health (in progress)

### 0a memori capture measurement — DONE, NOT capturing
- DB: single shared `/Users/desac/.hermes/memori_byodb.db`
- Command: `sqlite3 ~/.hermes/memori_byodb.db "SELECT id,role,datetime(date_created),content FROM memori_conversation_message ORDER BY date_created DESC LIMIT 10;"`
- NEWEST literal `date_created` = `2026-06-13 02:50:29` (id 5,6). Rows newer than 2026-06-13 date literal = 6, but ALL are on 2026-06-13 (ids 1-6, Twitter autosave cron). ZERO rows from any later date, incl. today 2026-09-26.
- Fix IS deployed: installed `/Users/desac/.hermes/plugins/memori_byodb/local_augmentation.py` IDENTICAL to `origin/main` (diff -q), which writes conversation/message/fact directly via driver (lines 102-124). t_57e5b3f7/47e3ab2 confirmed on main.
- Profile effective config (direct `HERMES_HOME=/Users/desac/.hermes/profiles/hscc-orch load_config()`): provider=`memori_byodb`, memory_char_limit=4000, threshold_tokens=200000, aux.compression.base_url=192.168.88.247 → memori IS configured.
- => memori configured + fix deployed but STILL captures NOTHING newer than 2026-06-13. This is the contract's "rows still not appearing" condition.
- Root-cause leads for the worker card: (a) wiring gap — `memory_manager.sync_all` NOT called from conversation_loop.py/run_agent.py core; memori capture relies on `sync_turn` path; June-13 rows came from a cron agent run, not this interactive session. (b) whether interactive turn-finalization invokes provider.sync_turn at all. These are leads for the atomic card, not solved here.
- STATUS: ATOMIC CARD FILED (see below). Plainly: memori is NOT capturing.

### 0b archive dead cards — PENDING (verify supersession then archive)
- t_b0e3d750 (memori fix) superseded by t_57e5b3f7 @ 47e3ab2 (verified on main)
- t_d733f7c8 (pid handle) superseded by t_8334f251 @ 1037b22 (verified on main)

### 0c profile + 14-profiles audit — IN PROGRESS (provision-check discovered to have a PATH BUG)
- provision-check CLI resolves PROFILES_DIR = join(HERMES_HOME,"profiles") where HERMES_HOME=profile-dir at runtime → double-nested path `~/.hermes/profiles/<p>/profiles/<p>/config.yaml` → reads defaults, reports DEFAULTS not real values. Ineffective as-is in this runtime. Direct load_config() is the trustworthy verifier.
- Own profile hscc-orch (direct load_config): provider=memori_byodb, limit=4000, threshold=200000, aux=worker-node URL → CORRECTLY provisioned. But fallback_model commented out (§0c.1 prime suspect) + skills.preload=[brainstorming,writing-plans] (§0c.2). Memory alive this compaction.
- TODO: fix hscc-roles provision-check PROFILES_DIR bug (real defect); audit all 14 profiles via direct load_config.

### 0d heartbeat cron — NOT YET CREATED (name hscc-orch-goal-heartbeat)

## Phase 1 — Branch reconciliation (40 unmerged audit/* branches)
- Pending: enumerate all 42 audit/* branches, decide LAND/SUPERSEDED/STALE with evidence

## Phase 2 — iOS theme + UI/UX polish

## Phase 3 — Missing surfaces (API first, then iOS) — /v1/cron + iOS cron view priority

## Phase 4 — Decode-drift recheck

## Final report
- GOAL_REPORT_2026-09-26.md (write+commit at end of run)

## Card log
- [FILED] memori capture still not landing rows (newest 2026-06-13 02:50:29) — atomic card filed Phase 0a
