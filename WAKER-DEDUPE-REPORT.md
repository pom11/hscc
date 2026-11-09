# WAKER-DEDUPE — attribution + fix (t_ff520296)

Branch `wt/t_ff520296` · base `origin/main` @ 3e718832

## STEP 1 — attribution (evidence, not inference)

### Emitter A — hscc waker, owns `[orch-waker] …` (OURS)
- Text built by `hscc_daemon/orch_waker.py:434` `build_message()`
  (`"[orch-waker] board {b}: " + " · ".join(lines) + " — review/merge/unblock as
  needed, then carry on."`), delivered from `tick()` at `orch_waker.py:616-640`
  through the W2 seam `make_inject`/`repl_inject` (entry `run_tick` :1576).
- What it dedupes on today: **one per-board integer watermark**, `st["last"][board]`
  = the highest `task_events.id` already scanned; `scan_board` selects
  `WHERE e.id > after_id` (`orch_waker.py:392-405`). Verified claim from the
  card: there is **no** per-event delivery key — `grep -n
  "last_delivered|delivered_event|event_id|seen_event|dedup" hscc_daemon/orch_waker.py`
  returns **zero** hits. The whole state schema is `{"last","pending","sent"}`
  (+ its own `fail`, `orch_map_fp`) and `pending` stores rendered *text lines*
  only (`[ts, line]`, `orch_waker.py:581`), so once a nudge goes out the waker
  has forgotten *which events* it woke the orchestrator for. It therefore cannot
  answer "has anyone already delivered this event?" — that is the gap.

### Emitter B — gateway kanban notifier, owns `[kanban] Task … completed.` (HERMES-OWNED)
- Repo: **hermes**, not ours. Live install `/Users/desac/.hermes/hermes-agent`;
  nothing in the hscc repo emits `[kanban] ` (grep over `*.py` = zero hits).
- Wake text built by `gateway/kanban_watchers_notifier.py:574`
  `build_wake_text()` → i18n `gateway.kanban.wake.message` (:592) → delivered by
  `wake()` (:631) through `gateway/wake.deliver_wake`, targeting the profile
  recorded on the subscription. Per-kind ping text: `_fmt_completed` (:392).
- What it dedupes on today: **a per-subscription event-id cursor**,
  `kanban_notify_subs.last_event_id`, advanced atomically inside
  `BEGIN IMMEDIATE` by `hermes_cli/kanban_db_notify.py:352`
  `claim_unseen_events_for_sub` (CAS via `_cas_cursor` :384, rewound on delivery
  failure by `rewind_notify_cursor` :421). Key is
  `(task_id, platform, chat_id, thread_id)`; the wake only happens when
  `delivery_mode` ∈ {`notify+wake`, `wake`} (`wake_agent`, notifier :518).
- Ownership verdict: the second channel is **hermes-owned** and cannot be
  changed from the hscc repo → the dedupe goes on our side (the waker suppresses
  an event the gateway already delivered), with the symmetric upstream half
  filed as a named follow-up.

### Board evidence — ONE event row, TWO deliveries
Read-only (`mode=ro`) queries of `~/.hermes/kanban/boards/hscc/kanban.db`,
script `scratch/wd_evidence.py`:

```
task t_24e1fd68   58 task_events rows
  events in the waker's KINDS: 1  ->  id=93871 kind=completed created_at=1794241332
  kanban_notify_subs rows: 1
    platform=local chat_id=local-41d4…  notifier_profile=hscc-orch
    delivery_mode=notify+wake  last_event_id=93871  last_ping_event_id=93871

task t_75b25a6e   109 task_events rows
  events in the waker's KINDS: 2  ->  id=93736 review_requested @1794239739
                                       id=93770 completed        @1794240139
  kanban_notify_subs rows: 1  (notifier_profile=hscc-orch, delivery_mode=notify+wake,
                               last_event_id=93770)
```

That is the whole case: t_24e1fd68 has exactly **one** `completed` row (93871) and
the gateway's own cursor sits **exactly on it** (`last_event_id=93871`) — the
gateway claimed and woke for it — while the waker separately nudged the same row.
Two messages, one event. t_75b25a6e has exactly **two** such rows and produced
four messages (2 × 2), matching the report. Not a replay, not two events.

The sub row also gives the identity link that makes a cross-channel dedupe
possible at all: `notifier_profile=hscc-orch` is exactly the profile
`orch_waker.resolve_orch("hscc")` (→ `orch_name` → `hscc-orch`) injects into, and
`delivery_mode=notify+wake` is exactly the mode that makes the notifier wake an
agent. Same event id, same board DB, same receiving brain.

## STEP 2 — the fix (ours, identity-based)

The gateway already keeps the durable record of what it delivered — the
subscription cursor in the *same* board DB the waker already opens read-only.
So the waker does not invent a second ledger; it consults that one at
**delivery-decision time**:

`gateway_wake_cursors(db, tasks, profile)` returns, per task, the highest
`task_events.id` a wake-bearing subscription *named after the waker's own
orchestrator profile* had already claimed, and only for subscriptions that
existed when the event was written. A pending line whose event id is at or below
that cursor is dropped before the batch is ever sent.

Why that is identity and not wall-clock: the key is
`(task_id, kind, event created_at, event id)`; nothing compares two events to
each other, so two genuine state changes one second apart stay distinct (ids
differ). Nothing consults task *status*, so a `done` card is still a wake when
nobody delivered it (crash, missed tick, gateway down).

Ordering that makes "either order → one wake" hold: the notifier claims on a ~5 s
cadence while the waker cannot deliver before `DEBOUNCE_S=90` has aged the batch,
so whichever emitter finishes second is the waker — suppression is decided at
delivery time from the then-current cursor, so it covers "waker scanned first,
gateway claimed during the debounce" as well as "gateway delivered first".

## Tests

TBD
