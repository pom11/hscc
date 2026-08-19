# Flightdeck — feature exploration

> **Historical.** This is a brainstorm from 2026-08-09, superseded by the
> shipped commands. Kept for the reasoning behind them; do not treat it as a
> spec of current behaviour — see `README.md` / `flightdeck --help`.

Brainstormed 2026-08-09. Every candidate below traces to a failure actually
observed while running this fleet, not to speculation. Priority is by time saved
per unit of build effort.

## The operator's real day

1. Wake up. Ask: *did anything need me overnight?* — today this means reading a
   board that lies, and scanning six Telegram topics.
2. For each thing that needs review: find the branch, read the diff, work out
   what to test, run it, merge, close the card.
3. Notice something is stuck or broken — usually late, usually by accident.
4. Ship: bump version, write changelog, tag, release, install, verify live.
5. Decide what's next; there is no roadmap surface.

Steps 2 and 4 are where the hours actually go. Step 3 is where the damage happens.

---

## P0 — attacks the measured bottleneck

### `flightdeck review <card|project>`
The single highest-value command. Today reviewing means: find the branch, `git
show`, guess what to test, run the suite, merge, archive the card — by hand, per
card. This does it in one place: shows the diff summary, runs the project's verify
command, shows test timings, checks the branch merges cleanly, and offers merge +
card-close as one confirmed action.

*Evidence:* every merge this week was that sequence typed manually, ~10 cards.
Also the place to enforce the review bar — a card whose tests reach the network,
or whose suite slowed down, should be flagged before merge, not after.

### `flightdeck verify <project>`
Runs the project's `verify` command from the registry and reports pass/fail with
timings. Manual testing is the operator's stated bottleneck; this makes "did I
actually test it" a command rather than a memory.

*Extension:* record the last verify result + timestamp per project, so `standup`
can show "last verified 3 days ago" — staleness of *confidence*, not just of code.

### `flightdeck release <project>`
Version bump, changelog section, commit, annotated tag, push, GitHub release,
install, and post-install live verification — one command with a dry-run.

*Evidence:* eight releases were cut by hand this week (v1.6.0 → v1.8.1), each
~6 steps. Twice the install step was forgotten and "merged" was not "live".

### Card quality lint (`flightdeck lint-cards`)
Refuses/flags cards missing a `VERIFY:` line, an acceptance criterion, or file
paths when the task touches a large module.

*Evidence:* the strongest correlation observed all week. An identical task phrased
abstractly stalled 1h45m with zero commits; the same task naming exact functions
and line numbers succeeded first try. Card quality *is* throughput.

---

## P1 — prevents the failure modes that already bit

### Stall detection with escalation
Already in `standup`, but should also *act*: after N minutes with zero commits,
post to the topic asking the worker to report status, and after 2N, flag for
archive. Three cards burned ~4 hours silently this week; two more failed twice.

### Duplicate card detection
The decomposer, while crash-looping, minted near-identical cards — four "MCP
Server Core" cards, two with identical titles. Detect by title similarity within
a board and propose merging.

### Triage rescue
Cards that land in the `triage` column are unrecoverable through normal commands:
`unblock` refuses ("not blocked/scheduled"), `promote` refuses ("only todo or
blocked"), and the only exits — `specify`/`decompose` — crash-loop on a
`NOT NULL tasks.session_id` bug. Five cards were lost this way; each needed
manual archive + recreate. Detect and offer one-command rescue (archive +
recreate preserving the branch, since the work is usually already done).

### Worktree and branch hygiene
Merged branches and their `.worktrees/` directories accumulate indefinitely.
Report and offer cleanup of worktrees whose branch is merged.

### Live-vs-installed audit (`flightdeck live`)
First-class command for the lesson that cost the most time: **merged is not
live**. Four separate incidents — an orphaned daemon running days-old code from a
different Python env and invisible to `launchctl`; a CLI executing from an install
path rather than the repo; a proxy holding config that predated a change; template
changes needing a payload install. Checks repo vs installed version, process code
paths vs source, and config values against what endpoints actually serve.

### Session health
Detect a wedged Hermes session (`max compression attempts`) — which silently
drops every message sent to that topic — and offer the export/delete/restart fix.
Two sessions wedged this week; both looked healthy from the board.

---

## P2 — worth having, lower urgency

- **Timeline** — what happened across all projects today (merges, releases,
  incidents), assembled from git + kanban history.
- **Metrics** — cards/day, review latency, stall rate, first-time-pass rate.
  Would have made "card quality decides outcomes" visible rather than anecdotal.
- **Incident log** — `flightdeck incident "..."` appends a dated entry with the
  fix, so hard-won lessons live in the repo rather than in one operator's memory.
- **Cost/attention per project** — where the fleet's effort actually went.
- **Watch mode** — long-running `standup --watch` for a second monitor.
- **Push notification** on NEEDS-YOU transitions.

## Explicitly rejected

- **A web UI.** A terminal digest was chosen deliberately; a second surface to
  maintain is how the previous monitor app died.
- **Reimplementing kanban/HSCC.** Flightdeck reads and reconciles; it never
  becomes a second source of truth.
- **Auto-merging.** The operator's judgement is the product. Flightdeck removes
  the *toil* around review, never the review itself.

## Build order

P0 first — they attack the measured bottleneck. P1 next; each prevents a failure
that has already cost hours. P2 only if it stays cheap.
