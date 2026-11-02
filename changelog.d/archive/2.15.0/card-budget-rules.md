kind: Changed
task: card-budget-rules

- **Workers hand off instead of grinding; orchs size cards by a call budget.**
  On 2026-11-01 three cards ran 5-7 h, with 93-100% of their wall time inside
  model calls: the context grew to 100-200K tokens, so every call re-read it
  (p50 36-67 s, worst 54 min). Worker SOULs gain "Stay small — commit slices,
  hand off early": commit each green slice at once, and wrap up (commit, then
  complete with a DONE / LEFT handoff) on a `[budget]` note, after a context
  compression, or after about 40 tool calls without a commit. The project-orch
  "How you manage work" rule 1 now states the budget (about 40 model calls,
  under ~120K tokens, slice list in the card body), says handoffs are normal
  (card each LEFT item at once), and limits rebase-only cards to one attempt.
