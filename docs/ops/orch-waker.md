# Orchestrator waker — the built-in (`hscc` ≥ 2.15.0)

**Cards:** W1 `t_4cc65dae` (pure core) · W2 `t_2b2a78ae` (delivery) ·
W3 `t_2b34b5c7` (daemon service + CLI + doctor) · W4 `t_1b38b45e` (this doc).
**Code:** `hscc_daemon/orch_waker.py` (module), `hscc_daemon/daemon_ops.py`
(cadence + `CHECKS` row), `hscc-cluster/hscc.py::cmd_orch_waker` (CLI),
`hscc-bootstrap/doctor.py::_orch_waker_check_row` (the doctor row).

What it does, in one line: every 30 s the daemon reads each kanban board's new
card events and, for each board that has a live orchestrator, types **one
batched summary line** into that orchestrator's REPL — so a completed, blocked
or crashed card stops sitting unnoticed until a human types something.

Why it exists: `kanban notify+wake` only reaches gateway chat platforms (zero
subscribers here), Hermes has no wait tool, and an orch SOUL bans polling — so
before this, nothing moved a card forward on its own.

---

## ⚠️ RETIREMENT NOTE — this replaces the `~/bin/orch-waker` stopgap

**Read this section before you turn anything off, and before you turn the
stopgap off.**

The built-in waker **replaces** the operator's stopgap `~/bin/orch-waker` — a
detached loop (`POLL_S/DEBOUNCE_S/MIN_GAP_S = 30/90/300`) that shell-outs to
`~/bin/hermes-inject` and logs to `~/fleet-check/orch-waker.log`. It is not
supervised today: `launchctl list` has no job for it and no plist under
`~/Library/LaunchAgents` names it, so it is a plain background process — it
survives logout only because it was detached (its PPID is 1, i.e. the shell
that started it has exited), and a reboot loses it. The built-in is
behaviour-identical by construction — same event kinds, same labels, same
debounce/rate rules, same nudge text — and it reads and writes the
**stopgap's own state file schema verbatim** (`{"last", "pending", "sent"}`),
so the handover is seamless: the first built-in tick continues from the
stopgap's last ids and **no backlog floods the orchestrators**.

Before you start, capture the stopgap's current behaviour so the handover is
comparable:

```bash
tail -5 ~/fleet-check/orch-waker.log      # real nudges it is delivering now
python3 -c "import json;print(json.load(open('$HOME/.hscc/state/orch_waker.json'))['last'])"
```

Retire the stopgap in this order — the order is the point:

```bash
# 1. install 2.15.0+, restart the daemon so the stream starts
hscc --version                       # must be >= 2.15.0
hscc start                           # (or the launchd restart you normally use)

# 2. verify the built-in is ACTUALLY WAKING, not merely ticking — see below
hscc orch waker status

# 3. only then stop and remove the stopgap
pkill -f orch-waker
rm ~/bin/orch-waker                  # and its launchd/loop entry, if any
```

**The verification must be of the boards, not of the tick stamp.** `status`
prints two independent things: the *stream tick* (proof the daemon loop reached
the check) and the *per-board watermarks* (proof the scan is actually walking
events). A healthy waker shows, twice a minute apart, `last-id` values that have
**increased** and `pending` counts that move. A stream that ticks while every
board is missing from the list, or while `last-id` never moves, means the waker
is **not waking anyone** — do not retire the stopgap on a fresh tick alone.

### Known blocker (open as of this writing): the built-in is currently inert

(tracked as card `t_fa9a2faf`; the fix also has to update this section)

The waker's own state file and the daemon's per-stream state file are **the same
file** — both resolve to `~/.hscc/state/orch_waker.json` (`DEFAULT_STATE_PATH`
vs `state.STATE_DIR/<STREAM_NAME>.json`, and `STREAM_NAME == "orch_waker"`).
Every tick, `waker_tick`'s finally-block stamps the stream through
`state.write_state`, which **overwrites** the `{"last","pending","sent"}` state
the scan just wrote. Consequence: `load_state` reads the stamp-shaped document,
finds no `last` key, and every board re-tail-starts on **every** tick — so no
event is ever batched, no nudge is ever sent, and the audit JSONL stays empty.
Reproduced offline: 6 ticks over a board holding a `completed` event → **0
nudges**, file content = the stream stamp. The suites do not catch it because
every W3 test injects a stream spy (`write=StreamSpy().write`) and a
*separate* `state_path`, so the deployed combination is never exercised.

Until that is fixed (see the blocker card), the **stopgap is the thing that
actually wakes orchestrators. Leave it running.** The built-in's own doctor row
and `status` tick stamp both read healthy while it delivers nothing, so
neither is a sufficient retirement gate — check the board watermarks.

---

## What counts as a wake-worthy event

`KINDS` — everything else a board logs (`spawned`, `reclaimed`, `comment`,
`heartbeat`, unknown future kinds) is invisible to the waker:

| event kind | label in the nudge line |
|---|---|
| `completed` | `done` |
| `blocked` (worker-raised) | `BLOCKED: <reason>` (payload reason, flattened, ≤140 chars) |
| `gave_up` | `GAVE UP` |
| `crashed` | `CRASHED` |
| `timed_out` | `timed out` |
| `spawn_failed` | `spawn failed` |
| `review_requested` | `review requested` |
| `block_loop_detected` | `block loop` |

One line per event, formatted `{task_id} {label} — {title[:70]}`.

## Batch, debounce, rate

Four numbers, all defaults, all overridable (see [Config](#config)):

| rule | value | mechanism |
|---|---|---|
| scan cadence | 30 s | `PERIODIC_INTERVALS["orch_waker"]` (daemon loop) and `poll_s` — the effective scan interval is `max(cadence, poll_s)` |
| debounce | 90 s | a board nudges only once its **oldest** pending line is this old — a burst of events becomes one nudge, not N |
| min gap | 300 s | at most one nudge per board per 5 min; events keep accumulating meanwhile, nothing is lost |
| line cap | 6 | dedup, first 6 distinct lines, then ` · +N more` |

The nudge is one message per board, byte-identical to the stopgap's (one line —
shown here wrapped for width only):

```
[orch-waker] board hscc: t_a1b2c3d4 done — Fix the waker · t_e5f6a7b8 BLOCKED: needs_input — Docs pass — review/merge/unblock as needed, then carry on.
```

Delivery goes through the in-process parity module `hscc-api/repl_inject.py::
inject(profile, msg, mode="auto")` — the same entry `orch_message` and
`hscc kanban pause` use. `mode="auto"` resolves to `/queue` while the orch is
busy and a plain send when idle, so **a nudge never cuts off a running turn**.
Never a shell-out to `~/bin/hermes-inject`, never raw `tmux` from the daemon.

## What gets skipped

A board is silently skipped (pending dropped, watermark still advances) when:

- it is **paused** — `~/.hscc/state/kanban_paused.json`, read through
  `kanban_pause.paused_boards` so the pause tool's `_`-prefixed bookkeeping keys
  are filtered exactly as the pause tool filters them;
- it has **no live orchestrator** — `~/.hermes/profiles/<board>-orch` is not a
  directory. Board → profile uses the repo's single naming rule
  (`kanban_pause.orchestrator_for`), so there is no hardcoded board list. A board
  with no orch is never nudged **and never audited** (there is no REPL to reach);
- the event is a **block raised by the orchestrator or the operator** —
  `run_id IS NULL` in `task_events`. The orch does not need waking for its own
  action. Worker-raised blocks (`run_id NOT NULL`) do wake it.

Two more safety rules: a board seen for the **first time** tail-starts at the
current `MAX(id)` (never a backlog flood), and an **unreadable board** is warned
once per run and skipped — one broken `kanban.db` cannot stall the rest.

Boards covered: every sub-directory of `~/.hermes/kanban/boards/` holding a
`kanban.db`, **plus** the legacy `default` board at `~/.hermes/kanban.db` (which
the stopgap missed). Every connection opens `mode=ro` with a 2 s timeout — the
waker observes a board, it can never mutate, migrate or schema-init one.

## Config

The waker's own file, `~/.hscc/orch_waker.json` (the autodown posture: one small
JSON per feature; **not** a block inside `~/.hscc/config.json`):

```json
{ "enabled": true, "debounce_s": 90, "min_gap_s": 300, "poll_s": 30 }
```

| key | default | meaning |
|---|---|---|
| `enabled` | **`true`** | the master switch. Absent file ⇒ enabled |
| `debounce_s` | 90 | batching window (floor 0) |
| `min_gap_s` | 300 | per-board rate limit (floor 0) |
| `poll_s` | 30 | scan cadence, clamped to `[1, 3600]` |

- **Fail-open, deliberately.** A missing file, an unreadable file, invalid JSON
  or a non-object document all resolve to *enabled at the stopgap's tuning*. This
  is intentionally **not** autodown's fail-closed: a lost file that silently
  stopped the waking is the exact failure this feature exists to prevent.
- **A typo cannot make the loop hot.** A garbage seconds knob falls back to its
  default (booleans and non-finite values rejected — `true` in a seconds field is
  a typo, `Infinity` in a rate limit would silence a board forever).
- **Hot reload, no restart.** The config is re-read every tick, so
  `hscc orch waker disable` from another process takes effect on the next tick.
- **`poll_s` longer than 30 s is a poll gate, not a broken stream.** The loop
  still wakes the stream every 30 s (so staleness stays quiet) but the scan only
  runs every `poll_s`. A `poll_s` *shorter* than 30 s cannot make it scan faster
  than the loop wakes.

## CLI

`hscc orch waker status|enable|disable` — on both CLI entries (unified
`hscc_daemon/hscc.py` and the standalone engine `hscc-cluster/hscc.py`), all
with `--json` for the machine contract. The verbs own only argv parsing +
rendering; the logic is `orch_waker.py`'s, so the CLI, the loop and the doctor
read and write one file through one loader.

Shape of `status` — every field and column below comes from
`hscc_daemon/hscc.py::_waker_status_lines` (the ids are fictional; the double
`sent` on the audit line is real: the ok label followed by the outcome text):

```
$ hscc orch waker status
orch-waker: ENABLED  (poll 30s, debounce 90s, min gap 300s)
  last tick: 1793500600 (12s ago)
  boards:
    hscc: last-id 76173 pending 2
    kronos: last-id 2264 pending 0
  last audit:
    1793500588.311 hscc -> hscc-orch: sent sent
```

- `status` **never writes** and never opens a board DB — safe to run while the
  daemon ticks.
- `enable` / `disable` flip `enabled` through the module's own atomic writer
  (`orch_waker.set_enabled`, tmp + `os.replace`), the same posture as
  `hscc autodown enable`. No hand-editing.
- Nothing is invented when there is nothing to show: never-ticked prints
  `last tick: never`, no boards prints `boards: none tracked yet (first tick
  tail-starts)`, no deliveries prints `last audit: no deliveries yet`.

## State + audit files (schemas)

| file | owner | shape |
|---|---|---|
| `~/.hscc/orch_waker.json` | **operator** | the config block above |
| `~/.hscc/state/orch_waker.json` | **daemon** | the waker state below — ⚠️ also the stream stamp today, see the blocker |
| `~/.hscc/state/orch_waker.jsonl` | **daemon** | audit, one JSON line per delivery *attempt*, append-only |
| `~/.hscc/state/kanban_paused.json` | `hscc kanban pause` | read-only input (the skip rule) |

Waker state — the stopgap's schema, verbatim, nothing more (`empty_state()`):

```json
{ "last":    { "<board>": 76173 },
  "pending": { "<board>": [[1793500000.0, "t_a1b2c3d4 done — Fix the waker"]] },
  "sent":    { "<board>": 1793500090.5 } }
```

`last` = the per-board event-id watermark; `pending` = `[ts, line]` batches not
yet delivered; `sent` = the last successful delivery time (what `min_gap_s`
measures against). Written atomically (tmp + `os.replace`). A corrupt or absent
file reads as a **fresh state** — every board then tail-starts, so a lost state
file can never cause a backlog flood (a *present but unparseable* file is
logged, because silently resetting it would drop pending batches).

Audit line — one per attempt, whether or not it succeeded:

```json
{"ts": 1793500588.311, "board": "hscc", "profile": "hscc-orch", "ok": true,
 "outcome": "sent", "msg": "[orch-waker] board hscc: t_a1b2c3d4 done — …"}
```

`outcome` is `sent`, or `failed: <last 200 chars of the error>`; `msg` is capped
at 300 chars. Append-only, never rewritten, and a failed *write* is logged but
never raises: losing the evidence line must not stop the waker mid-tick, and a
delivery that already happened must not be reported as never attempted. A failed
*delivery* keeps the batch pending for a later tick and audits that retry too.

## Doctor + supervision

The waker is a normal daemon stream, not a private loop: a
`PERIODIC_INTERVALS["orch_waker"] = 30` entry plus a `CHECKS` row, so it rides
the loop's per-stream supervision — its own thread, error isolation, respawn on
thread death — and `hscc verify`'s per-stream staleness math (a 30 s stream,
floor 600 s).

`hscc doctor` gets its own row (`orch-waker (orchestrator waker)`), the verdict
living in `orch_waker.doctor_check` so doctor can never disagree with the daemon
about what "enabled" or "last tick" means:

| situation | verdict |
|---|---|
| `enabled: false` | **ok**, detail `disabled (orch_waker.enabled=false)` — a choice, not a fault |
| enabled, ticked within `2*poll_s + 60 s` | **ok** |
| enabled, stale | **warning** (non-fatal) — the loop is alive, this stream is behind |
| enabled, never ticked, daemon not running | **warning** — "nothing is ticking" is the honest verdict, and doctor is bootstrap's Stage-1 gate (before the daemon ever starts), so a fatal here would abort an install |
| enabled, never ticked, daemon running **and started after this build** | **RED / fatal** — the wiring itself is broken |

The upgrade race matters: doctor is Stage 1 and the daemon restart is a *later*
bootstrap stage, so a live daemon may predate the waker stream entirely.
`_daemon_predates_build` compares `daemon.pid`'s mtime against the module's own
file and downgrades that verdict to a warning; an unverdictable stat answers the
same safe way. The row also honours `HSCC_DIR` (config, stream state *and* the
pid file all relocate together) so a sandboxed bootstrap — which keeps the
operator's real `HOME` — cannot fire a fatal verdict over the live daemon; and
inside a pytest process the row refuses to read anything unless
`_orch_waker_check` is injected.

A stream failure reads as a **failed** stream, never a stalled one: a raising
`run_tick` is isolated and still stamps (with `ok: False`), and a disabled or
poll-gated tick stamps too — so a deliberately-off feature never false-fires
staleness.

## Testing this feature offline

`hscc_daemon/tests/test_orch_waker.py` (core, 37 collected), `test_orch_waker_w3.py`
(52), `test_cli_orch_waker.py` (18) + `hscc-cluster/tests/test_orch_waker_cli.py`
(14), `hscc-bootstrap/tests/test_doctor_orch_waker.py` (8) — 129 cases, counts
re-verified with `--collect-only`. All offline: the core takes
the clock, the board set, the paused set, the orch resolver and the delivery
callable as **injected arguments**, so nothing in the core touches tmux, a real
`~/.hscc`, or a board DB.

⚠️ When you change state-file handling, add a test that drives
`check_orch_waker()` (the real loop entry) with the **real** `state.write_state`
and the **default** `state_path`. Every existing W3 test injects a stream spy and
a separate `state_path`, which is precisely why the path collision above stayed
green through two cards and a full 11-leg suite.
