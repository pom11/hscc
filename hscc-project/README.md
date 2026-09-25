![FlightDeck — Project Control. Manage projects in Hermes.](docs/assets/banner.png)

# Flightdeck (hscc project)

> **What you're reading.** This tool is the project/kanban orchestration
> domain of HSCC. It started life as a standalone tool named *flightdeck*;
> during the flightdeck→hscc port it moved into the repo under `hscc-project/`
> and is now reached through the hscc CLI as **`hscc project …`**. The
> standalone `flightdeck` / `flightdeck-mcp` console scripts are no longer how
> you use it — type `hscc project <command>` instead (e.g.
> `hscc project standup`, `hscc project review <card>`). The full command
> reference is [`docs/COMMANDS.md`](docs/COMMANDS.md); the
> `flightdeck X` ↔ `hscc project X` mapping and naming-collision notes live in
> [`../docs/PROJECT-COMMANDS.md`](../docs/PROJECT-COMMANDS.md).

This tool answers the one question you have every morning when a fleet of AI
agents works across many projects: **what actually needs me right now?**
Instead of opening every project's kanban board, git log, and chat to figure
out what's stuck, blocking, finished, or drifting, you run one command and it
tells you — in plain language, ordered by how much it should interrupt you.

It's MIT licensed, needs only Python 3.10+, git, and the Hermes kanban DB. It
runs entirely on your own machine against local state, and is **read-only
until you explicitly pass `--apply`** — so you can explore it without any
risk.

---

## If you get stuck

- **Common failures first:** a missing dependency is never silently ignored —
  `doctor` checks your environment and tells you exactly what's off. If a
  board is unreachable, commands say so loudly instead of crashing or
  pretending.
- **Full reference:** every command and flag lives in [`docs/COMMANDS.md`](docs/COMMANDS.md).
- **The mental model** (what a "card", "board", "project", "milestone" means)
  is spelled out in [`docs/CONCEPTS.md`](docs/CONCEPTS.md).
- **Real failures and the lessons they taught:** [`docs/INCIDENTS.md`](docs/INCIDENTS.md).
- **Config and your project registry,** field by field: [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md).

---

## Quick start

The tool ships in the hscc repo and is already on your PATH once hscc is
installed (`hscc project …`). If you are reading this from a source checkout,
install hscc normally, then bootstrap the project registry and read your first
digest. Each command is followed by *why you'd run it*:

```sh
# Create ~/.flightdeck (config + registry + templates) and check your
# environment. Dry-run by default — nothing is written until you add --apply.
hscc project init

# Find your existing repos and boards and wire them into the registry, so the
# tool knows what projects you have.
hscc project project sync --apply

# Your daily digest: what's happening across every project and what needs you.
hscc project standup
```

The first `init` is a dry run by default — it shows you what it *would* create
and checks the environment before writing anything. (This is true of every
mutating command: the plan always comes first, and nothing changes until you
pass `--apply`.)

```text
$ hscc project init
flightdeck home: ~/.flightdeck (dry run — use --apply to write)
Would create:
  would create ~/.flightdeck/config.yaml
  would create ~/.flightdeck/registry.yaml
  would create ~/.flightdeck/templates/

Environment check:
  python           [ok] Python 3.11.16 (/…/venv/bin/python)
  mcp-sdk          [ok] mcp SDK present (v?): MCPServer (2.0 layout)
  git              [ok] git on PATH: /usr/bin/git
  hermes-kanban    [ok] Hermes kanban DB reachable and readable at ~/.hermes/kanban.db

Next steps:
  1. Run `hscc project project sync --apply` to adopt your existing repos
     into the registry.
  2. Register the MCP server with your MCP client so an agent can drive it
     (see "MCP — let an agent drive it" below).
```

`init` creates `~/.flightdeck`, seeds `config.yaml` and `registry.yaml` from
the shipped examples (never overwriting anything that already exists), copies
the prompt templates, and prints your exact next steps.

`project sync` then scans what you already have — git repos under `~/dev` and
board slugs on the kanban — and reports every match, conflict, and orphan
before wiring anything into the registry. It never writes without `--apply`
and never overwrites an existing binding. A real run (trimmed, paths shortened
to `~`):

```text
$ hscc project project sync
PARTIAL (N)
  ecofire-app  repo ✓   topic —  board ecofire-app
  ...

ORPHAN REPOS (N)  ...  ORPHAN BOARDS (N) ...

--apply writes only the unambiguous matches above.
```

Pass `--apply` to write only the unambiguous matches into the registry.

Then your first digest:

```text
$ hscc project standup
NEEDS YOU (1)
  [hscc] make the release installable (branch wt/t_abc123)

FAILING (0)  STALE (0)

RUNNING (5)
  [hscc] README review … (branch wt/t_0ae232a4)
  ...

DRIFT (12)
  ...

read 4 projects | 4 boards | 8 cards | 8 attributed | 0 unreadable
in flight: 3 cards across 3 boards (cap 3/board)
```

That's the whole tool in under a minute. Read on for what a normal day looks
like, or jump to [`docs/COMMANDS.md`](docs/COMMANDS.md) for the full reference.

---

## A day in the life

Here's what someone might actually do with it once it's set up. You mostly
live in two commands — `standup` (what needs me?) and `review` (does it
merge?).

**1. Morning: `hscc project standup`**

Every day starts the same way — a digest of what needs you:

```text
$ hscc project standup
NEEDS YOU (2)
  [hscc] [t_3f2a1b] fix login redirect — blocked, needs a decision
  [hscc] [t_755d41a3] README is hard to follow — awaiting review

RUNNING (14)
  [ecofire-app] [t_8c1d2e] migrate auth to v2 — on track
  ...

DRIFT (3)
```

Two cards genuinely need you. One is a question only you can answer, the other
finished and is waiting for you to merge it.

**2. See what's actually blocking: `hscc project why <card>`**

Don't guess what "blocked" means. Ask the tool to trace the card's full story
across the kanban board and git:

```text
$ hscc project why t_3f2a1b
t_3f2a1b  fix login redirect
  status: blocked
  last event: await_review (2h ago) — agent asked a question
  branch: wt/t_3f2a1b — 3 commits, HEAD not on main
  question: "Should the redirect preserve the ?next= param? Product hasn't decided."
```

Now you know exactly what to answer.

**3. Merge the finished one: `hscc project review <card>`**

`review` reads the branch, checks it merges cleanly, and gives you a verdict —
then, with `--apply`, merges it into `main` and closes the card in one action:

```text
$ hscc project review t_755d41a3
t_755d41a3  README is hard to follow
  branch wt/t_755d41a3: 4 commits, merges cleanly into main
  files changed: README.md
  verdict: READY — merge & close

$ hscc project review t_755d41a3 --apply
merged wt/t_755d41a3 into main; card closed.
```

Or, to see everything the fleet is waiting on you to review at once:

```text
$ hscc project review --queue
review queue: 2 card(s) awaiting review.
```

**4. Answer the block, then let it continue**

Back where the agents work, you answer the question. The agent picks the card
back up, and tomorrow morning `standup` reflects it.

**That's the loop.** Look at the digest → drill into what needs you → merge
what's ready / answer what's blocked → repeat. Three commands cover the
questions that used to cost a morning:

| Command | What it answers |
|---------|-----------------|
| `hscc project standup` | What is happening across every project, and what needs me? |
| `hscc project qa` | What do I actually have to click or run by hand? |
| `hscc project review <card>` | Is this ready to merge, and does it merge cleanly? |

`qa` and `standup` can also watch live (`--watch`).

---

## What you get

- **An honest digest** — `standup` tells you exactly what needs you, ordered
  by how much it should interrupt (NEEDS YOU, FAILING, STALE, RUNNING, DRIFT).
- **Roadmaps derived from your own history** — `ingest` drafts `ROADMAP.md`
  from your existing skills, repo, and git log.
- **decompose → dispatch → QA** — `decompose` breaks goals into atomic,
  quality-gated cards, `start` releases them concurrency-aware, and `qa`
  tells you what you actually have to test by hand.
- **Release with post-install verification** — `release` gates on real
  preconditions and `verify` runs the actual verify command, never a fake pass.
- **A board that stops lying** — `reconcile` and `hygiene` close cards whose
  work already landed and surface the decay.
- **An MCP server** — an agent can drive the whole surface through the Model
  Context Protocol (`flightdeck-mcp`).

---

## The work loop

From roadmap to shipped work — each step is a separate command because each
is a genuine handoff, and every one ships its own `--apply` (dry-run by
default):

```sh
hscc project ingest <project>            # draft ROADMAP.draft.md from existing context
hscc project roadmap adopt <project>     # promote the reviewed draft into ROADMAP.md
hscc project decompose <project> --milestone <id>   # break a milestone into atomic cards
hscc project start <project> --milestone <id>       # release the cards to the fleet
hscc project qa [--watch]                # what you actually have to test by hand
hscc project review <card>               # review, merge, close
hscc project report <project>            # post a learnable summary; Hermes learns
```

`report` is the step that lets the fleet itself learn — the orchestrator
can't see board execution, so `report` tells it what shipped.

---

## Requirements

- **Python 3.10+** and **git**.
- **Hermes kanban DB** — the board-facing commands (`standup`, `qa`,
  `review`, `metrics`, `why`, `reconcile`, `hygiene`, `lint-cards`,
  `legacy-cards`, `migrate-card`, `start`, `decompose`, `report`, `roadmap
  progress`) read and reconcile cards. Without a board, those report loudly
  that the board is unreachable rather than crash — and `standup` still
  produces its non-board sections.

Telegram was removed from the fleet: no delivery channel exists, so
`message`, `report`, and the deprecated `qa --notify` / `ingest --limit` /
`ingest --ask-inline` flags either render without delivering or are honest
no-ops rather than pretending to post.

A set of commands needs neither the board nor anything else and runs with just
Python + git: `init`, `project` (list/remove; `new`, `repair`, `pull`, `push`,
`sync` wire the board + roadmap), `roadmap` (show/add/move/done/adopt;
`progress` reads the board), `release`, `verify`, `incident`, `doctor`, `ask
template`, and `update`. That is the git/roadmap/registry core of the tool —
an absent board never breaks it.

---

## MCP — let an agent drive it

`flightdeck-mcp` (the console script behind `hscc project`'s MCP server)
exposes **24 tools** covering the whole surface: standup, qa, doctor, roadmap
show/progress/adopt, lint-cards, reconcile + preview, legacy-cards, list
projects, why, metrics, review, reconcile, decompose, start, ingest, release,
migrate-card, message send/dispatch, report, incident, and hygiene. Every
mutating tool shares the CLI's safety rule and defaults to `apply=False` — it
reports what *would* happen and mutates nothing until you pass `apply=True`.

Register it with your MCP client (Hermes takes the same shape under its
`mcp:` config key):

```json
"flightdeck": { "command": "flightdeck-mcp", "args": [] }
```

---

## Docs

- [`COMMANDS.md`](docs/COMMANDS.md) — every command, every flag (the full reference)
- [`CONFIGURATION.md`](docs/CONFIGURATION.md) — config + registry, field by field
- [`CONCEPTS.md`](docs/CONCEPTS.md) — the mental model (project, card, milestone)
- [`INCIDENTS.md`](docs/INCIDENTS.md) — real failures and the lessons they taught
- [`../docs/PROJECT-COMMANDS.md`](../docs/PROJECT-COMMANDS.md) — the `flightdeck X` ↔ `hscc project X` mapping + naming notes

---

## Why this exists

The board used to lie. On 2026-08-09, 28 cards showed as *blocked / awaiting
review*; **zero actually needed attention** — 14 had branches already merged
into `main`, 11 never started or were cancelled, and the 3 "real" ones were
duplicates of shipped work. An 89–100% false signal.

Root cause: no transition closes a card when its code lands. Work flowed
card → branch → *blocked-for-review* → human merges → **the card stayed
blocked forever**. Nothing reconciled the board against git.

This tool makes the signal true. It reads the board and git, and only ever
tells you a card *needs you* when its work is genuinely **unmerged**.

---

## Two design rules

1. **Read-only by default.** Every mutating command (`project
   new/remove/repair/sync/pull --apply`, `review --apply`, `hygiene --apply`,
   `roadmap add/move/done/adopt --apply`, `ingest --apply`, `decompose
   --apply`, `start --apply`, `reconcile --apply`, `release --apply`,
   `incident --apply`, `update --apply`) prints exactly what it will do and
   changes **nothing** until you pass `--apply`. The plan always comes first.
2. **Never report a state you have not verified.** Every claim traces to a
   real check — git ancestry, a live board read, a process probe. **UNKNOWN
   is a distinct result** and is shown as such, never silently rendered as
   OK. A missing field, an unrun verify, an unresolvable branch — each is
   surfaced, never papered over.

---

## Scope

This tool manages **work** across projects, using Hermes (kanban). It is a
general-purpose operator tool — **not** coupled to any particular project's
internals (no GPU/model/vLLM/managed-project logic). It reads and reconciles;
it never becomes a second source of truth. See [`docs/DESIGN.md`](docs/DESIGN.md)
for the full contract.
