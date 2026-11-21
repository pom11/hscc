# Placeholder ids and the parked-vs-gated distinction

## Never write a card id before it exists

Write a plan comment/graph with card TITLES, and add the id table only after
`kanban_create` has returned each id. A placeholder id pasted into a plan
comment or into another card's body is a phantom: the kernel's prose scan flags
`t_<hex>` ids that do not resolve, comments are append-only (there is no edit,
so the wrong id is permanent unless corrected), and a downstream worker can
branch from a card that was never created. If a placeholder already shipped,
immediately `kanban_comment` the correction on the card that carries it, name
the real id, and repeat the corrected table on the decomposition task.

## `todo`+parents and `blocked` are different promises

`kanban_create(parents=[...])` with the default status lands the card in
`todo`/gated and it AUTO-PROMOTES to `ready` (the dispatcher claims it) the
moment the parents finish. `initial_status="blocked"` parks it: parents
finishing does not release it — only an explicit unblock does. So a card that
must not be claimed by the cluster (OpenCode-routed, human-gated) has to be
`blocked`, and a card that should flow on its own must not be. Creating a
parentless "fix" card with the default status puts it in `ready` immediately:
if it must wait for a sibling decision, link the parent (that demotes it) or
park it — do not rely on the dispatcher being slow.
