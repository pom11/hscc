# MON-ENV-PIN audit — `HERMES_KANBAN_DB` outranks an explicit `board=` (t_b014c2b8)

Scope: every surface in this repo that opens a kanban board store with an
explicit `board=` argument, and the precedence claim itself. Written before any
code change, per the card.

## 1. The mechanism, confirmed against live hermes source

`~/.hermes/hermes-agent/hermes_cli/kanban_db.py`:

| line | what it does |
|---|---|
| `kanban_db.py:522` `_explicit_board_intent_pinned()` | True when `_PIN_FIRST_BOARD_RESOLUTION` (hermes' own machine-flow contextvar) is set **or** `agent.delegation_context.explicit_board_intent_is_pinned()` is True |
| `agent/delegation_context.py:76-93` `explicit_board_intent_is_pinned()` | True for an in-process delegate child, for any process carrying the `HERMES_DELEGATED_CHILD_CONTEXT` env marker, **or** whenever `HERMES_KANBAN_TASK` is set (i.e. every dispatched worker) |
| `kanban_db.py:540-555` `_board_path()` | `if pin and (slug is None or _explicit_board_intent_pinned()): return Path(pin)` — the env pin **wins over the explicit `slug`** whenever either fence marker is present |
| `kanban_db.py:563-566` `kanban_db_path(board=…)` | the resolver every board-file question funnels through |

So the claim on the card is correct, and it is **wider** than stated: the fence
fires for a dispatched worker (`HERMES_KANBAN_TASK`) **and** for a delegated
child / descendant (`HERMES_DELEGATED_CHILD_CONTEXT`), which is the context this
very session runs in.

### Measured, this shell, read-only path math (no DB opened)

```
fence markers: HERMES_KANBAN_TASK=None  HERMES_DELEGATED_CHILD_CONTEXT='~/.hermes'
               HERMES_KANBAN_DB='~/.hermes/kanban/boards/hscc/kanban.db'
               HERMES_KANBAN_BOARD='hscc'  HERMES_HOME='~/.hermes/profiles/backend-engineer'
_explicit_board_intent_pinned() = True
list_boards() -> 24 slugs  (enumeration itself is UNAFFECTED by the pin)

kanban_db_path(board='hscc')            -> …/boards/hscc/kanban.db    (the pin; coincides)
kanban_db_path(board='default')         -> …/boards/hscc/kanban.db    (WRONG board)
kanban_db_path(board='some-other-board')-> …/boards/hscc/kanban.db    (WRONG board)
```

The last three lines are the whole bug: `list_boards()` still reports 24 boards,
so a monitor loops 24 times and opens the **same DB 24 times**. The panel is
well-formed — populated rows, `errors: []` — and reports one board as the fleet.

`scripts/run_tests.sh:147` runs every leg with `env -u
HERMES_DELEGATED_CHILD_CONTEXT` (so fixture boards can be created) but does
**not** unset `HERMES_KANBAN_DB`. So a test that used the *real* resolver under a
worker shell would resolve to the worker's live board — which is why the
regression test in this change builds its own fixture pin file and its own fence
markers instead of relying on ambient env.

## 2. Surfaces, with the classification applied by this change

`multi` below is autodown's `_enum_board_names()` flag: True when the injected
lib enumerates boards (so `board=` is passed), False for the legacy single-board
shape (no `board=`, pin honoured — correct, never guarded).

### 2a. Read-only fleet readers — guarded: warn once + scrub the pin for our own open

| file:line | surface | reachable from a worker shell? |
|---|---|---|
| `hscc_daemon/kanban_live.py:374` | `collect()` — one pass over every board. Reached by `hscc kanban monitor`, `hscc kanban --json`, `hscc kanban monitor --live` (`kanban_cli.py:334,339,341`) and by `cards_live.Monitor` (`cards_live.py:472`, entry points `cards_live.py:772,802`) | **yes** — this is the surface the QA gate hit |
| `hscc_daemon/autodown.py:393` | `board_db_paths()` — the PATH view (`kanban_db_path(board=slug)`), consumed by `orch_waker.board_enumeration` (`orch_waker.py:367`) which opens its own `mode=ro` URIs | yes (`hscc` CLI, daemon loop, doctor/verify) |
| `hscc_daemon/autodown.py:465,468` | `_board_has_active_work()` — the idle predicate / C3 interlock; feeds `snapshot()` `kanban_ok`/`kanban_blocking` (`autodown.py:1363,1365`) which is what **`hscc verify`** reads (`verify.py:2512,2531`) | yes (`hscc verify`, `hscc autodown status` — `autodown_cli.py:240,241`) |
| `hscc_daemon/autodown.py:566,569` | `list_stale_tasks()` — `hscc kanban stale`, `hscc autodown status` | yes |
| `hscc_daemon/kanban_blocked.py:208` | the blocked-card listing | yes |
| `hscc_daemon/dispatcher_wedge.py:312,456,1339` | read-only spawnable/cap probes (docstrings: "Read-only") | yes (also daemon loop) |
| `hscc_daemon/kill_switch.py:155,206` | running-card listing / find-by-id — **left fenced on purpose**, see 2c | yes |

### 2b. No explicit `board=` — pin honoured, correct by design, untouched

`hscc_daemon/escalate_watcher.py:122`, `hscc_daemon/autodown.py:3012`,
`hscc_daemon/kill_switch.py:158,209`, `hscc_daemon/autodown.py:468,569,637`
(the `multi=False` branches), `hscc_daemon/dispatcher_wedge.py:313,457,1340`.
Guarding these would change the single-board path the card requires to stay
unaffected.

### 2c. Write surfaces — deliberately **not** scrubbed. Guard = raise naming both

A reader that silently reads one board is misleading. A *writer* that silently
writes one board is a wrong-board mutation, and scrubbing it would let a worker
shell create/mutate cards on boards it does not own. So these get the loud
option instead: `BoardPinConflict` naming the pinned board and the requested
one, before any write.

| file:line | write |
|---|---|
| `hscc_daemon/autodown.py:634,637` (`archive_stale_task` → `_archive_one` `UPDATE tasks SET status='archived'`) | pinned scan can archive a *different* board's card with the same id — task ids are per-board |
| `hscc_daemon/kanban_blocked.py:262` (`UPDATE tasks SET status='ready'` + `_record_recovery` events/comment) | same id-collision shape |
| `scripts/dep_pr_watcher.py:208` (`create_task(...)` + `UPDATE tasks SET workspace_kind…`, `board=BOARD` from `HSCC_KANBAN_BOARD`, default `hscc`, `dep_pr_watcher.py:103`) | a Hermes-cron-watched shell carries the pin, so verification cards land on whatever board the pin points at |
| `hscc_daemon/kill_switch.py:155,206` (read of the kill verb) | the verb kills worker processes; handing a worker shell a scrubbed view would point a destructive verb at boards it does not own — so it stays fenced and the operator sees the conflict |
| `hscc_daemon/orch_waker.py` inject path | writes to *profiles* (tmux panes), not to a board store — out of scope |

## 3. Chosen shape (and why)

The card allowed two shapes. Both are implemented, split by access mode, in one
new seam, `hscc_daemon/board_pin.py`:

* `open_board(..., mode="read")` → detects, **warns once per process naming both
  boards and the pin**, then opens with `HERMES_KANBAN_DB` removed from
  `os.environ` for the duration of our own open, so hermes resolves the board we
  actually asked for.
* `open_board(..., mode="write")` / `checked_db_path(..., mode="write")` →
  **raises `BoardPinConflict` naming both boards** before any write.

Detection asks hermes' own resolver twice — once with the pin visible, once with
it scrubbed — and compares (`kanban_db_path(board=X)` vs the same call under a
scrub). It therefore never re-derives a hermes path rule, which keeps the
`"hscc never re-derives a path"` contract of `autodown.board_db_paths`
(`autodown.py:342`) intact, and it stays correct if hermes changes its precedence
rules. No conflict is reported when the pin *is* the board we asked for (the
normal single-board worker case), and never when `multi` is False (the legacy
single-board shape).

Scrubbing weakens one hermes property on purpose: the pin is part of the
"workers physically cannot see other boards" fence
(`agent/delegation_context.py:81-87`). It is weakened only for **reads**, only
for our own monitors, and only with a warning naming the override. The mutation
half of that fence is untouched: under the delegated-child marker hermes still
opens every board `?mode=ro` (`kanban_db_connect.py:713-727`
`kanban_path_is_fenced`), and every write surface keeps the fence.

## 4. STEP 3 — upstream issue draft (NOT opened from here)

Deliberately not filed from this card: it names file:line in hermes source and
an operator-visible path shape, and this board has previously shipped reports
with live addresses in them. An operator (or the next card that owns upstream
contact) should re-check the rendered body and open it upstream. hscc's
mitigation above does not depend on it being accepted.

> **Title:** `kanban_db`: an explicit `board=` is silently overridden by
> `HERMES_KANBAN_DB` inside a worker/delegation fence — no warning, and
> `list_boards()` still reports every board
>
> **Component:** `hermes_cli/kanban_db.py` board resolution.
>
> **What happens**
>
> `_board_path()` (`hermes_cli/kanban_db.py:540-555`) returns `Path(pin)` when
> `HERMES_KANBAN_DB` is set and `_explicit_board_intent_pinned()`
> (`kanban_db.py:522` → `agent/delegation_context.py:76-93`) is true — i.e. for
> every dispatched worker (`HERMES_KANBAN_TASK`) and every delegated child or
> descendant (`HERMES_DELEGATED_CHILD_CONTEXT`), not just the in-process case.
> The caller's explicit `board=` argument is discarded, silently.
>
> **Why that is a trap for programmatic callers**
>
> The fence is invisible and asymmetric: `list_boards()` is *not* affected, so a
> reader that enumerates boards and then opens each slug via
> `connect_closing(board=slug)` — the documented, correct-looking pattern — opens
> the SAME pinned file once per slug. Measured in a worker shell with the pin at
> `<hermes-root>/kanban/boards/hscc/kanban.db`:
>
> ```
> _explicit_board_intent_pinned()     -> True
> list_boards()                       -> 24 slugs
> kanban_db_path(board="default")      -> …/boards/hscc/kanban.db
> kanban_db_path(board="some-other")   -> …/boards/hscc/kanban.db
> ```
>
> The output is not an error. It is a full, well-formed, plausible panel whose
> numbers describe one board while reading as a statement about all 24 — the
> worst failure shape, because every downstream judgement inherits it. This bit
> a monitoring/verification tool in production use (HSCC `hscc kanban monitor`,
> card t_b014c2b8); the QA worker only found it by comparing against a run with
> the env vars unset.
>
> **Requested change (no precedence change asked for)**
>
> Keep the fence — it is a real isolation property and we rely on the mutation
> half of it. Just make the override observable at the point it happens:
>
> 1. when `slug is not None` and the pin wins anyway, emit one warning per
>    process naming both the pinned path and the board that was asked for
>    (e.g. `_kb._log.warning("kanban board=%r ignored: %s pins %r (worker fence
>    active)", …)`). Today a *human* running `hermes kanban --board X` from a
>    worker shell gets no signal at all either.
> 2. consider surfacing it programmatically too — e.g. `kanban_db_path(board=X)`
>    could expose a `board_resolution_was_pinned(board, path)` helper, or return
>    the slug it actually resolved, so callers can assert intent without
>    re-deriving path rules or mutating `os.environ`.
>
> A third option we did **not** take, so it is on the record: hscc works around
> this by removing `HERMES_KANBAN_DB` from `os.environ` around its own read-only
> opens (with a warning naming both values) and by refusing writes outright when
> the pin would win. That works, but it is a caller-side workaround for a
> silent resolver decision, and it weakens the read half of a fence hermes
> intends to enforce — better fixed where the decision is made.

