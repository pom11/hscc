# Approach: putting Hermes back in the driving seat

**Date:** 2026-08-11 · **Superseded in part on 2026-08-13 — read this first**

> **Correction.** The premise below ("Hermes learns nothing") was wrong, and the
> table of "measured" facts was drawn from a single file. Hermes writes memory
> **per profile** (`~/.hermes/profiles/<profile>/memories/MEMORY.md`) as well as
> globally; those per-profile files were being written daily throughout. On
> 2026-08-13 the global file was also written unprompted, capturing a lesson from
> a summary posted to a topic — the loop works.
>
> Two real faults were found and fixed while chasing the wrong conclusion: a
> `memori` Cloud plugin directory shadowed the `memori` PyPI SDK on `sys.path`,
> so the BYODB provider failed to initialise and **all four of its recall tools
> were dead**; and its augmentation model id was pinned to a model no longer
> served. Separately, `memori_byodb` has **no write path at all** — it is a
> recall-only layer over a database you populate yourself, so its unchanged DB
> was expected behaviour, not a failure.
>
> The architecture argued for below (topic session = brain, MCP = hands, boards =
> muscle, CLI = panel) still stands on its own merits. The diagnosis that
> motivated it does not. See `docs/INCIDENTS.md`.


## The problem, stated precisely

Work executes fine. Hermes learns nothing from it.

Measured on this host:

| fact | value |
|---|---|
| `~/.hermes/memories/MEMORY.md` last written | 2026-08-09 19:44 |
| merges landed since then | 46+ |
| mentions of "flightdeck" in Hermes' memory | 0 |
| `SKILL.md` files patched in 7 days | 0 |
| HSCC version Hermes believes | `v1.0.0-beta.1` (actual: v1.8.1) |

Hermes learns through `background_review`, a post-turn fork that **replays a
session's conversation** and decides whether to save a memory or patch a skill.
Board execution produces no orchestrator conversation, so it teaches nothing.
Its memory is a snapshot of whenever we last talked, and it is drifting.

## What is actually bypassed

Not execution. The gateway dispatcher iterates `list_boards()` and runs cards in
worktrees — those workers *are* Hermes agents, and `auto_review` fires per card.
Six ingest cards on six different boards ran, reviewed and completed with no
chat involvement at all.

What is bypassed is **judgement**. I decide what to build, how to split it, who
to assign, when to merge. Hermes' orchestrator was demoted to a chat endpoint,
and `auto_decompose` never fires because it receives pre-decomposed atomic cards.

The reasoning still happened — it just happened in the wrong agent. Today
produced real procedural lessons (a 45s client timeout measures queue depth, not
liveness; empty worktree = starved vs files-no-commit = working; green tests
prove nothing about the interpreter the console script runs under). All of it
landed in the operator's notes. None of it reached Hermes' skills.

## Why "route everything through chat" is not the fix

Measured failures of chat as a work transport, all in one day:

- Telegram's hard 4096-character limit; a 191KB context cannot pass
- three consecutive 900s timeouts on that context (~50k tokens of prefill at
  ~25 tok/s, competing with kanban workers on the same span)
- a reply read that was a message from the **previous day**
- prose acknowledgements returned where JSON/roadmap was expected
- the orchestrator creating seven cards itself, on the wrong board

Wrapping chat in another MCP inherits every one of these.

## The architecture

Four parts, each doing what it is actually good at:

```
topic session   the brain     per-project context + where learning happens
flightdeck MCP  the hands     structured RPC: no size cap, no message matching
kanban boards   the muscle    parallel, worktree-isolated, reviewed execution
flightdeck CLI  the panel     what the human reads; same state, same guardrails
```

Each Telegram topic **is** an orchestrator session — verified: `session_key`
carries platform, chat and thread. So the sphoin topic already holds sphoin's
context, and `background_review` fires per session, which makes learning
naturally per-project. The per-project boards created on 2026-08-11 mirror the
per-topic sessions; topic ↔ board ↔ repo was always the right shape, we simply
were not using the topic half.

MCP dissolves the transport problem that made chat unusable: structured
request/response, no 4096 cap, no "which message was the answer" polling. Only
intent and decisions travel through chat. Bulk data never does.

## The rule

> **The reasoning goes in the session. The typing goes on the board.**

| goes in the topic session | goes on a board |
|---|---|
| diagnosis — why did this fail | bump the version |
| design decisions — why this approach | add the flag |
| review judgement | write the tests |
| deciding subagent vs kanban card | mechanical refactors |

High skill value and low volume on the left; zero skill value and high volume on
the right. Putting execution detail in-session just burns context; keeping
diagnosis out of it is what stopped Hermes learning.

`flightdeck report` remains, but only for **backfill and awareness** — a summary
of outcomes cannot reconstruct the dead ends, so it produces shallow skills. It
is not the learning mechanism; the session is.

## Migration

**Phase 0 — done.** Flightdeck is the instrument panel with guardrails, and the
MCP server exists: 25 tools, 11 mutating ones defaulting to `apply=False`,
verified by inspecting the signatures.

**Phase 1 — give Hermes the hands.** Register the server in `~/.hermes/config.yaml`
alongside the existing `powerbi` entry:

```yaml
mcp:
  flightdeck:
    command: ~/.hermes/hermes-agent/venv/bin/python3
    args: ["-m", "flightdeck.mcp_server"]
```

Then seed each topic session once with its project identity: name, repo, board,
roadmap path, and the instruction to use `flightdeck_*` tools for state and
dispatch.

**Phase 2 — move the driving seat.** State intent in the project's topic
("finish the release-flow milestone"). The session calls
`flightdeck_roadmap_progress` → sees what is open → decides **subagent for a
lookup, kanban card for real work** → calls `flightdeck_decompose` /
`flightdeck_start`. It is deciding, with real state, through the same guardrails
the CLI enforces.

**Phase 3 — close the gap.** `flightdeck report --all --backfill 72h` once, to
tell Hermes what it missed. Then verify: does `MEMORY.md` actually change? A
negative result is a finding, not a failure.

**Phase 4 — keep the signal clean.** Topic sessions carry a lot of stale
reasoning noise (the bot narrates chain-of-thought into topics). Prune it, or the
useful signal gets buried — that noise is what caused a day-old message to be
read as a reply.

## What changes day to day

- **Operator:** talks to a project's topic instead of hand-writing cards. Reads
  `flightdeck standup` / `qa` as now.
- **Me:** stop acting as the orchestrator. State symptoms in the topic and let
  the session diagnose and dispatch. Slower, sometimes wrong — that is the price
  of it learning.
- **Flightdeck:** stops being the planner, becomes the shared instrument panel
  and the guardrail layer for both human and Hermes.

## Costs and risks, honestly

- **Slower.** ~25 tok/s on a tp=2 span. Diagnosis in-session costs minutes that
  I currently spend in seconds.
- **The session can be wrong.** It created seven cards unprompted, on the wrong
  board. P4 (propose, do not create) and B1 (name the board explicitly) are the
  mitigations, and they must stay.
- **A session that only receives conclusions never learns to diagnose.** This
  has to start before Hermes is good at it, and it will be rough early.
- **Context bloat.** Long-lived sessions wedge (`max compression attempts`).
  Watch for it; export/reset when it happens.

## The measure of success

`~/.hermes/memories/MEMORY.md` changes on its own, mentions the projects by
name, and its version facts stay current — without anyone editing it by hand.
If that number stays frozen, the approach is not working and should be revisited
rather than defended.
