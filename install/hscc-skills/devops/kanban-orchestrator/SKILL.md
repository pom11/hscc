---
name: kanban-orchestrator
description: Decomposition playbook + anti-temptation rules for an orchestrator profile routing work through Kanban. The "don't do the work yourself" rule and the basic lifecycle are auto-injected into every kanban worker's system prompt; this skill is the deeper playbook when you're specifically playing the orchestrator role.
version: 3.1.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [kanban, multi-agent, orchestration, routing]
    related_skills: [kanban-worker]
---

# Kanban Orchestrator — Decomposition Playbook

> The **core worker lifecycle** (including the `kanban_create` fan-out pattern and the "decompose, don't execute" rule) is auto-injected into every kanban process via the `KANBAN_GUIDANCE` system-prompt block. This skill is the deeper playbook when you're an orchestrator profile whose whole job is routing.

## Profiles are user-configured — not a fixed roster

Hermes setups vary widely. Some users run a single profile that does everything; some run a small fleet (`docker-worker`, `cron-worker`); some run a curated specialist team they've named themselves. There is **no default specialist roster** — the orchestrator skill does not know what profiles exist on this machine.

Before fanning out, you must ground the decomposition in the profiles that actually exist. The dispatcher silently fails to spawn unknown assignee names — it doesn't autocorrect, doesn't suggest, doesn't fall back. So a card assigned to `researcher` on a setup that only has `docker-worker` just sits in `ready` forever.

**Step 0: discover available profiles before planning.**

Use one of these:

- `hermes profile list` — prints the table of profiles configured on this machine. Run it through your terminal tool if you have one; otherwise ask the user.
- `kanban_list(assignee="<some-name>")` — sanity-check a single name. Returns an empty list (rather than an error) for an unknown assignee, so this only confirms a name you're already considering.
- **Just ask the user.** "What profiles do you have set up?" is a fine first turn when the goal needs more than one specialist.

Cache the result in your working memory for the rest of the conversation. Re-asking every turn wastes a tool call.

## When to use the board (vs. just doing the work)

Create Kanban tasks when any of these are true:

1. **Multiple specialists are needed.** Research + analysis + writing is three profiles.
2. **The work should survive a crash or restart.** Long-running, recurring, or important.
3. **The user might want to interject.** Human-in-the-loop at any step.
4. **Multiple subtasks can run in parallel.** Fan-out for speed.
5. **Review / iteration is expected.** A reviewer profile loops on drafter output.
6. **The audit trail matters.** Board rows persist in SQLite forever.

If *none* of those apply — it's a small one-shot reasoning task — use `delegate_task` instead or answer the user directly.

## The anti-temptation rules

Your job description says "route, don't execute." The rules that enforce that:

- **Do not execute the work yourself.** Your restricted toolset usually doesn't even include terminal/file/code/web for implementation. If you find yourself "just fixing this quickly" — stop and create a task for the right specialist.
- **For any concrete task, create a Kanban task and assign it.** Every single time.
- **Split multi-lane requests before creating cards.** A user prompt can contain several independent workstreams. Extract those lanes first, then create one card per lane instead of bundling unrelated work into a single implementer card.
- **Run independent lanes in parallel.** If two cards do not need each other's output, leave them unlinked so the dispatcher can fan them out. Link only true data dependencies.
- **Never create dependent work as independent ready cards.** If a card must wait for another card, pass `parents=[...]` in the original `kanban_create` call. Do not create it first and link it later, and do not rely on prose like "wait for T1" inside the body.
- **If no specialist fits the available profiles, ask the user which profile to create or which existing profile to use.** Do not invent profile names; the dispatcher will silently drop unknown assignees.
- **Decompose, route, and summarize — that's the whole job.**
- **Do not file action cards against your own role.** Merges, PR closes and branch deletions belong in the orchestrator's own turn, where the measurement and the action sit together. A card assigned to the orchestrator profile runs as a *separate worker*, and this board has now seen such a card duplicate an action the orchestrator session performed live **repeatedly** — including a second card closing a PR the orchestrator had already closed, and one closing the *same* PR a third time. In every case the verdict happened to agree, so nothing broke — that is luck, not design. Worse, a card titled `merge PR X` can merge what you were still measuring. Route worker work to cards; keep board authority in your own turn. When such a card does arrive, the correct completion is "already executed; verified by state read, repeated nothing" — and prove it with the state fields (`state`, `mergedAt`, `closedAt`, `mergeCommit.oid`) rather than the handoff text. When a *worker* card also reports having closed a PR you closed, that is the same race from the other side; verify and move on rather than posting a second close comment.
  - **The mechanism is the completion notifier, not a bug in the worker.** When a parent card completes, the gateway wakes the *persistent* orchestrator agent with the handoff text — and that text often literally says "card t_xxx asks hscc-orch to merge #N and close #M". The orchestrator session acts on it within minutes, while the dispatcher is separately spawning `t_xxx`'s worker, so the worker arrives to find its own instructions already executed. Expect this on every self-assigned action card: check state first, and let "already done, verified" be a valid completion. Attribution note: `mergedBy`/`closedBy` cannot distinguish operator from agent, because every profile pushes under one `gh` identity — settle it from the gateway log (`kanban notifier: woke agent for …` + that session's env-cleanup timestamp) versus the dispatched worker's `owner_admitted` session id, not from the PR payload.

## Anchor on content, never on coordinates

- **Name a target by content, never by line number.** A brief that says "`*.sqlite` at `.gitignore:75-76`" is stale within hours: the sqlite block in this repo moved `75/76 -> 84/85 -> 87-94 -> 100-102` in one day, and every worker that followed a quoted line number had to rediscover the real location. State what to find ("the sqlite block in `.gitignore`") and require the worker to re-check at branch time. Shared index lists are the same hazard: three separate cards collided on one list in `docs/README.md` the same day.
- **A file's owner is written in its name.** `changelog.d/t_<id>.md` belongs to card `t_<id>`. Two cards were dispatched against one fragment because both briefs quoted the same sentence, and the collision was caught mid-run by a human rather than by the process. Before carding a wording or content fix, resolve the owning card from the filename; if that card is still open, hand the finding to it as a comment instead of creating a second card for the same file. `changelog.d/` is otherwise collision-free by construction — one file per card — so a collision always means the brief pointed at someone else's file.

## A cumulative count is not evidence about the present

**When a card's premise is "the log / audit trail / queue is dominated by X", re-measure it as a RATE back from the newest row before you accept the question it asks.** A file's total row count answers "what has ever happened"; a noise, churn or backlog question needs "what is happening now". The two can point in opposite directions, and the misleading one usually reads as more important: a waker audit trail of 9780 rows made the lane that was actually the *whole* live failure stream look like 2.6 % of it, while the six lanes the card blamed as the real problem were 2–19 days dead — four of them stopped the day a rate-limiter shipped, two stopped ~12 min after the operator's own mute, and most of them had working panes the whole time (their `ok=true` rows were in the same file).

The cheap shape that settles it, per subject, before any design work is dispatched:

- `count`, `first_ts`, **`last_ts`** and `rows/day while active` per subject;
- the same count restricted to the last 1 h / 6 h / 24 h / 7 d **measured back from the newest row**, not from wall-clock now;
- the inter-row gap census. Gaps clustering at one tick = a rate limiter is not engaged; gaps clustering in two buckets (tick + window) = it is, and the tick-bucket count per window is its real cost.

Then read the shipped code for the mechanism the numbers imply, and let the arithmetic decide: `3 × 86400 / (window + 2×tick)` predicted 270 rows/day/board against a measured 267.6 — a model that matches to 1.5 % means the residual rate is *designed cost*, not a broken knob, and the thing worth reporting is the code comment that overstates it, not a card. Corollary to the rule above: **an empty relation you walked is not evidence the thing is absent, and a big total you walked is not evidence the thing is present.** Both are properties of the file, not of the system's current state.

Same trap in the other direction: do not conclude a fix "is holding" from silence either. Silence can mean "nothing pending" rather than "the gate fired". Prove it from the component's own persisted state (the live state document, the report key, the watermark advancing) — if the skip record that a fired gate would leave behind is absent, the correct statement is *unproven in production*, with the named tripwire that would settle it, never "working".

## Prove absence by searching, not by walking edges

Before carding anything, confirm the work is not already carded by **searching the board by title and content** — not by reading the `children` list of the card that inspired it. An unparented card is a real card.

**The pre-flight is a SUBJECT match over the active-status pages, and it runs before the first `kanban_create` — an id-match search is not a dedupe.** Measured failure: an operator order naming four existing cards (`t_45fa38e2`, `t_d0a3ab8a`, `t_adf89feb`, `t_b384fd53`) as context was re-issued ~50 min after a complete six-card series had already been cut from that same order file. I resolved those four ids, found them, and concluded the series was uncarded — then created six duplicates. Checking *the ids a brief mentions* tells you what it references; it says nothing about whether the *subject* is already carded. So: take 2-3 distinctive nouns from the order ("main-only", "cluster surface", "never rm") and require them to return zero active rows. Enumerate by status (`list --status ready|blocked|todo`), never by the default priority-ordered page — a 200-row page can be all `done` rows and hide the live sibling series entirely. Prove the search pattern fires by asserting it against a row you know exists before you trust its empties.

This produced a live collision: a worker reported on `t_317a0f30` that they had filed the p313 stamp-environment gap "as its own card". I read that card's `children` (two entries, neither this one), concluded it was uncarded, and created my own. It had been filed — as `t_02c98915`, without a parent edge back. The dispatcher claimed both and two `@devops-engineer` workers were pointed at one environment gap. Same class as a zero-hit grep: **an empty relation you walked is not evidence that the thing does not exist**; it is evidence that the relation you walked was empty.

Corollary: when a worker's claim conflicts with your check, believe the claim first and go find the artifact. Here the claim was true and my query was under-scoped.

**The same trap applies to the LOCATOR pattern, not just the relation you walked.** Two ways to look up the thing you are hunting, and both return a clean empty that means nothing:

- `hermes kanban list` **truncates titles**, so a keyword grep over its output can fail to match even the card you are looking at by id. A grep for a word that is verbatim in a card's title returned zero rows — including that card. Before concluding a card is uncarded from a `list` grep, prove the pattern fires against a row you know exists, or resolve candidates by id with `show`.
- **Never locate a PR by an assumed branch-name prefix.** Worker-dispatched branches and operator/OpenCode-routed branches use different prefixes, so a loop over `wt/t_<id>`-style names silently finds nothing for the operator's own PRs. Enumerate `gh pr list --state all --json number,title,headRefName,baseRefName,state` and match on the card id or title text instead.

The compounding hazard: when the lookup comes back empty inside a measurement loop, the loop does not stop — it keeps going with an empty ref and prints confident numbers (`behind=2 CONFLICTS=0`) computed from a revision that does not exist. Assert the ref resolved and echo it in the same command that measures, and discard every number in that iteration if it did not.

## Verify the PR before you card against it

**A card whose verb is `close`/`merge`/`rebase`/`port` against a PR number is the most likely card on the board to be void before it is claimed — so check the PR at creation, not only at claim time.** These cards are written from a snapshot of a moving repo, and the operator cherry-picks and merges while the card sits in `ready`. Three such cards went stale on one board in a single afternoon:

- `t_44519305` "close PR #319 as superseded" was created at 18:23 local for a PR that had been closed at 16:28:26Z — **1h55m earlier** — with a 2500-char per-file closing comment and `--delete-branch`. The worker spent its whole run before noticing, and its only real output was the doc-residue audit the earlier run had skipped.
- `t_97fe7790` "port the projects widget" was created 90 seconds before the commit that shipped the port landed.
- `t_72129edc` "rebase #333" found its successor already merged while it ran.

**The pre-flight is three cheap reads, and two of those three incidents were caught by the first one alone.** Run it before `kanban_create`, and again if the card will not be claimed within minutes:

```
gh pr view <N> --repo <owner>/<repo> \
  --json state,closedAt,mergedAt,mergeable,headRefOid,headRefName
git ls-remote origin refs/heads/<head-branch>   # empty => branch already deleted
gh pr view <N> --json comments --jq '.comments|length'
```

Decide from what you read, not from the intent that prompted the card:

| Read | Meaning | Action |
|---|---|---|
| `state=CLOSED`, `mergedAt=null` | already retired | **do not create the card**; if a real gap remains (e.g. an audit nobody ran), card *that* gap and say the close is done |
| `state=CLOSED`, `mergedAt` set | already landed | do not create the card; nothing to merge |
| `state=MERGED` | already landed | do not create the card |
| head branch absent from `git ls-remote` | retired with `--delete-branch` | a rebase/close card is void; content survives at `refs/pull/<N>/head` |
| `mergeable=CONFLICTING` while `main` moved | the premise may have shipped by another route | prove it by blob before writing any instruction — see the volatile-card pitfall in Pitfalls below |
| comment count already large | a prior run already documented this verdict | your card must say what it adds beyond that comment |

**Put the read in the card body as a baseline line**, timestamped:

> `PR #319 as of 2026-11-17T18:20Z: state=CLOSED closedAt=16:28:26Z mergedAt=null mergeable=CONFLICTING head=wt/t_6d8731ab@223f863b branch_absent=yes ncomments=2`

That is not decoration — it is the worker's drift baseline. Without it the worker has no way to tell whether the card's premise still holds, and every worker so far had to re-derive it from scratch or trust a dead instruction.

### Stacked PRs: prevent the ghost, then test supersession correctly

**Merge stacked PRs strictly bottom-up.** A worker that bases PR B on PR A's *open* branch makes A's
content land inside B's squash-merge. A then keeps a `CONFLICTING` badge that reads as "rebase me" but
means "already landed", and any card written to rebase it is void on arrival — the conflict is two
sides adding the same lines in different context, the signature of landed content. If stacking is
unavoidable, merge the lower PR first or pre-commit to closing it as superseded; never rebase it.
Rebasing such a PR only clears the badge by deleting the branch's content, producing an empty PR with
a green badge.

**Three tests, in this order — and the second one always lies.**

1. Per path: `git rev-parse <head>:<path>` vs `git rev-parse origin/main:<path>`. Identical blob = landed.
2. `git merge-base --is-ancestor <pr-head> origin/main` returns **NO for every squashed stack**, because
   the replay carries new SHAs. A NO proves nothing; a YES is only reachable against the child's
   *branch tip*, which is normally deleted at merge. Never route to a rebase card on the strength of this.
3. When a shared file's blob differs, test **direction**, not equality: take the lines the PR added
   over its own merge-base and look each up in `main`'s copy. Match loosely (`grep -Fq` on a trimmed
   line), **not** exact whole-line equality — when `main` reflowed a `case` list, reindented a block, or
   reworded a comment, exact matching reports the *same* content as a real delta (it manufactured 31
   phantom lines on one PR, every one rewording, reformatting, or `main` simply being ahead). Then read
   each surviving line individually; never conclude "unique content" from a count.

**The reason to close can be quality, not just duplication.** `main`'s copy of a shared harness may
contain a rule and a strengthening that the stale branch never had — in which case merging the branch
would *regress* the gate. Establish which side is weaker before framing `mergeable` as something to
achieve.

**Assert verdicts and method, not coordinates of `main`.** A body that says "`main` is `36c80041`" is wrong within the hour; `t_44519305` named two different `main` SHAs (its body, then its correction comment) and **both** were historical by claim time. Write "`this content is on main`; prove it per-path with `git rev-parse <head>:<path>` vs `git rev-parse origin/main:<path>`, identical-blob = shipped" instead. Same reason as the "Anchor on content, never on coordinates" rule above, applied to repo state rather than line numbers.

**Re-assert any repo fact older than ~30 minutes at claim time.** If the card was created more than about half an hour before it ran, the claimant re-reads the facts the body asserts — `main` tip, PR state, branch presence — before touching anything. Card bodies are snapshots; the checks are not.

Cross-reference: the "`merge PR X` / `retire PR Y` card is the most volatile card on the board" pitfall is the **claim-time** half of this rule; this section is the **creation-time** half. Do both — one catches drift after you wrote the card, the other stops you writing a void one.

## Operator scope directives outrank your decomposition plan

When the operator says the board has drifted — gates about gates, docstring tense, roster and count wording, guards about guards — that is a stop-class directive on **card creation**, not a request to slow down:

- **Stop creating the named class.** The bar for reopening it is concrete: something is actually broken on `main` and you can name the breakage. "A gate could be stronger" is not breakage.
- **Finish what is running; spawn nothing from it.** A running or just-completed card in the banned class still gets its normal disposition (gate it, merge or close it) and nothing more. Anything that surfaces during it is a comment on that card, not a follow-up card.
- **Leave operator-picked-up cards parked.** When the operator pulls your dispatched cards into their own toolchain, the worker parks them with a block reason naming that handoff. The status nudge you then receive says "review/merge/unblock as needed" — an automated nudge does not override an explicit instruction to leave cards alone. Do not unblock, reassign, re-dispatch, or review and merge the PRs the operator runs from their own pane. Expect a whole wave to be taken, not one card.
- **Park the child too, the tick its last parent lands — if the operator said they will run it personally.** A parked card's children promote on their own when it reaches `done`, which is what you want for a chain you intend to run and a trap for one you do not: the instant the last parent completes, the child flips to `ready` and the dispatcher claims it in seconds, spawning a worker to do the operator's own task. A verbal "leave that one alone" has no effect on a `ready` card and the worker cannot read your chat with the operator. So on the notification that the final parent is done, run `hermes kanban schedule <child>` and post the hold **on the card** ("operator runs this personally; do not dispatch; all parents done") in the same turn — parking is the only thing that stops promotion, and doing it after the worker spawns is too late. Verify it stuck by reading the status back; a child left `ready` is a card you have not actually parked.
- **A capacity directive ("serialize ONLY these N cards at profile X's cap, hold every other ready card") is executed with `schedule`, and `running` cards are the deliberate exception.** Schedule every other `ready` card with the directive quoted in the reason; do NOT kill running cards — stopping a live worker destroys committed-but-unpushed work, so let it drain and say so in your reply. When you then close one of the priority cards (e.g. its gap already shipped), its children auto-promote to `ready` and ignore the hold — re-park every promoted child in the same turn and verify their statuses read back `scheduled`.
- **Archive stale blockers by proving them, in the same turn.** A card whose PR is closed and whose content is verifiably on `main` gets archived after the per-path blob check — not left `blocked` as a reminder — and the stale remote branch gets deleted alongside it. Prove, then archive; never archive on the card's own say-so.
- **Disposition an already-open PR in the banned class by closing it unmerged, on the PR, with the reason.** Keep its branch and quote the sha in the closing comment so the work is recoverable if the operator changes their mind; do not spend a rebase attempt on prose. Note that this is the *PR*, not a new card — closing it is board authority and belongs in your own turn.
- **"Don't touch their PRs" does not transfer `main`'s health to the operator.** After a wave of operator-run merges, re-run the repo's executed gates at the new tip and compare the *counts* to the previous tip (compile warnings, decode checks-over-fixtures, per-target file lists, coverage banners). A count that quietly moved is the only visible symptom of a merged wave that dropped fixtures or resurrected a target. Report drift as drift; do not restate the operator's own status line as verification.
- **Discover gate areas by enumeration, not by habit.** A new top-level app/target area arrives with its own gates in its own `scripts/`, and no CI lane, card, or existing habit runs them — a never-executed gate fails silently forever. After any wave that adds a top-level directory: `git ls-tree -r --name-only origin/main | grep -E 'scripts/.*_check\.sh$'`, subtract the directories you already gate, and run the remainder once to establish what passing looks like. Call a first measurement a first measurement — you cannot report a regression against a baseline you have never taken.
- **New release machinery gets a collision check against what you already maintain.** When a wave adds release scripts, grep them for the tap/repo identifiers you own and settle collision by *path*, not by name similarity (`Formula/<name>.rb` and `Casks/<other>.rb` in one tap coexist; similar names alone are not a conflict). Publish paths — notarization, tap writes through the contents API, page pushes — stay operator-run even once you have read the code; report what they touch rather than running them.
- **Never `git checkout` a revision in the primary checkout to run gates.** That directory is shared with the operator's own panes and tools, and every registered worktree shares its `.git`, so a detached HEAD left mid-review reads to them as a broken checkout. Gate in a throwaway `git worktree add --detach` instead. If you do check a revision out in place, restore in the *same* command (`git checkout main && git merge --ff-only origin/main`) and say in your reply that the window existed.

## Dispatcher respawn-guard pitfall (measured 2026-11-23)

A `ready` card can sit unspawnable forever while its lane is idle: the dispatcher's `active_pr` guard is a REGEX over the card's body+comments matching any `github.com/<org>/<repo>/pull/<N>` URL within 24h — it does not check PR state nor whether the card ever ran (proof: a zero-run card was guarded solely by a PR URL in its own body). Upstream issues: hermes-agent #29458, #80231, #85663. No sanctioned clear path exists (reclaim/reassign/repair/edit do not lift it; never hand-edit the board DB).

Recovery that works: create a TWIN card whose body/comments reference PRs only in prose (`PR #632`, never a raw URL), carry the original statement verbatim + why it was cloned, archive the poisoned original with an explanatory comment, verify with `dispatch --dry-run` (twin appears, `Guarded (active_pr)` gone). Diagnose first with: `dispatch --dry-run | grep Guarded`, then `runs <id>` (zero runs = text-poison, not stale-PR) and grep the card text for pull URLs.

Prevention: when authoring cards or long comments, write `PR #N (repo)` — the guard's regex only matches full URLs.

**`dispatch --dry-run` lists the ELIGIBILITY pool, not spawns.** Its `Spawned: N` ignores the fleet-wide and per-profile caps, so a long list is eligibility, not intent to run. When a lane sits ready with zero running: run the dry-run to rule out `Guarded` events, then count running across ALL boards against the global cap — slot pressure self-clears and is not a stall, and the lever is priority, never a hand-dispatch. Set it with `hermes kanban edit <id> --priority N`; `show` has no priority field, so verify from the `reprioritized {'priority': N}` event-log entry, and state the intended ordering in a comment on each affected card so the board reads intentional rather than stalled.

## Local-green can be a dev-machine artifact (measured 2026-11-23)

On this operator Mac, a LIVE proxy answers :4000 (and real hermes trees are importable). New external calls added to daemon hot paths (probes, POSTs) pass unstubbed locally and flip existing tests' verdicts ONLY on CI runners where nothing is bound. Measured case: PR #642's completion probe — orch's full daemon leg (4892 green) passed precisely because the live proxy answered the unstubbed probe; CI's runner showed 10 pre-existing verdict tests turning UNHEALTHY. Gate rule: for health/proxy-surface changes, do not treat local green as sufficient witness — require the module's tests to pass with the port FREE (or trust only the fresh CI legs at the exact head), and when local-green + CI-red on a check_proxy-adjacent diff, suspect missing stubs for the NEW external before suspecting the runner.

The mirror case is equally real (measured 2026-11-23, PR #645): running `hscc-cluster/` combined with `hscc_daemon/tests/test_plugin_manifest_drift.py` outside the sanctioned per-leg harness yields 5 failed + 2 errors (fence/fleet modules are order-sensitive) on CLEAN MAIN TOO. NEVER hold a green PR on self-inflicted red: reproduce the failing command on a clean-main worktree FIRST — an identical failure set on main is my invocation's fault, not the PR's; then run the PR's own modules standalone for the real verdict. A second artifact of the same harness: plugin test modules using the `import __init__ as plugin` idiom depend on the per-leg sys.path — one pytest invocation carrying test files from TWO different plugins' `tests/` dirs raises a spurious collection ImportError even though each leg passes alone. Run each plugin's tests from its own directory (leg-style) before suspecting the PR, and when a DELASSERTS grep is high, read the hunk: removed pins that return through parametrized parity arms are re-parenting (compare the ADDED-assert count and check the risky site), not weakening. **The leg-style choice is decided by reading the test module's import lines first, because the two idioms need OPPOSITE cwds:** a module using `import __init__ as plugin` must run from INSIDE the module dir, while a module importing a SIBLING top-level package (`from hscc_daemon import …` from a cluster test) must run from the REPO ROOT — the inside-dir run of the latter dies with `ModuleNotFoundError: No module named '<sibling>'` on every test that imports it. A first standalone run failing that way is your cwd, not the PR: re-run from the other cwd before opening any failure, and state the correction in the gate record if you reported the artifact first.

## Re-check verdicts before dispatching cards cut from a pre-verdict list

Cards minted from an audit's findings section BEFORE operator sign-off can directly contradict the signed verdicts (measured: my `kanban_paused` tool card, cut ~2h before the operator signed D6 "kanban pause/resume/paused/enforce = CLI-only. Yes."). After processing any sign-off, sweep every still-ready card from the pre-verdict list against the verdict section before dispatch; a card that ADDS a surface must have its verb's surface rows read — "no route" does not mean "CLI-only" (they are different verdicts). When a worker blocks on such a contradiction: the operator's signed word governs (adopt the supersede option as default); the override option stays operator-only, fully scoped on the card so a flip is one card away. Rescope downstream cards (drift checks, QA counts) in the same pass, or they become permanent false positives.

## Decomposition playbook

### Step 1 — Understand the goal

Ask clarifying questions if the goal is ambiguous. Cheap to ask; expensive to spawn the wrong fleet.

### Step 2 — Sketch the task graph

Before creating anything, draft the graph out loud (in your response to the user). Treat every concrete workstream as a candidate card:

1. Extract the lanes from the request.
2. Map each lane to one of the profiles you discovered in Step 0. If a lane doesn't fit any existing profile, ask the user which to use or create.
3. Decide whether each lane is independent or gated by another lane.
4. Create independent lanes as parallel cards with no parent links.
5. Create synthesis/review/integration cards with parent links to the lanes they depend on. A child created with unfinished parents starts in `todo`; the dispatcher promotes it to `ready` only after every parent is done.

Examples of prompts that should fan out (using placeholder profile names — substitute whatever exists on the user's setup):

- "Build an app" → one card to a design-oriented profile for product/UI direction, one or two cards to engineering profiles for implementation, plus a later integration/review card if the user has a reviewer profile.
- "Fix blockers and check model variants" → one implementation card for the blocker fixes plus one discovery/research card for config/source verification. A final reviewer card can depend on both.
- "Research docs and implement" → a docs-research card can run in parallel with a codebase-discovery card; implementation waits only if it truly needs those findings.
- "Analyze this screenshot and find the related code" → one card to a vision-capable profile for the visual analysis while another searches the codebase.

Words like "also," "finally," or "and" do not automatically imply a dependency. They often mean "make sure this is covered before reporting back." Only link tasks when one card cannot start until another card's output exists.

Show the graph to the user before creating cards. Let them correct it — including which actual profile name should own each lane.

### Step 3 — Create tasks and link

Use the profile names from Step 0. The example below uses placeholders `<profile-A>`, `<profile-B>`, `<profile-C>` — replace them with what the user actually has.

```python
t1 = kanban_create(
    title="research: Postgres cost vs current",
    assignee="<profile-A>",  # whichever profile handles research on this setup
    body="Compare estimated infrastructure costs, migration costs, and ongoing ops costs over a 3-year window. Sources: AWS/GCP pricing, team time estimates, current Postgres bills from peers.",
    tenant=os.environ.get("HERMES_TENANT"),
)["task_id"]

t2 = kanban_create(
    title="research: Postgres performance vs current",
    assignee="<profile-A>",  # same profile, run in parallel
    body="Compare query latency, throughput, and scaling characteristics at our expected data volume (~500GB, 10k QPS peak). Sources: benchmark papers, public case studies, pgbench results if easy.",
)["task_id"]

t3 = kanban_create(
    title="synthesize migration recommendation",
    assignee="<profile-B>",  # whichever profile does synthesis/analysis
    body="Read the findings from T1 (cost) and T2 (performance). Produce a 1-page recommendation with explicit trade-offs and a go/no-go call.",
    parents=[t1, t2],
)["task_id"]

t4 = kanban_create(
    title="draft decision memo",
    assignee="<profile-C>",  # whichever profile drafts user-facing prose
    body="Turn the analyst's recommendation into a 2-page memo for the CTO. Match the tone of previous decision memos in the team's knowledge base.",
    parents=[t3],
)["task_id"]
```

`parents=[...]` gates promotion — children stay in `todo` until every parent reaches `done`, then auto-promote to `ready`. No manual coordination needed; the dispatcher and dependency engine handle it.

**Card-body boilerplate (HSCC repo):** every card body that ships a code or
docs change should carry one line: `Changelog: add changelog.d/<task-id>.md —
NEVER edit CHANGELOG.md (its [Unreleased] block is generated; see
changelog.d/README.md).` This keeps the no-touch rule in front of every worker
at authoring time, which is what prevents the old Unreleased/Fixed merge
collisions from coming back. Likewise, every card whose handoff says "open a PR" must
name the base: `gh pr create --base main` (or the project's integration branch) —
workers left to choose their own base pick sibling worker branches, which strands
the merge off main.

If the task graph has dependencies, create the parent cards first, capture their returned ids, and include those ids in the child card's `parents` list during the child `kanban_create` call. Avoid creating all cards in parallel and linking them afterward; that creates a window where the dispatcher can claim a child before its inputs exist.

**Pass `idempotency_key` on every card in a fan-out wave — an operator order can be re-delivered to you verbatim, and a duplicate wave is far worse than a skipped one.** `kanban_create` accepts `idempotency_key`; if a non-archived card with that key already exists it returns that card's id instead of creating a duplicate. This is not theoretical: a session can receive the same imperative many times in a row (a stuck/automated trigger, or a re-delivered directive) and a wave that is created on each delivery multiplies cards, assignees and dispatcher spawns against one intent. Key by series + slot (`main-only-surface-2-bootstrap`, `...-3-install-skills`), so re-running the whole order is a no-op that returns the existing ids. Verify the ids you report are the ones the tool actually returned — a key hit returns the pre-existing id, which is the correct answer but not a new card.

**A human-review gate is a `blocked` child of an `ready` gate card — two mechanisms, not one.** When the operator wants cards 2-N to run only after they personally review card 1's artifact, create card 1 `ready` (dispatchable now) and each child with `parents=[card1]` AND `initial_status="blocked"`. The parent edge encodes the artifact dependency; `blocked` encodes the human step and, unlike `todo`, never auto-promotes — so card 1 completing does NOT release five workers onto unreviewed assumptions. The block reason and body must name who unblocks ("operator reviews the doc, then unblocks"), because `blocked` is reserved for a named human step. If you instead left the children `todo`+parents, they would fire the instant card 1 hit `done` and the review would be decorative.

### Step 4 — Complete your own task

If you were spawned as a task yourself (e.g. a planner profile was assigned `T0: "investigate Postgres migration"`), mark it done with a summary of what you created:

```python
kanban_complete(
    summary="decomposed into T1-T4: 2 research lanes in parallel, 1 synthesis on their outputs, 1 prose draft on the recommendation",
    metadata={
        "task_graph": {
            "T1": {"assignee": "<profile-A>", "parents": []},
            "T2": {"assignee": "<profile-A>", "parents": []},
            "T3": {"assignee": "<profile-B>", "parents": ["T1", "T2"]},
            "T4": {"assignee": "<profile-C>", "parents": ["T3"]},
        },
    },
)
```

### Step 5 — Report back to the user

Tell them what you created in plain prose, naming the actual profiles you used:

> I've queued 4 tasks:
> - **T1** (`<profile-A>`): cost comparison
> - **T2** (`<profile-A>`): performance comparison, in parallel with T1
> - **T3** (`<profile-B>`): synthesizes T1 + T2 into a recommendation
> - **T4** (`<profile-C>`): turns T3 into a CTO memo
>
> The dispatcher will pick up T1 and T2 now. T3 starts when both finish. You'll get a gateway ping when T4 completes. Use the dashboard or `hermes kanban tail <id>` to follow along.

**Placeholder card ids before creation.** Plan comments and card bodies take card
ids only after `kanban_create` returned them — see
`references/placeholder-ids-and-parking.md`. Order matters under pressure: when a
review rejects and needs a successor, **mint the successor first, then write the
reject comment** — an id named before it exists is permanent in an append-only
comment unless you post a correction on the card that carries it.

**`todo`+parents auto-promotes; `blocked` parks.** A card created with
`parents=[...]` and the default status promotes to `ready` (dispatcher claims it)
the moment its parents finish. `initial_status="blocked"` never auto-promotes.
Pick deliberately: an OpenCode/human-routed card must be `blocked`, and a
parentless card created with the default status is `ready` immediately.

## Common patterns

**Fan-out + fan-in (research → synthesize):** N research-style cards with no parents, one synthesis card with all of them as parents.

**Parallel implementation + validation:** one implementer card makes the change while one explorer/researcher card verifies config, docs, or source mapping. A reviewer card can depend on both. Do not make the implementer own unrelated verification just because the user mentioned both in one sentence.

**Pipeline with gates:** `planner → implementer → reviewer`. Each stage's `parents=[previous_task]`. Reviewer blocks or completes; if reviewer blocks, the operator unblocks with feedback and respawns.

**Same-profile queue:** N tasks, all assigned to the same profile, no dependencies between them. Dispatcher serializes — that profile processes them in priority order, accumulating experience in its own memory.

**Human-in-the-loop:** Any task can `kanban_block()` to wait for input. Dispatcher respawns after `/unblock`. The comment thread carries the full context.

## Pitfalls

Measured pitfalls that destroyed real work are in `references/pitfalls-card-writes.md` — `edit --body` blanking a card body on an empty shell substitution, and re-minting an operator-archived card. Read it before any bulk card edit.

**Inventing profile names that don't exist.** The dispatcher silently fails to spawn unknown assignees — the card just sits in `ready` forever. Always assign to a profile from your Step 0 discovery; ask the user if you're unsure.

**Bundling independent lanes into one card.** If the user asks for two independent outcomes, create two cards. Example: "fix blockers and check model variants" is not one fixer task; create a fixer/engineer card for the fixes and an explorer/researcher card for the variant check, then optionally gate review on both.

**Over-linking because of wording.** "Finally check X" may still be parallel with implementation if X is static config, docs, or source discovery. Link it after implementation only when the check depends on the implementation result.

**Forgetting dependency links.** If the task graph says `research -> implement -> review`, do not create all tasks as independent ready cards. Use parent links so implement/review cannot run before their inputs exist.

**A child you create `initial_status="blocked"` with no `parents` is a parked fix hiding under a parent you are about to close.** `blocked` never auto-promotes, so when the parent completes the child stays parked indefinitely and the defect you just carded stays live — while the parent reads `done`, so the board shows nothing wrong and the operator is the one who finds it. This happened four times in one day on one board (a health-handler pair, a waker fix, a merge-queue runner pair) despite this skill already saying `todo`+parents promotes and `blocked` parks — stating the mechanic is not the same as stating the obligation. The obligation: **never complete a parent while any card you created from it is parked.** Before `kanban_complete`, enumerate the cards you created in this run and give each one either `parents=[<parent-id>]` in its creating call, or a `kanban_link` in the same turn. `blocked` is reserved for a named human step or an operator/OpenCode-run lane, and the block reason must say who unblocks it. The mechanical check is a re-read of each new card asserting `parents` is non-empty — an empty `children` list on the parent is a symptom you already missed, not a diagnosis, and "I will wire it when I get the nudge" is how the card got left parked. **Verify a child is actually parked before "fixing" it, even when the parked diagnosis arrives from a sweep or another agent — their status summary is a claim, not a measurement.** Read the child's `parents`/`unsatisfied_parents` and its `created` event first: a `todo` child with `unsatisfied_parents` is the *correct* gated state, not a parked one, and unblocking or re-linking it is at best a no-op you will then report as a repair and at worst releases a card someone deliberately gated. Only `blocked`, or `todo` with empty `parents`, is parked.

**Two cards that touch the same file are a dependency, even when their features are unrelated.** Feature lanes that share a hot file — a build manifest with per-target file lists, a shared models file, a generated index — are not independent, and fan-out by feature alone hides the edge. I dispatched a 20-card wave from one base with no edges between the cards editing the same manifest and models file; the strip cards then deleted files on `main` serially, and five of those branches came back stale, several silently re-adding code a merged sibling had deleted. Either serialise shared-file cards with `parents=[...]`, or budget for a rebase rail. Expect stacking on top of it: a worker that branches off a *sibling's* branch re-derives that sibling's work, and when both merge, the earlier PR is left with nothing to deliver and must be closed as superseded rather than rebased.

**Reassignment vs. new task.** If a reviewer blocks with "needs changes," create a NEW task linked from the reviewer's task — don't re-run the same task with a stern look. The new task is assigned to the original implementer profile, and its body must say **build on the rejected branch/PR, do not restart from scratch** — name which parts of the rejected attempt are correct and stay, and which measured defects the successor closes. A green part of a rejected attempt re-derived from zero is wasted work and risks a different regression.

**A CI gate can be advisory, which makes every "the suite passed" claim on the board a claim about a signal that does not exist — scan annotations, never conclusions.** On hscc, `pytest-legs.yml` runs all 12 legs and `leg_verdict.sh` turns a failed leg into `::warning::a test leg is red (advisory: HSCC_TESTS_ENFORCE is not set to true)` + `exit 0` unless the repo *variable* is truthy. Measured: a full-roster push run on `main` reported **all 12 jobs `success` while 3 of them were red** — and those three legs had been red across four+ consecutive rosters, so two PRs had merged over them unnoticed. The instrument is per-check-run annotations, not job status, and it costs one loop:

```
gh api "repos/<o>/<r>/commits/<sha>/check-runs" \
  --jq '.check_runs[] | select(.name|startswith("leg ")) | .id' \
  | while read -r id; do gh api "repos/<o>/<r>/check-runs/$id/annotations" \
      --jq '.[] | select(.message|test("test leg is red")) | "RED: \(.message)"'; done
```

Apply it before executing any card whose ask is "flip enforcement / require the check / the gate is green". Two consequences for the ask itself: (1) a policy card written weeks ago can be **stale in the direction that matters** — this card named one red (already fixed) and the real answer was three other reds, so executing its ask literally would have frozen every PR in the repo; re-measure the precondition, don't inherit it. (2) an ask can be **mechanically wrong** and still be the right decision: it said "require the `leg *` checks", but the matrix runs only the legs a PR's paths can break (measured 2 of 12 on one PR), so a wildcard requirement leaves ten checks permanently "expected" and nothing ever merges. Require the repo's own aggregation check instead — find it by reading the workflow's comments and the verdict job, and write the wedge reasoning into the successor card so nobody re-literalises the ask.

**A red-on-main fix card's premise can die mid-flight — re-measure main at gate time and frame the merge accordingly.** A sibling/successor PR can turn the card's "red" green before you review it. If main measures green when you gate: a test-only delta is coverage armor — merge it and state on the card "premise stale at merge; merged as hardening, not a red-fix"; a behaviour delta that now fixes nothing gets closed superseded citing the commit that fixed it. Never let a merged card keep the title "RED ON MAIN fixed" when there was no red at merge time — and when several cards chase one defect, land the single one whose acceptance you re-measured yourself and retire the rest (superseded/archived) with a pointer to the survivor.

**A red roster run on `main` is a triage target before it is a card — measure flaky-vs-real first, then read the failing source.** Two measurements settle the class cheaply: (1) run the same leg twice locally at the same sha and compare the **collected test count**, not just pass/fail — a count that MOVES between runs of identical code means environment-dependent parametrization (a host tool misbehaving shrinks or grows the parameter set; its crash report often prints in the run output), i.e. the machine flaked, not the code; with CI legs annotation-zero plus two clean re-runs that is the basis to merge what's in flight and card the machine-state hermeticity cause separately instead of halting. (2) When the failure IS deterministic, open the shipped source before writing the card: the failure signature usually names the real defect — a SIGTERM-shutdown test failing with `rc=-15` is the "handler not yet installed" signature, and a readiness probe that polls the PORT is complicit (the kernel completes handshakes off the listen backlog before the child is scheduled), so the durable fix is register-handlers-before-listen plus a readiness line that means "handler installed", not a sleep or retry patch. Card the root cause with `file:line` anchors for both consumers of the shared builder, never the symptom. (3) Before accepting a "CI-only red" label (red on the runner, green locally), compare the **wall-clock times** of the green local runs against the red CI runs and grep the failing test for absolute datetime anchors (a `_T0 = <today>` fixture constant). A fixture anchored to a real-world instant goes red deterministically for EVERYONE once wall time passes it — the "green locally" observations were simply taken before the anchor passed, and the flaky-vs-real split above will call it deterministic-but-impossible. The durable fix is relative timing plus assertions made against the real clock (`at(-1)` past-due, `+1h` held) so the file cannot rot again; a red that flips at a known hour is a clock bomb, not machine state.

**You cannot reassign a card from inside a dispatched worker run — route by successor card instead.** When the operator hands a card to the orchestrator's own profile ("fix the plist drift" assigned to `hscc-orch`), the routing verb the Recovery section advertises is unavailable twice over: the `kanban_*` tool surface has **no assign/reassign action at all**, and the CLI refuses every mutation with `delegate_task child contexts cannot mutate Kanban tasks via the CLI` (reads like `show`/`stats` still work, so the failure looks selective, not structural). Three wrong exits: implementing it yourself (breaks the role rule), blocking on `needs_input` so a human runs one CLI command (idle work), and completing the card while the work is undone (false `done`). The right exit is a **successor card**: `kanban_create` the task with the specialist as assignee and a fully standalone body, then complete the original as a routing record whose summary opens `ROUTED, NOT FIXED — no repo change made, the defect is still on main`, naming both successor ids in `metadata`.

- **Create the successor FIRST, then the follow-up card with `parents=[<successor-id>]` in the creating call** — never parent the follow-up to your own routing card. It reads as correct (the guard descends from the drift report) but the routing card completes in minutes while the successor's PR takes hours, so the follow-up promotes before the code it asserts on exists. Adding the successor as a *second* parent afterwards patches it, and it is only safe because the routing card was still `running` at the time; do the ordering instead.
- **The tool gap is worth reporting upstream** with the evidence: the smallest addition is an assign/reassign action (with reclaim) on the `kanban_*` surface, which would make this a one-call routing move.

**Argument order for links.** `kanban_link(parent_id=..., child_id=...)` — parent first. Mixing them up demotes the wrong task to `todo`.

**To change orchestrator behaviour, pick the artifact by REACH, not by tidiness — the skill is not the live surface.** Measured from `hscc-roles/orch_sync.py`: `compose()` rewrites every live `<project>-orch` SOUL's `## Operational` + `## How you manage work` tail from `generator.py::_PROJECT_ORCH_OPS` on sync (the `## Role:` section is kept byte-for-byte), while `sync_profile` copies a shared skill into a profile **only when that profile lacks it by name** — "Nothing is ever removed or overwritten." So: editing `install/hscc-skills/**` changes only profiles created from now on and no running orchestrator; hand-editing a live SOUL.md's ops block is worthless because the next sync overwrites it; editing your **own** live skill copy is the safe immediate mutation, precisely because sync never touches it. Route the change accordingly — skill for behaviour now, template for behaviour fleet-wide, and never a live-SOUL hand-edit. Then check the drift actually exists before trusting a `version:` string: `diff` the repo skill against your live one and count the `^<` lines (repo-only). I found both copies claiming `3.0.0` while the live one carried 14 extra lesson-blocks, and simultaneously the live SOUL lagging its own template — drift runs in opposite directions on the two surfaces, so neither file can be assumed canonical.

**You cannot attach a file to another worker's card** (`kanban_attach` refuses: "worker is scoped to task …") — hand off a file's contents with `kanban_comment` instead, or list absolute paths in your own `kanban_complete(artifacts=[…])`, which uploads them from disk.

**A dispatched orchestrator run is scoped to its own card for every mutation — decisions for other cards go by comment, not by completion.** `kanban_complete` on a card you did not claim refuses with `worker is scoped to task t_<yours>; refusing to mutate t_<theirs>. Use kanban_comment to hand off information to other tasks, or kanban_create to spawn follow-up work` — this applies even when the routing rule says the decision is *yours* to make (an unsettled design card gating a chain is squarely yours). The correct move is exactly what the error says: write the full, decision-complete ruling as a `kanban_comment` on that card (so the next reader can implement from it without your context), record in your own completion `metadata` that the ruling exists and that the card still needs its completion applied by a session scoped to it. Do not `kanban_block` your own card waiting to fix the other one, and do not treat the refusal as a tool bug — the scoping is deliberate. Check `unsatisfied_parents` after commenting: a decision comment on a `ready` parent does nothing for its children until that parent reaches `done`, so say which session owes the transition.

**A second session of your own profile can be working the same board while you run — re-read before you mutate, and do not repeat a move you find already made.** Measured: mid-review, `t_0f0a433f` was archived and deduped into a richer card, and a "content is on main" gate comment appeared, both authored by `hscc-orch` with timestamps inside my own run and neither written by me. So before any mutation on a card you do not own, `kanban_show` it and compare the event history against your run's start; if it changed under you, stop and record the state in your completion `metadata` instead of deduping/archiving it a second time. Two orchestrators deduping one seam is how a real card gets archived into a duplicate.

**Don't pre-create the whole graph if the shape depends on intermediate findings.** If T3's structure depends on what T1 and T2 find, let T3 exist as a "synthesize findings" task whose own first step is to read parent handoffs and plan the rest. Orchestrators can spawn orchestrators.

**Tenant inheritance.** If `HERMES_TENANT` is set in your env, pass `tenant=os.environ.get("HERMES_TENANT")` on every `kanban_create` call so child tasks stay in the same namespace.

**Creating a card on the wrong board is silent, and there is no move verb.** Pass
`board="<slug>"` explicitly on every `kanban_create` / `kanban_comment` / `kanban_link` /
`kanban_complete` / `kanban_show` call. Make the verification **mechanical, not remembered**: the tool
call immediately following every `kanban_create` must be `show --board <slug> <id>`. This trap has bitten
the orchestrator that wrote this very pitfall — on a wave, and again on a single release-blocker card
minutes later — because the reply to the requester feels like the deliverable and the show is the step
that gets skipped. Note the symptom that misleads: a wrong-board `kanban_comment` or `kanban_show` fails with
**`unknown task` / `no such task`**, which reads as "this card was never created" and invites you to
re-create a card that is alive and running on another board. Before concluding a card does not exist,
re-issue the same call with the board you *meant*. The create returns `ok: true` and never echoes the board, so nothing tells you at the
moment you got it wrong. The default board comes from a `current` pointer that can name a
*different project's* board for weeks and quietly absorb a whole wave; **it can also come from
`HERMES_KANBAN_DB`/`HERMES_KANBAN_BOARD` inherited into your session env** — measured: an entire
17-card linked wave landed on another project's board this way, with parents and a dispatcher already
running workers on it. The board you are discussing with the operator is never implied by your
session. **Do not report a wave as queued or dispatched until every card resolves with `show` on the
intended board** — reciting returned ids is not evidence they landed on your board.

Cross-board parents are impossible (per-board DB), so the recovery split is by dispatcher claim:
unclaimed cards -> comment + `archive` on the wrong board (post the reason with `comment` first — `hermes kanban archive` takes no `--reason` and errors on one) and recreate on the right one with parents
where intra-board; running/done cards stay where they are — never duplicate a claimed card, its PR
lands in the same repo either way. A recreated card whose dependency stayed live on the wrong board
carries a **block-if-missing line** in its body ("if <module> is not on origin/main when you start,
kanban_block kind=dependency naming it"), because the worker cannot verify cross-board state. When
such a card blocks claiming the dependency "does not exist on board X", treat that as a read-block
artifact until you have checked that board yourself — workers have blocked on a dependency whose PR
was in fact open on the other board. Post the ground truth (branch, PR number, state, when you will
unblock) as a comment on the blocked card so the chain reads without guessing, and unblock the tick
the dependency merges. Misroute depth (one card vs a linked graph): `references/wrong-board-cards.md`.

**The same env leak can poison the orchestrator's own persistent shell.** A worker run can export
variables (measured: `HOME=0`) that survive into your long-lived pane, and HOME-keyed credential
lookups then fail mid-chain — gh reports "not logged into any GitHub hosts" while its token still
sits in the keychain. When a tool that worked minutes ago dies with a login/auth error inside a
multi-step chain, diagnose the shell before re-authenticating: `echo $HOME` (and `env | grep -iE
'^GH_TOKEN|^GITHUB_TOKEN'`), restore the correct value with `export`, and retry the same command.
Re-login or re-entering credentials is the wrong fix — the credential was never the problem.

**A parentless card is `ready` the instant it exists** — the dispatcher can claim it within seconds,
so a graph built as `create` then `link` (e.g. during a move) briefly exposes every card. Always pass
parents in the creating call. If you must link after, expect to `reclaim` cards the dispatcher already
grabbed, and reclaim alone is not enough on a card still in `ready`: the dispatcher re-spawns, so
close or gate it in the same breath. Once a child is *claimed*, `kanban_link` refuses it outright
(`child is already running`) and there is no way to gate it retroactively — the only recovery is a
comment instructing the worker to `block kind=dependency` if the parent's artifact is not on
`origin/main` yet, so it refuses to invent the parent's interface rather than guessing a schema that
will silently mismatch. Same channel applies to changing an in-flight card's spec: **amend a running
card by comment**, because a body edit never reaches a worker that is already running. Do not treat
"the chain is running ungated" as recoverable after the fact — put `parents` in the creating call.

**`gh pr close` on an already-closed PR is a silent no-op that swallows your closing comment.** It exits `0` and prints "already closed" — posting nothing, including the `-c` comment. When that comment is the actual deliverable (an audit trail another agent must find), post it separately with `gh pr comment` and confirm the returned issue-comment URL.

**A green `legs` conclusion computed before `main` moved is stale — never merge on it under an
enforced gate.** When the base moves, GitHub recomputes checks against the merge ref, but until the
new run completes `mergeStateStatus` can read `CLEAN` while `statusCheckRollup` still prints the OLD
`SUCCESS` — a conclusion reported seconds after the base moved cannot be a completed fresh run; it is
a ghost of the previous base. Under `strict=false` protection that stale green passes the API gate and
your merge lands content never measured jointly. When enforcement (`enforce_admins=true`, required
aggregation check) is live, the cost is amplified: one red you merge onto `main` jams every subsequent
merge until it's fixed. **GitHub does NOT re-run a PR's checks when the base moves** (measured: a
~20-minute poller watched one stale `completed/success` check-run that never changed), so do not wait
for a fresh run that will never come. Procedure when `main` moved after the PR's legs last ran:
MEASURE THE UNION locally — `git worktree add --detach $D origin/main && cd $D && git fetch origin
<head-branch> && git merge --no-ff --no-commit FETCH_HEAD`, then run the affected leg(s) there. That
measures exactly the content the squash-merge will land, in minutes, and it also proves the merge
applies without conflict; merge on that green. The converse move also exists: when a PR head's leg is red
because of something on `main` (root cause outside the PR's diff), you do not need the worker to push
anything — `gh run rerun <run_id> --failed` re-evaluates the PR's checks against the merge ref on the
fixed base. Assert the run id resolved before calling it; `gh run rerun ""` fails silently and you
will believe the re-validation is running.

**Check `baseRefName` before you squash-merge any worker PR.** A worker that branched from a
sibling card's branch can open its PR with `base=wt/t_<sibling>` instead of the integration branch;
`gh pr merge` then exits 0 and the content lands on the dead worker branch — main never moves, and
the card would read `done` with its deliverable stranded. Gate review with
`gh pr view <n> --json baseRefName,mergeable,state,isDraft` and require `base` == the intended branch
and NOT draft — `gh pr merge` refuses a draft while the rest of your `&&`-chain prints the UNCHANGED
`origin/main` tip as if it were new, and a claim posted off that echo is false and must be retracted
on the card in the same turn. A PR can also flip to draft AFTER its legs passed (the draft-residue
flip class): when the merge refuses on `isDraft=true`, re-read the state, `gh pr ready <n>`, re-verify
`mergeStateStatus` and the legs at the same head, then merge — and record the flip on the card, so a
reader doesn't conclude the green evidence was invalid; the cheapest behavioural post-check is a sha delta: capture
`BEFORE=$(git rev-parse origin/main)` in the same command before the merge and require `AFTER != BEFORE`
before saying ON MAIN anywhere;
if it differs, do not merge that PR. Recovery once it has already merged to a stale base: never
rebase — open a fresh PR from the same surviving head against the intended branch, prove the
three-way merge adds only the PR's own files
(`T=$(git merge-tree --write-tree <intended> <head> | head -1); git diff --name-only <intended> $T`
lists exactly the PR's files, no CONFLICT), squash-merge the fresh PR, leave the original as
history (merged-to-stale-base, superseded), and delete the stale branches only after
`gh pr list --base <branch>` proves zero PRs remain on either. This is what the post-merge
behavioural check catches — a merge that exits 0 without moving `origin/main` is not a merge.

**The merge queue is live state — re-measure before you mention it, and disclose every pause's reason the same tick.** Never say "the queue holds #N/#M" from the memory of the turn you enqueued; re-read `hscc merge-queue status` (grep your project's line: `queued/waiting/in-flight/last`) in the same turn as the claim. Once the queue shows a PR as `in-flight` or `waiting`, **stop hand-merging it — racing the queue is how double-lands happen**; instead verify your own fresh legs at the exact head, then watch for LANDING by content markers on main (`git show origin/main:<file> | grep -c '<symbol only that PR adds>'` in a poller), never by queue bookkeeping — `last=` values are history and `queued=0` proves nothing about whether your PR landed. A queue that self-paused on a red-gate records the cause only in its internal state (`last=red-gate` is the forensic clue) and surfaces nothing to the board — an operator-ordered landing then sits silently blocked, which the operator counts against the orchestrator who reported a healthy-sounding queue. The rule: any pause, red-gate, or resume you discover or cause gets a one-line reason comment on the release/gate card the tick you learn of it, whether or not you caused it. While items wait, prefer `merge-queue add <project> <pr>` over a direct `gh pr merge` whenever the PR's required aggregation check has not reported yet (`mergeStateStatus=BLOCKED` with no legs check is the gate working, not an incident). Don't trust rollup fields to prove either outcome: `statusCheckRollup` can print `NO-LEGS` for a PR that did merge — settle landed-vs-waiting by reading `state,mergedAt,mergeCommit.oid` back. A jam read as `in-flight=<pr> last=red-gate` can be a **stale gate verdict**: the red may have been measured against a since-superseded `main` (a repo-wide red leg, fixed later, reddens every queue-gated PR at once). Re-measure the stuck PR against current `main`; if its own content and legs are clean, `hscc merge-queue remove <project> <pr>` (the project argument is required — bare `remove <pr>` errors) and land it directly where a standing directive orders it, saying on the card that the red-gate was stale so the record reads as history, not an unresolved gate.

**`gh pr merge --delete-branch` can close a SIBLING PR.** Chain children often use their parent's
branch as the PR base (`--base wt/t_<parent>`), so squash-merging the parent with `--delete-branch`
deletes that base and GitHub auto-closes every PR based on it. `gh pr reopen` then FAILS (base gone);
the only repair is `gh pr create --base main --head <surviving child branch>` reusing the original
title/body, followed by `git rebase --onto origin/main <last-parent-commit>` on the child branch so
it stops re-submitting the parent's commits and files (a squash-merged parent is content in `main` but
not history, so the new PR will otherwise show CONFLICTING). Before deleting any merged branch, check
`gh pr list --base <branch>`; when in doubt merge without `--delete-branch`. Re-pointing a child afterwards is not available: `gh pr edit <n> --base main` refuses with "Cannot change the base branch of a closed pull request", so re-point every child's base **before** the parent merge, in the same turn. If you find out late, do not reopen — leave the child's own branch in place with its sha recorded, then either open a fresh `--base main` PR reusing the title/body or drop it, and say which you chose and why.

**A `merge PR X / retire PR Y` card is the most volatile card on the board — re-verify it before you touch it, because a sibling card may already have shipped the work.** These cards are created off a snapshot of `main`, and they sit in `ready` while other cards keep landing. Twice in one day the card's premise died before I claimed it: a squash-merged sibling PR bundled the *same* work into its own branch and shipped it, so the PR the card told me to merge had nothing left to deliver — and `main` had moved twice since the handoff, so even `MERGEABLE`/`CLEAN` was stale. Rule: on claiming any merge/retire card, first `git fetch origin --prune`, `git ls-remote origin refs/heads/main`, and `gh pr view <n> --json state,mergedAt,mergeCommit` (the last two distinguish *closed* from *merged* — a `CLOSED` PR with `mergedAt: null` was retired, not landed, so do not "helpfully" re-open it). Then decide by blob, not by file list: for **every** path in the PR's diff, `git rev-parse <head>:<path>` vs `git rev-parse origin/main:<path>`. Identical blob = already shipped. Differing blob = check the direction before calling it divergence — a target that grew (15 files vs the PR's 14, the extra one being a refactor of the PR's own code) means `main` is **ahead**, not divergent, and merging is still wrong. When all paths are present, merge nothing: close the PRs unmerged citing the commit that shipped it, delete the dead refs (they stay reachable via `refs/pull/*/head`), and say plainly on the card that the gates it quoted were real but describe work that shipped by another route — otherwise the next reader redoes it. Do not create follow-up work for "the widget that isn't on main": it is.

**Execute a "retire X" ruling only after you find out what reads X — including surfaces you merged yourself.** A ruling naming one endpoint canonical and another retired is not a work order to delete the latter: first enumerate readers across *every* layer (`git grep` both the route paths and their client-side wrappers, server and UI/client sources), then check whether the retired surface uniquely provides a parameter axis that some merged CLI, client, or widget already exposes. If it does, deleting it removes a shipped capability and breaks the surface that depends on it; the correct output is a dependency note naming the card that must move the axis onto the canonical path first, plus a guard proving no reader remains before deletion is safe. Two endpoints serving one dataset is a real defect — but so is a widget that silently renders nothing because the path it read was retired on a ruling's text alone.

**When several gates compete for one machine, gate narrow diffs on a targeted subset and name the debt explicitly.** `-k "<topic>"` over the two suites a change touches (reporting `N passed, M deselected`) is proportionate for a few-file change while two or three long legs are already running; a full-suite leg per small PR starves them. State in the same reply that the full legs are still owed before any release cut, so a proportionate gate never quietly becomes the standing standard. **Per-leg alone-green is not the combined signal when a change touches shared collection surfaces (root conftest, pytest.ini, a hyphenated payload dir):** cross-leg state leaks redden only in the combined order — run the multi-directory invocation once alongside the per-leg gates before calling such a change green. **The converse hazard is equally real:** the combined cross-directory invocation is not a universally supported shape. Where the suite runner deliberately uses one pytest process per dir (it documents bare-name collisions), two legs carrying the SAME-named test module (e.g. per-leg copies of one guard test) error on collect with `import file mismatch` — that error is the documented runner hazard, not the PR's regression. Gate such a PR in the supported per-dir shape before suspecting the change, and read the runner script's own comments for the collision note. Background gate output often carries an unrelated crash report above the real lines — judge the gate by its own echoes (`LEG_RC=`, `IOS:`, counts), never by the noise, and never call a leg green from exit code alone when its echoes are what you asked for.

**A two-dot `git diff main..branch` does NOT tell you what merging that branch would do — and "this stale branch will resurrect deleted files" is a claim you must measure before you repeat it.** A diff compares two trees; a merge is three-way against the merge-base, so files `main` deleted that the branch never modified **stay deleted**, and files `main` added stay present. I shipped this mistake in public: a card body claimed a stale branch would re-add five deleted screens plus eight fixtures and delete 13 shipped sources (read straight from a `git diff main..branch` full of `A`/`D` rows), I repeated it verbatim into a GitHub close comment and three cards, and the real answer was **8 files and 3 conflicts, nothing resurrected, nothing destroyed**. Before writing or acting on any "dangerous to merge" claim, run `git merge-tree --write-tree <main> <branch>`, take the tree sha on line 1, and inspect *that* tree (`git ls-tree -r --name-only <tree> -- <path>` per file you care about, plus `grep '^CONFLICT'` for the conflict list). It is one cheap command and it is the only thing that answers the question. Stale bases are still worth fixing — the real cost is conflicts in the files where `-X ours` silently eats your own additions, which is a different and smaller claim than the scary one.

**`git grep -E` has no `\b`, so a word-boundary absence grep silently matches nothing.** `git grep -cE '^(struct|enum) Foo\b'` printed `0` for four types that were each declared exactly once — a zero that reads as "the types are missing from main" and would have justified the wrong conclusion entirely. Use a plain grep, or `[^A-Za-z]` in place of `\b`. Treat a zero-hit absence grep as unproven until the same pattern with the boundary removed returns hits.

**Prove "the deleted thing came back" against expected tree state, never against a list of names you happen to remember.** A resurrection grep assembled from the first deletion card (`Views/(Sessions|Approvals|Autodown|…)`) printed `RESURRECT=0` on a PR whose added-file list plainly re-added five console screens *and a whole retired extension target* — the pattern simply did not know the deletions that happened after I wrote it. Worse, `mergeable` reported `MERGEABLE/CLEAN` on every one of those cases, because re-adding a file `main` deleted is not a conflict. State the expected tree and assert it instead: the exact target list from the project build check, per-target file counts (`HSCC: 71`, `HSCCWidgets: 12`, extension `10`), and the decode/fixture count. Counts fail loudly for *every* stale-content class at once; name lists only catch the ones you thought of. Off-by-the-baseline counts with a clean compile mean a stale base, not bad code — and when the card is behind `main`, the branch's own green gate is not the merged tree's green gate: build a throwaway local merge (`git worktree add` off `main`, `git merge --no-commit --no-ff <head>`, gate there) before merging.

**A count you quote in a close-out or gate comment must come from an executed grep, never from the count you expected.** Measured on myself: a merge-verification comment shipped with "markers x14" written before the marker grep had run; the real count was 4 and a same-turn correction comment on the card was required. Build the comment AFTER the verification commands return (capture their output into variables and interpolate), and if you did quote an expectation, correct it on the same card in the same turn — the record's accuracy is the deliverable, and an uncorrected wrong number teaches the next reader to distrust every count you post.

**A worker's gate numbers are a claim, and a *correct* claim can still come from the wrong repo.** Re-run the card's stated gates yourself in a clean detached worktree at the exact head sha, and compare counts *and* the repo you measured against (`git ls-remote`, `git rev-parse origin/main`). A card once quoted `HSCC: 73 / HSCCWidgets: 10 / main 76614675` where `76614675` did not exist in the remote at all — the worker had honestly measured a divergent clone. Reproducing the numbers is only half the check; reproducing them against the real remote is the whole check. **Silent-empty ref lookups turn into measured-the-wrong-tree reviews.** `gh pr list --jq` has no `--arg`, so a parameterised filter returns nothing without error; my `PR=` came back empty twice, the worktree fell back to `HEAD`, and I reported `main`'s own gate numbers as a PR's while they looked green. Same class: a `$(gh pr view …)` that resolves to empty makes `git worktree add --detach $D ""` succeed at `main`. Always echo the resolved PR number and `head=${H:0:8}` in the same command that gates, and treat an empty head or a target list identical to `main`'s as an invalid run — never as a pass. **A worktree checked out at a revision older than the test path you point pytest at returns `no tests ran in 0.00s`** — a false empty that reads as "the defect does not reproduce" on both sides of an A/B repro pair, so both your red and your green are fake. Assert each path exists at each revision before measuring (`git cat-file -e <rev>:<path>`), and treat `no tests ran` as an invalid run, never as green or red. **`gh pr create` likewise has no `--jq`** — passing it prints the whole help text and your `$(...)` comes back empty, so a chained `gh pr merge $P` silently merges nothing while the command reports success. Capture the number by parsing the returned URL (`... | grep -oE 'pull/[0-9]+' | grep -oE '[0-9]+'`). **Never merge by an assumed number:** PR numbers are not issued in your order — another profile's PR can take the number you expected for yours, and `gh pr merge <assumed>` then lands a PR you never reviewed. Resolve the number from the head branch at merge time (`gh pr list --head <branch> --json number`) and echo `PR=` in the same command as the merge; no resolved number = no merge. End any conditional merge chain with a **behavioural post-check that fails if the merge did not happen** (re-run the command that was broken on `main` and assert its new exit code, or grep `main` for a symbol only that PR adds). A chain guarded only on `conflicts=0` cannot tell "merged" from "never created". **Never merge by an assumed number:** PR numbers are not issued in your order — another profile's PR can take the number you expected for yours, and `gh pr merge <assumed>` then lands a PR you never reviewed. This bites inside your OWN create→merge chain too: `gh pr create` prints the real number, which is often not the one you mentally reserved while the create was running — capture the returned URL/number and merge only that. Resolve the number from the head branch at merge time (`gh pr list --head <branch> --json number`) and echo `PR=` in the same command as the merge; no resolved number = no merge. Related trap: `out=$(bash gate.sh); rc=$?` captures the script's status; `bash gate.sh | tail -2; echo $rc` captures `tail`'s, which is always 0. See `references/landing-a-superseded-pr.md` for the full landing procedure (content-equivalence proof after a squash merge, why `--is-ancestor` lies, and the new-ref pattern for workers who cannot force-push).

**Stopping a running card as "superseded" can orphan real work.** A worker's posted comment
describes what it *concluded*; its commit is often something else entirely, in a different
file. Before `reclaim` + `complete` on a running card, read the branch tip itself (`git
rev-parse` the expected branch, then `git show --stat <tip>`) — and note that "no PR exists"
is NOT the same question as "no work exists", because a worker stopped mid-run has committed
without pushing. Record the tip sha in the close-out comment so the work stays recoverable,
and if the commit is not what the card's verdict was about, card it fresh to land that commit
rather than letting the branch sit: a card's *deliverable* can be legitimately superseded while
its *artefact* is not.

**A completion summary's "merged `<sha>`" is a claim about a verb, and the sha is usually just the branch head.** Settle it with `gh pr view <n> --json state,mergedAt` — never with `git merge-base --is-ancestor`, which returns NO for every squashed landing and would 'disprove' a true merge too. When state reads OPEN, the card is not done: land it in your own lane per the normal gate and post a correction on the card distinguishing the claimed sha (branch head) from the actual landing sha, or the board keeps carrying a merge that never happened.

**On a card reporting itself done, the first question is "does a PR exist" — before you read the diff.** Several cards report "shipped as PR #N" while no PR exists for that card id; the work then lives only on an unpushed local branch, and `main` is still red. Check `gh pr list --state all` matched on the card id **and on title text** (a body that merely mentions another card's id will pollute an id search and hand you an unrelated merged PR), then look for a stranded branch: `git branch -a --list '*<card-id>*'`. Two gotchas while doing it: `git branch -a` prefixes a branch checked out in a worktree with `+`, and that `+` is **not part of the ref** — `git show +wt/<branch>:<path>` reads an empty blob and your `grep -c` of zero looks like "the fix is absent". Second: if the branch exists, compare it per-path against `origin/main` to confirm it is a superset, then land it yourself — `git push origin <branch>`, `gh pr create --base main --head <branch>`, gate, squash-merge — and say on the PR that the orchestrator opened it because the worker never pushed. Say the same on the card, or the next reader assumes the worker opened a PR and looks for it in vain.

**A spec/rule change lands while cards are mid-flight — steer every card first, then re-check every handoff against the LATEST rule.** The moment a directive supersedes a rule your running/queued cards were briefed with, post the correction verbatim as a comment on EVERY affected card before doing anything else — comments are the only channel that reaches a running worker (body edits never do). Handoffs that arrived after the change are suspect until measured: a worker often names the revision it wrote to ("written to the 08:30 rule change") — that sentence is a staleness flag, not a detail. Then measure the **tree at the head sha, not the PR diff's added lines** — `git grep '<banned-token>' <head-sha> -- <paths>` plus a grep for the mandated strings — and quote the counts in the reject. Diff added-lines yield false positives in both directions: tests and changelogs legitimately quote a banned token (a mutation test asserts the flag is gone; a changelog sentence declares its removal), so the diff can flag a compliant PR and hide a stale one. Trust neither the handoff text nor the diff; the tree at head is the arbiter. Reject with the stale markers itemized and card the fix as a successor building on the rejected branch, naming what stays (good shape/placement) and what must be rewritten; offer the running worker the exit to block for re-mint if it is too far along rather than burning its work halfway.

**Before calling a public-repo identifier a leak, prove it is new.** Ban-list greps (LAN IPs, home paths, attribution) stay mechanical, but a judgment call on a fleet/repo-specific identifier (template name, internal codename) must be settled by novelty: `git grep -l '<identifier>' origin/main` — an identifier already shipped in repo artifacts (changelogs, audits, templates) is repo vocabulary, not new exposure, and rejecting a release-blocker PR over it wastes a cycle. Reject only where the diff is the identifier's first appearance in the public tree, and record the grep result in the review comment either way. Pre-classify the mechanical greps too: loopback addresses (`127.0.0.1`) and fictional fixture accounts (`/Users/hscc-fixture/...`, `/home/hscc-fixture/...`) are documentation-grade and not leaks — read every flagged line before rejecting, or you will reject a guard-test PR whose whole teeth corpus is fake home paths.

**`gh pr diff` accepts no pathspec** — `gh pr diff <n> -- <path>` dies with `accepts at most 1 arg(s), received 2`. To scope a PR diff to one file (e.g. checking whether an edit to an already-landed test module deletes assertions), `git fetch origin <head-branch>` and use `git diff origin/main...<head-sha> -- <path>`.

**A review or merge hold stated only in chat strands the card.** Workers cannot read your conversation with the operator. A condition phrased as "open the PR once I report the tag" or "wait for the release" is a signal only you hold, so the card sits `running`/`review` while both sides believe the other is acting — and the notification stream keeps re-arriving as if work were pending. Either state the gate on something the worker can observe itself (board state, a tag that exists in `origin`), or post the unblock **on the card** in the same tick the condition clears, carrying the facts it needs (tag → sha, gate verdict, what is still outstanding). The same applies to authority: if a worker sets `reviewer=<its own profile>` as a placeholder and asks whether some other profile must formally approve, answer on the card with who actually holds the gate, or it idles waiting for a verdict that will never arrive.

**You cannot close a card you did not claim, and a merged card left `blocked` strands its entire child chain.** `kanban_complete` refuses ("unknown id, stale run, or already terminal") on a card owned by a finished worker run — so the orchestrator who reviewed and merged the PR cannot mark it done. Do not leave it `blocked` to wait for someone else: children promote only on `done`, so every downstream card waits on a deliverable that already shipped. Post the close-out **on the card** — the commit sha now on `main`, one file the next worker can confirm is gone, "complete with nothing under `Left:`, do not re-implement, re-delete or re-open a PR" — then `kanban_unblock`, which returns it to `ready` for a two-or-three-call worker that verifies and closes. Say in the comment *why* it was re-queued ("bookkeeping handoff, not a defect in the work"), or the next reader concludes the work was rejected and redoes it. Then check the closed card changed no code: no branch, no new commit, `main` tip unmoved.

**When a worker blocks on a contradiction between two artifacts you authored, you adjudicate — do not escalate to the operator.** Your cards, your docs and your scope statements are the authority set, and the operator's own spec sentence outranks a recommendation written in a doc under that spec. Rule on the card: name the artifact that loses and why, and say what happens to the interim shape the worker built to keep the gate green (typically: correct interim, explicitly temporary, removed by the later card that already exists in the graph — say which, so nobody mistakes it for a survivor). Accept a worker's correction of errors in your card body as a fix to your brief, not scope creep; and a worker that opens the PR as a *draft* rather than guessing at a spec conflict has done exactly the right thing — un-draft it only after you re-measured the gate on the pushed tree yourself.

**`hermes kanban comment` takes the body as POSITIONAL text — there is no `--body` and no `--body-file`** (passing `--body` dies with `unrecognized arguments: --body`). Write the long body to a file and pass it as one positional argument in a list-argv subprocess call, then verify by re-reading the card and comparing the stored length *and* the tail. Note the trap in the verification itself: an immediate re-read can return the pre-write payload, so a new comment can look missing when it stored fine — re-read once more before concluding the post failed, or you will post a duplicate. Same asymmetry on creation: the `kanban_create` **tool** takes `body` only — `body_file` is a CLI-only flag and the tool refuses it as an unknown parameter; pass long card bodies inline.

## Taking over a worker's branch — inspect its workspace before any git verb

- **A branch you cannot check out is a live worker's workspace, not a stale ref.** `git worktree add` failing with `already used by worktree at <path>` names the worker's own checkout — which is where its uncommitted work lives. Read `git status --porcelain` and the newest file mtime there before deciding anything: dirt with a recent mtime means the worker is mid-flight and a rebase destroys live work; dirt hours old with the branch head unmoved means it stalled and left real work on disk.
- **The converse case: a worker workspace that exists but is NOT a git repository means the worker built and pushed from elsewhere (a scratch clone).** `git status` there dies with `fatal: not a git repository` — that is not corruption, it is the worker's build dir after cleanup. Attach a scratch `git worktree add -f <dir> <branch>` to the pushed branch and do the rebase there; the remote branch is the whole deliverable.
- **"No new commit on the branch" is not evidence a worker is idle.** A worker killed mid-run has uncommitted edits and no push, so head-sha checks alone will make you call a live worker stalled. Bank the dirt before any destructive git op: in that worktree `git checkout -b safety/<task>-wip && git add -A && git commit`, then `git checkout <pr-branch>` to restore it clean. The artefact survives, the PR branch is untouched, and you can mine or drop it later — one such capture was the only copy of a card's real implementation.
- **Bank first, then decide; never rebase to find out.** Rebase discards uncommitted edits with no undo, so a large conflict set is exactly where you must not experiment.
- **add/add on a hot shared file is a design call, not a merge conflict.** When `main` and the branch both add a file serving the same purpose, settle which implementation is canonical, write it on the card, and resolve everything else mechanically. State the file-level split explicitly (which paths take `main`, which are append-only keep-both, which need real re-work) so the worker does not re-fix `main`'s defects in its own PR — duplication is the expected failure when a worker sees a bug in the base it was told to build on.
- **When two merged-in-flight PRs each fix the same hot seam, resolve the rebase to keep BOTH intents and prove survival before force-pushing.** Land whichever went green first; the second rebases once with a stated rule (typically: main's new structure stays, the branch's helper/verdict wires into it at the call sites). Before `git push --force-with-lease`, prove both mechanisms still exist: `python -c 'import ast; ast.parse(...)'` on the file, a per-mechanism `grep -c` of each side's unique symbol reported as witness counts, and the branch's own test module plus the sibling PR's test module run green together. A conflict-free-looking rebase that silently dropped one side's helper passes every check except these.
- **Tell a running worker when `main` moves under it on a file it is editing**, and re-measure `conflicts` at merge time rather than reusing a count taken earlier. `conflicts=0` has a shelf life of one merge.

## Park a card with the verb its state accepts

`hermes kanban block` refuses `todo` and `scheduled` cards — it prints `cannot block <id>` with no explanation, so retrying with a different `--kind` looks like the next fix and fails identically. The park verb for those states is `hermes kanban schedule <id> "<reason>"` (the reason is also posted as a comment). `schedule` in turn refuses a card already `scheduled`, and at that point post the reason with `comment` and say in your reply that the card carries the reason rather than the status.

**`unblock` is also the UNPARK verb for a `scheduled` card (scheduled→ready), and `complete` then succeeds from `ready` when no live run owns the card — while `promote` refuses `scheduled` outright and `complete` from `scheduled` dies with the useless `unknown id or terminal state`.** So closing a parked bookkeeping root yourself is: post the verification the closure must quote (comment first — it is append-only and survives you), then `unblock <id>`, then `complete <id>` in the same turn; the dispatcher could otherwise claim it for a full worker run instead.

**Before dispatching a card whose body was written before its children shipped, grep `origin/main` for the gap the body names.** A decomposed parent's implement-style body describes the pre-child world: once the children's PRs have landed the seam, dispatching the parent points a fresh worker at stale instructions and it re-implements shipped code (or trusts the body's "the gap exists" and invents work). The check is one grep of the body's own gap marker against `origin/main` — absent = the gap closed, close the card yourself with the per-path proof instead of dispatching, and name in the completion which child PR carries the content.

Enumerate work **by status**, not by the default list: `hermes kanban list --status <s>` across `triage todo scheduled ready running review`. A plain `list` is capped and priority-ordered, so it can return two hundred `done` rows while hiding every active card, and `list` has no `--limit` flag. (Same zero-hit class as the truncated-title grep above.)

**Migration rule: when the operator moves a whole lane to another toolchain, park it and prove quiescence in the same turn.** After mass-blocking, re-run the status enumeration and require zero in `running`, `ready` and `todo`. A wave you "blocked" that still has a `ready` card is not parked — the dispatcher claims it within seconds. Blocking a running card also stops its worker, so do not go looking for a separate stop verb first.

## Do not land a check, registration, or coverage row ahead of the surface it asserts

A manifest line, a dispatch check file, or a coverage-matrix row naming a widget/target/endpoint whose source is not on `main` makes a gate or a doc vouch for a capability the tree cannot build — the same lying-surface class as an assertion that cannot fail. Order merges so implementation lands first, then the registration that inspects it, then the docs that describe it. Holding the later ones is not conservatism: merging them because their own gate is green is exactly how a green check ends up certifying something absent. Same reason a stale roster entry that names a deleted route is worth reporting but not worth a card while its leg is green — cosmetic leftovers do not earn cards, and a card that merely tidies a passing test invites someone to weaken the test.

## Reading the notification stream

The gateway re-delivers task-status nudges, and a nudge describes the event it carried, not the
card's current state. Acting on the text without reading state churns cards that are already settled.

- **A `blocked` nudge can be a replay of a block you already dispositioned — read the event history
  before you touch the card.** Count the `blocked` events and read the newest event line: one
  `blocked` whose timestamp predates your own `unblocked` means the waker is echoing the old event and
  the card is already `ready`. This matters most for the *repeat-block escalation* rule: a card that
  blocks twice on a new event usually needs a fresh card instead of another unblock, and a replay read
  as a second block will reland a card that never re-blocked. Same discipline for `completed` echoes —
  they must not re-trigger a review or a merge.
- **`show` prints `status:` indented by two spaces**, so an anchored `grep -m1 '^status:'` matches
  nothing and a per-card status loop reports blank for every card in the wave. Match `status:`
  unanchored, and treat an empty field inside a loop as an unproven measurement — the same zero-hit
  class as the truncated-title `list` grep above, and it reads as 'these cards do not exist'.
  Likewise there is **no `title:` field** — the title is on line 1 as `Task t_<id>: <title>`; read it
  with `head -1`, and an empty `grep 'title:'` is an invalid read, not an absent title.
- **A worker that blocks on its own context budget with the work green is a handoff, not a failure —
  and do NOT unblock that card.** Unblocking respawns the same exhausted worker onto the same brief.
  Instead: verify the handoff yourself (gates at the head sha, conflict re-measure against the current
  `main`), card the ONE remaining step (rebase / land / close) as its own small card naming the head
  sha, and leave the original card `blocked` with a comment accepting its verdict and saying "do not
  resume; the remaining step is carded as <id>; the orchestrator closes this when that PR merges". Say
  on the comment which part of the fault was yours — when the collision exists because sibling cards
  shared files without edges, that belongs on the card, not in chat.
- **Never leave the blocked card as a PARENT of the card that lands its own PR — that deadlocks the
  chain.** The pattern: card A blocks on budget with a green PR; you card the one remaining step (the
  rebase) and parent it to A, because it is A's work; A can only complete once that PR merges; the
  rebase card only runs once A is `done`. Nothing runs, and the board looks idle while every card is
  waiting on a card you deliberately parked. Break it by reading the card's own handoff line: if it
  says `open a PR, never merge`, then **the green PR at its own head IS the card's deliverable**, and
  landing on `main` is the orchestrator's step, not the card's. So once you have re-run the stated
  gates yourself at that head, `kanban_complete` A — recording in `metadata` that the PR is still
  `OPEN`/not merged, the head sha, the conflict count against current `main`, and the id of the card
  that owns the remaining step — and say the same in the summary. Withholding completion because the
  content is not yet on `main` is a category error about the contract, and it strands every child
  behind it. Alternative when the card's contract really does require landing: create the follow-up
  step with NO parent edge to A, and gate it on whatever actually blocks it instead.

## Recovering stuck workers

When a worker profile keeps crashing, hallucinating, or getting blocked by its own mistakes (usually: wrong model, missing skill, broken credential), the kanban dashboard flags the task with a ⚠ badge and opens a **Recovery** section in the drawer. Three primary actions:

1. **Reclaim** (or `hermes kanban reclaim <task_id>`) — abort the running worker immediately and reset the task to `ready`. The existing claim TTL is ~15 min; this is the fast path out.
2. **Reassign** (or `hermes kanban reassign <task_id> <new-profile> --reclaim`) — switch the task to a different profile (one that exists on this setup) and let the dispatcher pick it up with a fresh worker.
3. **Change profile model** — the dashboard prints a copy-paste hint for `hermes -p <profile> model` since profile config lives on disk; edit it in a terminal, then Reclaim to retry with the new model.

Hallucination warnings appear on tasks where a worker's `kanban_complete(created_cards=[...])` claim included card ids that don't exist or weren't created by the worker's profile (the gate blocks the completion), or where the free-form summary references `t_<hex>` ids that don't resolve (advisory prose scan, non-blocking). Both produce audit events that persist even after recovery actions — the trail stays for debugging.
