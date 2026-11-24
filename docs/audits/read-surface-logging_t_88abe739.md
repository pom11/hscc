# Read surfaces that inherit a log write from a borrowed call — audit (t_88abe739)

Base: `52494e03` (origin/main at authoring time), rebased onto `f2f8bc2d`. Parent:
t_2c218de8 (PR #716) — **merged while this doc was open** (`1e024992`, then hardened by
#732 `ce2dc23d`), so row `orch_wake_subs` no longer writes here and the fence §5 asks
for already exists on main. Every measurement below was taken at the original base,
where that fence did not exist yet: the `door:` row in §2 is that pre-fix door and is
now closed. The other five surfaces are unaffected — none of the files carrying them
changed in between, and their call sites are still unwrapped at this base.

Verdict up front: **the class is real and it is not one instance.** Five documented
read surfaces create `$HSCC_HOME/daemon.log` today, all through ONE borrowed seam.

## 1. The one leak point

`hscc_daemon/daemon_ops.py:597` `log()` → `append_rotating()` (`:575`) →
`LOG_FILE = ~/.hscc/daemon.log` (`:19`). The **file write is unconditional**; only
the stderr echo is conditional on a missing pid file (`:630`). So "the read echoed
nothing" is not evidence — the file is the invariant (as the card states).

Six once-per-process WARN leaves sit on read paths. Each records a reason in a
module dict *and* writes, gated by a `warned` flag that the write consumes:

| leaf | file:line (log call) | warned budget | reached by |
|---|---|---|---|
| `autodown._note_kanban_unavailable` | `hscc_daemon/autodown.py:829` (log at `:842`) | `_KANBAN_LOAD["warned"]` | `_has_active_work:549`, `_board_has_active_work:578`, `_load_kanban_db_or_default:877`, `:897` |
| `autodown._note_prci_unavailable` | `hscc_daemon/autodown.py:1440` (log at `:1453`) | `_PRCI_LOAD["warned"]` | `_has_active_pr_ci:1380`, `:1396` |
| `board_pin._emit` | `hscc_daemon/board_pin.py:192` (log at `:203`) | `_WARNED` set | `guard_board(mode="read")` at `:226` ← `open_board:232`, `open_board_ro:257`, `resolve_board_db_path:284` |
| `kanban_live._note_caps_unreadable` | `hscc_daemon/kanban_live.py:149` (log at `:160`) | `_CAPS_READ["warned"]` | `_read_caps:191` (kanban/cards monitor) |
| `node_pauses._warn` | `hscc_daemon/node_pauses.py:96` (log at `:106`) | module `_WARNED` | the pause-file **parser** `:234,:253,:261,:273,:280,:289,:313,:320,:326,:353` → `hscc node paused` |
| `kanban_pause._warn_pin_once` | `hscc_daemon/kanban_pause.py:200` (log at `:213`) | module `_WARNED` | read-refused branch `:291` |

The amplifier: **`autodown._load_kanban_db_or_default` (`autodown.py:847`)** is the
repo's single "import hermes' kanban lib" seam, and on failure it calls
`_note_kanban_unavailable` (`:877`, `:897`). It has exactly three callers inside
autodown — `board_db_paths:469`, `_has_active_work:538`, `list_stale_tasks:654` —
and each is a read. Every module that borrows that seam inherits the write:

* `hscc_daemon/kanban_blocked.py:199` (`hscc kanban blocked`) and `:262` (`stale`)
* `hscc_daemon/kanban_live.py:386` (`hscc kanban monitor`, `hscc cards monitor`)
* `hscc_daemon/autodown.py:538` / `:654` — via `_has_active_work()` and
  `list_stale_tasks()` inside `_status_interlock_context` (`:1744`, calls at `:1754`,
  `:1757`, `:1776`) ← `status_snapshot` (`:1802`) ← `autodown_cli._cmd_status`
  (`autodown_cli.py:223`, call at `:234`), ← agent tool `autodown_status`
  (`hscc-cluster/autodown_tools.py::_report_status`) ←
  `GET /v1/autodown/status` (`hscc-api/routes_autodown.py:90`, handler `:168`)
* `hscc-bootstrap/doctor_orch_subs.py:265` (`autodown.board_db_paths`) — the
  parent's instance, fixed in #716: that row now borrows the logger for the ONE
  call and gives it back in `finally` together with the `_KANBAN_LOAD["warned"]`
  budget (`:181-193`). That helper is the shape §5 asks the other doors to copy.

## 2. Measured, not reasoned

Each surface run in a fresh `$HOME` sandbox (the proven `_seal_door_env` shape from
`hscc-cluster/tests/test_doctor_tool.py`: HOME/HERMES_HOME redirected, sparkrun /
curl / ssh stubbed, TEST-NET-1 + closed ports, uv cache sealed outside the
snapshot), tree snapshotted before/after:

```
SURFACE                                  rc  daemon.log  other new paths
door: doctor --json                       1  YES         (.hscc/daemon.log)          [known: #716]
hscc autodown status --json               0  YES         .local/state/gh (gh's own)
hscc autodown status                      0  YES         .local/state/gh (gh's own)
hscc kanban blocked --json                0  YES         -
hscc kanban stale --json                  0  YES         -
hscc kanban monitor --json                0  YES         -
```

Every written line is the same one — proof of the single seam:
`[ WARN] Autodown kanban interlock unevaluable: Hermes agent source not found at
'<sandbox>/.hermes/hermes-agent' — cannot import hermes_cli.kanban_db.`

Clean in the same harness (no daemon.log, no HSCC-state writes): `hscc cluster
status|hosts|info|history`, `hscc profiles`, `hscc merge-queue status|list`,
`hscc heal-status`, `hscc stats`, `hscc throughput`, `hscc autoscale`,
`hscc escalate`, `hscc node paused`, `hscc main status`, `hscc main wakeups list`,
`hscc kanban paused`, `hscc triggers`, `hscc check --repo`, the cluster plugin's
`cluster-status|hosts|jobs|info|orch waker status`, and `hscc verify --json`
(**except** one separate thing, §4).

## 3. Verdicts

* **VIOLATION** — `hscc autodown status [--json]`. `autodown_cli.py:223-229` says in
  its own docstring: *"Never writes, never creates, never acts."* It is also the
  agent tool's and `GET /v1/autodown/status`'s door, so a machine caller that
  snapshots the tree sees the file. Same class as the parent row, unfixed.
* **VIOLATION** — `hscc kanban blocked`, `hscc kanban stale` (`hscc kanban --help`
  sells them as list verbs; `--json` is a machine channel), `hscc kanban monitor`
  (help: *"read-only"*). All three write via the borrowed import seam.
* **VIOLATION (latent — same leaf, needs a pin conflict to fire)** —
  `board_pin._emit` from `guard_board(mode="read")`: every board reader that goes
  through `open_board`/`open_board_ro`/`resolve_board_db_path` writes when the env
  pin conflicts with the named board. It did not fire above because the sandbox has
  no pin. Any read that inherits it is a violation by the same rule.
* **VIOLATION (latent — needs a malformed/odd file)** — `node_pauses._warn` from the
  pause-file *parser*, reached by `hscc node paused`, whose help promises
  *"paused is read-only and never touches the fleet"*; and
  `kanban_pause._warn_pin_once` from the read-refused branch.
* **ACCEPTABLE** — `hscc check <stream>` and the daemon loops. `hscc check` is
  "run one check cycle now", not advertised read-only, and the checks it runs
  (`health.check_workers` etc.) write stream state by design; a log line there is a
  foreground diagnostic of an action the operator asked for. Same for
  `lifecycle`/`post_boot`/`serving`/`cron_watchers` WARNs — daemon-side.
* **ACCEPTABLE** — the stderr echo in general. Only the file is the invariant.

## 4. Adjacent, NOT this class (do not fold it into the fix)

`hscc verify --json` and `hscc api status` create `$HSCC_HOME/api-token` during a
read (rc=0/0; the token is minted because it is absent). That is a *credential
created by a read*, not a log write, and it needs its own card and its own verdict.

## 5. The smallest fix that closes the class (decision, for whoever cards it)

Mute at the **borrower**, in the shape #716 proved — do not add allow-list entries,
do not weaken the door guard:

1. One helper next to the seam in `autodown.py`: a context manager that swaps
   `autodown.log` for a no-op for the duration of ONE borrowed call and, in
   `finally`, restores it **together with `_KANBAN_LOAD["warned"]` and
   `_PRCI_LOAD["warned"]`** (the once-per-process budget — the write must not be
   *spent*, only deferred, or the daemon loses its one WARN forever). `_KANBAN_LOAD`
   ok/reason stays recorded, so `hscc autodown status` still owns the answer.
   **This helper is no longer hypothetical** — #716 shipped exactly it for
   `_KANBAN_LOAD`, inside the consumer (`doctor_orch_subs.py:181-193`). The card is
   therefore "lift that shape to the seam and point the existing consumer at it",
   not "invent it"; `_PRCI_LOAD` is the half nobody has covered yet.
2. Wrap the four read doors: `autodown_cli._cmd_status`'s `status_snapshot()` call,
   `kanban_blocked.py:199` and `:262`, `kanban_live.py:386`.
3. Same treatment for `board_pin` reads (`_WARNED`), `kanban_live._CAPS_READ`,
   `node_pauses._WARNED` — each is a separate call site, each has its own budget.
4. Add the snapshot guard **in the same commit as the fix**, per surface (a red
   guard on main is what made the parent a "main is red" card): a subprocess-sandbox
   test that runs the verb and asserts `daemon.log` did not appear, with the
   canary control the door guard uses, and with the cross-directory assertion in
   the surface's own suite (the #704 lesson).
