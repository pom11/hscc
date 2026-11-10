kind: Fixed
task: t_dad0da32

- **The dispatcher-wedge gate no longer hides the `default` board from itself.**
  `dispatcher_wedge`'s layer-2 board-state gate rebuilt autodown's `multi` flag
  from the *slug* — `bool(board) and board != "default"`, in the per-profile read
  and in both sanctioned-CLI argv builders — and `multi` is documented to mean
  exactly one thing to `board_pin.open_board`: *did the caller name a board?*
  (`board_pin.py:231-242`). So for `default` the gate said "no intent", the guard
  deferred to the worker's `HERMES_KANBAN_DB` pin, and the gate read **whatever
  board the pin pointed at while the panel reported `default`**. Not cosmetic: a
  card explained by a per-profile cap needs no further evidence, so the gate
  suppressed a gateway restart using `running` counts taken from a board it never
  read. Measured in a live worker shell: `list_boards()` returns 23 slugs
  *including `default`*, and `kanban_db_path(board="default")` is
  `<root>/kanban.db` with the pin scrubbed vs `<root>/kanban/boards/<slug>/kanban.db`
  with it visible — a different board's file.
  Split across two cards that reached the same rule mid-flight: **t_147201fa
  (PR #271) removed the heuristic from the per-profile DB read**
  (`multi = hasattr(kanban_db, "list_boards")`). This card covers what that left —
  the two argv builders still omitted `--board default`, so `hermes kanban list`
  read whichever board `HERMES_KANBAN_BOARD` / the pin named while the answer was
  presented as `default`, and `multi` was still re-derived per site instead of once
  per pass. Now `_gate_multi` holds that one rule and the answer is computed once
  per gate pass and threaded into `_board_ready_facts`, `_capped_assignees` and
  `_card_hold_from_cli`; both argv sites share one builder, `_gate_board_argv`, so
  the CLI argv and the DB open can no longer disagree about which board was named.
  `--board <slug>` is stated for every slug when the lib enumerates boards — the
  sanctioned CLI accepts `--board default` (measured: exit 0, valid JSON), so this
  is not a new contract. The legacy no-`list_boards` shape is byte-for-byte
  unchanged: no `--board`, `connect_closing()` with no `board=`, guard never
  consulted, no warning — which is also why the card's suggested
  `multi = bool(board)` was not used (it hands `multi=True` to a lib that cannot
  honour a board argument).
  8 tests in `hscc_daemon/tests/test_dispatcher_wedge_default_board_pin.py`, driving
  the real gate end-to-end with `HERMES_KANBAN_DB` + a fence marker armed and
  asserting observables only (the `WARN` line naming pin + both boards, the argv a
  human would type, which rows the probe read), each conflict case paired with a
  non-vacuity assertion that the same armed environment reads the WRONG board
  unguarded. Control probes: against `1b5e9eb0`-era code 4 of 8 fail; against
  `origin/main` after #271, 2 fail and both are the argv leak. Decision record:
  `docs/audits/wedge-default-board-pin-t_dad0da32.md`.
