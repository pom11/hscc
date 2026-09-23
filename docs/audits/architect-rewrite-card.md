# Card t_7a771891 — project digest <name> (bounded archive digest)

STATUS: done

## What shipped
- NEW `flightdeck/core/digest.py` — bounded, read-only synthesis of a project's
  archive history. Per session: title / date range / message count / bounded
  `decided/outcome` extract (head+tail of user/assistant words, tool dumps
  NEVER surfaced) / resume line (`hermes -p default --resume <id>`). Hard
  capped at `DIGEST_MAX_CHARS`, per-thread slice capped at `SLICE_MAX_CHARS`.
- `flightdeck/commands/project.py` — new `project digest <name>` subcommand
  (`cmd_digest` + argparse wiring; `--json` support; hidden `--archive-dir` /
  `--mapping-path` seams for tests). Reads archive md files directly; never
  merges or mutates sessions.
- `tests/test_digest.py` — 11 tests: bounded size, per-thread fields present,
  head/tail decision slice, genuine "no history" vs `runtime_error` env fault,
  JSON clean output, subcommand registered, read-only (never writes).

## ENV FAULT vs DATA
An unreadable Hermes runtime renders as `runtime_error` (exit 3), never as an
empty digest implying "no history". A project with no archive dir is genuine
"no data" (exit 0, empty sessions).

## Verification
- tests/test_digest.py: 11 passed
- full hscc-project suite: 1240 passed
- Ran under hermes venv, HERMES_DELEGATED_CHILD_CONTEXT unset (per harness fix).

## Test helper bug found (test-side)
`_session_md` computed `message_count` from `body.count("**")` BEFORE assigning
the default body — yielding 0 for every default-body session. Moved the count
computation after the default-body assignment. (digest.py itself was correct.)
