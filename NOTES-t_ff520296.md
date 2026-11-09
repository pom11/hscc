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
    platform=local chat_id=local-<session-id>  notifier_profile=hscc-orch
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
**delivery-decision time** (`hscc_daemon/orch_waker.py`):

- `event_identity(ev)` → `[task, kind, created_at, event_id]`, now persisted as
  the third element of each `pending` entry (the stopgap's `[ts, line]` shape
  stays readable and simply means "not suppressible" — fail-open, so the
  takeover file and a rollback to an older build are both safe).
- `gateway_wake_cursors(db, tasks, profile)` → per task, every
  `(last_event_id, sub_created_at)` pair for subscriptions whose wake lands in
  **this waker's own orchestrator profile** (`notifier_profile == resolve_orch(board)`,
  `delivery_mode ∈ {notify+wake, wake}`). Read with `mode=ro` + the same
  2 s budget as `scan_board`; it never writes to a board.
- `gateway_already_delivered(ident, cursors)` → True only when a matching sub
  predates the event AND its cursor has passed that event id.
- `tick(..., wake_ledger=)` consults them right after `_clean_pending`, before
  the debounce math, and persists the drop. `run_tick` threads the seam;
  `waker_tick` stamps `suppressed=N` and `_tick_message` names it.

Why that is identity and not wall-clock: the test is `claimed >= event_id` for
one specific row. Nothing compares two events to each other and no time window
is involved, so two genuine state changes in the **same second** stay two wakes
(pinned by `test_two_events_same_task_same_second_are_not_collapsed`, where the
gateway claimed id 1 of 2). Nothing consults task *status*, so a `done` card
nobody delivered still wakes (crash, missed tick, gateway down — pinned by
`test_one_event_gateway_never_delivers_wakes_exactly_once`).

Why the ordering argument holds in either direction: the notifier claims on a
~5 s cadence, while a batch cannot be delivered before `debounce_s=90` has aged
it. The waker is therefore by construction the emitter that finishes second, and
suppression is evaluated from the cursor as it is *at that moment* — so "waker
scanned first, gateway claimed during the debounce" is suppressed just like
"gateway delivered first". The residual race (the notifier claims *after* our
delivery) is not observable from this side; that is the upstream half below.

Three deliberate fail-open edges, each one "deliver" rather than "suppress",
because a doubled wake is the status quo and a dropped wake is a regression:
unreadable/missing ledger → deliver; entry without an identity → deliver;
event kind the notifier cannot wake for (`spawn_failed`) → deliver, never
suppress. A subscription registered *after* the event also never suppresses —
`add_notify_sub` seeds a new sub caught-up to `MAX(id)`, which is a boot policy,
not a delivery.

Anti-silent-inert (the t_fa9a2faf / `orch_waker.py:356` rule): every suppression
logs at INFO with its event id, `report["suppressed"]` carries the list out of
`tick`, and the stream stamp gains `suppressed=N` plus
`"N wake(s) suppressed as gateway-delivered"` in the human message. A dedupe
eating every event shows up as a big number in the field doctor already prints,
not as a healthy-looking "steady".

## Tests

`hscc_daemon/tests/test_orch_waker_dedupe.py` — 18 tests. The other emitter is
*modelled*, not stubbed away: `FakeGateway` claims an event by advancing
`kanban_notify_subs.last_event_id` in the board DB (what hermes'
`claim_unseen_events_for_sub` does atomically) and records its own wake, so
assertions count wakes **across both channels**. The fixture's
`kanban_notify_subs` mirrors hermes' real DDL (`hermes_cli/kanban_db.py:957`).

Required by the card:

| case | test |
|---|---|
| same event, gateway first → one wake | `test_one_event_gateway_first_wakes_the_orchestrator_once` |
| same event, waker first → one wake | `test_one_event_waker_first_wakes_the_orchestrator_once` |
| two distinct events seconds apart → two wakes | `test_two_distinct_events_seconds_apart_wake_twice` |
| same task + same SECOND, ids 1/2, gateway claimed only 1 | `test_two_events_same_task_same_second_are_not_collapsed` |
| restart: state persisted, replay → still one wake | `test_replay_after_restart_still_wakes_once` |
| nobody delivered → still exactly one wake | `test_one_event_gateway_never_delivers_wakes_exactly_once` |

Plus the fail-open edges (late-registered sub, `notify`-only sub, another
profile's sub, `spawn_failed`, board without the table, raising ledger), the
batching-vs-dedupe separation, "suppressed is reported not silent", "a dropped
event is never re-examined", and the reader's own contract (including that it
never writes to the board).

One existing assertion changed on purpose: `test_takeover_continues_from_hand_
written_stopgap_state` pinned every pending entry at `len(p) == 2`. It now pins
`[2, 3]` — the inherited stopgap line plus the freshly scanned line with its
identity — which is the documented additive-schema contract.

## Command + result

```
~/.hermes/hermes-agent/venv/bin/python -m pytest -q -p no:cacheprovider \
    hscc_daemon/tests/test_orch_waker_dedupe.py
18 passed in 0.17s

~/.hermes/hermes-agent/venv/bin/python -m pytest -q -p no:cacheprovider \
    --ignore=hscc_daemon/tests/test_cli_orch_waker.py hscc_daemon/tests
HEAD     3460 passed in 81.69s
baseline 3442 passed in 82.58s   (origin/main @ 3e718832, same invocation, back-to-back)
```

The delta is exactly the 18 new cases. `test_cli_orch_waker.py` is excluded
because a solo pytest invocation in this repo cannot import `hscc` (no top-level
`hscc` module is tracked here) — it errors **identically at origin/main**, so it
is not this diff; `scripts/run_tests.sh` supplies the environment it needs.

`git status --short` empty after the commit.

## LEFT (named, not done here)

1. **Upstream (hermes): the notifier has no idea a second emitter exists.** The
   symmetric half — the notifier suppressing a wake an orchestrator waker already
   delivered — cannot be done from this repo. It needs hermes to either expose a
   "already woken by" hook or own both emitters. File against
   `gateway/kanban_watchers_notifier.py::build_wake_text` /
   `hermes_cli/kanban_db_notify.py::claim_unseen_events_for_sub`.
2. **Residual race**: if the notifier claims an event *after* the waker delivered
   it (possible only when the notifier's tick is delayed past `debounce_s`), both
   fires still happen. Unobservable from this side without a durable "the waker
   already woke for event X" record that hermes could consult — which is the same
   upstream feature as (1).
3. **Human-notification path untouched (per the card's scope).** The same
   subscription cursor feeds the card→human pings, and `trigger.py` cooldowns were
   not examined. If humans see the same doubling, that is a separate card.

