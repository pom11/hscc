# Flightdeck — round 2: the roadmap→work→review loop

> **Historical.** This is a brainstorm (round 2), superseded by the shipped
> commands. Kept for the reasoning behind them; do not treat it as a spec of
> current behaviour — see `README.md` / `flightdeck --help`.

Round 1 made the *signal* true (what needs me, what drifted, what's stale).
Round 2 closes the *loop*: idea → roadmap → atomic cards → cluster work → my
review → merge → release. Today flightdeck only reads a `ROADMAP.md`; the
operator still hand-writes every card and hand-tracks what came back.

Everything below is grounded in what actually happened building this repo with
the cluster over one day — 12 cards, 9 merged, 3 stalls, 1 infrastructure
incident.

---

## The gap

An idea becomes work through four manual steps today:

1. decide what's next (no surface — `ROADMAP.md` is hand-edited)
2. break it into cards (hand-written, one at a time)
3. dispatch, remembering per-profile concurrency caps
4. notice what came back and review it

Steps 2 and 3 are where quality is won or lost, and step 4 is where things
silently rot.

---

## P0 — closing the loop

### `flightdeck roadmap add|move|done`
Edit `ROADMAP.md` from the CLI: add an item to Now/Next/Later, promote between
sections, mark done. Plain versioned markdown stays the source of truth — no new
datastore, and the diff shows in code review.

### `flightdeck decompose <roadmap-item>` — the centrepiece
Ask the cluster orchestrator to break a roadmap item into atomic cards, then
**gate the result on card quality before anything is created.**

This is the highest-leverage feature in the tool, because card quality is the
single strongest predictor of whether work lands. Measured this week:

- a card phrased abstractly ("reuse apply's write-set logic") stalled **1h45m
  with zero commits**; the identical task naming exact functions and line numbers
  succeeded **first try**
- cards bundling several commands stalled 60–80 minutes; every single-concern
  card landed
- five cards were lost to a triage trap after repeated vague re-blocking

So `decompose` refuses to create a card that would predictably stall:

- exactly **one concern** per card (one command, one module, one behaviour)
- a **`VERIFY:`** line — how the operator proves it works
- **concrete references** — file paths, function names, line numbers where the
  task touches an existing module. If the orchestrator cannot name them,
  flightdeck locates them first and injects them.
- explicit **acceptance criteria**, phrased so a test would fail if the feature
  were removed
- **dependency order** between the cards it produces

Output is a proposal. `--apply` creates the cards; nothing is created silently.

### `flightdeck dispatch <plan>` — concurrency-aware
Dispatch a decomposed set, respecting what the fleet can actually absorb:

- honour `max_in_progress_per_profile` (2) and the global cap (6) — read them,
  do not hardcode
- **spread cards across profiles** (coder, backend-engineer, devops-engineer, qa,
  architect, technical-writer) rather than piling them on one. Assigning six
  cards to `coder` serialised them two at a time while four fleet slots sat idle
  — a self-inflicted 3× slowdown that looked exactly like a stall.
- hold dependent cards until their parents merge, and release automatically

### `flightdeck qa [project]`
The operator's manual-testing queue: for everything awaiting review, show the
`VERIFY:` line, the diff summary, and what has *not* been proven. Answers "what
do I actually have to click or run?" — which today has to be reverse-engineered
from a diff.

---

## P1 — keeping the loop honest

### `standup --watch`
Auto-refreshing digest for a second monitor. Same renderer, redrawn on an
interval, no new interaction model. (A full TUI is deliberately deferred until
the one-at-a-time review loop demonstrably annoys us.)

### Starvation vs confusion detection
`standup`'s STALE section must distinguish two very different states:

- **worktree empty** → the worker is *starved*, not confused. Cause is usually
  infrastructure.
- **files present, no commit** → genuinely working.

This distinction cost hours: eight cards heartbeated for hours writing nothing
while the worker model span was wedged — it answered `/v1/models` and showed
containers "Up 2 days" while every completion hung. I nearly archived eight
healthy cards. So flightdeck should also **probe the executor before blaming a
card**, and say "the fleet is not answering" rather than "8 cards stalled".

### Roadmap → cards → progress
Link cards back to the roadmap item that spawned them and show real progress
("Anulare tranzactie: 3/7 cards merged, 1 awaiting review"). Today the
connection between a roadmap line and the work exists only in the operator's
head.

### `flightdeck why <card>`
One card's full story: roadmap item, branch, commits, test state, review history,
current blocker. Assembling this by hand across kanban and git is a recurring tax.

---

## P2 — later

- **`plan`** — interactive: pick a roadmap item, decompose, review the proposed
  cards, dispatch, in one flow.
- **Metrics** — first-time-pass rate, stall rate, review latency. Would have made
  "card quality decides outcomes" visible rather than anecdotal.
- **Incident log** — `flightdeck incident` appends a dated entry with the fix, so
  lessons live in the repo instead of one operator's memory.
- **Review TUI** — only if `review --queue` proves annoying in practice.

## Still rejected

A web UI, reimplementing kanban, and auto-merging. The operator's judgement is
the product; flightdeck removes the toil around it.

---

## P0 — prompt templates (stop retyping the same framing)

The operator repeatedly types the same shapes of message into Telegram topics:
"decompose this task…", "this is the project and this is where I want to get
to…", "please review X". Retyping them is tedious and, worse, **inconsistent** —
and inconsistent framing is what makes cards stall.

### `flightdeck ask <project> <template> [--set key=value]`

Renders a stored template, fills it with **context flightdeck already knows**,
and sends it to that project's topic. The operator supplies only what is genuinely
new.

Auto-filled from the registry and repo, never retyped:

- project name, repo path, current branch, HEAD sha
- the project's `ROADMAP.md` **Now** items
- open cards on its board and what is awaiting review
- the project's `verify` command

So "this is the project" is never typed again — it is derived.

### Templates

Stored as markdown with `{{slots}}` in `~/.flightdeck/templates/` (user-editable),
with a shipped starter set copied on first run:

| template | purpose |
|---|---|
| `decompose` | break a goal into atomic cards — embeds the card-quality rules below |
| `brief` | "here is the project, here is where I want to reach" — current state vs target |
| `review` | ask for review of specific work, with the diff summary attached |
| `status` | ask the cluster where a piece of work stands |
| `bugfix` | symptom, repro, expected — with repo context prefilled |
| `spike` | investigate-and-propose, explicitly no code changes |

`template list` / `template show <name>` / `template edit <name>`. Unknown slots
are an error listing what the template expects — never a message sent with a
literal `{{goal}}` in it.

### Why the `decompose` template matters most

It embeds, by construction, the rules that decide whether work lands: one concern
per card, a `VERIFY:` line, concrete file/function references, acceptance criteria
that would fail if the feature were removed, and dependency order. Measured this
week — an abstractly-phrased card stalled 1h45m with zero commits while the same
task naming exact seams succeeded first try; every bundled card stalled, every
single-concern card landed.

Encoding that in a template means the operator gets a well-formed dispatch every
time without remembering the rules — which is the whole point of not retyping.

`--dry-run` prints the rendered message; sending is the default action since this
is the interactive path.
