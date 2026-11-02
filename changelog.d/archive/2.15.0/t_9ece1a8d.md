kind: Fixed
task: t_9ece1a8d

- **`hscc verify` crashed outright on the operator machine — the WHOLE table
  died on one list-shaped state file.** `check_daemon_streams` called
  `data.get("ok")` unconditionally on every `~/.hscc/state/*.json`, but the
  state dir is shared with non-stream writers: cards-monitor's token ledger
  `kmon_orch_tokens.json` (kanban_live.py `LEDGER_PATH`, `_update_ledger`) is
  a top-level JSON **list**. The `AttributeError: 'list' object has no
  attribute 'get'` did not just fail the row — it aborted `run_all`, so every
  later check (incl. the served-profile endpoint guard, proven working at
  122 base_urls/48 profiles) never ran and `verify --json` emitted rc 1 with
  zero JSON. Reproduced fixture-style on 2.14.0 AND main (audit F GAP-F-1 —
  not release drift). The check is now schema-tolerant: a non-dict file is
  reported `unrecognized schema (<shape>)`, named, skipped, and the loop
  continues. Same fix also closes GAP-F-2: a dict-shaped file with **neither**
  `ok` **nor** `last_check`/`timestamp` (live: `orch_waker.json` in the
  stopgap `{last,pending,sent}` schema, `kanban_paused.json` = `{}`,
  `reasoning_policy.json`) used to be counted inside "all N streams healthy"
  while asserting nothing; it is now reported `not-checked (no ok/timestamp)`
  and the row goes UNVERIFIED (`ok=None` — run_all's established "could not
  check this" vocabulary), never green.
---
kind: Verified
task: t_9ece1a8d
order: 1

- `hscc_daemon/tests/test_verify_state_schema.py`: 6 new cases, **fail-then-
  pass** — the three GAP-F-1 legs die pre-fix with the exact live traceback
  (`AttributeError` at verify.py:333), the GAP-F-2 legs assert the
  not-checked/unverified semantics pre-fix fails. Fixtures are shape-for-shape
  copies of the real live `~/.hscc/state` (IPs redacted to the sanctioned
  placeholder), all under pytest `tmp_path` — the live dir is never touched.
- Acceptance probe `probe_verify_live_fixture.py` (card workspace): real
  `hscc verify --json` from the worktree under a fake HOME seeded with an
  exact copy of the live 17-file state dir — post-fix prints all 13 rows,
  `daemon_streams` names all four unverifiable files, pre-fix printed zero
  JSON at rc 1.
- `hscc_daemon` suite green with the fix (956 passed / 1 skipped, incl. the
  intentional-autodown writer/reader round-trips in `test_lifecycle.py`,
  untouched); full `scripts/run_tests.sh`: **ALL GREEN** (12 dirs);
  `scripts/address_guard.py --staged` rc 0; `changelog_fragments.py check` OK.
