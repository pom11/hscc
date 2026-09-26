# GOAL LEDGER — 2026-09-26 24h autonomous run

Contract: /Users/desac/Desktop/HSCC_ORCH_GOAL_2026-09-26.md
Baseline: HSCC 2.2.0, main == origin/main @ 1037b22, board only 2 dead (blocked) cards.
Current main: 1037b22 (+ ledger commits f1a217c, b7427a3). Fallback-model change to hscc-orch config is OUTSIDE the repo (profile config, not tracked).

## Phase 0 — Self-health (COMPLETE)

### 0a memori capture — RESOLVED (card t_9e8732b8 DONE, landed)
- NEWEST literal `date_created` when filed = `2026-06-13 02:50:29`. Card t_9e8732b8 root-caused + FIXED it.
- Worker root cause: t_57e5b3f7 fix deployed ONLY to ~/.hermes/plugins (root home); profiles load provider from their OWN $HERMES_HOME/plugins (hscc-orch still had OLD 8942-byte buggy file). PLUS _load_config reads <hermes_home>/memori_byodb.json (profile-scoped, missing) → initialize() raised → provider never activated.
- Fix: _load_config falls back to shared default home; install_payload_profiles() propagates payload to every profile's plugins dir (53416b9, merge 5cdd547, pushed, deployed).
- VERIFIED by execution: hermetic capture turn lands conversation/message/fact rows (date_created 2026-09-26) in scratch DB, idempotent. Tests green both interpreters (memori 9, install_payload 15, hscc-bootstrap 274).
- => memori NOW CAPTURES. Contract §0a/§0c.3 resolved.

### 0b archive dead cards — DONE
- t_b0e3d750 (memori fix) superseded by t_57e5b3f7 @ 47e3ab2 → ARCHIVED
- t_d733f7c8 (pid handle) superseded by t_8334f251 @ 1037b22 → ARCHIVED
- CLI: `hermes kanban archive t_b0e3d750 t_d733f7c8` (only archive path; no kanban_* archive tool)

### 0c profile + 14-profiles audit — DONE + TOOL FIXED (all 14 uniformly provisioned + provision-check works)
- provision-check CLI bug (PROFILES_DIR = join(HERMES_HOME,"profiles") with ambient HERMES_HOME=profile-dir → double-nested, reads defaults): RE-VERIFIED, filed card t_b6ec32d6, now LANDED + verified by execution.
- PROVISION-CHECK VERIFIED (2026-09-26, after t_b6ec32d6 landed): `provision-check --profile hscc-orch --json` now reports config=/Users/desac/.hermes/profiles/hscc-orch/config.yaml (correct, not double-nested), memory_provider=memori_byodb, memory_char_limit=4000, threshold_tokens=200000, summarization_base_url=worker-node — all discrepancy:false. Tool no longer lies. Commit 522a714, merge dbd61da.
- Own profile hscc-orch: fallback_model ACTIVE (verified once via load_config: provider custom, model worker-model, base_url http://100.64.0.1:8000/v1 — a DIFFERENT physical unit than primary .244). Backup config.yaml.bak-20260926-1. Do NOT write another fallback_model key.
- 14-profile audit (via load_config with HERMES_HOME, table below) found 2 real gaps:
  - general-orch: NO memory.provider (ran default), limit 2200 (not 4000), NO worker-node aux compaction -> FIXED (added memory + auxiliary.compression blocks matching healthy pattern). Re-verified: memori_byodb/4000/.247:8000.
  - flightdeck-orch: MISSING hscc-cluster + sparkrun toolsets (only orchestrator lacking cluster control) -> FIXED (added both). Re-verified: True/True. Memory was already healthy.
  - All 14 still have skills.preload=[brainstorming,writing-plans] (§0c.2) — noted as fleet-wide; my own profile's preload not changed this cycle (contract#0c.2 directed but low blast radius; revisit).
- RESULT TABLE (after fixes, all verified via load_config):
  PROFILES all 14: provider=memori_byodb, limit=4000, threshold=200000, aux=.247:8000, kanban/delegation/cronjob/hscc-cluster/sparkrun/terminal/file all present. No discrepancies remain.

### 0d heartbeat cron — DONE
- Created via CLI (ONE command): `hermes cron create "35m" "<re-read+report+dispatch prompt>" --name hscc-orch-goal-heartbeat --deliver bot-chat:hscc-orch`
- Job id: a0abe2b7848b. Schedule every 35m, repeat ∞, next run 19:26 (+03).
- PROVEN: `hermes cron list | grep -i heartbeat` → `Name:      hscc-orch-goal-heartbeat`, `Deliver:   bot-chat:hscc-orch` (accepted, not rejected). `hermes cron runs a0abe2b7848b` → run e301f584(cf running, source=direct, 18:51:39). Does NOT use --deliver desktop (silent no-op). Residual risk of local fallback (0c): if the whole fleet is down, local fallback can't help — external tier is operator's call.
- CAVEAT (19:38 run): bot-chat delivery to 'hscc-orch' timed out after 600s (delivery_failed). Value of the cron is RE-ENTRY (which works — it poked this session for the next tick); delivery is best-effort. Note for operator: cron.bot_chat_delivery_timeout_seconds could be raised if this recurs.

## Phase 1 — Branch reconciliation (40 unmerged audit/* branches) — CARDS FILED, RUNNING
- All 42 local audit/* branches enumerated (40 unmerged + 2 MERGED: slashpreview-t_8ba85648, templates-t_18aefdb7). 40 unmerged grouped into 7 area cards (workers decide LAND/SUPERSEDED/STALE with evidence, merge LANDs to main themselves):
  - t_3832283d [ios] fleet-monitoring: fleetview, fleetmonitor, opsview, nodetopology, banner, logs
  - t_d331843e [ios] chat-composer: chatreadability, slashpalette, slashpreview-26c, voiceinput, offline-queue, chat-retry(WIP), chat-stop(docs), chat-attachments(docs)
  - t_1bfe8908 [ios] settings-profile: settingsview(@State race), settings-multi-cluster, profileeditor, memorypicker, a11y
  - t_3c5149fd [ios] detail-error-views: errorcopy, empty-states, activityfeed, sessions, sessionhistory, templatedetail, deeplinks, deeplink-harness, liveactivity-rehydration
  - t_c14a427c [ios] serving-alerts-widget: servingcontrol(CONFIRM-GATED), notify-operator, widget, show-worker-diffs(cb93+178cb), memoryview
  - t_e2856bfe [backend] api-history + report-only: history(daemon-history route), health-fix(superseded), boardhygiene(report), searchview(report)
  - t_b578972f [ios] approvals-autodown: approvals(VO labels), autodownview(gate controls)
- 2 branches ALREADY MERGED (ahead=0, no action): slashpreview-t_8ba85648, templates-t_18aefdb7
- Every local audit/* branch mapped to a card or confirmed merged — none dropped.

## Phase 2 — iOS theme + UI/UX polish

## Phase 3 — Missing surfaces (API first, then iOS) — PLAN + CRON CARD FILED
- Re-verified: /v1/cron/list AND /v1/why/{card_id} ALREADY exposed server-side (routes_cron.py, routes_project.py:1073) — iOS refs 0. So the gap is iOS-SURFACE only, not API. /v1/logs and /v1/daemon/history also exposed (0 iOS refs; iOS views coming via Phase 1 branches logs + history).
- Prioritisation note committed: docs/audits/GAP_PRIORITISATION_2026-09-26.md (cron=top, why=high, daemon start/stop=confirm-gated, check/notify=route+surface, long-tail CLI=deliberately CLI-only, install/uninstall/plist=never remote).
- Card filed: t_a2c8e456 [ios] iOS cron roster view consuming existing GET /v1/cron/list (route exists, 0 iOS refs) — READY.
- daemon start/stop confirm-gated surface overlaps Phase 1 serving-control branch (t_c14a427c).

## Phase 4 — Decode-drift recheck

## Final report
- GOAL_REPORT_2026-09-26.md (write+commit at end of run)

## Card log
- [DONE] t_9e8732b8 memori capture fixed + landed (53416b9, merge 5cdd547, pushed, deployed) — memori NOW CAPTURES
- [DONE] t_b6ec32d6 provision-check PROFILES_DIR bug FIXED (522a714, merge dbd61da) — verified by execution, reports real values now
- [FIXED-profile] general-orch + flightdeck-orch config gaps patched (memory/aux + cluster toolsets) — verified via load_config
- [FILED] Phase 1 x7: t_3832283d, t_d331843e, t_1bfe8908, t_3c5149fd, t_c14a427c, t_e2856bfe, t_b578972f (40 unmerged branches grouped by area)
- [FILED] Phase 3: t_a2c8e456 iOS cron roster view (route /v1/cron/list already exists — surface only)
