---
name: kanban-orchestrator
description: Decomposition playbook + anti-temptation rules for an orchestrator profile routing work through Kanban. The "don't do the work yourself" rule and the basic lifecycle are auto-injected into every kanban worker's system prompt; this skill is the deeper playbook when you're specifically playing the orchestrator role.
version: 3.0.1
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

## Prove absence by searching, not by walking edges

Before carding anything, confirm the work is not already carded by **searching the board by title and content** — not by reading the `children` list of the card that inspired it. An unparented card is a real card.

This produced a live collision: a worker reported on `t_317a0f30` that they had filed the p313 stamp-environment gap "as its own card". I read that card's `children` (two entries, neither this one), concluded it was uncarded, and created my own. It had been filed — as `t_02c98915`, without a parent edge back. The dispatcher claimed both and two `@devops-engineer` workers were pointed at one environment gap. Same class as a zero-hit grep: **an empty relation you walked is not evidence that the thing does not exist**; it is evidence that the relation you walked was empty.

Corollary: when a worker's claim conflicts with your check, believe the claim first and go find the artifact. Here the claim was true and my query was under-scoped.

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
collisions from coming back.

If the task graph has dependencies, create the parent cards first, capture their returned ids, and include those ids in the child card's `parents` list during the child `kanban_create` call. Avoid creating all cards in parallel and linking them afterward; that creates a window where the dispatcher can claim a child before its inputs exist.

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
`references/placeholder-ids-and-parking.md`. Comments are append-only, so a
placeholder id written early is permanent unless you post the correction naming
the real id on the card that carries it.

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

**Inventing profile names that don't exist.** The dispatcher silently fails to spawn unknown assignees — the card just sits in `ready` forever. Always assign to a profile from your Step 0 discovery; ask the user if you're unsure.

**Bundling independent lanes into one card.** If the user asks for two independent outcomes, create two cards. Example: "fix blockers and check model variants" is not one fixer task; create a fixer/engineer card for the fixes and an explorer/researcher card for the variant check, then optionally gate review on both.

**Over-linking because of wording.** "Finally check X" may still be parallel with implementation if X is static config, docs, or source discovery. Link it after implementation only when the check depends on the implementation result.

**Forgetting dependency links.** If the task graph says `research -> implement -> review`, do not create all tasks as independent ready cards. Use parent links so implement/review cannot run before their inputs exist.

**Two cards that touch the same file are a dependency, even when their features are unrelated.** Feature lanes that share a hot file — a build manifest with per-target file lists, a shared models file, a generated index — are not independent, and fan-out by feature alone hides the edge. I dispatched a 20-card wave from one base with no edges between the cards editing the same manifest and models file; the strip cards then deleted files on `main` serially, and five of those branches came back stale, several silently re-adding code a merged sibling had deleted. Either serialise shared-file cards with `parents=[...]`, or budget for a rebase rail. Expect stacking on top of it: a worker that branches off a *sibling's* branch re-derives that sibling's work, and when both merge, the earlier PR is left with nothing to deliver and must be closed as superseded rather than rebased.

**Reassignment vs. new task.** If a reviewer blocks with "needs changes," create a NEW task linked from the reviewer's task — don't re-run the same task with a stern look. The new task is assigned to the original implementer profile.

**Argument order for links.** `kanban_link(parent_id=..., child_id=...)` — parent first. Mixing them up demotes the wrong task to `todo`.

**To change orchestrator behaviour, pick the artifact by REACH, not by tidiness — the skill is not the live surface.** Measured from `hscc-roles/orch_sync.py`: `compose()` rewrites every live `<project>-orch` SOUL's `## Operational` + `## How you manage work` tail from `generator.py::_PROJECT_ORCH_OPS` on sync (the `## Role:` section is kept byte-for-byte), while `sync_profile` copies a shared skill into a profile **only when that profile lacks it by name** — "Nothing is ever removed or overwritten." So: editing `install/hscc-skills/**` changes only profiles created from now on and no running orchestrator; hand-editing a live SOUL.md's ops block is worthless because the next sync overwrites it; editing your **own** live skill copy is the safe immediate mutation, precisely because sync never touches it. Route the change accordingly — skill for behaviour now, template for behaviour fleet-wide, and never a live-SOUL hand-edit. Then check the drift actually exists before trusting a `version:` string: `diff` the repo skill against your live one and count the `^<` lines (repo-only). I found both copies claiming `3.0.0` while the live one carried 14 extra lesson-blocks, and simultaneously the live SOUL lagging its own template — drift runs in opposite directions on the two surfaces, so neither file can be assumed canonical.

**You cannot attach a file to another worker's card** (`kanban_attach` refuses: "worker is scoped to task …") — hand off a file's contents with `kanban_comment` instead, or list absolute paths in your own `kanban_complete(artifacts=[…])`, which uploads them from disk.

**Don't pre-create the whole graph if the shape depends on intermediate findings.** If T3's structure depends on what T1 and T2 find, let T3 exist as a "synthesize findings" task whose own first step is to read parent handoffs and plan the rest. Orchestrators can spawn orchestrators.

**Tenant inheritance.** If `HERMES_TENANT` is set in your env, pass `tenant=os.environ.get("HERMES_TENANT")` on every `kanban_create` call so child tasks stay in the same namespace.

**Creating a card on the wrong board is silent, and there is no move verb.** Pass
`board="<slug>"` explicitly on every `kanban_create` / `kanban_comment` /
`kanban_complete` call and verify with `show --board <slug> <id>` in the same breath as
creating — the create returns `ok: true` and never echoes the board, so nothing tells you at the
moment you got it wrong. Recovery depends on whether the dispatcher already claimed the orphan
(re-create + archive vs keep the live worker and never duplicate it):
`references/wrong-board-cards.md`.

**A parentless card is `ready` the instant it exists** — the dispatcher can claim it within seconds,
so a graph built as `create` then `link` (e.g. during a move) briefly exposes every card. Always pass
parents in the creating call. If you must link after, expect to `reclaim` cards the dispatcher already
grabbed, and reclaim alone is not enough on a card still in `ready`: the dispatcher re-spawns, so
close or gate it in the same breath.

**`gh pr close` on an already-closed PR is a silent no-op that swallows your closing comment.** It exits `0` and prints "already closed" — posting nothing, including the `-c` comment. When that comment is the actual deliverable (an audit trail another agent must find), post it separately with `gh pr comment` and confirm the returned issue-comment URL.

**`gh pr merge --delete-branch` can close a SIBLING PR.** Chain children often use their parent's
branch as the PR base (`--base wt/t_<parent>`), so squash-merging the parent with `--delete-branch`
deletes that base and GitHub auto-closes every PR based on it. `gh pr reopen` then FAILS (base gone);
the only repair is `gh pr create --base main --head <surviving child branch>` reusing the original
title/body, followed by `git rebase --onto origin/main <last-parent-commit>` on the child branch so
it stops re-submitting the parent's commits and files (a squash-merged parent is content in `main` but
not history, so the new PR will otherwise show CONFLICTING). Before deleting any merged branch, check
`gh pr list --base <branch>`; when in doubt merge without `--delete-branch`.

**A `merge PR X / retire PR Y` card is the most volatile card on the board — re-verify it before you touch it, because a sibling card may already have shipped the work.** These cards are created off a snapshot of `main`, and they sit in `ready` while other cards keep landing. Twice in one day the card's premise died before I claimed it: a squash-merged sibling PR bundled the *same* work into its own branch and shipped it, so the PR the card told me to merge had nothing left to deliver — and `main` had moved twice since the handoff, so even `MERGEABLE`/`CLEAN` was stale. Rule: on claiming any merge/retire card, first `git fetch origin --prune`, `git ls-remote origin refs/heads/main`, and `gh pr view <n> --json state,mergedAt,mergeCommit` (the last two distinguish *closed* from *merged* — a `CLOSED` PR with `mergedAt: null` was retired, not landed, so do not "helpfully" re-open it). Then decide by blob, not by file list: for **every** path in the PR's diff, `git rev-parse <head>:<path>` vs `git rev-parse origin/main:<path>`. Identical blob = already shipped. Differing blob = check the direction before calling it divergence — a target that grew (15 files vs the PR's 14, the extra one being a refactor of the PR's own code) means `main` is **ahead**, not divergent, and merging is still wrong. When all paths are present, merge nothing: close the PRs unmerged citing the commit that shipped it, delete the dead refs (they stay reachable via `refs/pull/*/head`), and say plainly on the card that the gates it quoted were real but describe work that shipped by another route — otherwise the next reader redoes it. Do not create follow-up work for "the widget that isn't on main": it is.

**A two-dot `git diff main..branch` does NOT tell you what merging that branch would do — and "this stale branch will resurrect deleted files" is a claim you must measure before you repeat it.** A diff compares two trees; a merge is three-way against the merge-base, so files `main` deleted that the branch never modified **stay deleted**, and files `main` added stay present. I shipped this mistake in public: a card body claimed a stale branch would re-add five deleted screens plus eight fixtures and delete 13 shipped sources (read straight from a `git diff main..branch` full of `A`/`D` rows), I repeated it verbatim into a GitHub close comment and three cards, and the real answer was **8 files and 3 conflicts, nothing resurrected, nothing destroyed**. Before writing or acting on any "dangerous to merge" claim, run `git merge-tree --write-tree <main> <branch>`, take the tree sha on line 1, and inspect *that* tree (`git ls-tree -r --name-only <tree> -- <path>` per file you care about, plus `grep '^CONFLICT'` for the conflict list). It is one cheap command and it is the only thing that answers the question. Stale bases are still worth fixing — the real cost is conflicts in the files where `-X ours` silently eats your own additions, which is a different and smaller claim than the scary one.

**`git grep -E` has no `\b`, so a word-boundary absence grep silently matches nothing.** `git grep -cE '^(struct|enum) Foo\b'` printed `0` for four types that were each declared exactly once — a zero that reads as "the types are missing from main" and would have justified the wrong conclusion entirely. Use a plain grep, or `[^A-Za-z]` in place of `\b`. Treat a zero-hit absence grep as unproven until the same pattern with the boundary removed returns hits.

**Prove "the deleted thing came back" against expected tree state, never against a list of names you happen to remember.** A resurrection grep assembled from the first deletion card (`Views/(Sessions|Approvals|Autodown|…)`) printed `RESURRECT=0` on a PR whose added-file list plainly re-added five console screens *and a whole retired extension target* — the pattern simply did not know the deletions that happened after I wrote it. Worse, `mergeable` reported `MERGEABLE/CLEAN` on every one of those cases, because re-adding a file `main` deleted is not a conflict. State the expected tree and assert it instead: the exact target list from the project build check, per-target file counts (`HSCC: 71`, `HSCCWidgets: 12`, extension `10`), and the decode/fixture count. Counts fail loudly for *every* stale-content class at once; name lists only catch the ones you thought of. Off-by-the-baseline counts with a clean compile mean a stale base, not bad code — and when the card is behind `main`, the branch's own green gate is not the merged tree's green gate: build a throwaway local merge (`git worktree add` off `main`, `git merge --no-commit --no-ff <head>`, gate there) before merging.

**A worker's gate numbers are a claim, and a *correct* claim can still come from the wrong repo.** Re-run the card's stated gates yourself in a clean detached worktree at the exact head sha, and compare counts *and* the repo you measured against (`git ls-remote`, `git rev-parse origin/main`). A card once quoted `HSCC: 73 / HSCCWidgets: 10 / main 76614675` where `76614675` did not exist in the remote at all — the worker had honestly measured a divergent clone. Reproducing the numbers is only half the check; reproducing them against the real remote is the whole check. **Silent-empty ref lookups turn into measured-the-wrong-tree reviews.** `gh pr list --jq` has no `--arg`, so a parameterised filter returns nothing without error; my `PR=` came back empty twice, the worktree fell back to `HEAD`, and I reported `main`'s own gate numbers as a PR's while they looked green. Same class: a `$(gh pr view …)` that resolves to empty makes `git worktree add --detach $D ""` succeed at `main`. Always echo the resolved PR number and `head=${H:0:8}` in the same command that gates, and treat an empty head or a target list identical to `main`'s as an invalid run — never as a pass. Related trap: `out=$(bash gate.sh); rc=$?` captures the script's status; `bash gate.sh | tail -2; echo $rc` captures `tail`'s, which is always 0. See `references/landing-a-superseded-pr.md` for the full landing procedure (content-equivalence proof after a squash merge, why `--is-ancestor` lies, and the new-ref pattern for workers who cannot force-push).

**Stopping a running card as "superseded" can orphan real work.** A worker's posted comment
describes what it *concluded*; its commit is often something else entirely, in a different
file. Before `reclaim` + `complete` on a running card, read the branch tip itself (`git
rev-parse` the expected branch, then `git show --stat <tip>`) — and note that "no PR exists"
is NOT the same question as "no work exists", because a worker stopped mid-run has committed
without pushing. Record the tip sha in the close-out comment so the work stays recoverable,
and if the commit is not what the card's verdict was about, card it fresh to land that commit
rather than letting the branch sit: a card's *deliverable* can be legitimately superseded while
its *artefact* is not.

**A review or merge hold stated only in chat strands the card.** Workers cannot read your conversation with the operator. A condition phrased as "open the PR once I report the tag" or "wait for the release" is a signal only you hold, so the card sits `running`/`review` while both sides believe the other is acting — and the notification stream keeps re-arriving as if work were pending. Either state the gate on something the worker can observe itself (board state, a tag that exists in `origin`), or post the unblock **on the card** in the same tick the condition clears, carrying the facts it needs (tag → sha, gate verdict, what is still outstanding). The same applies to authority: if a worker sets `reviewer=<its own profile>` as a placeholder and asks whether some other profile must formally approve, answer on the card with who actually holds the gate, or it idles waiting for a verdict that will never arrive.

**You cannot close a card you did not claim, and a merged card left `blocked` strands its entire child chain.** `kanban_complete` refuses ("unknown id, stale run, or already terminal") on a card owned by a finished worker run — so the orchestrator who reviewed and merged the PR cannot mark it done. Do not leave it `blocked` to wait for someone else: children promote only on `done`, so every downstream card waits on a deliverable that already shipped. Post the close-out **on the card** — the commit sha now on `main`, one file the next worker can confirm is gone, "complete with nothing under `Left:`, do not re-implement, re-delete or re-open a PR" — then `kanban_unblock`, which returns it to `ready` for a two-or-three-call worker that verifies and closes. Say in the comment *why* it was re-queued ("bookkeeping handoff, not a defect in the work"), or the next reader concludes the work was rejected and redoes it. Then check the closed card changed no code: no branch, no new commit, `main` tip unmoved.

**When a worker blocks on a contradiction between two artifacts you authored, you adjudicate — do not escalate to the operator.** Your cards, your docs and your scope statements are the authority set, and the operator's own spec sentence outranks a recommendation written in a doc under that spec. Rule on the card: name the artifact that loses and why, and say what happens to the interim shape the worker built to keep the gate green (typically: correct interim, explicitly temporary, removed by the later card that already exists in the graph — say which, so nobody mistakes it for a survivor). Accept a worker's correction of errors in your card body as a fix to your brief, not scope creep; and a worker that opens the PR as a *draft* rather than guessing at a spec conflict has done exactly the right thing — un-draft it only after you re-measured the gate on the pushed tree yourself.

**`hermes kanban comment` takes the body as POSITIONAL text — there is no `--body-file`.** Write the long body to a file and pass it as one positional argument in a list-argv subprocess call, then verify by re-reading the card and comparing the stored length *and* the tail. Note the trap in the verification itself: an immediate re-read can return the pre-write payload, so a new comment can look missing when it stored fine — re-read once more before concluding the post failed, or you will post a duplicate.

## Recovering stuck workers

When a worker profile keeps crashing, hallucinating, or getting blocked by its own mistakes (usually: wrong model, missing skill, broken credential), the kanban dashboard flags the task with a ⚠ badge and opens a **Recovery** section in the drawer. Three primary actions:

1. **Reclaim** (or `hermes kanban reclaim <task_id>`) — abort the running worker immediately and reset the task to `ready`. The existing claim TTL is ~15 min; this is the fast path out.
2. **Reassign** (or `hermes kanban reassign <task_id> <new-profile> --reclaim`) — switch the task to a different profile (one that exists on this setup) and let the dispatcher pick it up with a fresh worker.
3. **Change profile model** — the dashboard prints a copy-paste hint for `hermes -p <profile> model` since profile config lives on disk; edit it in a terminal, then Reclaim to retry with the new model.

Hallucination warnings appear on tasks where a worker's `kanban_complete(created_cards=[...])` claim included card ids that don't exist or weren't created by the worker's profile (the gate blocks the completion), or where the free-form summary references `t_<hex>` ids that don't resolve (advisory prose scan, non-blocking). Both produce audit events that persist even after recovery actions — the trail stays for debugging.
