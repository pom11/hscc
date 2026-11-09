# UPSTREAM: make a kanban event wake its recipient **once** across emitters

Card `t_f26d89c0` · base `origin/main` @ `2e477e15` · hermes live install `045881137d`
Status: **filed upstream** as
[NousResearch/hermes-agent#152206](https://github.com/NousResearch/hermes-agent/issues/152206)
— proposed, not implemented. Cross-linked into #144882 (the wake-path map) and #146361
(same `uuid4` root cause).
Follows `t_ff520296` (PR #265, merged `2e477e15`), which fixed only the half this
repo owns. Full attribution for the doubling: `NOTES-t_ff520296.md` (in that commit).

**Anchor provenance.** Line numbers below are the fleet-pinned runtime
(`~/.hermes/hermes-agent` @ `045881137d`). Each was re-checked against upstream `main`
@ `a5664060c1` and matches, except three in `hermes_cli/kanban_db_notify.py`:
`claim_unseen_events_for_sub` :352 → :372, `_cas_cursor` :384 → :404,
`rewind_notify_cursor` :421 → :441.

## TL;DR — the ruling

Two emitters wake the same orchestrator profile for the same `task_events` row:

| emitter | wake text | dedupes on | owner |
|---|---|---|---|
| gateway kanban notifier | `[kanban] Task … completed.` | per-subscription cursor `kanban_notify_subs.last_event_id` | hermes |
| hscc orch-waker | `[orch-waker] board …` | per-board high-water id + a read-only read of the notifier's cursor | hscc |

`t_ff520296` taught the waker to read the notifier's cursor and stand down. The
symmetric case — **the waker delivered first, the notifier has no idea** — cannot be
fixed from this repo, because no durable record in hermes says "profile P was already
woken for event E" in a form a *second* emitter can write.

Hermes already owns a durable per-session wake ledger (`session_admissions`) with an
exact-identity UNIQUE key. The kanban wake path throws that property away by minting
a fresh random identity on every call. **Proposal: stop minting, and give the one
missing record — a wake claim keyed by event identity — a home in the board DB where
both an in-tree notifier and an out-of-process waker can reach it.** Not a new
subsystem: two functions and one small table in the DB the waker already opens.

## 1. Root cause, verified in the live tree

The notifier's push wake does not post a turn directly; it admits one to the
session's durable FIFO:

- `gateway/kanban_watchers_notifier.py:631` `wake()` → `gateway/wake.py:127`
  `deliver_wake()` → `gateway/wake.py:145` `admit_route_wake(runner, adapter, source,
  text, "kanban-wake", category)`.
- `gateway/session_automation.py:821` `admit_route_wake()` builds the admission
  identity at **`gateway/session_automation.py:837`**:

  ```python
  identity = json.dumps([producer, key, uuid.uuid4().hex], separators=(',', ':'))
  ```

  The third element — the dedupe token — is **random per call**.
- The fence it feeds is exact-identity keyed, and it works:
  `hermes_state_runtime.py:130` `admit_session_input()` returns the existing row on an
  exact replay (`hermes_state_runtime.py:157-160`), the row lives under a UNIQUE
  `(principal_id, target_session_id, request_id)` key, and
  `hermes_state_runtime.py:187` `find_session_admission()` is a **public read** of
  exactly that key ("the admission at its exact durable identity").

So the substrate the card asked for (option 1's "durable already-woken-for record",
option 3's "ask the notifier") **already exists** — it is `session_admissions`, and
the write that populates it is transactional with the wake. It cannot help only
because nobody passes a deterministic token: every kanban wake, cron wake
(`gateway/session_cron.py:60`), loop tick and goal continuation
(`gateway/run_goals.py:345`, `:518`) is by construction a never-before-seen identity.
That single `uuid4()` is also why #146361 (one non-push wake running the same agent
turn N times) has no idempotency key to hang on.

## 2. Why "just pass the event id to the existing fence" is not enough

Three facts each kill a slice of that idea. Verified, not assumed.

1. **A cross-emitter duplicate is an error, not a replay.** `admit_session_input`
   replays only when `payload_digest` matches (`hermes_state_runtime.py:158`);
   otherwise it raises `RuntimeStoreError('admission_conflict',
   ADMITTED_REQUEST_CONFLICT)` (`hermes_state_runtime.py:38`). The two emitters build
   different text, so identical identities across emitters produce a *refusal*, not a
   suppressed wake. Useful as an answer to "was it already woken?", unusable as an
   invisible dedupe.
2. **`state.db` is fenced, and the second emitter is out of process.** Admission goes
   through `authority._require_admission_open()`, an owner epoch and a principal
   (`gateway/session_automation.py:605` `admit_automation`). A plugin/daemon writing
   admission rows into `state.db` by hand is off the table by design — and it is the
   same rule this fleet applies to hermes state generally.
3. **The waker's transport is a keystroke, deliberately.** It injects through
   `~/bin/hermes-inject`, which types into the tmux pane running the operator's REPL
   (bracketed paste, `/queue` when busy). Its header comment records why the API relay
   is not a substitute: the relay types into a *second* REPL that `hermes serve`
   hosts, and a REPL on the screen never sees it. So "hermes owns both emitters behind
   one gate" (option 2) is not available to this emitter by moving code — the gate has
   to be reachable from outside the delivery path.

Conclusion: thread the identity through the fence (it is cheap and it fixes retry
amplification), **and** add one small cross-process claim in the store both emitters
already share — the board DB, which the waker already opens read-only
(`hscc_daemon/orch_waker.py:543` `gateway_wake_cursors`) and the notifier already
writes (`hermes_cli/kanban_db_notify.py:352` `claim_unseen_events_for_sub`).

## 3. Proposal

### 3.1 The wake-once claim (the symmetric half)

One additive table in the **board** DB, next to `kanban_notify_subs`
(`hermes_cli/kanban_db.py:957`, migrations in `hermes_cli/kanban_db_connect.py:1093`):

```sql
CREATE TABLE IF NOT EXISTS kanban_wake_claims (
  profile       TEXT NOT NULL,   -- the recipient profile woken ("hscc-orch")
  task_id       TEXT NOT NULL,
  event_id      INTEGER NOT NULL,
  emitter       TEXT NOT NULL,   -- "notifier" | external id, e.g. "hscc-orch-waker"
  claimed_at    INTEGER NOT NULL,
  confirmed_at  INTEGER,         -- set once the wake was actually accepted
  PRIMARY KEY (profile, task_id, event_id)
);
```

Two public functions in `hermes_cli/kanban_db_notify.py` (the module that already
owns the notifier's dedupe — no new import boundary), both `write_txn` + CAS in the
style of `_cas_cursor` (`hermes_cli/kanban_db_notify.py:384`):

```python
def try_claim_wake(conn, *, profile, task_id, event_id, emitter,
                   now=None, lease_s=120) -> bool
    """True = you owe this wake. False = someone already claimed it.
    A claim is a short lease: an unconfirmed claim older than lease_s stops
    suppressing and may be re-taken, so a crashed emitter mutes one event for
    at most lease_s seconds."""

def confirm_wake(conn, *, profile, task_id, event_id, emitter) -> None
    """Mark the claim delivered (called after the wake is accepted)."""

def wake_already_claimed(conn, *, profile, task_id, event_ids,
                        now=None, lease_s=120) -> set[int]
    """Read-only ask — the public hook (option 3). Never raises to a caller
    that is deciding whether to deliver: on any error it returns set()."""
```

Insertion points:

- **Notifier** — `gateway/kanban_watchers_notifier.py:748` `deliver()`, after the
  pings and before `wake()`: drop from `self.wake_kinds` every event whose claim is
  already held for `sub["notifier_profile"]`; if none remain, skip `wake()` and
  `_log_woke()`. Claim → `wake()` → confirm; on `WakeNotAccepted`/exception, release
  (delete) so the other emitter can still take it — mirroring the existing
  `rewind_notify_cursor` discipline (`hermes_cli/kanban_db_notify.py:421`).
  **Never** touch the human ping path or `advance()` (`:816`): the cursor is the
  notifier's own retry mechanism and a skipped wake is still a completed delivery.
- **External waker** — claim before it injects; if the claim is held, stand down
  (that is today's `gateway_already_delivered` decision, moved from inference to fact).

### 3.2 Identity-threading the fence (companion, independently useful)

`admit_route_wake(..., dedupe: Optional[str] = None)` →
`json.dumps([producer, key, dedupe or uuid.uuid4().hex])`, plumbed through
`deliver_wake(..., dedupe=None)`; the kanban notifier passes
`f"kanban:{board}:{task_id}:{sha1(sorted(event_ids))}"`.

- Keep the identity a JSON **list whose first element is the producer** —
  `move_route_wakes()` (`gateway/session_automation.py:653`) parses `request_id` and
  only carries route wakes whose head is in `_ROUTE_WAKE_PRODUCERS`
  (`gateway/session_automation.py:639`) across a `/new` rotation. An opaque string
  identity would silently stop wakes from surviving a session reset.
- Default `None` → byte-identical to today: fail-open by construction.
- This is what makes a same-emitter retry idempotent (the #146361 class) and makes
  "was event E woken?" answerable from `find_session_admission` without a second
  store. It is **not** a substitute for §3.1 (see §2.1).

## 4. Posture — carried from t_ff520296, keep it upstream

- Dedupe key is **event identity** `(task, kind, created_at, event id)`. Never
  wall-clock proximity, never task status. Two genuine changes in the same second
  stay two wakes (`t_75b25a6e`: two rows → two wake sets is *correct*).
- Every unknown fails open towards **DELIVERING**: missing/locked table, unreadable
  row, unparseable schema, a kind the notifier cannot wake for (`_WAKE_KINDS`,
  `gateway/kanban_watchers_notifier.py:50`, e.g. `spawn_failed`), an emitter that has
  never claimed — all deliver. A doubled wake is the status quo; a dropped wake is a
  regression.
- Do **not** "fix" this by suppressing `done` wakes for cards already in `done`;
  that drops the wake a crashed or missed worker was supposed to recover.
- Claims are keyed on the **recipient profile**, not the subscription: the point is
  "one brain woken per event", and a profile may be reached by several subs.
- Partial-overlap batches double up rather than dropping (exact-match only): the
  notifier wakes once per claimed *range*, the waker once per debounced batch
  (`debounce_s=90`), so the sets differ — status quo, and honest.

## 5. When this lands, hscc retires its own stopgap

`hscc_daemon/orch_waker.py:543` `gateway_wake_cursors()` and `:602`
`gateway_already_delivered()` exist only to *interpret* a cursor whose meaning is
"claimed", not "delivered" — which is why they need guard rails (sub must predate the
event, kind must be wake-bearing, anything unknown → deliver). With §3.1 they are
deleted and the waker calls `wake_already_claimed(...)` / `try_claim_wake(...)` on
the same board DB it already opens. That removal is the acceptance test for the
upstream API: if the stopgap cannot be deleted cleanly, the API is wrong.

## 6. Tests the upstream PR must carry

| case | pins |
|---|---|
| waker claimed first, notifier ticks → no wake, cursor still advances | the symmetric half this card exists for |
| notifier claimed first, waker asks → False | the direction #265 does with cursor inference |
| claim unconfirmed past `lease_s` → claimable again, wake fires | no permanent mute from a crash |
| claim released on `WakeNotAccepted` → other emitter still wakes | no dropped wake from a failed send |
| two events, same task, same second, ids 1/2 → two wakes | the identity-not-wall-clock decision |
| table absent / read raises → wake | fail-open posture |
| `spawn_failed` (not wake-bearing) → never suppresses | kind boundary |
| `dedupe=None` → identity shape unchanged | default-off for §3.2 |
| deterministic dedupe survives `/new` (route wake carried) | `_ROUTE_WAKE_PRODUCERS` parsing |

## 7. Prior art upstream (read before reviewing; do not re-propose)

- **#144882** — tracking: consolidate claim/lease/settle + wake paths; design doc
  #136009. This proposal is a wake-path primitive and belongs under that map; it does
  not add a new subsystem.
- **#146361** — `gateway/wake.py` retries on client timeout so one non-push wake runs
  the same turn N times (no idempotency key). §3.2 supplies the key; same root
  symptom class, different emitter pair.
- **#129862** (open, szihs) — wake persistence receipt (`X-Hermes-Turn-Persisted`,
  `require_persist_ack`). Complementary: a receipt answers "did the turn commit",
  a claim answers "should anyone else wake for this". §3.1's `confirmed_at` wants
  #129862's receipt when it lands.
- **#129876** (open) durable task notifications, **#109277** (open) persist delivery
  obligations + quarantine ambiguous outcomes, **#107902** (open) kanban notifier
  dedupe — all in this neighbourhood; scope must not overlap silently.
- **#145581** (open) per-subscription kind allowlist — changes which kinds are
  wake-bearing, so it changes what may ever be suppressed.
- **#49190** — generalize kanban notifications into an event substrate with a
  delivery-adapter registry. If that lands, §3.1 becomes the registry's dedupe key and
  should be shaped to fold into it rather than compete with it.

## 8. Non-goals

- Not moving the orch-waker into hermes (option 2): §2.3 — the transport it needs is
  a keystroke into an operator-facing REPL, not an admission.
- Not touching the card→human ping path (`trigger.py`, cooldowns) — untouched in
  `t_ff520296` too. If humans see doubling, that is its own card.
- Not a config knob. A claim that suppresses only what another emitter confirmed (or
  is actively leasing) cannot drop a wake that would otherwise have fired.

## 9. LEFT

1. ~~File §3 + §6 against hermes as one issue~~ — **done**: filed as
   [hermes-agent#152206](https://github.com/NousResearch/hermes-agent/issues/152206)
   with the anchors above (re-verified on upstream `main`, not just the pinned fork),
   cross-linked into #144882 and #146361 rather than opening a competing third fix.
2. Upstream PR: §3.2 is small and self-contained (3 signatures + 1 test module) —
   worth landing first; §3.1 needs the migration + notifier insertion.
3. hscc side after upstream ships: delete `gateway_wake_cursors` /
   `gateway_already_delivered`, retarget `test_orch_waker_dedupe.py`'s six required
   cases at the public API, and drop the `suppressed=` stopgap wording from the
   stream stamp.
4. ~~`local`-platform wake branch: confirm whether the hscc-orch sub
   (`platform=local`, profile `hscc-orch`) takes the push branch
   (`gateway/wake.py:135`) or the self-post branch (`:153-161`); only the former
   reaches the fence, so a self-post setup needs §3.2's token on
   `_self_post_chat_completion` too.~~ — **resolved by `t_12350485`: the PUSH branch.
   §3.2 reaches our path, so the ask does NOT extend to `_self_post_chat_completion`.**

   **The branch** (every anchor re-checked at runtime
   `045881137d6a177cac690b1d373316ce8c271f43`, the same pin as the provenance header;
   session id redacted to `local-<session-id>`):
   `platform=local` → `LocalSessionAdapter` (`gateway/session_local.py:21`), which never
   sets `supports_async_delivery` and so inherits `True`
   (`gateway/platforms/base.py:1831` — the only `= False` in the tree is
   `APIServerAdapter`, `gateway/platforms/api_server.py:1039`) → `adapter_supports_push`
   (`gateway/wake.py:30`, body `:35`) is True → `deliver()` records it at
   `gateway/kanban_watchers_notifier.py:766` → `wake()` takes the push leg `:646-673` and
   calls `deliver_wake(..., source=_source)` at `:671` → `gateway/wake.py:135` →
   `:145 admit_route_wake(runner, adapter, source, text, "kanban-wake", category)` →
   `gateway/session_automation.py:821` LOCAL case `:838-851` → `admit_automation` `:850`
   → the `session_admissions` row. The self-post leg (`notifier.py:635`; `wake.py:153-161`,
   def `:231`) sits behind `adapter_supports_push` being **False**, so it is structurally
   unreachable for this sub — not merely untaken. `LocalSessionAdapter.__init__` sets
   `gateway_runner` on purpose (`gateway/session_local.py:27-29`) precisely so this push
   wake rides the authority FIFO.

   **Runtime proof** (`t_12350485`, read-only via hermes' own `SessionDB(path,
   read_only=True)` — no CLI, no `sqlite` by hand, no writes): **9** rows in the
   `hscc-orch` profile's `session_admissions` with
   `request_id = ["kanban-wake","agent:hscc-orch:local:dm:local-<session-id>","<32-hex uuid4 nonce>"]`,
   joined **9/9 (0 unmatched)** to the 9 `kanban notifier: woke agent … profile=hscc-orch`
   lines in the live `gateway.log`, each row carrying the `uuid.uuid4().hex` nonce minted
   at `gateway/session_automation.py:837` — i.e. the exact token §3.2 replaces, on our
   path. One task (`t_75b25a6e`) woke twice with two different nonces: the retry
   amplification §3.2 removes, measured on the branch we actually take.

   **Consequence for the ask:** §3.2 is enough for hscc. It lands on the branch our
   notifier wake takes, so **nothing is added to NousResearch/hermes-agent#152206** —
   that follow-up was conditioned on the self-post branch and #152206 stays untouched.

   **Two caveats that survive the resolution** (both measured in the same store; they
   bind §3.2, and they are why §3.1 is still the load-bearing half):

   - **(a) a fence row is evidence, not a receipt.** `admit_route_wake` returns `False`
     when no authority/route entry serves the key (`gateway/session_automation.py:834-835`)
     or the local route holds no live session (`:844-845`); `deliver_wake` then still
     delivers, through `admit_internal_event` (`gateway/wake.py:149-151`), writing **no
     admission row at all**. 0 of the 9 observed wakes took that fallback — a live branch,
     not a hypothetical. So "was event E woken?" is answerable from the fence only *in the
     affirmative*: a row proves yes, absence proves nothing. §3.1's claim in the board DB
     remains the authority.
   - **(b) the fence is not a durable index.** On session *retirement* admission rows are
     copied into `state_meta` and removed from `session_admissions`
     (`hermes_state_mutation_retirement.py:103-141`), after which they are reachable only
     by `admission_id` (`get_session_admission`, `hermes_state_runtime.py:180`);
     `find_session_admission` (`:187`) no longer sees them. So §3.2's dedupe token must be
     **re-derivable from event identity**, never read back out of the fence. The
     `f"kanban:{board}:{task_id}:{sha1(sorted(event_ids))}"` shape in §3.2 already is —
     keep it that way: no per-wake counter, no clock, nothing that needs the row to
     still exist.
