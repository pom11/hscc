# MON-ENV-PIN residual — the wedge treated `board="default"` as "no board" (t_dad0da32)

Follow-up deliberately left out of t_b014c2b8 / PR #268 and named there
(`docs/audits/board-pin-t_b014c2b8.md`, the PR body). Read that audit first: it
carries the precedence mechanism (`hermes_cli/kanban_db.py:540-555`
`_board_path()` → `kanban_db.py:522` → `agent/delegation_context.py:76-93`) and
the measured facts this card inherits. Scope here is the three sites in
`hscc_daemon/dispatcher_wedge.py` only — no hermes precedence change, nothing
under `~/.hermes/**`.

## 1. The defect, as shipped by #268

`board_pin.open_board(..., multi=…)` is documented (`board_pin.py:231-242`) to
read exactly one thing out of `multi`: *did the caller name a board?* `False`
means "the lib is the legacy single-board shape, the caller passed no `board=`,
the pin is the intended answer — stay silent and byte-for-byte old". That is the
contract, and `board_pin` honours it correctly.

The wedge layer-2 gate invented `multi` from the **slug** instead of asking the
lib, in three places:

| site | expression | effect |
|---|---|---|
| `dispatcher_wedge.py:1254` | `[--board, board] if board and board != "default"` | the CLI never sees the slug the panel is reporting |
| `dispatcher_wedge.py:1345` | `multi = bool(board) and board != "default"` | handed to the guard as "no intent" for `default` |
| `dispatcher_wedge.py:1374` | same idiom, `show` argv | per-card hold lookup also loses the slug |

So `default` was the only slug the guard could not see: every other slug got
warn-and-scrub (reads), `default` silently read whatever board the worker pin
pointed at — and the panel labelled the result `default`.

## 2. Measured in a live worker shell (this session, path math only, no DB opened)

```
fence markers:    HERMES_KANBAN_TASK=None  HERMES_DELEGATED_CHILD_CONTEXT=<hermes-root>
pin:              <hermes-root>/kanban/boards/hscc/kanban.db
_explicit_board_intent_pinned()      -> True
list_boards()                        -> 23 slugs, 'default' among them  -> True

kanban_db_path(board="default")  scrubbed = <hermes-root>/kanban.db               (legacy flat file)
kanban_db_path(board="default")  pinned   = <hermes-root>/kanban/boards/hscc/kanban.db   DIFFERS
kanban_db_path(board="hscc")     scrubbed = <hermes-root>/kanban/boards/hscc/kanban.db
kanban_db_path(board="hscc")     pinned   = <hermes-root>/kanban/boards/hscc/kanban.db   same (pin IS the board)
```

Two facts settle the shape of the fix:

1. **`default` is a real board in hermes' own enumeration.** `list_boards()`
   returns it as a slug, `autodown.DEFAULT_BOARD_NAME` (`autodown.py:317-320`)
   already names it, and its canonical file is the legacy flat
   `<hermes-root>/kanban.db`. Treating the slug as "no board" is a
   wedge-local fiction, not a hermes rule.
2. **The pin moves `default`'s resolved path.** Under a fence the answer for
   `default` is a *different board's* file, so "no conflict here" was never
   true — it was simply never asked, because `multi=False` made
   `open_board` take the no-intent branch before the resolver was consulted.

Also measured: the sanctioned CLI accepts the slug explicitly —
`hermes kanban --board default list --status ready --json` exits 0 with valid
JSON, so passing `--board default` is the same CLI path #268 already uses for
every other slug, not a new contract.

## 3. DECISION — (a): `default` is a real board

**(a) chosen.** `default` names a real board whenever the lib enumerates
boards, so it is guarded like every other slug: warn-once naming both boards
(reads) / refuse (writes).

Why not (b) (`--board default` stays a no-op, refuse a pinned run instead):
the wedge's own scan produces the `default` label on the ordinary path.
`_board_snapshot` labels a `None` slug `"default"` (`dispatcher_wedge.py:530`)
and `_capture_kanban` rewrites `None` → `"default"` for the stream
(`dispatcher_wedge.py:723`), so `spawnable=["default"]` is what the daemon
loop hands the gate whenever the legacy board has work. Refusing there would
make the fleet watchdog go **deaf on the legacy board** — a bigger failure than
the leak it prevents. (b) also cannot be implemented without inventing a
third state ("the caller meant default but meant it as a no-op"), which is the
contract change this card was told not to smuggle in.

**(a) as implemented does *not* use the card's suggested `multi = bool(board)`.**
That form would hand `multi=True` to a lib that cannot enumerate boards at all
(`_enum_board_names`'s `[None], False` legacy shape, `autodown.py:310-312`), and
then `open_board` would call `connect_closing(board="default")` on a lib whose
`connect_closing` may not accept the keyword — turning the legacy single-board
path that #268's companion test pins byte-for-byte
(`test_board_pin.py:243-256`) into an exception, i.e. the gate failing closed on
a healthy old runtime and the restart proceeding.

Instead `multi` is **read from the same source the scan uses** — autodown's
single enumeration seam (`_enum_board_names`, `autodown.py:290-314`), which is
where `multi` is *defined* — and threaded down once per gate call:

```
board_state_explains_stall   → multi = _enum_board_names(kanban_db)[1]
  └ _board_ready_facts(…, multi=multi)      → list_argv + _capped_assignees
      └ _card_hold_from_cli(…, multi=multi) → show_argv
```

Consequences, stated so a reviewer can check them:

* `multi` now has **one** meaning across the whole module: "the lib enumerates
  boards, therefore a slug names a real board". It is never re-derived from a
  slug again, which is what makes the class of bug non-recurring.
* `--board <slug>` is passed for **every** slug when `multi` is True — including
  `default` — and omitted when it is False, so the argv and the DB open always
  describe the same board. Before, they disagreed exactly for `default`.
* The legacy `multi=False` shape is byte-for-byte unchanged: argv as before,
  `connect_closing()` with no `board=`, no guard consulted, no warning.
* Enumeration failure falls back to `hasattr(kanban_db, "list_boards")` — the
  same rule `_enum_board_names` itself uses — so a lib that raises
  mid-enumeration does not silently flip the guard off.

## 4. The acceptance case, and what proves it

`hscc_daemon/tests/test_dispatcher_wedge_default_board_pin.py` drives the real
gate (`board_state_explains_stall`) with `HERMES_KANBAN_DB` armed at another
board + a fence marker, a `HERMES_KANBAN_HOME`-shaped pin-aware fake lib, and
`spawnable=["default"]`. The assertion is the **observable**, not a flag: a
`WARN` line through `daemon_ops.log` naming the pin path *and* `default`, and —
the part the label alone cannot prove — the capped-assignee query returning
`default`'s rows instead of the pinned board's.

Paired non-vacuity, in the same armed environment: the *unguarded*
`lib.connect_closing(board="default")` really does answer with the pinned
board's rows and says nothing. Without that assertion the suite would pass just
as happily in an env where nothing was ever pinned.

## 5. Landing order: two sibling cards took the other halves mid-flight

This card was written against `1b5e9eb0`. Before it could merge, `main` took #270,
#272, **#271 (t_147201fa) — "wedge stops guessing `multi` from the slug"** and
**#274 (t_8616f2a5) — "the board-state gate hands its CLI probes a child
environment a worker pin cannot steer"**. Both are the same hazard seen from a
different seam, and #274's own commit message records the split: *"argv builders are
untouched (t_dad0da32 owns `--board default`)"*.

`multi` is the lib's capability, not a property of the slug —
`autodown._enum_board_names`, `autodown.py:310`, `hasattr(kanban_db, "list_boards")`
— a rule #271 reached independently while this card derived it. `_gate_multi` now
holds #271's rule in ONE place and threads it, and #274's `_gate_child_env`
scrubbing sits beside it rather than under it.

The shipped split, stated plainly so no reader has to diff three PRs:

| leak | fixed by |
|---|---|
| `_capped_assignees` asked the guard with `multi=False` for `default` | **#271 (t_147201fa)** |
| the CLI child inherited `HERMES_KANBAN_DB`, so `hermes kanban` printed the pinned board | **#274 (t_8616f2a5)** |
| `list_argv` omitted `--board default` | **this card** |
| `show_argv` omitted `--board default` | **this card** |
| `multi` re-derived per site instead of once per gate pass | **this card** (`_gate_multi` + threading) |
| acceptance case in the **wedge** suite driving the real gate end-to-end | **this card** (#271's three tests call `_capped_assignees` directly; #274's ten tests drive the child env) |

The two CLI halves are **not** redundant, which is why both are asserted together in
the acceptance case: #274 removes the pin from the child, and with the pin gone an
*unnamed* board still resolves through `HERMES_KANBAN_BOARD` — so a scrubbed child
with no `--board default` still answers for the wrong board.

`_gate_multi` uses #271's `hasattr` rule rather than calling `_enum_board_names`, so
the module keeps ONE derivation of `multi` and a lib that raises mid-enumeration keeps
its `board=`-capable answer instead of losing the guard (and no board metadata is read
to answer the question).

Measured control probes for `hscc_daemon/tests/test_dispatcher_wedge_default_board_pin.py`,
run against code this card did *not* write:

* at `97359b71` (before any of it, i.e. `1b5e9eb0` + the docs commit): **4 failed /
  4 passed** — no warning fires for `default`, the argv drops the slug, and the
  no-pin case loses its `board=`.
* at `a2e23c9d` (after #271, before #274): **2 failed / 6 passed**.
* at `e5d4a2c4` (`origin/main`, after #271 **and** #274): **2 failed / 6 passed**, and
  both failures are the argv omission — the half only this card fixes. The DB-read half
  and the child-environment half are green there because #271 and #274 fixed them, which
  is the honest statement of what this card still contributes.

## 6. Not touched

hermes precedence, `~/.hermes/**`, `board_pin.py` (it was already right —
it was handed a lying `multi`), and the two probe sites at
`dispatcher_wedge.py:312,456`, which already receive `multi` from the scan's own
enumeration and were never part of the leak.
