kind: Fixed
task: CR-1

- **The orchestrator waker's wake-once dedupe never suppressed anything.**
  `_claim_connect` handed out plain tuples while Hermes' claim helpers index
  rows by name, so every wake-claim read raised a `TypeError` that escaped the
  fail-open path; the waker treated every event as owed and logged "failing
  open towards delivering" per board per tick. It now sets `sqlite3.Row`, and
  tests drive Hermes' real claim helpers over the real connector (#290, #289).
