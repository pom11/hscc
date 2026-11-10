# MON-ENV-PIN's pause half — `kanban_pause`'s CLI calls, one of them a WRITE (t_70c97264)

Third card on the fork half of the hazard measured in
`board-pin-t_b014c2b8.md` §1, and the one `board-pin-fork-t_8616f2a5.md` §4 named
and left to this card. The wedge card guarded only its own gate, because that is
what its card scoped. `hscc_daemon/kanban_pause.py` has the same fork — a
`subprocess.run` with no `env=` — and one of its two calls is a **write**, where
`board_pin` §2c rules the scrub out outright.

## 1. The sites, as they stood at `fe530887`

| site | what it did |
|---|---|
| `_run_hermes` (`:147-149`) | `subprocess.run(["hermes","kanban",*args], capture_output=True, text=True, timeout=60)` — **inherited** environment, no `env=` |
| `list_cards` (`:174-182`) | `--board <b> list --json`, parsed as fact: the pause's whole "every card is accounted for" inventory |
| `_hold` (`:185-189`) | `--board <b> reclaim <id>` then `--board <b> block --kind dependency <id>` — **writes** |
| `resume` (`:287`) | `--board <b> unblock <id>` — **also a write**, and the card body did not name it (see §3) |

Hermes resolves `HERMES_KANBAN_DB` ahead of `--board` in any process carrying a
worker/delegation fence marker (`hermes_cli/kanban_db.py:540-555` +
`_explicit_board_intent_pinned()` → `agent/delegation_context.py:76-93`), and
`board_pin`'s scrub only covers *our own* connection — a forked child carries the
environment copy made before the restore. So from a worker or delegated-child
shell (which is exactly what an orchestrator agent running `hscc kanban pause`
is):

1. the **read** inventoried the PINNED board while the report named the board
   being paused, so the pause "succeeded" against an inventory of another board's
   cards; and
2. the **write** reclaimed and blocked cards **on the pinned board** while the
   report said it paused board X. Task ids are per-board (§2c), so a same-id card
   on the pinned board got blocked — the class of harm `board_pin` refuses writes
   for, reached through a subprocess where `board_pin` cannot see it. `resume`
   does the mirror: it would *release* a hold it never placed.

Unlike the wedge, this module's argv builders always state the slug
(`["--board", board, …]` for every board, `default` included), so the
`t_dad0da32` hole (`board != "default"` ⇒ no `--board`) never existed here.
Nothing in this card touches argv.

## 2. Chosen shape — card option (a) for the read, option (b) for the write

Same split the wedge card used, because the two accesses have different
consequences: a misdirected read misinforms, a misdirected write mutates the
wrong store, and §2c's rule is that a pinned shell must not be handed cards on a
board it does not own. Detection and wording stay `board_pin`'s (`pin_conflict`,
`read_warning`, `write_error`) — this module adds no path rule of its own.

| state of the world | the READ (`list_cards`) | the WRITE (`pause` / `resume` / `enforce`) |
|---|---|---|
| no pin / no fence / pin already **is** the board / lib cannot be interrogated | nothing handed — `run(argv)`, child inherits (byte-identical to before) | proceeds, nothing said |
| pin would outrank `--board`, runner accepts `env` (the real `_run_hermes` and any runner written against the grown contract) | handed a copy of `os.environ` **minus `HERMES_KANBAN_DB`**, warned once per (pin, board) in `board_pin`'s `read_warning()` wording | **refused before anything is spawned**: the board's report is `{"refused": <board_pin's write_error>, …}` and no verb runs at all — not `list`, not `reclaim`/`block`, not the orchestrator inject, not the cron park; the board is not written into the paused-state file either, so `enforce` will not keep retrying a hold that never happened |
| pin would outrank `--board`, runner is the legacy `runner(argv)` shape | **never called**; `[]` returned and the warning names the pin — an answer describing another board is not a fact | (same refusal — the write never reaches the runner) |

Three properties worth keeping:

* **Nothing is refused when the pin is the honest answer.** A worker pausing its
  own board keeps the inherited environment and says nothing — the same posture
  as `board_pin`'s reads, pinned by companion tests.
* **The refusal is per board.** `hscc kanban pause alpha beta` from a shell pinned
  at `alpha` pauses `alpha` and refuses `beta`, and the report names which — the
  card's "the pause report must say which boards it refused".
* **The scrub is a copy, never a mutation.** `os.environ` keeps the pin, so the
  worker fence hermes intends is not weakened in our own process; only the child's
  view of one variable is narrowed, for a read-only probe. The write's environment
  is **never** scrubbed — that is §2c.

`_hold` itself also refuses (`board_pin.BoardPinConflict`) as the last line for a
direct caller: the three passes pre-check so they *report*, a bare `_hold` call
has no report to write into and must fail loudly.

## 3. Scope the card did not name: `resume`'s `unblock`

`resume` (`:287`) runs `--board <b> unblock <id>` through the same inherited
environment. It is the same §2c write class, and arguably worse than a stray
block: it *releases* a hold on a board the caller does not own, and it pops the
paused-state record while doing so. Refusing `pause` and leaving `resume` would
have shipped a half fix through the same one-line call, so `resume` refuses the
same way and **keeps** the paused-state entry (a refused resume must not forget
the board is paused). The report shape is `{"refused": …, "released": [],
"failed": []}`.

## 4. `enforce`'s report and the daemon stream

`enforce()` keeps its `{board: [held ids]}` shape and adds one reserved
bookkeeping key, `_refused: {board: reason}`, following the module's own
`_`-prefixed convention (`_auto_decompose_was`, and `paused_boards()` skipping
`_` keys). `enforce_tick` pops that key before its arithmetic — so `held`/`paused`
keep counting what they counted before — and stamps the stream with `ok: False`
and a message naming the pin and saying the hold is NOT being enforced. That
routes into `doctor_check`'s EXISTING red row ("last enforce tick FAILED … the
hold is NOT being enforced"), so a pinned daemon that cannot enforce a paused
board is visible instead of reading as "paused, nothing new to hold". Consequence
for the CLI: `hscc kanban enforce --json` prints a `_refused` entry alongside any
board holds — honest, and no consumer parses it today.

## 5. What this does NOT fix (deliberate, and where it is owned)

* **Hermes' precedence is untouched**, as in every card of this series: no
  resolver patch, nothing under `~/.hermes/**`. The upstream ask stays the
  (unfiled) draft in the parent audit §4.
* **`--board` argv builders** — not needed here (§1), and owned by `t_dad0da32`
  for the wedge.
* **Other `hermes`/`hermes-cli` shell-outs in the repo** were re-scanned; the
  remaining ones (`_run_cron` → `hermes -p <profile> cron …`, `kanban_live`,
  `orch_waker`) express no `--board` intent, so the pin is their intended answer
  and there is nothing to override. If one later names a board, it needs this
  same decision.
* **A lib that cannot answer `kanban_db_path`** falls through to the untouched
  path, including for writes. That is `board_pin`'s documented posture repo-wide
  (`pin_conflict` returns None rather than guessing); this card does not invent a
  stricter rule for one caller.

## 6. Evidence

`hscc_daemon/tests/test_board_pin_pause.py` — 11 tests, every conflict case
paired with a non-vacuity assertion, all assertions on observables (the
environment the child was handed, the argv, what the report says, whether anything
was spawned at all).

## 6. Evidence

- **12 tests** in `hscc_daemon/tests/test_board_pin_pause.py`. Every conflict case
  is paired with a non-vacuity assertion, and every assertion is on an observable:
  the environment the child was actually handed, the argv, what the report and the
  status stream say, whether anything spawned **at all**. The non-vacuity pair that
  matters: the pre-guard `list` answers for the *pinned* board, and the pre-guard
  `block` argv **mutates** that board's same-id card while the asked board is
  untouched — the CLI fake answers and mutates the board the environment it was
  handed resolves to, so the harm is observable rather than merely flagged.
- **Targeted trio**: 45 passed — the 12 new plus the 33 existing
  (`test_kanban_pause.py` 20, `test_kanban_pause_enforce_stream.py` 13), the
  injected-`run` fakes unchanged as the card required.
- **Suite pair, back-to-back at `f5af6980`** (this card's code commit — the only
  later commits are this record and the changelog fragment, no code and no tests),
  with `run_tests.sh`'s identity vars unset (`HERMES_DELEGATED_CHILD_CONTEXT`,
  `HERMES_KANBAN_DB`, `HERMES_KANBAN_BOARD`, `HERMES_KANBAN_WORKSPACES_ROOT`,
  `HERMES_KANBAN_ATTACHMENTS_ROOT`):
  **hscc_daemon 3630 passed / 0 failed** (114s),
  **hscc-api 994 passed + 1 skipped** (253s).
  Branch-point pair on `fe530887`: 3618 / 994+1 — the delta is exactly the +12 here,
  so nothing was renamed or dropped. `changelog_fragments.py check` OK
  ([Unreleased] pointer, 3 fragment entries consistent); `readme_lint.py` 0 findings,
  docs-index 204/204.
- **Mutation probes: 13/13 decision points caught**, each probe reset to the
  pristine copy first, an unmutated CONTROL run green before any verdict, and
  provenance asserted inside every run (a plugin's FILE sentinel — `-q` suppresses
  report headers, so a check that only prints is not a gate). The points:
  never-scrub the read · scrub-but-empty-world (the read must inherit the rest of
  the environment) · the real runner dropping `env=` (the only route by which a
  scrub survives `subprocess.run`) · `list_cards` ignoring `blocked` · `pause`
  ignoring the write refusal · `pause` continuing after refusing · `resume`
  ignoring the write refusal · `enforce` ignoring the write refusal · `enforce`
  hiding the refusals · `enforce_tick` staying `ok=True` on refusals · `_hold`
  no longer refusing a direct write · the read capability always answering
  "cannot" (which would fail the read closed) · detection switched off entirely.
