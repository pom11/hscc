# Audit: Honest empty states and first-run guidance on every screen

Card: t_f57347f7
Branch: audit/empty-states-t_f57347f7
Base: 6510da5 (operator dev head)

Task: audit every screen for the four states (loading, empty-success, error,
stale) and make each unmistakable, with a next action where one exists. Reuse
the Offline.load/.stale machinery in place. Report the wrong ones with file:line.

## Findings (work in progress)

(Being populated as screens are audited.)

## Method

For each View file: read how `LoadState` / `Offline.load` results are consumed,
and how the four states (loading, empty-success, error, stale) are rendered.
Judge whether an operator can tell them apart and knows the next action.
