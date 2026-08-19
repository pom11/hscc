# Flightdeck — concepts

The mental model, in brief. Every command in the tool assumes this shape.

## The project

A **project** is three things bound together in the registry
(`~/.flightdeck/registry.yaml`):

- a **git repo** (the only required field; everything else degrades to `unknown`)
- a **Hermes kanban board**
- a **Telegram topic**

One line in the registry wires the three. A project is never dropped from
output because a field is missing — it is shown with that dimension marked
`UNKNOWN`, because a silently omitted project is the failure this tool exists
to prevent.

## Cards are attributed to projects by repo path, never by board slug

Many projects share the `default` Hermes kanban board. So a card's project is
resolved by walking card → branch (`wt/<card>`) → worktree → **repo path** →
registry entry. Treating board slug as the project is wrong: it
misattributes every card on a shared board. This caused real misattribution
before, and every card-facing command (qa, review, standup, reconcile,
hygiene) now resolves the project from the repo path.

## The milestone

A **milestone** is a named goal with a stable id, living in a project's
`ROADMAP.md` (a plain versioned file, not a new datastore):

```markdown
## Milestone: review-loop <!-- id: review-loop -->
status: later
```

Cards link back to the milestone that spawned them via a `MILESTONE: <id>`
body tag, stamped in by `decompose --milestone`. This is how
`roadmap progress` ties live cards to roadmap items and renders counts —
neither the roadmap file nor the board may hide the other.

## The loop, end to end

Flightdeck is built around one workflow. Each step is a separate command
because each is a genuine hand-off, and each ships `--apply` so nothing
changes until you see the plan.

```
ingest          -> adopted       decompose       start           qa              review          report
<project>          roadmap          -> cards          -> fleet         -> queue        -> merged      -> lesson
```

1. **ingest** — gathers what already exists (skills, repo docs + git log,
   topic) and synthesises a first roadmap **draft** (`ROADMAP.draft.md`). A
   proposal, never auto-written over a real roadmap.
2. **adopt** (`roadmap adopt`) — promotes the reviewed draft into `ROADMAP.md`,
   validating it and backing up the old one. Produces the roadmap of record.
3. **decompose** — asks the cluster to break a milestone/roadmap item into
   **atomic cards**, gated on card quality (one concern, a `VERIFY:` line,
   concrete references, acceptance criteria, dependency order). A proposal;
   `--apply` creates only the passing cards.
4. **start** — releases a milestone's cards to the fleet, concurrency-aware,
   holding any card whose dependency has not merged. Produces running work.
5. **qa** — the manual-testing queue: what you actually have to click/run,
   showing each card's `VERIFY:` line and whether the automated verify has
   run. `--notify` pings you when something enters the queue.
6. **review** — shows the diff, merges the branch, closes the card in one
   action. Produces merged work and an honest board.
7. **report** — posts a short, learnable completion summary to the project's
   topic so Hermes can learn from work it never directly saw. Produces a
   memory/skill update in the fleet.

Underneath it all, **reconcile** sweeps up the stragglers — closing cards
whose branch already merged, flagging dead/stale ones — which is the fix for
the 89–100% false signal this project was started to eliminate.

## What flightdeck is NOT

- **Not a kanban replacement.** It reads and reconciles Hermes kanban; it
  never reimplements it.
- **Not a chat client.** Telegram support is digests, topic management, and
  targeted sends — not a general messenger.
- **Not a CI system.** It reports test/verification state; it does not own
  pipelines.

## The two design rules that govern everything

1. **Read-only by default.** Every mutating command prints exactly what it
   will do and changes nothing until `--apply`. The plan always comes first.
2. **Never report a state you have not verified.** Every claim traces to a
   real check. `UNKNOWN` is a distinct result, never rendered as OK.
