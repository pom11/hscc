# t_32f97d20 — how `DEFAULT_TARGET_PROFILES` was derived (board-isolated)

Card: `t_32f97d20` (impl child of `t_16eb73ab`). This file documents the
seed-derivation rule the operator mandated in comment 1642 on t_16eb73ab:

> the hscc-board seed must come from a rule that actually isolates the hscc
> board — derive it from the hscc board's own task records (e.g. distinct
> assignees over that board's task history via the sanctioned hermes CLI),
> document the derivation in the PR description, and POST THE EXACT SEED LIST
> as a comment on this card BEFORE finalizing the config default.

The exact 13-name list was posted on t_32f97d20 (comment 1654) before the
constant shipped in `hscc-bootstrap/install_payload.py`.

## The correction that made board isolation possible

Phase 1 (`docs/t_16eb73ab-profile-target-measurement.md`) proved
`hermes kanban assignees` returns **byte-identical output across board
slugs** and concluded the assignees census cannot isolate a board. That is
true of the `assignees` verb (a fleet-global census). It is NOT true of
`stats`, and the phase-1 cross-slug byte-identity had a different cause:

**The worker environment pins `HERMES_KANBAN_DB` at the dispatching board's
DB** (`hermes_cli/kanban_db.py::kanban_db_path` — "HERMES_KANBAN_DB pins it
(injected into workers)"). Any `hermes kanban --board <slug> …` run from
inside a kanban worker resolves to the pinned DB regardless of `--board`, so
*every* verb looked board-blind from a worker shell. With the pin cleared in
a normal shell, `hermes kanban --board hscc stats --json` reads
`<root>/kanban/boards/hscc/kanban.db` — a per-board DB — and the outputs
differ per slug (measured 2026-11-03 on the daemon host):

| board slug | `stats.by_assignee` keys | vs hscc |
|---|---|---|
| hscc        | 16 names | — |
| sphoin      | 9 names (5 board-local: data-engineer, ml-engineer, ml-researcher, orchestrator, sphoin-orch) | DIFFERENT |
| ecofire-bc  | 8 names (2 board-local: ecofire-app-orch, ecofire-flows-orch) | DIFFERENT |
| default     | 2 names | DIFFERENT (subset) |

`stats` is also the sanctioned read for a worker context: unlike `list`
(which runs `recompute_ready`, a write, and is fenced for delegated
children), `stats` is read-only and survives the fence.

## The derivation (two sanctioned CLI calls)

1. **Board roster** — `hermes kanban --board hscc stats --json` →
   `by_assignee` keys = every distinct assignee over the hscc board's own
   task history (16 names). Query source: `board_stats` →
   `SELECT assignee, status, COUNT(*) … WHERE status != 'archived' AND
   assignee IS NOT NULL GROUP BY assignee` — the board's task records, not
   a global census.
2. **Home filter** — `hermes kanban --board hscc assignees` → `on_disk`
   flags. Names with `on_disk=no` are ghosts with no profile home to write
   to (phase-1 set B−A) and are dropped: `other`, `p4`, `sdlc-review`.

`SEED = roster(∩ non-archived task history) ∩ on_disk` = 13 names:

```
architect backend-engineer coder default devops-engineer
ecofire-bc-engineer ecofire-bc-orch hscc-orch ios-engineer qa reviewer
technical-writer worker
```

Convergence note: this is the same 13 the phase-1 doc computed as
"ever-assigned ∩ on-disk" — reached here independently through the
board-isolated path, so the phase-1 candidate was confirmed, not silently
substituted.

## Caveats (recorded honestly)

- `stats` counts **non-archived** tasks only. The hscc board has 95 archived
  cards; an assignee that appears ONLY on archived cards would not enter the
  seed. The seed is the DEFAULT, and the config file is the widening door —
  a stale name is one `target_profiles` edit away in either direction.
  (`list --archived --json` could include archived history but is a write
  verb under the hood — `recompute_ready` — and is fenced for delegated
  worker contexts; `stats` was chosen precisely because it is not.)
- The roster is a point-in-time reading (2026-11-03). New board assignees do
  not auto-enter the seed — by design for option 3: `doctor`/this doc name
  the rule, the operator edits `~/.hscc/target_profiles.json`
  (`{"target_profiles": [...]}`, or `["*"]` for all). The file is dedicated
  — deliberately NOT a key in `cluster.json`, which
  `hscc-cluster/discovery.py::_write_cache` rewrites from scratch on every
  live discovery (that rewrite also drops the existing `node_recovery`
  opt-out blocks; tracked as t_7c3ec484).
- Ghosts (`other`, `p4`, `sdlc-review`) are excluded: no profile home
  exists, and the installer deliberately never fabricates a home dir for a
  name that has none (`_selected_profile_plugin_dirs` iterates homes that
  exist).

## Reproduce

From a NON-worker shell (no `HERMES_KANBAN_DB` in env):

```bash
hermes kanban --board hscc stats --json     # by_assignee keys = roster
hermes kanban --board hscc assignees        # on_disk column
# seed = roster ∩ on_disk=yes
```
