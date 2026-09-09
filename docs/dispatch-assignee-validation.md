# Dispatch --assignee: validation guard (t_d3c929aa)

## What changed
`flightdeck message dispatch --assignee X` now validates X against Hermes' real
profile registry BEFORE creating the card. If X is not a live profile it prints
`error: unknown assignee '<X>'` on stderr and exits 2, creating no card and
sending no announcement.

- `flightdeck/core/kanban.py`: added `valid_assignee(name, _profiles=None)`
  (wraps `hermes_cli.profiles.profile_exists`) and `_load_profiles()` (lazy
  import mirroring `_load_kanban_db`).
- `flightdeck/commands/message.py` `cmd_dispatch`: up-front guard before any
  board resolution / mutation. Empty or absent `--assignee` = "no constraint"
  (still valid; dispatcher applies its default).
- Tests: 3 new cases in `tests/test_message.py` (valid assignee preserved
  verbatim; unknown assignee fails loudly with no card; valid still accepted).
  MCP dispatch seam in `tests/test_mcp_mutating.py` stubs ~valid_assignee~.

## Root cause of the reported "assigned to architect" symptom — NOT dispatch
Direct evidence from the live board (`task_events`) shows the four cards
(t_299d0dbd, t_a6697001, t_9e66d919, t_61f27161) were CREATED with the CORRECT
assignee (`devops-engineer` / `backend-engineer`). Hours later they were bulk
reassigned to `architect` via `assign_task(..., "architect")` calls (plain
`assigned` events, no `source: default_assignee` key). A live reproduction on a
temp board confirms `message dispatch --assignee backend-engineer --apply`
stores `backend-engineer` exactly.

The `architect` reassignment is a SYSTEMIC pattern across the board's whole
history (cards on 08-14, 08-30, 09-02, 09-03, 09-08 all get bulk-reassigned to
`architect`). The events carry no actor/run_id, so the source is not yet
attributable. The guard added here prevents the dispatch footgun going forward.

## Follow-up recommended (separate task)
Attribute the bulk `assign_task -> architect` calls (log caller/actor id) and
gate whether bulk reassign to `architect` should be allowed. See also
`docs/REPORT_t_d3c929aa_dispatch-assignee.md` (on-disk, gitignored) for full
evidence and card IDs.
