# BRAINSTORM — What is flightdeck missing?

**Date:** 2026-08-17
**Status:** design exploration for human review — nothing implemented.
**Prompt:** "maybe we need to make flightdeck update itself, a daemon or
something, dunno. i feel like flightdeck is missing something. do a brainstorm
to find what."

This is a *design document, not a spec*. Every gap below traces to friction
actually observed in today's session / the shipped incident history — nothing
here is speculative "nice to have." The recommendation is ranked by
value-per-effort, and the self-update-daemon question is answered explicitly
as one of the items.

---

## Context: what flightdeck already is

Flightdeck is a git-installed Python CLI (`pip install git+...`, no PyPI) that
answers one question every morning: *what actually needs me right now?* It
reads Hermes kanban boards + git across ~8 registered project repos and
reconciles card state against what actually landed, so the signal is true.

The two design rules that define the product:

1. **Read-only by default.** Every mutating command prints a plan and changes
   nothing until `--apply`.
2. **Never report a state not verified.** UNKNOWN is a distinct result;
   a false all-clear is the worst output this tool can produce.

Today (v0.6.0) it shipped a lot: the close_card silent-lie fix, real --help
examples, cwd auto-detection, migrate-card/cmd_start/review-attribution bug
fixes, a manual-QA tracking store, and project pull/push. It is a daily,
hands-on tool for a solo infra/ops builder running a real DGX Spark cluster +
Hermes agent fleet.

The user's standing rules (stated repeatedly this session):
- never deploy/publish without explicit go-ahead,
- never trust unverified success,
- prefers explicit control over most mutating actions.

Those rules are load-bearing for the recommendations below, especially the
self-update question.

---

## Gap 1 — No way to answer "is this data I'm looking at actually fresh?"

**The friction (real, today).** `flightdeck standup` shown stale/incorrect
status for archived cards. The *specific* bug has a fix card already
dispatched. But it points at a deeper pattern gap: across every flightdeck
command (`standup`, `qa`, `review`, `metrics`, `report`, `why`), there is no
per-command "this digest reflects state as of <timestamp>" or any warning when
the underlying sources can't be read freshly. Flightdeck has a *coverage
footer* (`read 8 projects | 10 boards | 20 cards | ...`) which proves it *tried*
to read everything, but it does not prove *freshness* — a cached/stale board
read looks identical to a current one.

**What it costs.** The operator makes a decision ("nothing needs me, back to
work") on a digest that reflects a board state that may be hours old or,
worse, a board whose archive flag flipped since the read. The whole product
is "make the signal true"; a signal you can't tell is stale is not trustworthy
in the operative sense — you trust it, and that trust can be a lie. This is
the single deepest structural gap and it traces to the product's own
founding purpose.

**Approaches.**

- **A. Staleness watermark on every read command.** Each command records and
  prints the newest `updated_at`/event timestamp it saw (board reads, git
  refs), and `standup` shows `data as of <time>` computed from the *oldest*
  critical input, not the newest — so if any one board behind the digest is
  stale, the digest says so. Cheap: a timestamp column on reads, one printed
  line. Builds directly on the existing "UNKNOWN is distinct" ethos.
  Tradeoff: minor noise per command; needs a decision about what "stale"
  means per source (a board with no activity isn't stale, it's quiet).
- **B. A `doctor` freshness dimension.** Extend `doctor` (which already
  verifies repos/boards/topics/three-way-binding) with "can I read every
  source, and are the cached/local copies fresh" — i.e. the orphaned/legacy
  board detection from Gap 3 folds in here as one dimension. Tradeoff:
  `doctor` is run on demand, so it doesn't protect the operator at the moment
  of trusting a digest; it complements rather than replaces A.

**Honest conclusion.** This is worth closing. It is the most on-mission gap
because "trustworthy signal" is the product. Recommend A (watermark on reads)
as the cheap, always-on protection, with B as a belt-and-suspenders addition.

---

## Gap 2 — Self-update: the honest answer is "explicit on-demand, no daemon"

**The friction.** Flightdeck is git-installed with no PyPI package. Getting a
new version onto the machine is a manual `pip install --upgrade git+...` (or
`project pull` + reinstall). It is a solo daily-driver CLI updated frequently.

**Is this a real gap?** Partially. For a tool the operator uses every day, the
*discovery* problem is real — today the operator shipped v0.6.0 but the
installed copy won't reflect it until they remember to reinstall. There is no
"a new version exists" signal. That *is* a genuine gap: version drift that the
tool is blind to. The *mechanism* (reinstall) is not the problem; the *signal*
is.

**What it costs.** The exact incident class DESIGN.md already names six times:
**merged is not live.** An operator runs `standup`, sees v0.6.0 in the DRIFT
section, and the running code still lacks a fix they think they're using —
most dangerously, they *trust* a behavior that isn't in the running binary.
The prod "merged is not live" failures (orphaned daemon, CLI from install
path, stale proxy, template payload) are all the same disease: no tripwire
that the thing on disk differs from what's running.

**Candidate forms, with real tradeoffs.**

1. **`flightdeck update` (explicit, on-demand).** A command that checks
   `git ls-remote` for the installed remote, compares the installed VERSION to
   upstream, prints what would change, and applies on `--apply` (or a
   confirmed yes) via the existing `project pull`-style safe path. Tradeoff:
   dead simple, fits the read-only-by-default rule perfectly, zero background
   risk. Weakness: still relies on the operator *running it* — discovery is
   only as good as the habit.

2. **Background daemon / launchd service** (mirroring HSCC's `hscc_daemon`).
   Tradeoff: it is *precisely* the risk the user's standing rules warn
   against. A daemon that can silently change the tool's own behavior
   mid-session (a) deploys without explicit go-ahead, (b) can apply an update
   the operator hasn't reviewed, and (c) — as `hscc_daemon` and the
   maintenance-window conflict found earlier this session show — is itself a
   new failure/coordination surface. A daemon to patch a solo daily CLI is a
   solution looking for a problem; it adds a launchd plist, a watcher loop,
   and an autoupdate path all to remove a single explicit command the operator
   runs anyway. **The honest verdict here is "leave it as manual process."**

3. **`flightdeck standup` notice (in between).** `standup` (already run
   constantly) does a cheap non-blocking `ls-remote` check and prints one
   line when a newer version exists: `flightdeck update available (run
   `flightdeck update`)`. No auto-apply, explicit consent always required.
   Tradeoff: solves the *discovery* gap (the real one) with minimal risk; the
   network call is one-off and local. Weakness: needs `standup` to do a remote
   fetch, which some may not want on a read-only daily command — mitigate by
   making it opt-in or rate-limited (once/day).

**Recommendation.** The gap worth closing is *discovery*, not *mechanism*.
Build option 1 (`flightdeck update`, explicit, `--apply`-gated, honest about
"installed X → would install Y") as the low-stakes core, plus option 3 (the
one-line `standup` notice, gated to once/day, never auto-applied) to close
the discovery loop the only command the operator actually runs daily provides.
**Do not build a daemon.** The autonomy it would grant directly violates the
user's stated rules ("never deploy without explicit go-ahead", "prefers
explicit control over mutating actions") and re-introduces the exact
"merged is not live / daemon on stale code" class DESIGN.md is fighting.
A self-updating daemon that can silently change the tool's behavior mid-session
is a meaningfully worse risk profile than a passive notice; for a solo daily
driver, the real update frequency (a few times a week at most) means the
manual step is a rounding error.

---

## Gap 3 — No cross-session / cross-board "is someone already doing this?" guard before a card is created

**The friction (real, today, twice).** A separate, parallel Hermes Telegram
session repeatedly worked the SAME requests as this session, independently,
on a different board — twice causing real collisions (once nearly costing
significant duplicate effort, once nearly causing a scope-wrong edit in the
wrong repo). Separately, raw `create_task` calls bypassed flightdeck's
`message dispatch` and minted duplicate cards as `workspace_kind='scratch'`
(the anchor fix only reached `dispatch`, not direct `create_task`).

**What it costs.** Wasted agent compute on duplicate work; worse, the risk of
two agents editing the same file with conflicting scope. The user is running
*both* a Telegram-topic Hermes session AND this CLI/board orchestration
against the same fleet — those two "hands" don't talk to each other, so a
request can be issued twice and no surface flags it until a merge conflict.

**Approaches.**

- **A. Pre-creation duplicate check inside flightdeck.** Before `dispatch` /
  `decompose --apply` / `start` creates a card, run the existing `hygiene`
  title-similarity check against the board's *open* cards and refuse (or
  warn) when a near-duplicate open card already exists. This reuses machinery
  that already exists (`hygiene` similarity), making it cheap. Tradeoff: only
  catches *flightdeck-originated* creation; the parallel Telegram session
  creating cards via its own flightdeck-MCP tools *is* caught (it goes through
  the same `dispatch`), but a raw `create_task` or a card created outside the
  board entirely is not.
- **B. A single-writer invariant at the board layer.** The deeper fix (root
  cause, matches the separately-dispatched GUARD card t_3e8ff045): enforce
  "one active/open card per logical unit of work" and route ALL card creation
  through one path (kill the bypass where `create_task` can be called raw).
  Tradeoff: higher effort, touches the core creation seam; overlaps with the
  guard card already dispatched — this brainstorm should not re-open that
  work, just note the direction.

**Honest conclusion.** Worth closing, but the right owner is the guard-card
work already dispatched (t_3e8ff045) — this brainstorm's job is to name the
pattern: *the fleet has two hands and no handshake.* The cheapest incremental
win is A (surface "an open card already looks like this" at create-time) since
it reuses existing similarity machinery and doesn't require waiting for the
bigger guard. But leave the architectural single-writer enforcement to the
guard card rather than diverging into a second implementation.

---

## Gap 4 — No built-in "detect orphaned/legacy boards" in `doctor`

**The friction (real, today).** A whole separate legacy kanban board
(`~/.hermes/kanban.db`, pre-dating the per-project board system) accumulated
7+ real, unreconciled cards. Flightdeck's own `standup` barely surfaced them
and they required manual archaeology to untangle. Flightdeck *has* the
machinery to find these — `legacy-cards` and `project sync`-style orphan
detection — but it is a separately-invoked command, not part of the
self-check the operator runs when they want to trust the fleet (`doctor`).

**What it costs.** The orphaned board is a hidden second source of truth. Work
that lives there is invisible to `standup`/`qa`/`review`, so the operator can
believe the fleet is idle while real cards sit unreconciled on a board no
registered command surfaces by default. This is the "silence is a bug"
principle from DESIGN.md, violated at the board-discovery level.

**Approaches.**

- **A. Fold orphan/legacy-board detection into `doctor` as a dimension.**
  `doctor` already verifies that every *registered* board exists; add the
  inverse check — "are there boards on this host / archived boards with cards
  that no registered command is surfacing?" Reuse `legacy-cards`' board
  attribution (which board slugs are registered) and surface any board with
  open cards outside the registry. Tradeoff: cheap (reuses existing
  attribution), but `doctor` is on-demand — it only helps when run.
- **B. Include the same flag in the `standup` coverage footer.** Today
  `standup` prints `read N projects | M boards | K cards` as a coverage proof;
  extend it to add `+ X unread board(s) (run legacy-cards)` when an orphan
  board holds cards. Tradeoff: one line, but it's on the command the operator
  actually runs daily, so the detection is always on. Slight noise in the
  common healthy case (nothing extra printed).

**Honest conclusion.** Worth closing, and cheap — the detection logic already
exists in `legacy-cards`; it just isn't surfaced where trust is checked.
Recommend B (extend the `standup` footer) as the always-on tripwire, with A
as the natural companion. The immediate reconciliation is already its own
dispatched card; this is the *feature* gap underneath it.

---

## Items considered and cut (ruthlessly)

- **"A web UI" / mobile app** — already explicitly rejected in
  `docs/FEATURES.md`; a second surface is how the previous monitor app died.
- **Auto-merging / auto-archiving with more autonomy** — the operator's
  judgement is the product; flightdeck removes toil around review, never the
  review. Explicitly rejected in DESIGN/FEATURES.
- **Finer metrics, cost-per-project, team boards** — real but speculative /
  lower-urgency; no *new* friction observed today to justify them over the
  four above.
- **Reimplementing kanban/HSCC internals** — non-goal by design; never
  becomes a second source of truth.

---

## Recommendation, ranked by value/effort

| Rank | Gap | Why now | Lowest-effort close | Verdict |
|------|-----|---------|---------------------|---------|
| 1 | **Freshness watermark** (Gap 1) | On-mission: product *is* trustworthy signal; a signal you can't date is not trustworthy | Per-read timestamp line + staleness note (A) | **Build** (cheap, always-on) |
| 2 | **`standup` orphan-board + version notices** (Gap 3B + Gap 2 step 3) | Closes two discovery loops on the one command run daily | Two one-line additions to the footer | **Build** (cheap) |
| 3 | **`flightdeck update` explicit command** (Gap 2 step 1) | Closes the "merged is not live / version drift" discovery gap | One `--apply`-gated command reusing `project pull` | **Build** (low-stakes core) |
| 4 | **Self-update daemon** (Gap 2 step 2) | — | launchd plist + watcher + autoupdate | **Do NOT build** — violates the user's explicit-control rules and re-creates the daemon risk class DESIGN.md fights |
| — | Duplicate/open-card guard (Gap 3) | Real collision risk | Reuse `hygiene` similarity at create-time (A) | **Defer to guard card t_3e8ff045**; add only the cheap create-time warn if trivial |

### The self-update-daemon question, answered plainly

**Do not build a self-updating daemon.** The *real* gap is discovery — the
operator has no signal that a newer version exists, and "merged is not live"
is the single most-repeated failure class in this project's own history. A
background autoupdater solves discovery by over-solving it: it introduces a
launchd service, a watcher loop, and an unattended update path — all to remove
one explicit command the operator already runs — and it grants the tool the
exact autonomy the user's standing rules forbid (deploy without explicit
go-ahead, change behavior mid-session). For a solo daily driver updating a few
times a week, the honest answer is:

- **`flightdeck update`** — explicit, `--apply`-gated, prints installed→would
  install. (build)
- **a one-line `standup` notice**, gated to once/day, never auto-applying.
  (build)
- **no daemon.** (don't)

This matches every prior-art pattern in the user's own toolchain — `sparkrun
update` / `setup update` are explicit on-demand commands, not background
services — and respects the standing rules.

---

## What I deliberately did not do

- Did not implement any of this (design doc only, per the card).
- Did not touch flightdeck's command files.
- Did not open a PR, commit, or run any `git reset` / `git checkout .` /
  `git clean` (as instructed).

The four gaps above are the ones with real, lived evidence behind them. Pick
what (if anything) to convert into a follow-up card.
