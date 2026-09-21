# Telegram Removal Plan — hscc_daemon, hscc-bootstrap, scripts, hscc-cluster, hscc-api

Scope: worktree `/Users/desac/dev/hscc/.worktrees/t_07fd5b86`. Telegram is being removed
entirely. This plan catalogues every case-insensitive `telegram` reference in the five
packages and prescribes the exact action for each.

## Governing rules (apply throughout)
1. **Registry `Project.topic` field MUST survive** — it is historical metadata consumed by
   `_resolve_identity`/`session_discovery`. Do NOT delete the field or its consumers.
2. **Archive readers + session_discovery keep READING telegram-sourced rows** — the string
   `source='telegram'` and reading `platform == "telegram"` rows stay valid READ paths.
   Only *writing/creating* telegram rows and *delivering* via Telegram are removed.
3. **Do NOT over-delete general commands.** `message`, `ask`, `report`, `qa`, `decompose`
   are general commands with a Telegram delivery branch. Those commands live OUTSIDE these
   five packages (in hscc-roles / hscc-orchestrator / gateway templates, not in this scope).
   In-scope, the only analog is the cron watchers/digest delivered via `--deliver telegram`
   — the script *itself* is delivery-agnostic (emits stdout); only the delivery target changes.

  **Explicit no-delivery notes:**
  - The five `message/ask/report/qa/decompose` commands are NOT in these packages — they
    are not touched by this plan. Verify their Telegram branches in their home package.
  - In this scope, removing Telegram delivery does NOT strand a command with no delivery:
    - `events digest` / `escalate watcher` cron: switch `--deliver telegram` → a non-Telegram
      target (see each file below). They are NOT deleted.
    - Daemon operator alerts (`_notify`, `notify operations`, executor alerts): these still
      deliver via desktop notification (`send_macos_notification`) and the JSONL logs. Only
      the Telegram leg is dropped; desktop remains. NOT stranded.

## Class legend
- **DELETE** — dedicated Telegram module/file; remove the whole file (and its dedicated tests).
- **PARTIAL** — general code with a Telegram branch; remove ONLY the Telegram-specific lines/branch, keep the general command/behaviour.
- **KEEP** — archive/registry/history references that must survive (rules 1–2); no functional change (may reword comments only).

---

## hscc_daemon/telegram.py — DELETE (dedicated Telegram module)
- **Class:** DELETE — entire module is the Telegram Bot-API transport.
- **Action:** `git rm hscc_daemon/telegram.py`. Delete ALL functions:
  - `send_message()` (line 67), `notify_operations()` (line 107), plus `OPS_CHAT_ID`,
    `OPS_THREAD_ID`, `BOT_TOKEN`, env/`.env` handling, `_env_file_value`+guard helpers.
- **After deletion,** identify and remove every importer of this module:
  - `hscc_daemon/autodown.py:28` `from .telegram import notify_operations`
  - `hscc_daemon/health.py:1503` `from .telegram import notify_operations`
  - `hscc_daemon/replay.py:42` `from .telegram import send_message`
- **`notify_operations` callers to remove/re-route** (the desktop leg survives):
  - `autodown.py:1831` (inside `_notify`, line 1828 — remove the notify_operations call, keep `send_macos_notification`)
  - `autodown.py:1862` (inside `_notify_wake_triggered` — whole function deleted, see autodown section)
  - `autodown.py:1902` (inside `_notify_wake_complete` — whole function deleted)
  - `health.py:1504` (executor-alert notifications — remove the telegram notify leg; keep the desktop/log side)
- **`send_message` callers:**
  - `replay.py:744` (`default_deliver_message` — function deleted; see replay section)

---

## hscc_daemon/replay.py — DELETE (dedicated Telegram inbound-message replay module)
- **Class:** DELETE — its entire purpose is queueing/delivering *inbound Telegram messages*
  that arrive while down/waking, and replying back via `telegram.send_message`. With Telegram
  gone there is no inbound Telegram to queue and no Telegram to deliver to. The rules do not
  name it, so this is flagged for **operator judgement**, but by the rules' logic it must go:
  it is neither a general command (message/ask/report/qa/decompose are elsewhere) nor an
  archive/registry reader — it is the Telegram delivery+replay *mechanism itself*.
- **Action:** delete `hscc_daemon/replay.py` ENTIRELY, along with its dedicated tests
  (below). Remove all import/wiring of it in `autodown.py`:
  - `autodown.py` `_capture_inbound_messages` (line ~3007, called at line 3027) — delete the call and the helper (part of the telegram probe, below).
  - Verify no other module imports `replay` (only tests did).
- **Watch:** removing the telegram probe (below) is the *only* producer of inbound messages;
  deleting replay.py is consistent. Do NOT try to keep replay.py as a generic mechanism — it
  is Telegram-bound end-to-end (parse `platform=telegram` lines, `telegram.send_message`).

---

## hscc_daemon/autodown.py — PARTIAL (remove Telegram branch + probe; keep general machinery)
- **Class:** PARTIAL. Keep the autodown cycle, `record_activity` choke point, wake bookkeeping
  (`wake_source`/`wake_trigger_text` fields stay as schema), `http`/`kanban`/`prci` probes.
  Remove only the Telegram-specific lines.
- **Rule 2 note:** `wake_source`/`wake_trigger_text` config *fields* stay (they are read by
  `autodown_cli.py` status/`wake_source` handling). Only the `"telegram"` *value* and the
  telegram *probe that sets it* are removed. The string `source='telegram'` / `platform=telegram`
  remains a valid READ for archives/session_discovery (rule 2) — we simply no longer *produce*
  it at daemon runtime after the probe is gone.

### Specific edits
1. **Line 27–28 imports:**
   - Line 28 `from .telegram import notify_operations` — **REMOVE.**
   - Line 27 `from .desktop import send_macos_notification` — KEEP.
2. **`_notify(msg, title, priority)` (lines 1828–1837):** remove the
   `notify_operations(msg)` block (1829–1833). Keep `send_macos_notification` (1834–1837).
   Update docstring "desktop + ops Telegram" → "desktop". Resulting `_notify` is desktop-only
   but still a valid operator-wake notifier — NOT stranded (has desktop delivery).
3. **`TG_QUOTE_CHARS` (line 1842)** — DELETE (only used by the two wake-notify functions below).
4. **`_notify_wake_triggered(cfg)` (lines 1845–1869)** — DELETE ENTIRE FUNCTION. It fires only
   on `wake_source == "telegram"`, which never occurs after the probe is removed. Its only
   callers gate on telegram wake. Removing it strands nothing — it is telegram-specific.
5. **`_notify_wake_complete(cfg)` (lines 1872–1904)** — DELETE ENTIRE FUNCTION. Same rationale.
   > If you keep these two functions, you must also keep the `wake_trigger_text` capture that
   > feeds the quote and `notify_operations` — which is exactly the telegram path. Delete all.
6. **Telegram probe machinery (lines 2721–3036):** delete the whole Phase-6 telegram probe:
   - `TELEGRAM_MARKER` (2724), `TELEGRAM_OFFSET_FILE` (2727), `_load_telegram_offset` (2863),
     `_save_telegram_offset` (2853), `_TELEGRAM_MSG_RE` (2876), `_extract_telegram_msg` (2889),
     `probe_telegram_activity()` (~2955–3028), `_first_marker_line` (3031–3036),
     `_capture_inbound_messages` (the replay hook, ~3005–3027 area).
   - **Keep the OTHER probes:** `probe_http_activity`, `probe_kanban_activity`,
     `probe_prci_activity` — they are general activity sources, not Telegram.
7. **`_default_probes` (lines 3039–3051):** remove line 3049
   `lambda: probe_telegram_activity(),`. Keep 3047/3048/3050. The cycle still has 3 probes
   (http, kanban, prci) — NOT stranded, no empty-probe state.
8. **Comments/docstrings** referencing Telegram (reword, non-functional):
   - line 54 (`# First ~120 chars of the Telegram message...`) — reword/remove (part of probe).
   - lines 2718–2724 probe-block comment header.
   - line 2614 (`# Telegram message. cfg still holds wake_source...`) — reword to generic wake.
   - line 1840–1841 comment precedes deleted TG_QUOTE_CHARS — remove with it.
   - Keep comments explaining `wake_source`/`wake_at` as persistent operator-facing fields
     (2369, 2658, 2683) — these fields survive, reword only if they mention telegram.

---

## hscc_daemon/health.py — PARTIAL (remove Telegram alert leg)
- **Class:** PARTIAL. Keep the health/alert logic; remove the Telegram notification leg.
- **Lines:** 
  - 1503 `from .telegram import notify_operations` — **REMOVE** (lazy import in the executor
    alert path).
  - 1504 `notify_operations(...)` call — **REMOVE**.
  - Keep any desktop/log-based alert that coexists; if this alert was Telegram-ONLY, keep the
    surrounding executor-alert logic but note it now delivers via any non-Telegram path already
    present (or add a desktop call) — do NOT delete the alert itself.
-  **(The `health.py:1` count from grep reflects the telegram.py import inside this block.)**

---

## hscc_daemon/autodown_cli.py — KEEP (no telegram removal; general wake field)
- **Class:** KEEP. Mentions `wake_source` (lines 178, 239–240, 457) which is a general field
  with values `cli`/`cycle`/`autoup`/etc. No telegram-specific removal needed. The
  `wake_source == "telegram"` value simply becomes historical/never-set — leave the field read
  code (rule 2: reading the value stays valid).

---

## hscc_daemon/README.md — PARTIAL (reword docs)
- **Class:** PARTIAL (documentation).
- **Edits:**
  - line 3 (README preamble) — remove "the daemon alerts via Telegram + the active fallback..."
    reword orchestrator alert to desktop/log.
  - lines 22 (`"telegram": { ... }` config JSON snippet) — remove the telegram key block.
  - line 47 (`Telegram alert + fallback`) — reword to desktop alert.
  - lines 78 (`Telegram rate-limited — max 5 alerts per 60s`) — remove.
  - line 89 (`Telegram not sending | Verify chat_id and bot_token`) — remove row.
- **Result:** README no longer documents Telegram.

---

## hscc_daemon tests

### hscc_daemon/tests/test_replay.py — DELETE (telegram-only module)
- **Class:** DELETE. Entire file tests `hscc_daemon/replay` (deleted). All tests build
  `platform=telegram` gateway lines and drive the replay queue. Delete with replay.py.
- **Action:** `git rm hscc_daemon/tests/test_replay.py`.

### hscc_daemon/tests/test_replay_delivery.py — DELETE (telegram-only module)
- **Class:** DELETE. Tests `replay.default_deliver_message` production path, which resolves via
  registry topic → runs orchestrator → posts reply via `telegram.send_message`. All telegram-bound.
- **Action:** `git rm hscc_daemon/tests/test_replay_delivery.py`.

### hscc_daemon/tests/test_telegram_extra.py — DELETE (telegram-only module)
- **Class:** DELETE. Dedicated extra tests for `hscc_daemon/telegram` (`notify_operations`,
  `ENV_FILE`, SSL context, etc.). Module under test is deleted.
- **Action:** `git rm hscc_daemon/tests/test_telegram_extra.py`.

### hscc_daemon/tests/test_autodown.py — EDIT (covers surviving behaviour; remove telegram branches)
- **Class:** PARTIAL — most tests cover surviving autodown machinery and MUST stay. Remove only
  the telegram-specific tests/branches/monkeypatches.
- **Edits:**
  - Lines 327–328 (`record_activity("telegram")` → assert `wake_source == "telegram"`): this
    is the telegram probe test — DELETE these assertions (the probe is gone). If the test
    otherwise covers generic `record_activity` behaviour, re-target to a surviving source
    (`record_activity("http")`/`"kanban"`).
  - Line 1952 `cfg["wake_source"] = "telegram"` (auto-clear-wake test): change to a surviving
    source value (e.g. `"cli"`/`"cycle"`) OR keep the line setting a never-triggering source;
    recommended: set `wake_source = "cli"` so the survive-clear behaviour is still exercised.
  - `TestProbeTelegramActivity` class (lines 2814–~2920): DELETE the whole class (telegram-only).
  - `monkeypatch.setattr(ad, "notify_operations", ...)` at lines 1271, 1696, 1851, 1893, 2065,
    2278, 2313, 2345, 2367, 2408, 2537: these stub the deleted telegram notify path. After
    removal, these stubs are no-ops on a now-gone attribute. **Remove the monkeypatch lines**
    (the assertions they guarded that verify `.called` — e.g. around 1880 "Loud notify:
    critical-priority desktop + ops Telegram both fired" — simplify to assert only the desktop
    notification fired).
  - Comments at 2391, 2519 ("never a real telegram/sparkrun") — reword.
- **Result:** test_autodown.py survives, minus telegram tests/stubs.

### hscc_daemon/tests/test_daemon_ops.py — EDIT (minimal)
- **Class:** PARTIAL.
- **Edit:** line 111 comment "redirects AUTODOWN_FILE (and the activity/telegram state paths)"
  — reword (drop "/telegram"); the redirect itself (offsets) may become moot once the telegram
  probe is gone but the fixture line is harmless if offset file no longer used.
- **Note:** this file does NOT contain telegram-focused tests; only a comment.

### hscc_daemon/tests/test_no_live_hscc_leak.py — EDIT (minimal)
- **Class:** PARTIAL.
- **Edits:** line 64 comment lists `telegram_probe.offset` — reword; line 82 plants
  `state/telegram_probe.offset` — remove once the probe+file are gone (or leave harmless).

### hscc_daemon/tests/conftest.py — EDIT (remove telegram/replay wiring)
- **Class:** PARTIAL.
- **Edits:**
  - line 71 / 92: drop `telegram`/`replay` from import lists (assert-isolated module lists).
  - line 103 `(autodown, "TELEGRAM_OFFSET_FILE", p("state/telegram_probe.offset"))` — REMOVE.
  - lines 109–113 (`replay.QUEUE_FILE`, `replay.REGISTRY_PATH`) — REMOVE (replay deleted).
  - lines 162–173 (`monkeypatch.setattr(replay, "default_deliver_message", ...)` hermetic stub)
    — REMOVE with replay.
- **Result:** conftest no longer wires telegram/replay state paths.

---

## hscc-bootstrap/telegram_choice.py — DELETE (dedicated Telegram module)
- **Class:** DELETE (explicitly named in rules). Resolves the optional Telegram install decision
  (env → --yes → prompt). Entirely Telegram.
- **Action:** `git rm hscc-bootstrap/telegram_choice.py`.

## hscc-bootstrap/strip_worker_telegram.py — DELETE (dedicated Telegram module)
- **Class:** DELETE (explicitly named in rules). Strips seeded TELEGRAM_* creds from worker
  role profiles. Entirely Telegram.
- **Action:** `git rm hscc-bootstrap/strip_worker_telegram.py`.

## hscc-bootstrap/bootstrap.sh — PARTIAL (remove Telegram interview + wiring stages)
- **Class:** PARTIAL (the installer is general; Telegram is an optional sub-stage).
- **Edits:**
  - Line 21 `HSCC_TELEGRAM="${HSCC_TELEGRAM:-}"` — REMOVE.
  - Lines 30–31 `--telegram=yes) HSCC_TELEGRAM=yes ;;` / `--telegram=no) ...` — REMOVE.
  - Line 32 usage string — drop the `[--telegram=yes|no]` segment.
  - Lines 96–106 (Telegram interview block) — REMOVE ENTIRE BLOCK (the "Add Telegram
    integration" ASK/prompt). This removes the `telegram_choice.py` invocation at line 100.
  - Lines 228–233 "Install: enable HSCC plugins + toolsets" — REMOVE the
    `HSCC_TELEGRAM="${TELEGRAM:-no}" ...` env (line 233); call `enable_plugins.py` without it.
  - Lines 238–255 "Install: worker Telegram credential hygiene" — REMOVE ENTIRE BLOCK
    (invokes `strip_worker_telegram.py` at 249).
- **Result:** bootstrap no longer asks about or wires Telegram; all other install stages stay.
  The `TELEGRAM` shell var (lines 101–106) goes away with the block.

## hscc-bootstrap/enable_plugins.py — PARTIAL (remove telegram config-key wiring)
- **Class:** PARTIAL.
- **Edits:**
  - Lines 550–639 section: remove the `telegram.enabled` config-key seeding (the block that
    sets `telegram: enabled: true/false` into `~/.flightdeck/config.yaml` when absent).
  - Lines 845–874 section: remove telegram-related key handling (same key, likely the
    write/seed path).
  - Keep the general plugins/toolsets routing wiring (`plugins.enabled`, `toolsets`, kanban,
    delegation, compaction, fallback provider) — these are Telegram-independent.
- **Result:** `~/.flightdeck/config.yaml` no longer seeds a `telegram` key.

## hscc-bootstrap/README.md — PARTIAL (reword docs)
- **Class:** PARTIAL.
- **Edits:** remove lines 32–37 (the "**Telegram is optional.** ... Add Telegram integration"
  paragraph) and line 51 (`| telegram_choice.py | ...` table row) and lines 57–59
  (the `Telegram:` flags paragraph). Reword the `Flags`/`What it does` text accordingly.

## hscc-bootstrap tests

### hscc-bootstrap/tests/test_telegram_choice.py — DELETE (telegram-only module)
- **Class:** DELETE. Tests `telegram_choice.py` (deleted). E.g. line 205/206 assert the
  "Add Telegram" prompt. Delete whole file.
- **Action:** `git rm hscc-bootstrap/tests/test_telegram_choice.py`.

### hscc-bootstrap/tests/test_strip_worker_telegram.py — DELETE (telegram-only module)
- **Class:** DELETE. Tests `strip_worker_telegram.py` (deleted). Delete whole file.
- **Action:** `git rm hscc-bootstrap/tests/test_strip_worker_telegram.py`.

### hscc-bootstrap/tests/test_enable_plugins.py — EDIT (remove telegram assertions)
- **Class:** PARTIAL — covers surviving enable_plugins wiring. Remove only the telegram-key
  assertions.
- **Edits:** delete the test cases / assertions that assert `telegram.enabled` is seeded
  (both the `--telegram=yes` → true and default → false cases). Keep tests for plugins/toolsets/
  kanban/delegation/compaction wiring.
- **Action:** edit, do NOT delete.

---

## scripts/ — PARTIAL (change delivery target; keep the scripts)

These three cron scripts are **delivery-agnostic**: they emit stdout lines and are offered to
Hermes cron via `--deliver telegram`. Removing Telegram does NOT delete them — the delivery
target must change to a non-Telegram mechanism (desktop, log file, kanban card).

**Explicit no-delivery note:** the scripts themselves are NOT stranded — they are cron jobs
whose delivery channel is a cron parameter. Switching `--deliver telegram` → `--deliver
desktop` (or a log-file/kanban delivery) leaves them fully functional. If the operator prefers
to keep stdout-based delivery, choose the available Hermes desktop/log delivery.

### scripts/escalate_watcher_run.py — PARTIAL (reword; delivery is cron-side)
- **Class:** PARTIAL.
- **Edits:** lines 5–6 docstring (`--no-agent --deliver telegram`) — reword to the new deliver
  target; line 76 comment ("Drive the human alert off stdout (Telegram), not the desktop
  notifier") — reword. No functional telegram code in the script; it writes to stdout.
- **Action:** edit docstring/comments only.

### scripts/install_escalate_watcher.sh — PARTIAL (change --deliver target)
- **Class:** PARTIAL.
- **Edits:** line 4 comment (`--deliver telegram`) — reword; line 35 `--deliver telegram` —
  change to `--deliver desktop` (or chosen target). The cron is still created; the job still
  works; only the delivery transport changes. **NOT stranded.**

### scripts/install_dep_watcher.sh — PARTIAL (change --deliver target)
- **Class:** PARTIAL.
- **Edits:** line 34 `--deliver telegram` — change to `--deliver desktop` (or chosen).
  **NOT stranded.**

### scripts/hscc_cluster_digest.sh — PARTIAL (reword comment; delivery is cron-side)
- **Class:** PARTIAL.
- **Edits:** line 3 comment (`Routed to delivery target (telegram)`) — reword to the new target
  (digest is `--deliver`-parameterised, so no functional change needed).

### scripts/README.md — PARTIAL (reword delivery docs)
- **Class:** PARTIAL.
- **Edits:** line 11 (digest "designed to be delivered to a chat (e.g. the HSCC Telegram
  channel)") — reword/remove "Telegram"; lines 46–47 (sample `--deliver 'telegram:<chat_id>'`)
  — replace with the new delivery target example.
- **Action:** edit docs.

---

## hscc-cluster/_telegram_compat.py — DELETE (dedicated Telegram compat shim)
- **Class:** DELETE. A thin shim re-exporting `notify_operations` from
  `hscc_daemon.telegram` for the workflow hook. Entirely Telegram.
- **Action:** `git rm hscc-cluster/_telegram_compat.py`.

## hscc-cluster/workflow.py — PARTIAL (remove telegram branch from kanban-blocked hook)
- **Class:** PARTIAL.
- **Edits:** in `on_kanban_task_blocked` (lines 233–298):
  - lines 272–278: remove the entire `try: from . import _telegram_compat as _tg;
    _tg.notify_operations(alert) except ImportError: pass` block.
  - Keep the JSONL persistence (lines 280–294) — that is the surviving delivery/history path.
  - Update the docstring lines 237–241 to drop the Telegram-topic sentence (keep the JSONL log
    description).
- **Explicit no-delivery note:** the blocked-task hook is NOT stranded — it still writes the
  `~/.hscc/blocked_tasks.jsonl` entry (line 280–294). Only the Telegram-post leg is removed.
  If the operator wants a live alert, add a desktop `send_macos_notification` — but the hook
  already has a durable log delivery.

## hscc-cluster/templates/4node/deepseek-v4-orchestrator.yaml — PARTIAL (reword comment)
- **Class:** PARTIAL.
- **Edit:** line 21 comment "trusting it for always-on Telegram/agent traffic" — reword
  (drop "Telegram"). No functional telegram in the YAML.
- **Action:** edit comment only.

---

## hscc-api/routes_bootstrap.py — KEEP (docstring/comment references only; reword)
- **Class:** KEEP (no functional Telegram code). All `telegram` matches are within
  comments/docstrings (lines ~180–239) using "Telegram-topic analog"/"Telegram topic" as
  explanatory language for how topics map to projects.
- **Edits:** reword those docstrings/comments to describe topics without the Telegram analogy
  (e.g. "the historical topic id mapped to a project"). Rephrase ALL matches. No code change.
- **Note:** does NOT touch bootstrap/routing logic — nothing to functionally remove.

## hscc-api/routes_orchestrator.py — KEEP (docstring/comment references only; reword)
- **Class:** KEEP (no functional Telegram code). Every `telegram` match is a
  docstring/comment "Telegram-topic analog" / "Telegram topic" explanatory phrase in this
  96k-char file.
- **Edits:** reword those docstrings/comments to remove the Telegram analogy. Rephrase ALL
  matches. No code change.
- **Note:** keep any archive/session_discovery READ of `source == 'telegram'` / topic fields
  INTACT (rule 2) — only the prose around them changes.

---

## Files NOT touched (no telegram reference): no action

`hscc_daemon/daemon_ops.py`, `hscc_daemon/state.py`, `hscc_daemon/desktop.py`,
`hscc_daemon/trigger.py`, `hscc_daemon/lifecycle.py`, `hscc_daemon/escalate_watcher.py`,
`hscc_daemon/cli.py`, `hscc_daemon/autodown_cli.py` (no telegram mention beyond the general
`wake_source` field — KEEP), plus all files in these packages that do not match.

---

## Remaining-lines justification (what intentionally stays, and why)

| Kept reference | Why |
|---|---|
| Registry `Project.topic` field (in `_resolve_identity`, `replay_test_delivery`, session_discovery, routes) | Rule 1 — historical metadata. Mapping topic→project persists. |
| `source='telegram'` / `platform=telegram` READ paths (archive readers, session_discovery, routes_orchestrator) | Rule 2 — reading existing telegram-sourced rows stays valid; only *writing* new ones is removed. |
| `wake_source`/`wake_trigger_text` config *fields* + `autodown_cli.py` status read of `wake_source` | Fields are general wake bookkeeping (`cli`/`cycle`/`http`/`autoup`/...); the `"telegram"` value simply becomes never-set. Reading old rows stays valid. |
| `probe_http_activity`, `probe_kanban_activity`, `probe_prci_activity` (+ their `record_activity("http"/"kanban"/"prci")`) | General activity sources, not Telegram. |
| `send_macos_notification` / desktop notification | Surviving operator-notify delivery. |
| JSONL logs (`blocked_tasks.jsonl`, `task_completions.jsonl`, executor alerts) | Durable non-Telegram delivery/history. |

---

## Execution order (suggested)
1. Delete dedicated modules + their tests: `hscc_daemon/telegram.py`,
   `hscc_daemon/replay.py`, `hscc-bootstrap/telegram_choice.py`,
   `hscc-bootstrap/strip_worker_telegram.py`, `hscc-cluster/_telegram_compat.py`.
2. Delete dedicated test files: `test_replay.py`, `test_replay_delivery.py`,
   `test_telegram_extra.py`, `test_telegram_choice.py`, `test_strip_worker_telegram.py`.
3. Edit `autodown.py` (probe, `_notify`, wake-notify fns, `_default_probes`, imports).
4. Edit `health.py` (remove telegram alert leg).
5. Edit `bootstrap.sh` + `enable_plugins.py` + bootstrap README; edit `test_enable_plugins.py`.
6. Edit daemon test files: `test_autodown.py`, `conftest.py`, `test_daemon_ops.py`,
   `test_no_live_hscc_leak.py`.
7. Edit `hscc-cluster/workflow.py` + template yaml; delete `_telegram_compat.py` (order with 1).
8. Reword `hscc-api/routes_bootstrap.py`, `routes_orchestrator.py` docstrings/comments.
9. Edit `scripts/*` (`--deliver telegram` → new target) + scripts README.
10. Reword `hscc_daemon/README.md`. Run full test suite to confirm 0 telegram references and
    green tests.
