![FlightDeck — Project Control. Manage projects in Hermes.](docs/assets/banner.png)

# Flightdeck

Flightdeck is a terminal tool that answers the one question you have every
morning when a fleet of AI agents works across many projects: **what actually
needs me right now?** Instead of opening every project's kanban board, git
log, and chat to figure out what's stuck, blocking, finished, or drifting, you
run one command and it tells you — in plain language, ordered by how much it
should interrupt you.

If you coordinate AI agents across a handful of repos (and a Telegram thread
per project), Flightdeck is the single pane of glass you've been missing. It's
MIT licensed, needs only Python 3.10+ and git, runs entirely on your own
machine against local state, and is **read-only until you explicitly pass
`--apply`** — so you can explore it without any risk.

---

## If you get stuck

- **Common failures first:** a missing dependency is never silently ignored —
  `flightdeck doctor` checks your environment and tells you exactly what's
  off. If a board or Telegram daemon is unreachable, commands say so loudly
  instead of crashing or pretending.
- **Full reference:** every command and flag lives in [`docs/COMMANDS.md`](docs/COMMANDS.md).
- **The mental model** (what a "card", "board", "project", "milestone" means)
  is spelled out in [`docs/CONCEPTS.md`](docs/CONCEPTS.md).
- **Real failures and the lessons they taught:** [`docs/INCIDENTS.md`](docs/INCIDENTS.md).
- **Config and your project registry,** field by field: [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md).

---

## Quick start

Install from git (this is the only supported way — there is no PyPI package),
bootstrap, adopt your existing projects, and read your first digest. Each
command is followed by *why you'd run it*:

```sh
# Install the tool. There's no PyPI package; these two lines get it on PATH.
pip install git+https://github.com/pom11/flightdeck

# Create ~/.flightdeck (config + registry + templates) and check your
# environment. Dry-run by default — nothing is written until you add --apply.
flightdeck init

# Find your existing repos, Telegram topics, and boards and wire them into
# the registry, so Flightdeck knows what projects you have.
flightdeck project sync --apply

# Your daily digest: what's happening across every project and what needs you.
flightdeck standup
```

The first `init` is a dry run by default — it shows you what it *would* create
and checks the environment before writing anything. (This is true of every
mutating command in Flightdeck: the plan always comes first, and nothing
changes until you pass `--apply`.)

```
$ flightdeck init
flightdeck home: ~/.flightdeck (dry run — use --apply to write)
Would create:
  would create ~/.flightdeck/config.yaml
  would create ~/.flightdeck/registry.yaml
  would create ~/.flightdeck/templates/

Environment check:
  python           [ok] Python 3.13.7 (/usr/local/bin/python3)
  mcp-sdk          [ok] mcp SDK present (v?): MCPServer (2.0 layout)
  git              [ok] git on PATH: /usr/bin/git
  hermes-kanban    [ok] Hermes kanban DB reachable and readable at ~/.hermes/kanban.db
  telegram-daemon  [ok] Telegram MCP daemon answered (7 tools)

Next steps:
  1. Set `telegram.group_id` in ~/.flightdeck/config.yaml ...
  2. Run `flightdeck project sync --apply` to adopt your existing repos
     into the registry.
  3. Register flightdeck's MCP server with your MCP client so an agent can
     drive flightdeck:

    "flightdeck": { "command": "flightdeck-mcp", "args": [] }
```

`init` creates `~/.flightdeck`, seeds `config.yaml` and `registry.yaml` from
the shipped examples (never overwriting anything that already exists), copies
the prompt templates, and prints your exact next steps.

`project sync` then scans what you already have — git repos under `~/dev`,
Telegram topics, and board slugs on the kanban — and reports every match,
conflict, and orphan before wiring anything into the registry. It never writes
without `--apply` and never overwrites an existing binding. A real run
(trimmed, paths shortened to `~`):

```
$ flightdeck project sync
MATCHED (N)
  flightdeck   repo ✓   topic 8903  board flightdeck
  ...

CONFLICTS (N)
  board 'ecofire' matches '~/dev/EcoFire_customizations_bc' which is already
    bound to board 'ecofire-bc'
  ...
  (existing bindings are never overwritten by sync)

ORPHAN REPOS (N)  ...  ORPHAN TOPICS (N)  ...  ORPHAN BOARDS (N) ...

--apply writes only the unambiguous matches above.
```

Pass `--apply` to write only the unambiguous matches into the registry.

Then your first digest:

```
$ flightdeck standup
NEEDS YOU (0)
  none

FAILING (0)
  none

STALE (0)
  none

RUNNING (20)
  [default] RM3 flightdeck: friendly README + correct install (no PyPI, git only) (branch wt/t_49224472)
  [default] CI2 flightdeck: tests must be hermetic — suite passes locally, fails on every CI runner (branch wt/t_84d728fa)
  ...

DRIFT (8)
  [flightdeck] flightdeck — OK (installed 0.2.1)
  ...

read 8 projects | 10 boards | 20 cards | 20 attributed | 0 unreadable
in flight: 10 cards across 10 boards (cap 3/board)
```

That's the whole tool in under a minute. Read on for what a normal day looks
like, or jump to [`docs/COMMANDS.md`](docs/COMMANDS.md) for the full reference.

---

## A day in the life

Here's what someone might actually do with Flightdeck once it's set up. You
mostly live in two commands — `standup` (what needs me?) and `review`
(does it merge?).

**1. Morning: `flightdeck standup`**

You start your day the same way every day:

```
$ flightdeck standup
NEEDS YOU (2)
  [hscc] [t_3f2a1b] fix login redirect — blocked, needs a decision
  [flightdeck] [t_755d41a3] README is hard to follow — awaiting review

RUNNING (14)
  [ecofire-app] [t_8c1d2e] migrate auth to v2 — on track
  ...

DRIFT (3)
```

Two cards genuinely need you. One is a question only you can answer, the
other finished and is waiting for you to merge it.

**2. See what's actually blocking: `flightdeck why <card>`**

Don't guess what "blocked" means. Ask Flightdeck to trace the card's full
story across the kanban board and git:

```
$ flightdeck why t_3f2a1b
t_3f2a1b  fix login redirect
  status: blocked
  last event: await_review (2h ago) — agent asked a question
  branch: wt/t_3f2a1b — 3 commits, HEAD not on main
  question: "Should the redirect preserve the ?next= param? Product hasn't decided."
```

Now you know exactly what to answer.

**3. Merge the finished one: `flightdeck review <card>`**

The other card is done and waiting. `review` reads the branch, checks it
merges cleanly, and gives you a verdict — then, with `--apply`, merges it
into `main` and closes the card in one action:

```
$ flightdeck review t_755d41a3
t_755d41a3  README is hard to follow
  branch wt/t_755d41a3: 4 commits, merges cleanly into main
  files changed: README.md
  verdict: READY — merge & close

$ flightdeck review t_755d41a3 --apply
merged wt/t_755d41a3 into main; card closed.
```

Or, to see everything the fleet is waiting on you to review at once:

```
$ flightdeck review --queue
review queue: no cards awaiting review.
```

**4. Answer the block, then let it continue**

Back in Telegram (or wherever the agent asked), you answer the question.
The agent picks the card back up, and tomorrow morning `standup` reflects it.

**That's the loop.** Look at the digest → drill into what needs you → merge
what's ready / answer what's blocked → repeat. Three commands cover the
questions that used to cost a morning:

| Command | What it answers |
|---------|-----------------|
| `flightdeck standup` | What is happening across every project, and what needs me? |
| `flightdeck qa` | What do I actually have to click or run by hand? |
| `flightdeck review <card>` | Is this ready to merge, and does it merge cleanly? |

`qa` and `standup` can also watch live (`--watch`), and `qa --notify` pings
your Telegram the moment something enters the manual-testing queue.

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
- **An MCP server** — an agent can drive all of flightdeck through the
  Model Context Protocol (`flightdeck-mcp`).

---

## The work loop

From roadmap to shipped work — each step is a separate command because each
is a genuine handoff, and every one ships its own `--apply` (dry-run by
default):

```sh
flightdeck ingest <project>            # draft ROADMAP.draft.md from existing context
flightdeck roadmap adopt <project>     # promote the reviewed draft into ROADMAP.md
flightdeck decompose <project> --milestone <id>   # break a milestone into atomic cards
flightdeck start <project> --milestone <id>       # release the cards to the fleet
flightdeck qa [--watch] [--notify]     # what you actually have to test by hand
flightdeck review <card>               # review, merge, close
flightdeck report <project>            # post a learnable summary; Hermes learns
```

`report` is the step that lets the fleet itself learn — the orchestrator
can't see board execution, so `report` tells it what shipped.

---

## Requirements

- **Python 3.10+** and **git**.

Both optional integrations, with the exact cost of missing them stated
plainly (a missing dependency is always surfaced, never silently ignored):

- **Hermes kanban DB** — the board-facing commands (`standup`, `qa`,
  `review`, `metrics`, `why`, `reconcile`, `hygiene`, `lint-cards`, `start`,
  `decompose`, `report`, and `roadmap progress`) read and reconcile cards.
  Without a board, those report loudly that the board is unreachable rather
  than crash — and `standup` still produces its non-board sections.
- **Telegram MCP daemon** — a separate single-writer process owning the
  Telethon session (see `docs/TELEGRAM.md`). Without it, only the
  Telegram-facing commands (`topics`, `message`, `ask`, `ingest`, `report`,
  `sync`, `decompose`) are unavailable; everything else runs normally.
  `doctor` reports an unreachable daemon as an UNVERIFIED dimension rather
  than crashing.

**Eight commands need neither** the Hermes board nor Telegram and run with
just Python + git: `init`, `project` (list/remove; `new`, `repair`, and
`sync` wire the board + topic), `roadmap` (show/add/move/done/adopt;
`progress` reads the board), `release`, `verify`, `incident`, `doctor`, and
`ask template`. That is the git/roadmap/registry core of the tool — an absent
board or daemon never breaks it.

---

## MCP — let an agent drive it

`flightdeck-mcp` exposes **15 tools** (standup, qa, doctor, roadmap
progress/show, lint-cards, reconcile preview, list projects, review,
reconcile, decompose, start, ingest, roadmap adopt, message send). Every
mutating tool shares the CLI's safety rule and defaults to `apply=False` — it
reports what *would* happen and mutates nothing until you pass `apply=True`.
The one exception is `message_send`, which posts immediately by design,
exactly like the CLI.

Register it with your MCP client:

```json
"flightdeck": { "command": "flightdeck-mcp", "args": [] }
```

(Hermes takes the same shape under its `mcp:` config key.)

---

## Docs

- [`COMMANDS.md`](docs/COMMANDS.md) — every command, every flag (the full reference)
- [`CONFIGURATION.md`](docs/CONFIGURATION.md) — config + registry, field by field
- [`CONCEPTS.md`](docs/CONCEPTS.md) — the mental model (project, card, milestone)
- [`TELEGRAM.md`](docs/TELEGRAM.md) — the Telegram MCP daemon contract
- [`INCIDENTS.md`](docs/INCIDENTS.md) — real failures and the lessons they taught

---

## Why this exists

The board used to lie. On 2026-08-09, 28 cards showed as *blocked / awaiting
review*; **zero actually needed attention** — 14 had branches already merged
into `main`, 11 never started or were cancelled, and the 3 "real" ones were
duplicates of shipped work. An 89–100% false signal.

Root cause: no transition closes a card when its code lands. Work flowed
card → branch → *blocked-for-review* → human merges → **the card stayed
blocked forever**. Nothing reconciled the board against git.

Flightdeck makes the signal true. It reads the board and git, and only ever
tells you a card *needs you* when its work is genuinely **unmerged**.

---

## Two design rules

1. **Read-only by default.** Every mutating command (`project new/remove/
   repair/sync --apply`, `review --apply`, `hygiene --apply`, `roadmap
   add/move/done/adopt --apply`, `ingest --apply`, `decompose --apply`,
   `start --apply`, `reconcile --apply`, topic create/rename/bind) prints
   exactly what it will do and changes **nothing** until you pass `--apply`.
   The plan always comes first.
2. **Never report a state you have not verified.** Every claim traces to a
   real check — git ancestry, a live board read, a process probe. **UNKNOWN
   is a distinct result** and is shown as such, never silently rendered as
   OK. A missing field, an unrun verify, an unresolvable branch — each is
   surfaced, never papered over.

---

## Scope

Flightdeck manages **work** across projects, using Hermes (kanban) and
Telegram (topics). It is a general-purpose operator tool — **not** coupled to
any particular project's internals (no GPU/model/vLLM/managed-project logic).
It reads and reconciles; it never becomes a second source of truth. See
`docs/DESIGN.md` for the full contract.
