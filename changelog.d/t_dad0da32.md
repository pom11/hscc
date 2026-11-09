kind: Fixed
task: t_dad0da32

- **`hscc`'s dispatcher-wedge gate no longer lets a worker's board pin steer the
  `default` board.** `dispatcher_wedge` rebuilt autodown's `multi` flag from the
  slug — `multi = bool(board) and board != "default"` in `_capped_assignees`, with
  the same idiom repeated in the two sanctioned-CLI argv builders
  (`_board_ready_facts`' `list --status ready` and `_card_hold_from_cli`'s
  `show <id>`) — and `multi` is documented to mean exactly
  one thing to `board_pin.open_board`: *did the caller name a board?*
  (`board_pin.py:231-242`). So for `default` the gate told the guard "no intent",
  the guard correctly deferred to the worker's `HERMES_KANBAN_DB` pin, and the
  layer-2 board-state gate read **whatever board the pin pointed at** while the
  panel reported it as `default`. Every other slug was protected; only `default`
  leaked — and it leaked in the one place a fleet-level wedge detector asks about
  the legacy board. The consequence was not cosmetic: `explainably held` by a
  per-profile cap needs no further evidence, so the gate suppressed a gateway
  restart using `running` counts taken from a board it never read. Measured in a
  live worker shell: `list_boards()` returns 23 slugs *including `default`*, and
  `kanban_db_path(board="default")` resolves to `<root>/kanban.db` with the pin
  scrubbed vs `<root>/kanban/boards/<slug>/kanban.db` with it visible — a
  different board's file.
  Decision (a) from the card: `default` **is** a real board, so it is guarded like
  every other one (warn-once naming both boards for reads). Implemented without
  the suggested `multi = bool(board)`, which would have handed `multi=True` to a
  lib that cannot enumerate boards (`autodown.py:310-312` returns `[None], False`)
  and then called `connect_closing(board=...)` on a lib that may not take the
  keyword — breaking the legacy single-board path PR #268 pins byte-for-byte.
  `multi` now comes from the same place it is defined, autodown's single
  enumeration seam (`_enum_board_names`), computed once per gate pass and
  threaded into the two argv builders and the per-profile read
  (`_gate_multi` / `_gate_board_argv`), so the CLI argv and the DB open can no
  longer disagree about which board was named. `--board <slug>` is now stated for
  every slug when the lib enumerates boards — the sanctioned CLI accepts
  `--board default` (measured: exit 0, valid JSON), so this is not a new contract.
  8 tests in `hscc_daemon/tests/test_dispatcher_wedge_default_board_pin.py`,
  asserted on observables only (the `WARN` line naming pin + both boards, the argv
  a human would type, which rows the probe read) and each conflict case paired
  with a non-vacuity assertion that the same armed environment reads the WRONG
  board unguarded; run against the pre-fix code 4 of the 8 fail. Companion cases
  pin what must not change: a worker pinned *to* `default` stays silent, the
  legacy no-`list_boards` shape keeps `connect_closing()` with no board and no
  warning, and a `list_boards` that raises cannot switch the guard off.
  Decision record: `docs/audits/wedge-default-board-pin-t_dad0da32.md`.
