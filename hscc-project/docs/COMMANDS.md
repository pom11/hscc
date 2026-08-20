# flightdeck COMMANDS.md — Command Reference

This is the operator's reference for every command `flightdeck` ships. It
documents what the code *actually* does. Every entry below was derived by
running `python -m flightdeck.cli <command> --help` against v0.2.1 and,
where behaviour was still ambiguous, reading the command module in
`flightdeck/commands/`.

**The definitive list is `flightdeck --help`.** There are exactly 26
commands. If a command you expected is missing here, or one here is missing
from `--help`, that is a bug in this document — file it.

> **Note on invocation in this doc.** The installed `flightdeck` console
> script and `python -m flightdeck.cli` are the same entry point. Examples
> below use `flightdeck`, which is the normal way to call it.

## Two design rules that apply everywhere

1. **Read-only by default.** Commands that change state (write to the
   registry, move cards, send messages, cut releases) require an explicit
   `--apply` flag. Without `--apply` they run in a verify/dry-run mode and
   change nothing. A bare invocation never writes state.
2. **Never report a state that was not verified.** A command that reports
   "done", "verified", or "all good" only does so after it has actually
   checked the underlying system. Anything that could not be verified is
   reported as "unknown"/"unverified", never spoken as fact.
3. **Auto-detect the project from your cwd.** Commands that scope to a
   project (`qa`, `metrics`, `verify`, `report`, `review`, `message
   send/read/dispatch`) default to "the project whose repo contains your
   current directory" when you omit the `project` argument. An explicit
   `project` always wins over detection, never the reverse, and when detection
   kicks in it prints a visible `using project '<x>' (detected from cwd)` note
   — it is never a silent, confusing default. From outside any registered repo
   (e.g. `~`), commands fall through to their exact pre-existing no-project
   behavior, so this is purely additive.

Commands are grouped below the way an operator thinks about a day: daily
driver, work loop, setup, and integrity.

---

## Daily driver

### `flightdeck standup`
**Purpose.** The daily digest: what's in flight, blocked, and new — one-shot
or a live redraw.

**Usage:**
```
flightdeck standup [--watch] [--interval N] [--max-fleet N]
```

| Flag | Effect |
|------|--------|
| `--watch` | Redraw the digest every `--interval` seconds until interrupted. |
| `--interval N` | Seconds between `--watch` frames (default: 30). |
| `--max-fleet N` | Fleet-wide warning ceiling for in-flight cards across ALL boards (default: 3); the digest warns when the total exceeds it. |

**Mutates?** No. Standup gathers and prints state; it never changes a card,
a report, or the registry.

**Coverage footer.** Below the sections, standup always prints what it actually
read (`read N projects | M boards | K cards | ...`) plus two trust signals:

- **Unread (orphan/legacy) board line** — `+ N unread board(s) (…) holding
  M card(s) — run legacy-cards` appears only when a board outside the registry
  mapping holds cards standup does NOT otherwise surface (cards whose
  workspace resolves to no registered project). It reuses `legacy-cards`' own
  board-attribution rule, so the two commands always agree. The shared
  `default` board's cards — attributed by workspace — are surfaced normally and
  never trigger this. When nothing is hidden, no line is printed.
- **Version-drift notice** — when flightdeck's own installed version is behind
  its git remote's newest tag, one line is printed:
  `flightdeck update available: installed 0.6.0, remote 0.7.0 (run flightdeck
  update)`. This is a pure notice — never auto-applies anything. The remote
  check (`git ls-remote`) is **rate-limited to once per day**, cached in
  `~/.flightdeck/update-check.yaml` (the same convention as `qa-notified.yaml`,
  honouring `HERMES_HOME`), so a long-running `--watch` session re-fetches it at
  most once a day, not on every redraw.
- **Cross-project dependents** — when any registered project is depended on by
  other registered projects (they list it in their own `depends_on`), one line
  per depended-on project names them, e.g.
  `ecofire-bc: 2 dependent project(s): ecofire-app, efsdriver — consider
  verifying they still work`. It is a nudge that cross-repo risk exists, never
  a blocker, and it is silent when no project has dependents (the common,
  self-contained case). The `depends_on` field itself is documented in
  CONFIGURATION.md.

**Examples:**
```
flightdeck standup
flightdeck standup --watch --interval 60
flightdeck standup --max-fleet 5
```

**Freshness watermark.** Every standup output ends with a `data as of <time>`
line in the coverage footer, e.g.:

```
data as of 2026-08-17 22:20 — board: default (32m)
```

It dates the **oldest critical input** behind the digest. For each board that
contributed cards, its freshness is the newest state-change it recorded (the
newest event or card-update timestamp); the digest's watermark is the oldest
of those, so a single stale board can never hide behind fresher ones. A board
that contributed **no card** (an empty/quiet board) is excluded entirely —
quietness is not staleness. When nothing is dateable (no contributing board
with a readable timestamp) it prints `data as of <unknown>` rather than
inventing a date. The line is a *factual date*, never a staleness verdict: the
loud stale alert stays reserved for read failures, which standup already
surfaces as `UNREADABLE`. In `--json` mode the same values appear on
`coverage.watermark` (epoch timestamp) and `coverage.watermark_boards`.

---

### `flightdeck monitor`
**Purpose.** A live cluster-activity view the operator keeps running in a spare
terminal to watch what the fleet is doing right now, across every registered
board at once. Each refresh redraws the screen and lists every RUNNING/CLAIMED
card, grouped by board/project, with id, title, status, assignee, and elapsed
time since the card was claimed. When nothing is active anywhere, it prints a
single "cluster idle" line instead of an empty screen. Read-only — it never
writes to a board.

**Usage:**
```
flightdeck monitor [--time N]
```

| Flag | Effect |
|------|--------|
| `--time N` | Seconds between refreshes (default: 5). |

**Mutates?** No. Monitor only reads boards and prints; it never changes a card,
a report, or the registry. Any one board that fails to read (locked/corrupt)
is shown as `(unreadable: <board>)` while the rest of the fleet keeps
displaying.

**Examples:**
```
flightdeck monitor
flightdeck monitor --time 10
```

---

### `flightdeck daemon`
**Purpose.** A persistent background process that **watches, logs, and
optionally notifies** — the domain's monitoring daemon. It keeps `standup`'s
signal (fleet health, board freshness, orphaned boards, version drift) current
without you having to run a command by hand.

**Hard scope: read-only.** The daemon may **never** merge a branch, apply a
template, run `--apply` on anything, update flightdeck's own installed code,
close/archive a kanban card, or otherwise mutate ANY project's state. It reads
and reports only; if something needs your attention it logs it and optionally
notifies — you decide. This deliberately mirrors HSCC's `escalate` pattern
(detect + report, human decides), NOT its apply-side commands.

**Usage:**
```
flightdeck daemon <start|stop|status|check|watch|log|notify|install|uninstall>
```

| Subcommand | Purpose | Mutates? |
|------------|---------|----------|
| `start` | Fork the daemon into the background; it begins checking every stream on its schedule. | No |
| `stop` | Signal the running daemon to shut down gracefully (SIGTERM, SIGKILL fallback). | No |
| `status` | Show whether the daemon is running and the last result of every check stream. | No |
| `check [stream]` | Run one check cycle **now** (one stream, or all when omitted) — without needing the daemon running. Results are persisted so `status` reflects them. | No |
| `watch [stream] [--interval N]` | Tail the persisted stream states in real time, printing a line when one changes. | No |
| `log [--lines N]` | Show the daemon's plain-text log file. | No |
| `notify [MESSAGE]` | Send a macOS notification (the daemon's optional notify path). | No |
| `install` | Install the launchd auto-start service (requires `--apply`). | **Yes**, gated |
| `uninstall` | Remove the launchd auto-start service. | Yes |

**Check streams** (each scheduled independently):

| Stream | Interval | What it checks |
|--------|----------|----------------|
| `fleet` | 60s | In-flight card counts across **all** boards vs the `--max-fleet` ceiling and the config `kanban.max_in_progress` cap. |
| `freshness` | 300s | Last successful board read per board; a board not read within `--threshold` (default 3600s) is flagged stale. |
| `orphans` | 300s | Legacy/unregistered boards holding cards (reuses `legacy-cards` attribution, so the two never disagree). |
| `version` | 3600s | flightdeck's own installed-vs-remote version drift, rate-limited to once per day via `~/.flightdeck/update-check.yaml`. |

**Persistence.** State lives under `~/.flightdeck/daemon/` (PID file, plain-text
log, and one `<stream>.json` per check stream). The staleness accumulator and
the rate-limited version cache survive daemon restarts.

**Mutates?** The daemon never writes to any board, project, or repo — only to
its own `~/.flightdeck/daemon/` log/state. `install --apply` is the sole
exception (it writes a launchd plist + runs `launchctl`), and it is gated so it
never happens as a side effect of anything else.

**Examples:**
```
flightdeck daemon start
flightdeck daemon status
flightdeck daemon check            # run every stream once now
flightdeck daemon check fleet      # just the fleet stream
flightdeck daemon watch            # tail all streams in real time
flightdeck daemon log              # show the log
flightdeck daemon stop
flightdeck daemon install          # dry-run: see what it would do
flightdeck daemon install --apply  # only with your explicit go-ahead
```

---

### `flightdeck qa`
**Purpose.** The manual-testing queue: what you actually have to test by
hand. Two kinds of items appear:

- **Pre-merge queue** — cards still genuinely awaiting review (review-required
  AND their branch is not an ancestor of `main`).
- **`NEEDS MANUAL VERIFICATION`** — post-merge items a human must physically
  check (printer byte output on real hardware, real production data in an
  external system), which would otherwise vanish once the card is merged and
  archived. Added with `qa add`, tracked in `~/.flightdeck/manual-qa.yaml`.

**Usage:**
```
flightdeck qa [--watch] [--interval N] [--notify] [project]
flightdeck qa add <project> "description" [--card CARD_ID]
flightdeck qa done <id>
```

| Flag | Effect |
|------|--------|
| `--watch` | Redraw the queue every `--interval` seconds until interrupted. |
| `--interval N` | Seconds between `--watch` frames (default: 30). |
| `--notify` | When a card enters the needs-QA queue, post one message to its project's Telegram topic (once per card, on the transition). |
| `project` | Restrict BOTH the queue and the manual section to one named project. When omitted, the project is **auto-detected from the cwd**: if your working directory sits inside a registered project's repo, that project is used and a `using project '<x>' (detected from cwd)` note is printed; from outside any registered repo it falls back to *all projects* (today's default). |

**`qa add`** — append a post-merge item a human must verify by hand. Validates
`<project>` against the registry (refuses an unknown project cleanly). Prints
the new entry's id (`mqa-<8 hex>`) on success. `--card CARD_ID` optionally
traces it back to the archived kanban card that produced it. No `--apply` gate:
this is additive and low-risk, matching `message send`.

**`qa done`** — mark a manual-QA entry checked (records `checked_at`). Unknown
and already-checked ids are refused cleanly. Checked entries are kept in the
store for history but drop out of the default view (and of `--json`'s
`manual_qa` list).

**Mutates?** `qa` itself never moves a card; `--notify` posts a Telegram
message when a card freshly enters the needs-QA queue and records the
notification in `~/.flightdeck/qa-notified.yaml`. `qa add` appends to
`~/.flightdeck/manual-qa.yaml`; `qa done` edits it. Checked entries are never
deleted.

**Examples:**
```
flightdeck qa
flightdeck qa --watch
flightdeck qa --notify ops
flightdeck qa add ops "verify Business Central figures against the live DB" --card t_abc
flightdeck qa done mqa-1a2b3c4d
```

---

### `flightdeck review`
**Purpose.** Review a card, list the awaiting-review queue, or gate a verify
run.

**Usage:**
```
flightdeck review [--queue] [--apply] [--base BASE] [--baseline PATH] [CARD_ID_OR_PROJECT]
```

| Flag | Effect |
|------|--------|
| `--queue` | List everything genuinely awaiting review, newest first (read-only). |
| `--apply` | Merge the branch into main AND close the card — one action. |
| `--base BASE` | Base branch the card merges into (default: main). |
| `--baseline PATH` | Test-baseline file (default `~/.flightdeck/test-baseline.yaml`). |
| `CARD_ID_OR_PROJECT` | A kanban card id (e.g. `t_abc123`) to review, or a project to verify+gate. |

**No positional?** When `CARD_ID_OR_PROJECT` is omitted (and no `--queue`),
the project is **auto-detected from the cwd**: if your working directory sits
inside a registered project's repo, that project's verify+gate runs and a
`using project '<x>' (detected from cwd)` note is printed; from outside any
registered repo it falls through to the "nothing to do" message. An explicit
`--queue` or `CARD_ID_OR_PROJECT` always wins over detection.

**Mutates?** Yes, when `--apply` is given: it merges the branch and closes
the card. Without it, review is read-only. (Contrast this with the design
rule — `review` is the one command whose apply action is merge+close.) With
a project argument it gates a verify run rather than reviewing a card.

**Examples:**
```
flightdeck review --queue
flightdeck review t_abc123 --apply
flightdeck review --base develop t_abc123
```

**Freshness watermark.** `review --queue` ends with the same `data as of
<time>` line as standup, dating the oldest contributing board. Each row already
shows its own age (how long the card has awaited review); the board-level
watermark adds the *floor* — if the board data itself is old, the queue says
so. It is a factual date, not a staleness verdict.

**Cross-project dependents.** When reviewing a card for a project that OTHER
registered projects declare in their own `depends_on`, review prints a
`dependents: N dependent project(s): a, b — consider verifying they still
work` line under the card header (also surfaced under `dependents` in `--json`
output). It is advisory only — it never gates a merge — and it is absent when
the project has no dependents, so the common case stays quiet.

---

### `flightdeck ask`
**Purpose.** Render a prompt template filled with project context and send
it; or manage the template store (`ask template list|show|edit`).

**Usage:**
```
flightdeck ask [--set KEY=VALUE] [--dry-run] [--editor EDITOR] [ARG ...]
```

`ARG` is `<project> <template>` to render+send, or `template list|show
<name>|edit <name>` to manage the store. `ask` has no separate subparser —
the first `ARG` word routes between the render+send form and the template
store form.

| Flag | Effect |
|------|--------|
| `--set KEY=VALUE` | Override/prefill a template slot (repeatable; render form only). |
| `--dry-run` | Print the rendered text and send NOTHING (render form only). |
| `--editor EDITOR` | Editor to use (default `$EDITOR` or `vi`; `template edit` only). |

**Mutates?** The render+send form sends a message (a "write" to Telegram,
gated only by `--dry-run`, which prints and sends nothing). Managing the
template store edits files under `~/.flightdeck/templates/`. There is no
`--apply` on `ask`; use `--dry-run` to preview without sending.

**Examples:**
```
flightdeck ask ops standup --dry-run          # preview, send nothing
flightdeck ask ops standup --set today=monday
flightdeck ask template list
flightdeck ask template edit standup
```

---

## Work loop

### `flightdeck roadmap`
**Purpose.** Show a roadmap, or add/move/done/adopt items in a project's
ROADMAP.md.

**Usage:**
```
flightdeck roadmap ROADMAP_CMD ...
```

**Subcommands:**

| Subcommand | Purpose | Positionals |
|------------|---------|-------------|
| `show` | Show one project's roadmap (or all). | `[project]` |
| `progress` | Per-milestone card progress. | `[project]` |
| `add` | Add an item to Now/Next/Later. | `project item` |
| `move` | Promote/demote an item between sections. | `project item` (needs `--to`) |
| `done` | Tick an item's checkbox. | `project item` |
| `adopt` | Promote a reviewed `docs/ROADMAP.draft.md` into `ROADMAP.md`. | `project` |

**Flags (per-subcommand):**

| Flag | Applies to | Effect |
|------|-----------|--------|
| `--apply` | add / move / done / adopt | Perform the change; mutating subcommands are dry-run by default. |
| `--section {now,next,later}` | add | Section to add to (default: now). |
| `--to {now,next,later}` | move | Section to move the item to (required). |

**Mutates?** `add`, `move`, `done`, and `adopt` write to the project's
`ROADMAP.md` and require `--apply`. `show` and `progress` are read-only.
`adopt --apply` backs up the existing roadmap and removes the draft.

**Examples:**
```
flightdeck roadmap show ops
flightdeck roadmap add ops "Q4 kanban migration" --section next --apply
flightdeck roadmap move ops "kanban migration" --to later --apply
flightdeck roadmap done ops "kanban migration" --apply
flightdeck roadmap adopt ops --apply
flightdeck roadmap progress
```

---

### `flightdeck decompose`
**Purpose.** Ask the cluster to break a goal into atomic cards, gated on
card quality.

**Usage:**
```
flightdeck decompose [--milestone MILESTONE] [--apply] [--timeout SECONDS] project [goal]
```

| Flag | Effect |
|------|--------|
| `--milestone MILESTONE` | Milestone id in the project's ROADMAP.md whose items become the goal (mutually exclusive with a free-text goal). |
| `--apply` | Create the ACCEPTED cards (dry-run by default; nothing is created without this). |
| `--timeout SECONDS` | How many seconds to wait for the orchestrator's JSON proposal before giving up (default: 300). |
| `project` | Project name in the registry. |
| `goal` | The roadmap item / goal to decompose (omit when `--milestone` is used). |

**Mutates?** Yes, when `--apply` is given — it creates the cards on the
project's board. Requires `--apply`; without it the proposal is printed but
nothing is created.

**Examples:**
```
flightdeck decompose ops "ship the API v2"
flightdeck decompose ops --milestone m3 --apply
```

---

### `flightdeck start`
**Purpose.** Release a milestone's cards to the fleet, concurrency-aware.

**Usage:**
```
flightdeck start --milestone MILESTONE [--max-concurrent N] [--apply] project
```

| Flag | Effect |
|------|--------|
| `--milestone MILESTONE` | Milestone id whose cards to release (matches a `MILESTONE: <id>` body line). Required. |
| `--max-concurrent N` | Additional cap on concurrent cards for this run (default: 3); effective ceiling = min(fleet `max_in_progress`, N). |
| `--apply` | Release the planned cards (dry-run by default; nothing is released without this). |
| `project` | Project name in the registry. |

**Mutates?** Yes, when `--apply` is given — it transitions the milestone's
cards out of the block/ready queue into work. Requires `--apply`; without
it only the plan is printed.

**Example:**
```
flightdeck start --milestone m3 --max-concurrent 2 --apply ops
```

---

### `flightdeck ingest`
**Purpose.** Draft a project's ROADMAP.md from existing context (skills,
repo, topic).

**Usage:**
```
flightdeck ingest [--limit LIMIT] [--timeout SECONDS] [--apply] [--ask-inline] project
```

| Flag | Effect |
|------|--------|
| `--limit LIMIT` | Number of Telegram topic messages to gather (default: 200). |
| `--timeout SECONDS` | How many seconds to wait for the orchestrator's reply before giving up and writing nothing (default: 300). |
| `--apply` | Write the drafted roadmap (dry-run by default; an existing ROADMAP.md is never overwritten). |
| `--ask-inline` | Synthesise synchronously by asking the orchestrator in chat (the default dispatches a kanban card instead; use this for small projects that fit the old behaviour). |
| `project` | Project name in the registry. |

**Mutates?** Yes, when `--apply` is given — it writes the drafted
ROADMAP.md into the project's repo. Requires `--apply`; without it nothing
is written. It also overwrites `~/.flightdeck/ingest-context-<project>.md`
on each run as scratch, but never overwrites an existing ROADMAP.md.

**Examples:**
```
flightdeck ingest ops
flightdeck ingest ops --apply
flightdeck ingest ops --ask-inline --apply
```

---

### `flightdeck report`
**Purpose.** Report board activity over a window and (optionally) post the
summary to a project's Telegram topic.

**Usage:**
```
flightdeck report [--since DURATION] [--all] [--backfill DURATION] [--apply] [project]
```

| Flag | Effect |
|------|--------|
| `--since DURATION` | Window to report over, e.g. `24h`/`90m`/`7d`/`2h30m` (default: last reported timestamp, else 24h). |
| `--all` | Report every registered project in one pass, skipping those with nothing to report. |
| `--backfill DURATION` | One-shot catch-up over a deliberately long window (e.g. `72h`) for every project, summarised harder so it fits the message cap. |
| `--apply` | Post the summary to the project's topic (dry-run by default). |
| `project` | Project name in the registry (omit with `--all`/`--backfill`). When omitted (no `--all`/`--backfill`), it is **auto-detected from the cwd**: if your working directory sits inside a registered project's repo, that project is used and a `using project '<x>' (detected from cwd)` note is printed; from outside any registered repo it falls through to the existing error. |

**Mutates?** Yes, when `--apply` is given — it posts the summary to
Telegram and records the last-reported timestamp in
`~/.flightdeck/report-state.yaml` (which becomes the default `--since`).
Without `--apply` it is a read-only dry-run that posts nothing.

**Examples:**
```
flightdeck report ops
flightdeck report ops --since 7d --apply
flightdeck report --all --apply
flightdeck report --backfill 72h --apply
```

---

### `flightdeck metrics`
**Purpose.** Print operational metrics for the whole fleet or one project.

**Usage:**
```
flightdeck metrics [--since DURATION] [PROJECT]
```

| Flag | Effect |
|------|--------|
| `--since DURATION` | Window, e.g. `24h` / `7d` / `2h30m` (default: 24h). |
| `PROJECT` | Registry project to scope to. When omitted, it is **auto-detected from the cwd**: if your working directory sits inside a registered project's repo, that project is used and a `using project '<x>' (detected from cwd)` note is printed; from outside any registered repo it falls back to *whole fleet* (today's default). |

**Mutates?** No — a read-only report.

**Examples:**
```
flightdeck metrics
flightdeck metrics --since 7d ops
```

---

### `flightdeck why`
**Purpose.** Explain the history of a single card — why it is in its current
state.

**Usage:**
```
flightdeck why CARD_ID
```

| Flag | Effect |
|------|--------|
| `CARD_ID` | Card id to inspect. |

**Mutates?** No — a read-only read. It traces the card's events and shows
what led to its current state.

**Example:**
```
flightdeck why t_abc123
```

---

## Setup

### `flightdeck init`
**Purpose.** Initialize flightdeck — create and seed `~/.flightdeck/`.

**Usage:**
```
flightdeck init [--apply] [--home PATH]
```

| Flag | Effect |
|------|--------|
| `--apply` | Write files; without it, print what WOULD be created and change nothing. |
| `--home PATH` | Flightdeck home to create/seed (default: `~/.flightdeck`). |

**Mutates?** Yes, when `--apply` is given — it creates the config file and
state directories under the flightdeck home. Requires `--apply`; without it
nothing is written.

**Example:**
```
flightdeck init --apply
```

---

### `flightdeck project`
**Purpose.** Project lifecycle + registry CRUD.

**Usage:**
```
flightdeck project PROJECT_CMD ...
```

**Subcommands:**

| Subcommand | Purpose | Key flags |
|------------|---------|-----------|
| `new` | Wire repo + topic + board + roadmap + registry. | `--repo`, `--github`, `--private`, `--apply`, `--dry-run` |
| `list` | Name, repo, board, topic, health (read-only). | (none) |
| `remove` | Remove a REGISTRY entry (never the repo/topic). | `--apply`, `--dry-run` |
| `repair` | Re-run `new` on an existing registry project. | `--repo`, `--github`, `--private`, `--apply`, `--dry-run` |
| `sync` | Adopt existing repos/topics/boards into the registry. | `--root`, `--apply`, `--ignore-topic`, `--json`, `--create-boards` |
| `pull` | Safe fetch + fast-forward-only pull of a repo. | `--json` |
| `push` | Push the current branch (gated, never --force). | `--apply`, `--json` |

**Flag details:**

| Flag | Applies to | Effect |
|------|-----------|--------|
| `--repo REPO` | new, repair | Repo path (new default `~/dev/<name>`; repair default from registry). |
| `--github [GITHUB]` | new, repair | Create/ensure a GitHub remote via `gh` and push. |
| `--private` | new, repair | With `--github`, make the remote private. |
| `--apply` | new, remove, repair, sync | Perform the change (mutating commands are dry-run by default). |
| `--dry-run` | new, remove, repair | Print the plan and touch nothing (alias of simply omitting `--apply`). |
| `--root PATH` | sync | Root to scan for repos (repeatable; default `~/dev`). |
| `--ignore-topic ID` | sync | Stop reporting a known-permanent topic (repeatable; persisted in the registry with `--apply`). Telegram's built-in General (id 1) is always ignored. |
| `--json` | sync | Emit machine-readable JSON for the sync result. |
| `--create-boards` | sync | Give every project lacking a board its own Hermes board (only meaningful with `--apply`; never moves existing cards). |

**Mutates?** `new`, `remove`, `repair`, and `sync` write to the registry and
require `--apply`. `list` is read-only. `project sync` discovers existing
repos/topics/boards and only writes the unambiguous matches to the registry
when `--apply` is given. `project pull` only ever fast-forwards or skips a
repo's default branch, so it is safe to run without `--apply`. `project push`
never reaches a remote unless `--apply` is given; without it, it only reports
what WOULD be pushed.

**`project pull`** fetches and fast-forward-only pulls each repo (or the one
named). It never touches a repo checked out on a non-default branch
(`SKIPPED: on branch <x>, not <default>, leaving untouched`), never touches a
dirty working tree (`SKIPPED: N uncommitted change(s), not touching`), and
never auto-merges diverged history (`SKIPPED: diverged history ... resolve by
hand`).

**`project push`** pushes only the branch currently checked out, to its own
configured upstream. It never force-pushes (`SKIPPED: push rejected (remote
has diverged), resolve by hand`) and reports a missing upstream clearly rather
than guessing one.

**Examples:**
```
flightdeck project list
flightdeck project new acme --repo ~/dev/acme --apply
flightdeck project new acme --github --private --apply
flightdeck project repair acme --apply
flightdeck project remove acme --apply
flightdeck project sync --create-boards --apply
flightdeck project sync --ignore-topic 7 --apply
flightdeck project pull            # safe ff-only pull of every project repo
flightdeck project pull flightdeck # just this one
flightdeck project push --apply    # push current branches of every project
flightdeck project push flightdeck # dry-run ahead-count (nothing pushed)
flightdeck project push flightdeck --apply
```

---

### `flightdeck topics`
**Purpose.** Telegram topic CRUD + overwrite audit.

**Usage:**
```
flightdeck topics TOPIC_CMD ...
```

**Subcommands:**

| Subcommand | Purpose | Positionals | Key flags |
|------------|---------|-------------|-----------|
| `list` | Every topic: id, name, mapped project. | (none) | — |
| `audit` | Flag mismatched + unmapped topics (read-only). | (none) | — |
| `rename` | Restore a topic name. | `id name` | `--apply` |
| `create` | Create a forum topic. | `name` | `--bind PROJECT`, `--apply` |
| `bind` | Map a topic id to a project. | `id project` | `--apply` |
| `unbind` | Clear a topic's project mapping. | `id` | `--apply` |

**Flag details:**

| Flag | Applies to | Effect |
|------|-----------|--------|
| `--apply` | rename, create, bind, unbind | Perform the change; mutating subcommands are dry-run by default. |
| `--bind PROJECT` | create | Bind the new topic to a project. |

**Mutates?** `rename`, `create`, `bind`, `unbind` write (rename the topic,
create a topic, or change the registry topic mapping) and require `--apply`.
`list` and `audit` are read-only. `topics bind/unbind` write the registry;
`topics rename` renames the live Telegram topic.

**Examples:**
```
flightdeck topics list
flightdeck topics audit
flightdeck topics create ops --bind ops --apply
flightdeck topics bind 12345 ops --apply
flightdeck topics rename 12345 "Ops cluster" --apply
flightdeck topics unbind 12345 --apply
```

---

### `flightdeck message`
**Purpose.** Send / read / dispatch / broadcast messages by project.

**Usage:**
```
flightdeck message MESSAGE_CMD ...
```

**Subcommands:**

| Subcommand | Purpose | Positionals | Key flags |
|------------|---------|-------------|-----------|
| `send` | Post a message to a project's topic. | `project message` | — |
| `read` | Recent messages from a project's topic. | `project` | `-n N` |
| `dispatch` | Create a card on a project's board AND announce it (dry-run by default). | `project task` | `--assignee X`, `--message MESSAGE`, `--body-file PATH`, `--apply` |
| `broadcast` | One message to several projects' topics. | `message` | `--to a,b,c` |

**Flag details:**

| Flag | Applies to | Effect |
|------|-----------|--------|
| `-n N` | read | Number of messages (default: 10). |
| `--assignee X` | dispatch | Profile to assign the card to. |
| `--message MESSAGE` | dispatch | Announcement text (default: the task title). |
| `--body-file PATH` | dispatch | Read the CARD BODY from PATH (`-` reads the full body from stdin). When given, `--message` becomes ONLY the announcement text and may differ from the (longer) card body. |
| `--apply` | dispatch | Actually create the card and send the announcement (dry-run by default). |
| `--to a,b,c` | broadcast | Comma-separated project names (default: all). |

**Auto-detected project.** `send`/`read`/`dispatch` take a project; when the
`project` positional is omitted, it is **auto-detected from the cwd** (if your
working directory sits inside a registered project's repo) and a `using
project '<x>' (detected from cwd)` note is printed. Because `send`/`dispatch`
have a `message`/`task` positional too, one omitting the project takes this
careful disambiguation: a single token that names a registered project is the
project, otherwise it is the message/task and the project is detected from the
cwd. An explicit `project` always wins over detection. From outside any
registered repo with no project, `send`/`dispatch` report the missing piece.

**Mutates?** `send` and `broadcast` post messages; `dispatch` creates a
card on the board AND announces it (and anchors a worktree). `send`/`broadcast`
post immediately — there is no `--apply` on those. `dispatch`, by contrast,
now defaults to a dry-run plan that prints the resolved board, card title, full
body, and announcement text, and only mutates with `--apply` — matching every
other mutating flightdeck command. `read` is read-only. Confirm before running
against real channels.

**Cross-project dependents on dispatch.** When `dispatch` targets a project
that OTHER registered projects declare in their own `depends_on` (e.g.
dispatching a fix on `ecofire-bc`), the announced message and card body carry a
dependent notice — `[flightdeck] 2 dependent project(s): ecofire-app, efsdriver
— consider verifying they still work` — so the worker fixing a BC extension is
reminded to check whether those apps actually use the endpoint they changed
before assuming the change is safe. It is a visible NOTE, not an automatic
cross-project test run, and it is absent when the target project has no
dependents.

**Examples:**
```
flightdeck message send ops "standup is at 9:30"
flightdeck message read ops -n 20
flightdeck message dispatch ops "fix the login bug" --assignee tech-writer
flightdeck message dispatch ops "fix the login bug" --body-file spec.md --message "fix the login bug" --apply
flightdeck message broadcast "maintenance at 22:00 UTC" --to ops,core
```

---

## Integrity

### `flightdeck doctor`
**Purpose.** Self-check: is flightdeck's view of the fleet trustworthy?

**Usage:**
```
flightdeck doctor
```

**Flags.** None.

**Mutates?** No — a read-only self-check. It inspects the config, registry,
and environment and reports what it can and cannot trust.

**Example:**
```
flightdeck doctor
```

---

### `flightdeck reconcile`
**Purpose.** Close cards whose branch merged; flag dead/stale cards.

**Usage:**
```
flightdeck reconcile [--apply] [--days N]
```

| Flag | Effect |
|------|--------|
| `--apply` | Perform the closes (dry-run by default). |
| `--days N` | Dead when no branch/no commits and older than N days (default: 14). |

**Mutates?** Yes, when `--apply` is given — it closes cards whose branch
already merged and flags dead/stale cards. Requires `--apply`; without it
only reports.

**Examples:**
```
flightdeck reconcile
flightdeck reconcile --apply
flightdeck reconcile --days 30 --apply
```

---

### `flightdeck hygiene`
**Purpose.** Detect board decay: duplicate cards, triage traps, stale
worktrees.

**Usage:**
```
flightdeck hygiene [--apply] [--similarity RATIO]
```

| Flag | Effect |
|------|--------|
| `--apply` | Perform the fixes (dry-run by default). |
| `--similarity RATIO` | Title-similarity threshold for duplicate detection (default: 0.88). |

**Mutates?** Yes, when `--apply` is given — it applies the fixes it finds
(duplicates, triage traps, stale worktrees). Requires `--apply`; without it
it only lists the decay.

**Examples:**
```
flightdeck hygiene
flightdeck hygiene --apply
flightdeck hygiene --similarity 0.95 --apply
```

---

### `flightdeck lint-cards`
**Purpose.** Flag cards missing VERIFY / acceptance / concrete references;
exit non-zero if any card would run in a repo MAIN TREE (read-only).

**Usage:**
```
flightdeck lint-cards [--repo-root REPO_ROOT] [board]
```

| Flag | Effect |
|------|--------|
| `--repo-root REPO_ROOT` | Root to resolve referenced `.py` modules against (default: cwd). |
| `board` | Board slug (default: all boards). |

**Mutates?** No — read-only. It flags violations and exits non-zero if any
card would run in a repo main tree, but never changes a card. There is no
`--apply`.

**Examples:**
```
flightdeck lint-cards
flightdeck lint-cards default
flightdeck lint-cards --repo-root ~/dev/acme
```

---

### `flightdeck verify`
**Purpose.** Run the registry verify command, record the result.

**Usage:**
```
flightdeck verify [--all] [project]
```

| Flag | Effect |
|------|--------|
| `--all` | Verify every project that has a verify command and summarise. |
| `project` | Project name in the registry. When omitted (no `--all`), it is **auto-detected from the cwd**: if your working directory sits inside a registered project's repo, that project verifies and a `using project '<x>' (detected from cwd)` note is printed; from outside any registered repo it falls through to the existing prompt to name a project or pass `--all`. An explicit `--all` or `project` always wins over detection. |

**Mutates?** Only its own bookkeeping: it records the verify result and
timestamp in `~/.flightdeck/state.yaml` so `standup` can show verify status.
It never changes a card or the registry. This command embodies the "never
report a state that was not verified" rule.

**Examples:**
```
flightdeck verify ops
flightdeck verify --all
```

---

### `flightdeck release`
**Purpose.** Check release preconditions and print the dry-run plan
(does nothing without `--apply`).

**Usage:**
```
flightdeck release [--apply] project version
```

| Flag | Effect |
|------|--------|
| `--apply` | Execute the release (bump VERSION), gated by the same preconditions; without it the command only prints the plan. |
| `project` | Project name from the registry. |
| `version` | Target version, e.g. `1.9.0` or `v1.9.0`. |

**Mutates?** Yes, when `--apply` is given — it bumps the version, subject
to the release preconditions (`install_cmd`, `installed_version_cmd`,
`version_file` in the registry). Without `--apply` it is strictly a dry-run
plan that changes nothing.

**Examples:**
```
flightdeck release ops 1.9.0
flightdeck release ops v1.9.0 --apply
```

---

### `flightdeck update`
**Purpose.** Check the installed flightdeck against its git upstream, or
update it — explicit, `--apply`-gated, **no daemon** (Gap 2 step 1 from
`docs/BRAINSTORM-what-is-missing.md`). Closes the "merged is not live" /
version-drift discovery gap: the operator has no signal a newer version exists
until this command looks.

**Usage:**
```
flightdeck update [--apply] [--dry-run]
```

| Flag | Effect |
|------|--------|
| `--apply` | Actually perform the update (mutating); without it the command only prints the plan. |
| `--dry-run` | Print the plan and touch nothing (alias of simply omitting `--apply`). |

**Dry-run (no `--apply`).** Detects HOW flightdeck is installed (from the
distribution's `direct_url.json` metadata — it never guesses), compares the
installed commit/version to the upstream's HEAD, and prints the plan:

```
flightdeck update
  install mechanism: editable (/Users/.../.worktrees/t_x)
  installed  0.1.0  (commit ecd86a5)
  upstream   0.6.0  (commit 659c0e3)
  would update this install by fast-forwarding the installed
  source clone (nothing performed; pass --apply to update)
```

Installed vs upstream is compared by **commit sha** (via `git ls-remote`, a
pure network read that writes no local refs). When the shas match it prints
`already up to date`. When the upstream cannot be reached or there is no
remote, it reports **UNKNOWN / cannot check** — it never claims "up to date"
when it could not actually compare.

**Install mechanisms and the update path each uses:**

* **editable** (`pip install -e` from a local clone): update by `git pull
  --ff-only` on that clone, using the same safe path `project pull` runs
  (`pull_project`). A dirty tree, a non-default-branch checkout, or diverged
  history is refused with a clear reason — never force-pulled, never touched.
* **git+ non-editable** (`pip install git+...`): no local clone to pull, so
  the plan shows the mechanism and `--apply` runs `pip install --upgrade
  --force-reinstall git+<url>`.
* **non-git / not-installed / unknown**: cannot be self-updated; reported
  honestly, nothing guessed.

**Mutates?** Yes, when `--apply` is given and an update is actually available
— it updates the tool's own installed source. Requires `--apply`; a bare
invocation changes nothing. After applying it re-checks the running source's
commit against the intended upstream commit and reports `verified` only when
they match — a failed or interrupted update is reported honestly as
UNVERIFIED/FAILED with a non-zero exit, never as a clean success.

**Examples:**
```
flightdeck update              # dry-run plan (nothing performed)
flightdeck update --apply      # actually update, then verify it took
```

---

### `flightdeck incident`
**Purpose.** Log an incident for a project, appending a dated entry to the
project's `docs/INCIDENTS.md`.

**Usage:**
```
flightdeck incident [--project PROJECT] [--fix FIX] [--cause CAUSE] [--lesson LESSON] [--apply] symptom
```

| Flag | Effect |
|------|--------|
| `--project PROJECT` | Project in the registry whose repo to write (default: flightdeck). |
| `--fix FIX` | What actually resolved it. |
| `--cause CAUSE` | Why it happened. |
| `--lesson LESSON` | The general rule, stated so it applies next time. |
| `--apply` | Write the entry (dry-run by default; never rewrites existing entries). |
| `symptom` | What happened: first line becomes the heading, full text the Symptom field. |

**Mutates?** Yes, when `--apply` is given — it appends a dated incident
entry to the project's `docs/INCIDENTS.md`. Requires `--apply`; without it
the composed entry is printed but nothing is written. It never rewrites an
existing entry (append-only).

**Examples:**
```
flightdeck incident "MCP endpoint unreachable" --fix "restarted daemon" --apply
flightdeck incident --project acme --cause "auth token expired" --lesson "rotate before expiry" --apply
```

---

## Quick reference: mutability at a glance

| Command | Mutates? | Gate |
|---------|----------|------|
| standup | No | — |
| monitor | No | — |
| qa | Only `--notify` side-effect; `qa add`/`qa done` edit `manual-qa.yaml` | `--notify`; none for add/done |
| review | Yes (merge+close) | `--apply` |
| ask | Sends / edits templates | `--dry-run` to preview |
| roadmap | add/move/done/adopt | `--apply` |
| decompose | Yes (creates cards) | `--apply` |
| start | Yes (releases cards) | `--apply` |
| ingest | Yes (writes ROADMAP.md draft) | `--apply` |
| report | Yes (posts + records state) | `--apply` |
| metrics | No | — |
| why | No | — |
| init | Yes (creates `~/.flightdeck`) | `--apply` |
| project | new/remove/repair/sync | `--apply` |
| topics | rename/create/bind/unbind | `--apply` |
| message | send/broadcast | none (send immediately) |
| message | dispatch | `--apply` |
| doctor | No | — |
| reconcile | Yes (closes cards) | `--apply` |
| hygiene | Yes (applies fixes) | `--apply` |
| lint-cards | No | — |
| verify | Own state only | — |
| release | Yes (bumps version) | `--apply` |
| update | Yes (updates the tool itself) | `--apply` |
| incident | Yes (appends to INCIDENTS.md) | `--apply` |

---

## MCP tools

The same commands are exposed to MCP clients (Claude Code, an agent driving
flightdeck through a Telegram-topic session, ...) by the `flightdeck-mcp`
server (`flightdeck/mcp_server.py`). Each MCP tool is a **thin adapter**: it
builds an `argparse.Namespace` and calls the SAME `run(args, registry_path)`
entry point the CLI dispatches to, then returns whatever the command printed —
so a tool's output is identical to running the CLI command with the matching
flags. The `apply` param on a mutating MCP tool maps 1:1 onto that command's
own CLI `--apply` gate; `apply` always defaults to `False` (nothing mutates).

Read-only tools (never mutate):

| Tool | Maps to |
|------|---------|
| `flightdeck_why(card_id)` | `why <card>` |
| `flightdeck_metrics(project, since)` | `metrics <project> --since` |
| `flightdeck_topics_list()` | `topics list` |
| `flightdeck_topics_audit()` | `topics audit` |
| `flightdeck_standup()` | `standup` |
| `flightdeck_qa(project)` | `qa <project>` |
| `flightdeck_doctor()` | `doctor` |
| `flightdeck_roadmap_progress(project)` | `roadmap progress <project>` |
| `flightdeck_roadmap_show(project)` | `roadmap show <project>` |
| `flightdeck_lint_cards(board)` | `lint-cards --board` |
| `flightdeck_reconcile_preview()` | `reconcile` dry-run plan (apply never reachable) |
| `flightdeck_legacy_cards(include_archived_boards)` | `legacy-cards` |

Apply-gated tools (`apply` defaults to `False` and maps to the CLI's `--apply`):

| Tool | Maps to |
|------|---------|
| `flightdeck_review(card, apply)` | `review <card>` |
| `flightdeck_reconcile(apply)` | `reconcile` |
| `flightdeck_decompose(project, milestone, goal, apply)` | `decompose` |
| `flightdeck_start(project, milestone, max_concurrent, apply)` | `start` |
| `flightdeck_ingest(project, limit, apply)` | `ingest` |
| `flightdeck_roadmap_adopt(project, apply)` | `roadmap adopt` |
| `flightdeck_migrate_card(card_id, project, apply)` | `migrate-card` |
| `flightdeck_message_dispatch(project, task, apply)` | `message dispatch` |
| `flightdeck_report(project, since, all, backfill, apply)` | `report` |
| `flightdeck_incident(symptom, project, fix, cause, lesson, apply)` | `incident` |
| `flightdeck_hygiene(apply, similarity)` | `hygiene` |

One tool posts immediately by design (mirrors the CLI, no gate):
`flightdeck_message_send(project, text)`.

Deliberately NOT exposed as MCP tools: `release` (too high-stakes — it pushes
git tags + GitHub releases), `update` (mutates the tool's own installed code —
keep it explicit and CLI-gated), `monitor` (interactive loop that never
returns), `init`/`project` (bootstrapping/CRUD, CLI-only fine), `ask`, `verify`.
