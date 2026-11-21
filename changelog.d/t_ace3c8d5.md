kind: Docs
task: t_ace3c8d5

- New audit `docs/audits/require-real-hermes-lazy-sibling-seam_t_ace3c8d5.md`
records a **measured-clean** verdict on the predicate weakness `t_dd814180` fixed
at one site: `require_real_hermes` / `skip_without_real_hermes` ask "did the name
import", and on a thin interpreter that answers yes while hermes' own `connect()`
still cannot run — but none of the five gated files reaches that door, so no site
needed the fix and `hscc_daemon/tests/real_hermes.py` was deliberately left alone
(an unneeded capability probe in the owner module is its own hazard). The record
carries the file-alone sweep on three interpreters, the import-attempt counter that
shows the door is never entered, why (`list_boards()` never connects; `orch_waker`
opens its own read-only URIs by design), the reproduction of the red at
`hermes_cli/kanban_db_connect.py:766` from a control that does enter it, and the
`HERMES_DELEGATED_CHILD_CONTEXT` fencing posture that masks this seam and must be
stated by any future measurement of it.
---
kind: Verified
task: t_ace3c8d5

- Premise re-measured before concluding anything, on `origin/main` `8eb96984`, in a
  task worktree, one pytest process per file
  (`-m pytest -q -rs hscc_daemon/tests/<file>.py -p no:cacheprovider`). The gated
  roster is five files: `test_orch_waker` (:509), `test_autodown` (:857),
  `test_hermes_cli_import_isolation` (:159), `test_orch_waker_state_collision`
  (:120/:254/:306), `test_orch_waker_dead_pane` (:491).
- File-alone tallies — CI-faithful thin venv (`/private/tmp/hscc-ci-venv`, ruamel +
  pydantic present, `hermes_cli` absent from site-packages, tree off `sys.path`):
  41 / 204 / 7+1s / 7 / 26 passed, **rc 0 on all five, zero errors**. `p313`:
  39+2s / 202+2s / 6+2s / 1+6s / 25+1s. canonical: 41 / 204 / 8 / 7 / 26 passed. On
  the thin venv the gate does **not** skip — the gated tests run there and pass; the
  only thin skip across all five files is the unrelated nested-venv line at
  `test_hermes_cli_import_isolation.py:420`.
- The seam is real, reproduced on thin three ways: after the loader returns the real
  library, `import hermes_state_wal` and `import hermes_state` both raise
  `ModuleNotFoundError` while `agent.delegation_context` resolves (the loader's own
  window leaves `agent` resident) — so the card's `:723` site is not a thin failure
  site, only `:645` and `:766`; `hermes_cli.kanban_db_connect._open_configured()`
  called directly raises `No module named 'hermes_state_wal'` at
  `hermes_cli/kanban_db_connect.py:645`; and a control test in the gated files' own
  shape (loader gate, then `lib.connect_closing()`, as `autodown.py:3338` /
  `board_pin.py:245` call it) ends `ModuleNotFoundError: No module named
  'hermes_state'` at `kanban_db_connect.py:766` on thin and passes on canonical.
- Green alone can mean "reached and fine", so reachability was measured with a
  `sys.addaudithook` import-attempt recorder (verified to fire for imports that then
  raise). `from agent.delegation_context import kanban_path_is_fenced` is
  `connect()`'s first unconditional lazy import, so its attempt count answers "was
  `connect()` called?" — it is **0 in all five gated files on thin** (and
  `hermes_state` / `hermes_state_wal` 0 as well), while the same hook goes 1/1/1 on
  the control and 12 `connect()` calls land in `test_board_pin.py` on canonical. All
  zero too over the wider 16-file door-adjacent set (`test_kanban_live`,
  `test_dispatcher_wedge`, `test_escalate_watcher`, `test_merge_queue`, …), all rc 0.
- Measured reason the seam is unreached, not lucky: hermes' `list_boards()` — what
  `autodown.board_db_paths` calls — reads only board metadata
  (`kanban_db_boards.py:122-143`) and never connects, and `orch_waker` opens the
  reported paths as read-only URIs itself (`orch_waker.py:404`/`:584`/`:763`) exactly
  because its docstring refuses `connect_closing`'s read-write schema-init path.
- Two instruments were discarded for producing **false** zeros, both recorded in the
  audit: a tracer that patched at `pytest_configure` silently failed on thin (the
  module is not importable there — the very seam) and printed `connect=0` for
  everything; a `builtins.__import__` hook patched a partially-initialised module and
  corrupted the loader into returning `None`. And `HERMES_DELEGATED_CHILD_CONTEXT`
  (this worker exports `/Users/desac/.hermes`) fences every path under that root —
  including pytest's `TMPDIR` — routing `connect()` to its read-only early-return and
  masking both sibling imports, so the sweep was re-run with `-u
  HERMES_DELEGATED_CHILD_CONTEXT -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD`; both
  postures give the same all-zero counters.
- No product code, no test-code, no `real_hermes.py` edit (card item 2 is the
  outcome; adding an unneeded probe was refused), and no `scripts/run_tests.sh`
  roster / `OWNER_PROBES` / `owners` inventory edit. The diff is the audit record, its
  `docs/README.md` index line, and this fragment.
