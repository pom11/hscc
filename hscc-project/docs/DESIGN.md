# Flightdeck — design

**Status:** approved, implementation in progress
**Date:** 2026-08-09

## Problem

Work runs across ~7 projects, each with a git repo, a Telegram topic, and cards on
a shared Hermes kanban board. The agent fleet produces work faster than it can be
verified, and the surfaces that should tell the operator what needs attention do
not.

Measured on 2026-08-09, before any of this was built:

- **28 cards showed as blocked/awaiting review. Zero actually needed attention.**
  14 had branches already merged into `main`, 11 never started or were cancelled,
  and the 3 "real" ones were duplicates of shipped work. An 89–100% false signal.
- **Root cause:** no transition closes a card when its code lands. Work flows
  card → branch → *blocked-for-review* → human merges the branch → **the card stays
  blocked forever**. Nothing reconciles the board against git.
- **Telegram topic names are overwritten** by bot message text on active topics
  (`140` became *"Acknowledged — v1.8.1 completes…"* instead of *HSCC cluster*).
  Navigation degrades exactly where work is busiest.
- **One board for everything** — 358 cards spanning six unrelated projects.
- Three cards burned ~4 hours running with **zero commits** and no alarm.
- Four separate times, code was merged but **not live** (an orphaned daemon on
  days-old code, a CLI running from an install path rather than the repo, a proxy
  holding stale config, templates needing a payload install).

The bottleneck is not fleet throughput. It is **operator review capacity, and
trust in the signal**. Flightdeck exists to make the signal true.


## Scope

Flightdeck manages **work** across projects, using Hermes (kanban) and Telegram
(topics) as its two integrations. It is a general-purpose operator tool.

It is **not** part of, aware of, or coupled to any particular project it manages.
HSCC happens to be one registered project among several and appears below only as
a worked example — flightdeck holds no knowledge of GPU clusters, model
endpoints, serving topologies or any other project's internals.

Where a project needs something checked that only it understands, the registry
declares a **command** and flightdeck runs it. Flightdeck knows *that* a project
has a verify step or an installed-version check; it never knows *what* that means.

## Non-goals

- Not a replacement for Hermes kanban, the HSCC CLI, or the web dashboard.
  Flightdeck **reads** those systems and reconciles them; it does not reimplement
  them.
- Not a chat client. Telegram support is limited to digests and topic management.
- Not a CI system. It reports test/verification state; it does not own pipelines.

## Principles

1. **Never report a state you have not verified.** Every claim traces to a real
   check — git ancestry, a live HTTP probe, a process listing. This is the whole
   product.
2. **Silence is a bug.** A stalled card, an unmerged branch, config that drifted
   from what is running — each must surface on its own, without being asked.
3. **Merged is not live.** Anywhere the two can differ, check the running
   artifact, not the source.
4. **Read-only by default.** Commands that mutate (reconcile, topic CRUD) require
   an explicit flag and print exactly what they will do first.

## Architecture

```
flightdeck/
  core/
    registry.py     project registry: repo <-> board <-> topic
    git_state.py    branch/merge/dirty/staleness facts
    kanban.py       Hermes kanban reads + reconciliation
    probes.py       live checks (HTTP endpoints, processes, installed vs repo)
    roadmap.py      ROADMAP.md parsing
    telegram.py     topic list/create/rename/archive + digest send
  commands/
    standup.py      the daily digest
    projects.py     registry CRUD + health
    reconcile.py    close cards whose branch merged; archive dead ones
    roadmap.py      milestone view
    topics.py       Telegram topic CRUD
    hygiene.py      board decay report + fix (duplicates, triage traps, stale worktrees)
    doctor.py       self-check: is flightdeck's own view trustworthy
  cli.py            entry point
```

Each core module answers one question and is independently testable. Commands
compose them; commands contain no logic of their own beyond presentation.

## The registry

One entry per project, in `~/.flightdeck/registry.yaml`:

```yaml
projects:
  - name: hscc
    repo: ~/dev/hscc
    board: hscc              # Hermes kanban board slug
    topic: 140               # Telegram topic id
    topic_name: HSCC cluster # expected Telegram topic NAME (audit fallback: name)
    verify: "cd ~/dev/hscc && ./scripts/run_tests.sh"
    roadmap: docs/ROADMAP.md
```

`repo` is the only required field. Everything else degrades gracefully: no board
means no card data, no topic means no digest target. A project is never dropped
from output because a field is missing — it is shown with that dimension marked
unknown, because a silently omitted project is the failure this tool exists to
prevent.

## `flightdeck standup`

The daily digest. Five sections, ordered by what should interrupt the operator:

| section | rule |
|---|---|
| **NEEDS YOU** | card is review-required/blocked **and** its branch is NOT an ancestor of `main`. Shows the `VERIFY:` line. |
| **FAILING** | test command exits non-zero, or an apply/provision step reported an error |
| **STALE** | card claimed/running > threshold (default 45m) with **zero commits** on its branch |
| **RUNNING** | in flight, nothing needed |
| **DRIFT** | repo vs installed version mismatch; config value vs live endpoint mismatch; a process running code older than its source |

NEEDS YOU is deliberately the strictest: a card only qualifies if its work is
genuinely unmerged. That single rule removes the 14 phantoms.

## `flightdeck reconcile`

The integrity fix. For every card with a branch:

- branch is an ancestor of `main` → **close the card** (work landed)
- no branch and no commits, older than N days → **flag for archive**
- branch exists, unmerged, no commits → **flag as stale**

Dry-run by default; `--apply` performs the changes. Prints a diff of intended
actions first. Auto-close is the missing transition that caused the phantom
backlog, so this also runs as a pre-step inside `standup` (read-only, reporting
what reconcile would do).

## `flightdeck hygiene`

The board-decay report: three decay modes, each observed for real.

- **DUPLICATES** — near-identical card titles on one board (a crash-looping
  decomposer minted four "MCP Server Core" cards, two identical). Titles are
  normalised (fold case, strip to alphanumerics) and compared with a
  similarity ratio (default threshold 0.88, calibrated so genuine duplicates
  clear it while the "Soconn C-XX" platform family of distinct cards does
  not). Proposes archiving all but the newest of each group.
- **TRIAGE TRAP** — cards in the triage column are unrecoverable: `unblock` and
  `promote` refuse, and `specify`/`decompose` crash-loop on a NOT NULL
  `tasks.session_id` bug. Proposes archive + recreate, PRESERVING the branch
  reference. It checks `git log wt/<card>` first and reports how many commits
  the branch carries, because the work is usually already done and must not be
  thrown away.
- **STALE WORKTREES** — `.worktrees/<card>` directories whose card is closed
  AND whose branch is merged. Proposes cleanup via `git worktree remove`
  (never `rm -rf`) + safe `git branch -d`.

Read-only by default; `--apply` performs the fixes one item at a time. Each
item is idempotent, so a re-run never double-archives. Like every mutating
command, it prints what it will do and requires `--apply` before touching the
board or git.

## `flightdeck topics`

Telegram topic management, and the fix for name destruction:

- `list` — every topic with id, name, and mapped project
- `create <name>` — create a forum topic, optionally bind to a project
- `rename <id> <name>` — restore a name that was overwritten
- `bind <id> <project>` / `unbind <id>`
- `audit` — report topics whose name no longer matches their registry name (the
  overwrite detector), and topics with no project mapping

`audit` matters most: it detects the corruption automatically rather than relying
on someone noticing. The comparison target is the project's `topic_name` (the
expected topic title, e.g. "HSCC cluster"), falling back to the project `name`
when `topic_name` is unset — so a project whose human topic title differs from
its key is not a false positive.

## `flightdeck roadmap`

Reads `ROADMAP.md` from each repo — a plain versioned file, no new datastore:

```markdown
## Now
- [ ] Anulare tranzactie — direct ILE insert
## Next
- [ ] Stripe subscription lifecycle
## Later
- [ ] Multi-tenant client portal
```

Shows current milestone per project plus open card counts. A missing ROADMAP.md is
reported as "no roadmap", never silently skipped.

## `flightdeck doctor`

Self-check: can this tool be trusted right now? Verifies each registry repo exists
and is a git repo, each board exists, each topic id resolves, the Telegram
transport works, and reports any project whose data could not be read. If
flightdeck cannot see something, it says so loudly rather than reporting a clean
board.

## Testing

- Every core module unit-tested against fixtures; no test performs network or git
  I/O against real systems — all external calls injectable, following the
  `_http_get=None` pattern.
- **Test timings are part of the contract.** A suite that reaches the network is
  broken even when green: it burns time in CI and goes flaky off-network. Target
  full suite under 5s; any single test over 1s is a defect.
- Golden-file tests for digest rendering so output changes are deliberate.
- The reconciler must be tested against the real failure that motivated it: a
  card whose branch is merged must close; a card whose branch is unmerged must
  not.

---

## Addendum — project lifecycle and messaging

Flightdeck is the control plane for the whole workflow, not just a reporter.

### `flightdeck project new <name>`

One command creates a fully wired project. Every step is optional via flags and
each is idempotent — re-running repairs a partially-created project rather than
duplicating it:

1. git repo (`--repo PATH`, `--github [--private]`) — init, first commit, optional
   GitHub remote
2. Telegram forum topic named for the project, in the HSCC group
3. kanban board (slug = project name)
4. `ROADMAP.md` seeded with Now/Next/Later
5. registry entry binding repo ↔ board ↔ topic

`--dry-run` prints the plan. Partial failure never leaves a half-registered
project: what succeeded is recorded, what failed is reported with the exact
command to retry.

### Messaging

- `flightdeck send <project> "msg"` — post to that project's topic
- `flightdeck read <project> [-n N]` — recent messages
- `flightdeck dispatch <project> "task"` — create a kanban card on the project's
  board **and** announce it in the topic, so chat and board never diverge
- `flightdeck broadcast "msg" [--to a,b]` — one message to several topics

Messaging resolves the topic through the registry, so an operator never handles
raw topic ids.

### Design constraint

Every mutating command (`project new`, `send`, `dispatch`, `broadcast`, topic
create/rename) prints what it will do and requires `--apply`, except `send`/`read`
which are the interactive path and act immediately. The Telegram session is
single-writer: "database is locked" must surface as a clear message with a retry
hint, never a traceback.

### `flightdeck project sync`

The counterpart to `project new`: adopt what already exists rather than hand-writing
the registry. Discovers from three independent sources and correlates them:

- **repos** — git repositories under the configured roots (default `~/dev`)
- **topics** — Telegram forum topics in the HSCC group
- **boards** — Hermes kanban board slugs

Correlation is by normalised name (case, separators, and common prefixes folded),
plus any binding already in the registry, which always wins over a guess.

Output is a proposal, never a silent write:

```
MATCHED (5)      hscc          repo ✓  topic 140 ✓  board hscc ✓
PARTIAL (2)      sphoin        repo ✓  topic 2 ✓    board — (none)
                 soconn        repo ✓  topic 7074 ✓ board — (none)
ORPHAN REPOS     hermes-prfix, sparkrun-03x-audit_safetodelete
ORPHAN TOPICS    6369 "EFS Driver"  (no repo, no board)
ORPHAN BOARDS    ecofire       (4 cards, no registry entry)
AMBIGUOUS (1)    ecofire       repo ~/dev/ecofire OR ~/dev/EcoFire_customizations_bc
```

Rules:

- `--apply` writes only the **unambiguous** matches. Anything ambiguous is listed
  with the exact command to resolve it and is never auto-bound — a wrong binding
  sends work to the wrong topic, which is worse than no binding.
- Idempotent: re-running after `--apply` reports MATCHED and proposes nothing.
- Registry entries always win. Sync never overwrites an existing binding; it
  reports the conflict.
- Orphans on **all three sides** are reported. A repo with no topic, a topic with
  no repo, and a board with no project are each a real gap the operator should
  see — and orphan topics are how the group accumulates dead threads.
