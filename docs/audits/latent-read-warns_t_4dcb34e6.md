# The four latent read-path WARNs: verdicts and the fix that closes each one

Card `t_4dcb34e6`, child of `t_88abe739` (PR #721,
`docs/audits/read-surface-logging_t_88abe739.md`). Base `78be7fe3` (`origin/main`).
All `file:line` citations below were re-verified against this base — they match the
audit's numbers exactly, so the audit's map is still current.

The parent measured five read doors that write `$HSCC_HOME/daemon.log` **today**
(one borrowed seam, `autodown._load_kanban_db_or_default`) and named four further
WARN leaves that are **latent**: they write the same file only on a host that has
the state they need — an env pin that conflicts with the board being read, or a
malformed state file. A sandbox that plants no such state cannot see them, which is
why the doctor door guard is structurally blind to them.

## Verdicts

| # | leaf | reached by | verdict |
|---|---|---|---|
| 1 | `board_pin._emit` — `hscc_daemon/board_pin.py:192` (log `:203`, budget `_WARNED:84`) ← `guard_board(mode="read")` `:226` ← `open_board:232` / `open_board_ro:257` / `resolve_board_db_path:284` | documented read doors: `hscc kanban blocked`, `hscc kanban stale`, `hscc kanban monitor`, `hscc cards monitor`, `hscc autodown status` | **FIX** |
| 2 | `node_pauses._warn` — `hscc_daemon/node_pauses.py:96` (log `:106`) ← the pause-file parser (`load:336` → `_normalize`/`_normalize_entry`/`_clean_str_list`) | `hscc node paused`, whose own help says *"Read-only — this reads ~/.hscc/node_pauses.json and nothing else"* (`hscc_daemon/hscc.py:1423`) | **FIX** |
| 3 | `kanban_live._note_caps_unreadable` — `hscc_daemon/kanban_live.py:145` (log `:156`, budget `_CAPS_READ["warned"]:142`) ← `_read_caps:187` ← `collect:387` | `hscc kanban monitor`, `hscc cards monitor` (`kanban_cli.py:348,357`; `cards_live.py:635` via `cmd_cards:1056`) | **FIX** |
| 4 | `kanban_pause._warn_pin_once` — `hscc_daemon/kanban_pause.py:200` (log `:213`) ← read-refused branch `:291` in `read_child_env:262` | `list_cards:339` only, whose only callers are `pause:413` (`hscc kanban pause`) and `enforce:516` (`hscc kanban enforce` + the daemon's `kanban_enforce` stream) | **ACCEPT** |

Fixes ship in this branch (§"Shipped"); the accept carries the argument in §4.

## The shape, and why it is not "mute the WARN"

`t_88abe739` §5 fixed the shape for this class and both fix cards carry it:
borrow the WARN route for one read and give it back in `finally`, **together with
the module's once-per-process `warned` budget** — never mute the budget itself, and
never touch the module's emitter, which every existing suite pins
(`test_board_pin.py:159-169` captures through `daemon_ops.log`;
`test_node_pauses.py:579-586` pins `_warn`'s stderr fallback). Muting the emitter
would weaken both, and the daemon's loops — which the audit *accepted* as foreground
diagnostics — must keep writing the file exactly as they do now. So the mute goes on
the **borrower** (the documented read door), scoped to the one call.

One deliberate change from `autodown.silent_read()` (PR #723), and it is the whole
point of this card's fix: **the borrowed route echoes to stderr instead of writing
the file.** For the kanban-lib seam the muted fact survives in a payload
(`_KANBAN_LOAD.reason`, surfaced as `errors`/`kanban_reason`), so silence there loses
nothing. For these three surfaces there is *no* payload:

* `board_pin`'s `PinConflict` is returned to the caller but no door publishes it
  (`kanban_live.py:404` names `open_board`'s warning as the operator's only notice);
* `node_pauses.load` answers a corrupt overlay with `{"pauses": {}}` — the corruption
  exists **only** in the WARN (`node_pauses.py:336-352`, "fail-loud on CORRUPTION");
* `collect()["caps"]` is `(None, None)` whether the operator set no cap or the config
  could not be read; the difference lives only in `_CAPS_READ["reason"]`
  (`kanban_live.py:140-142`).

So a silent mute would turn a loud operator misconfiguration into a silent one —
the fail-open PR #716 called out ("the read is allowed to be quiet; it is not allowed
to spend somebody else's alarm", and here not allowed to *hide the fact either*).
`stderr` is not the invariant: the audit's own worker note says so ("the stderr echo
is not the invariant; the file is"), and `daemon_ops.log`'s docstring
(`hscc_daemon/daemon_ops.py:597-620`) why — stdout is the machine channel, stderr is
where a diagnostic belongs. Two consequences, both improvements:

* **Nothing goes invisible.** `daemon_ops.log` echoes to stderr *only when the daemon
  is not running* (`os.path.exists(pid_file)` gate, `daemon_ops.py:630`), so today a
  CLI read on a host with a live daemon leaves the operator with the log file as
  their only notice. The muted route echoes unconditionally.
* **The daemon loses nothing.** `install.py:47-50` points `StandardOutPath` *and*
  `StandardErrorPath` at `~/.hscc/daemon.log`, and the muted routes are entered only
  by CLI doors — the daemon's own loops never enter them, so their file writes are
  byte-identical. Where a daemon thread *did* end up on a muted route, its stderr is
  the same file by redirect.

Budgets are restored, not spent: `board_pin._WARNED`, `_CAPS_READ["warned"]` (and
`_CAPS_READ["reason"]`, which is the fact itself) are captured on entry and put back
in `finally`, so a long-lived process that imports a door still keeps its one WARN
per (pin, board) for the path that owns it. `node_pauses` has **no** budget — every
malformed field warns on every parse — so there is nothing to restore there, and the
helper says so rather than inventing one.

## 1. `board_pin._emit` on a read — FIX

`guard_board(mode="read")` (`board_pin.py:226`) is the only caller of `_emit`, and the
write mode never emits: it raises `BoardPinConflict`. So every `_emit` is a *read*
that has just overridden the operator's `HERMES_KANBAN_DB` pin — and on a documented
read door it writes `$HSCC_HOME/daemon.log` to say so.

`board_pin.silent_read()` borrows the module's WARN route (a new one-line
`_warn_route`, the `_kanban_warn` indirection from PR #723 so the mute has exactly one
nameable target) and restores `_WARNED` in `finally`. Applied at the documented read
doors that open a named board:

| door | call wrapped |
|---|---|
| `hscc kanban blocked` | `kanban_blocked.list_blocked_tasks` board loop (`kanban_blocked.py:215`) |
| `hscc kanban stale` | `kanban_cli._cmd_stale` → `autodown.list_stale_tasks` (`kanban_cli.py:264`) |
| `hscc kanban monitor` / `hscc cards monitor` | `kanban_cli._cmd_monitor` → `kanban_live.collect` (`kanban_cli.py:348,357`), `cards_live` renderer's collect (`cards_live.py:633`) |
| `hscc autodown status [--json]` | `autodown_cli._cmd_status` → `autodown.status_snapshot` (`autodown_cli.py:234`) |

Same door set as PR #723 (different route, same `with` line — see "Hotspots").

## 2. `node_pauses._warn` from the parser — FIX

`hscc node paused` (`hscc_daemon/hscc.py:1589`) advertises read-only in its own help
(`:1423`) and calls `node_pauses.list_pauses()` `:1602`. With a malformed
`~/.hscc/node_pauses.json` — unreadable JSON, a non-object document, a `pauses` that
is a list, a non-object entry, an unparseable `since`/`until`, a `units` that is not a
list — each defect is a `_warn` → `daemon_ops.log` → a file write **during that read**.

`node_pauses.silent_read()` borrows `_warn`'s route for the one `list_pauses()` call,
echoes to stderr, restores nothing but the route (no budget exists; stated in the
docstring). The door is the only thing wrapped: `health.paused_nodes`,
`lifecycle._paused`, `alerts.covering`, `heal_breaker._covering` are repair decisions
whose "fail-loud on CORRUPTION" contract (`node_pauses.py:336-341`) keeps its file
line — a parked node the daemon cannot see it is paused is exactly the event the file
must record.

## 3. `kanban_live._note_caps_unreadable` — FIX

`_read_caps` is called from exactly one place — `collect` (`kanban_live.py:387`) — and
`collect` has both kinds of consumer: the CLI monitor doors and the daemon's
`stream_silence` check (`stream_silence.py:331`). `hscc kanban monitor` /
`hscc cards monitor` are advertised read-only, so the mute goes on those doors, never
inside `collect`, which would silence the accepted daemon check and would break the
suite that pins the monitor's warn-once behaviour.

`kanban_live.silent_read()` borrows the caps route (again through a named
`_caps_warn_route`, so the "fleet caps unreadable" line is the only thing muted — no
other line in this module is in scope), echoes to stderr, and restores
`_CAPS_READ["warned"]` **and** `["reason"]`, because a read that consumed the reason
would report "the operator never set a cap" (`kanban_live.py:140-142` says those are
different facts) on the next call in the same process.

## 4. `kanban_pause._warn_pin_once` read-refused — ACCEPT

The card's invariant is "a documented read-only surface writes no files". This leaf is
not on one, and the reachability proof is short:

* `_warn_pin_once` has exactly one caller: `read_child_env` (`kanban_pause.py:291`),
  the branch that refuses to run a read whose child cannot be handed a scrubbed env.
* `read_child_env` has exactly one caller: `list_cards` (`:352`).
* `list_cards` has exactly two callers: `pause` (`:438`) and `enforce` (`:545`) —
  `hscc kanban pause <board>…` ("reclaims every running card … blocks it",
  `kanban_cli._PAUSE_HELP`) and `hscc kanban enforce` ("blocks cards that became
  ready/running on a paused board since"), plus the daemon's 60 s `kanban_enforce`
  stream that runs `enforce_tick`. A mutating verb and an accepted daemon loop: no
  advertised read among them.
* The module's *actual* read verb proves the gap closed: `hscc kanban paused` renders
  from `kp.load_state()` + `kp.paused_boards()` (`kanban_cli.py:382-386`) — the state
  file only. It cannot reach `:291`, so the read the help advertises never writes.

The card asks an accept to say why "no file writes at all — a log line is a write"
does not apply. It does not apply here for two independent reasons. First, the
invariant's subject is a documented read; this path is a write door, which the parent
already ruled acceptable for the daemon's loops ("Accepted as foreground diagnostics …
the log line diagnoses an action that was asked for") — and the action asked for here
is precisely the pause that is being refused. Second, muting it would be a *net
loss of signal with no invariant gained*: the refusal is logged once per
(`pin`,`board`) (`_PIN_WARNED:197`, deliberately separate from `board_pin._WARNED`
because `enforce` runs every 60 s), and the same fact reaches an operator by two
routes that both stay — the `blocked` string returned to the caller
(`read_child_env`'s second tuple slot) and `REFUSED_KEY` in `enforce`'s report, which
`enforce_tick` turns into a red stream row. There is no file write on a read to
protect, so the only way to "fix" this leaf is to make a refused write quieter. That
is the opposite of what the class is for, so this leaf stays as it is and no guard
is added for it (a guard here would pin behaviour the invariant does not reach).

Boundary, stated so nobody reads this as a blanket pass: the accepted set is
*exactly* `hscc kanban pause` / `hscc kanban enforce` and the daemon's
`kanban_enforce` stream. If a future read door starts calling `list_cards` — e.g. a
`hscc kanban paused --cards` that inventories boards — this leaf becomes a violation
on the day that door ships, and the guard to write is this card's §4 test against
that door.

## What stays accepted (unchanged from the parent)

Daemon loops (`health`/`lifecycle`/`post_boot`/`serving`/`cron_watchers`),
`hscc check <stream>`, and the mutating siblings of every verb above
(`kanban blocked --recover`, `kanban stale --archive`, `kanban pause|resume|enforce`,
`node pause|resume`). `kill_switch`'s and `dispatcher_wedge`'s board reads are
`hscc check`-stream paths and stay accepted; `kill_switch` additionally publishes the
conflict itself (`kill_switch.py:223-225`), so it is not log-only anywhere.

## Shipped

* `hscc_daemon/board_pin.py` — `silent_read()` + `_warn_route`; doors wrapped:
  `autodown_cli._cmd_status`, `kanban_blocked.list_blocked_tasks`,
  `kanban_cli._cmd_stale`.
  Guard: `hscc_daemon/tests/test_board_pin.py::TestReadDoorWritesNoLogFile`
  (armed pin + on-disk board DBs + sandbox HOME, canary + stderr presence control).
* `hscc_daemon/node_pauses.py` — `silent_read()` + `_warn_route`; door wrapped:
  `hscc.py::_node_paused`. Guard:
  `hscc_daemon/tests/test_node_pauses.py::TestPausedDoorWritesNoFile` (malformed
  overlay planted, real door driven, route proven to be given back afterwards).

### Open on this branch

* §3 (`kanban_live` caps) and the two monitor doors (`kanban_cli._cmd_monitor`,
  `cards_live.Monitor._default_collect`, which need the §1 wrap **and** the §3
  wrap on the same `with` line) are not in these commits yet: the helper
  `kanban_live.silent_read()` is unwritten, so `board_pin.silent_read()` is
  deliberately not applied to the monitor doors either — landing half a door's
  mute would be the #704 hole from the other side. The verdicts and the shape for
  them stand as written above.
* §4 is a verdict, not a change: nothing in `kanban_pause.py` is touched and no
  guard is added for it (there is no read door for one to guard).

Each guard plants the state the sandbox cannot otherwise have (the armed pin, the
malformed overlay, the unloadable Hermes tree), drives the real door, asserts the
sandbox snapshot is unchanged, **and** asserts the WARN reached stderr — the presence
control that proves the guard reached the latent branch instead of passing because the
condition never fired (the #704 hole), plus the canary control that proves the
snapshot can see a write at all.

## Hotspots for the orchestrator

* `hscc_daemon/kanban_live.py`, `kanban_cli.py`, `kanban_blocked.py`,
  `autodown_cli.py` and `hscc_daemon/tests/test_kanban_live.py` are shared with
  **PR #723** (`t_4564c680`, open at the time of writing). The two branches mute
  different routes at the *same* `with` line; the reconciliation is to nest them —
  `with autodown.silent_read(), board_pin.silent_read():`. Nothing semantic is
  duplicated: #723's helper mutes `autodown._kanban_warn`, this one mutes
  `board_pin._warn_route` / `node_pauses._warn_route` / `kanban_live._caps_warn_route`.
* `autodown.silent_read()` (#723), `doctor_orch_subs._silent_read()` (PR #716, merged)
  and the three helpers here are now five copies of one shape. Consolidating them
  into one `hscc_daemon/read_silence.py` is a real cleanup, and it is deliberately not
  folded into this card: it would move a merged, pinned seam (`doctor_orch_subs`) and
  an open one (`autodown`) for a logging refactor, and every copy currently carries
  its own module's budget rules in its docstring. Worth a card of its own after #723
  lands.
