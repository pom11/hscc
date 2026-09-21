# Telegram removal from HSCC repo — t_07fd5b86

Task: remove Telegram from the HSCC repo entirely, landing AFTER "make Telegram fully optional" (t_1a0a47e8 / wt/tg-optional).

## Status: DONE — code removal complete, all suites green (modulo pre-existing env failures)

## Invariants preserved (verified)
1. registry `Project.topic` field + data kept — `hscc-project/flightdeck/core/registry.py` (topic / topic_name fields, set_/clear_topic, topic audit helpers all retained). Historical metadata for archive attribution.
2. `flightdeck/core/archive.py`, `map_sessions.py`, `session_discovery.py` keep READING telegram-sourced rows (`source='telegram'` stays a valid read; `_list_telegram_sessions` reads Hermes' own SessionDB). READ-ONLY — retained intact.
3. `hscc project chat --resume` (sync.py) still opens an old session by id; the live Telegram transport is gone but the command and its session-disambiguation survive.
4. `hscc project sessions hscc` lists the telegram-sourced sessions (covered by invariant 2 — the READ path).

## What was removed / changed (by package)

### hscc-bootstrap  (committed 08fc90e)
- bootstrap.sh: removed `HSCC_TELEGRAM` env var, `--telegram=yes/no` flags, usage string entry, the Telegram interview block, and the worker credential-hygiene block. enable_plugins call no longer passes HSCC_TELEGRAM.
- enable_plugins.py: removed `telegram_choice` env constant, `_ensure_flightdeck_telegram` function and its call; no telegram keys seeded into ~/.flightdeck/config.yaml.
- DELETED: `telegram_choice.py`, `strip_worker_telegram.py`, `tests/test_telegram_choice.py`, `tests/test_strip_worker_telegram.py`. Removed the telegram.enabled test block from `tests/test_enable_plugins.py`. README updated.
- Tests: 248 passed (EXIT 0, full green).

### hscc-project (the flightdeck CLI/API package)
Source (flightdeck/):
- DELETED: `flightdeck/core/telegram.py` (dedicated transport module), `flightdeck/commands/topics.py` (entire Telegram topic CRUD; CLI auto-discovers so no cli.py wiring to edit).
- commands/message.py: `send`/`read`/`broadcast` now print "no delivery mechanism — Telegram has been removed"; `dispatch` keeps card creation (kanban + worktree anchor) but DROPS the telegram announce; dry-run line notes "(Telegram removed, so there is nothing to announce)".
- commands/ask.py, report.py, decompose.py, qa.py, ingest.py, sync.py: dropped Telegram delivery branch; kept the command surface.
- core/config.py: removed ENV_GROUP_ID / _CONFIG_KEY_GROUP_ID / MissingGroupIdError etc.; kept minimal (docstring notes telegram surface removed).
- core/project_lifecycle.py: `create-project` Step 2 (topic) now reports "skipped (Telegram removed)"; cleanup of stale `TelegramError` comment.
- core/probe.py, core/templates.py, cli.py, mcp_server.py: removed telegram references (cli.py docstring + probe reworded).
- DELETED tests: `tests/test_topics.py`; rewritten/trimmed telegram-delivery tests in test_message.py, test_mcp_mutating.py, test_init.py, test_cli.py, test_project_lifecycle.py.
- Tests: `python -m pytest hscc-project/tests/` → 1173 passed, 28 failed where ALL 28 are pre-existing environment failures (verified at a stashed baseline): 26× test_standup.py (PermissionError: delegate_task child contexts cannot mutate Kanban), 1× test_monitor.py::test_ctrl_c_exits_zero_without_traceback, 1× test_mcp_server.py::test_standup_thin_adapter. None are telegram-related.

### hscc_daemon
- DELETED: `telegram.py`, `replay.py` (entirely Telegram-bound — not a protected general command), `tests/test_replay.py`, `tests/test_replay_delivery.py`, `tests/test_telegram_extra.py`.
- autodown.py: removed Telegram probe constants (GATEWAY_LOG/TELEGRAM_MARKER/TELEGRAM_OFFSET_FILE), probe_telegram_activity, `_notify` Telegram leg, wake-notice fns, the replay §6a block, `deliver_message` param. Desktop notification + JSONL log retained.
- health.py: removed Telegram-only auto-heal announce block.
- README.md: removed Telegram config block + troubleshooting row.
- tests: 1029 passed / 0 failed (65 removed telegram tests; no new failures).

### hscc-cluster
- DELETED: `_telegram_compat.py`.
- workflow.py: removed ops-topic post block; reworded docstring.
- templates/4node/deepseek-v4-orchestrator.yaml: reworded comment.
- tests: 391 passed / 11 failed — ALL 11 pre-existing `sqlite3.OperationalError` kanban-DB failures (identical to baseline), not telegram.

### hscc-api
- routes_bootstrap.py, routes_orchestrator.py: reworded module/docstrings ("Telegram-topic analog" → "chat-topic analog"); kept the registry `topic` field (invariant 1).
- tests: 786 passed / 1 skipped (identical to baseline).

### scripts
- escalate_watcher_run.py, install_escalate_watcher.sh, install_dep_watcher.sh, hscc_cluster_digest.sh, README.md: `--deliver telegram` → `--deliver desktop`; reworded comments so watchers keep a working delivery target via the surviving desktop/notification path.
- tests: 5 passed (baseline).

### Root README.md
- Reworded live-operational Telegram descriptions (escalation desktop notices, idle-autodown wake-source list, "Messages arriving while the cluster is down").

## Commands that now have NO delivery mechanism (per task guidance, said rather than deleted)
- `message send` — prints a "no delivery target" notice (no mechanism).
- `message read` — prints a "no source" notice (no mechanism). The archived telegram session history remains reachable via `project sessions` (invariant 2), but the live read is gone.
- `message broadcast` — prints a "no delivery mechanism" notice (no mechanism).
- `ask` — the command framed a prompt and sent it to a project topic via Telegram; with Telegram gone it has no delivery mechanism. Kept as a stub/reporting command; the READ/archive side (session discovery) still works.
- `report` — sent a summary to a project topic; no delivery mechanism now. Kept; no mechanism.
- `decompose` — sent to a Telegram topic and read replies; no delivery mechanism now. Kept; no mechanism.
- `qa notify` — sent to a Telegram topic; no delivery mechanism now (other qa subcommands survive).
- `message dispatch` — SURVIVES with full function (card creation + worktree anchor + assignee/board/dependents); only the announce is gone.
All the above were KEPT rather than deleted, and each prints/returns an honest "Telegram removed — no delivery" message so they fail loud, not silently.

## Remaining `grep -ri telegram` hits (non-archive) — ~778 lines, categorized with justification
LEFT INTENTIONALLY (correct/required):
- Invariant READ path: `core/archive.py`, `core/session_discovery.py`, `core/map_sessions.py` (+ their tests test_archive/test_session_discovery/test_map_sessions) — these READ telegram-sourced Hermes session rows from ~/.hermes/state.db and archive them. This is invariant 2/4, user-requested to keep.
- `core/registry.py` topic field + audit (invariant 1).
- Test fake stubs that still name `telegram_send`/`telegram_read` in FakeTG (test_ask, test_ingest, test_decompose, test_report, test_qa) — these pass and exercise the injectable-client seam; low-value to churn.
- Docstrings/comments explicitly noting "Telegram removed" (config.py, project_lifecycle, commands/message, sync docstring) — these document the removal and keep help text truthful.

NOT EDITED, left for operator decision (documentation/historical; no runtime behavior):
- `docs/audits/`, `docs/design/`, `docs/` (root) — historical audit & design records describing the Telegram-optional evolution; rewriting them would falsify a point-in-time record.
- `hscc-project/docs/` (12) — component design docs (PARTIAL: one TELEGRAM.md deleted by the code subagent; remaining are historical design narration).
- `install/hscc-skills/**` — Hermes agent-skill narration (documents the operator's skill library, includes telegram references in skill/procedure text). Editing these is a Hermes-skill change, out of the repo-removal remit; flagged for operator.
- `ios-app/` — the iOS app's references to telegram (a separate client surface), not part of the fleet runtime; flagged for operator.
- `CHANGELOG.md` — historical changelog; not rewritten.
- Build artifacts (`build/lib/`, `*.egg-info`, `__pycache__`, `.pytest_cache`) — regenerable, gitignored, not edited.
- `hscc_daemon/README.md` operational/config text already edited (above).

## Verify by execution (from card)
- bootstrap: 248 passed. hscc_daemon: 1029 passed/0 failed. hscc-cluster: 391 passed/11 failed (pre-existing env). hscc-api: 786 passed/1 skipped. scripts: 5 passed. hscc-project: 1173 passed/28 failed (pre-existing env, verified at baseline).
- Full repo grep: remaining hits are only invariant/read-path + docstring-notes-removal + non-edited historical/skill/ios buckets (see above).
- `hscc project sessions hscc` still lists the 3 archived Telegram sessions (invariant 4 — READ path intact).
- `hscc --help` / `hscc project --help`: no Telegram mention (verified — see Smoke checks below).

## Smoke checks
- The 4 other-package directories (hscc_daemon, hscc-cluster, hscc-api, scripts) grep clean of "telegram" (source + help strings), except the intentionally-retained READ path / removal-notes docstrings.
- hscc-project CLI: top-level help has zero Telegram mention. `project sessions` help and `chat --resume` help retain "telegram" because they describe the PRESERVED READ path (invariants 2/3).

## Commits (branch wt/tg-remove)
- 404770b report scaffold (this file, first version)
- 38a638b PLAN_other_packages.md
- 08fc90e hscc-bootstrap telegram removal
- <PENDING> hscc-project telegram removal
- <PENDING> hscc_daemon + hscc-cluster + hscc-api + scripts telegram removal

## Notes for reviewer
- PLAN_other_packages.md is the working plan for the non-hscc-project packages; may be deleted before merge if the operator wants a clean tree (it is not a deliverable).
- The read-path modules (archive/session_discovery/map_sessions) intentionally still say "telegram" — they archive/attribute Telegram-originated history, which is the whole point of keeping them.
