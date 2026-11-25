# Measured pitfalls: card writes and archived ids

Both of these cost real work on 2026-11-23. Read before any bulk card edit.

## `edit --body` silently blanks a body when the shell hands it an empty string

`hermes kanban edit <id> --body "$(cat b_$t.md)"` prints `Edited <id>` and reports
success even when `cat` failed (wrong filename, wrong prefix) and the substitution
expanded to nothing. The card is then left with NO body, and a dispatched worker
reads an empty spec and invents its own scope.

Measured incident: five `ready`, high-priority cards were blanked by one loop whose
files were named `b_e98c6ad4.md` while the loop built `b_t_e98c6ad4.md`. Every
iteration printed `Edited <id>`.

Rules:

1. Prefer `create/edit --body-file PATH` (and `create --body-file` exists precisely
   so long bodies survive shell quoting) over an inline `"$(...)"`.
2. If you must inline, assert the source first: `[ -s "$f" ] || { echo MISSING; exit 1; }`
   before the edit, inside the same command, so a typo cannot degrade into a blank write.
3. After any body write, re-read the card and compare the body's character count
   against the source file. `Edited <id>` is a status line, not proof that content landed.
4. Prefer `comment` for additive bindings (rulings, rationale, split notes): comments
   are append-only and cannot destroy a body. The CLI takes the body **positionally**
   (`hermes kanban comment <id> <text>`); `--body` aborts with `unrecognized arguments`.
   For long bodies use the comment tool rather than shell quoting.
5. `create` takes `--parent` (repeatable), not `--parents`. A wrong flag there aborts
   the whole create with `unrecognized arguments`, so verify flags with `--help` once
   rather than retrying variants blind.

## A duplicate hides in the status you did not query

An overlap check over `todo`, `ready` and `running` misses a card parked in `blocked` — and a blocked
card is exactly where an unimplemented fix for the same defect tends to sit, waiting on a dependency.
Measured: I created a card to stop docs-only diffs paying the full 12-leg gate after measuring the
defect live (three 3600s timeouts on one PR), checked only the active statuses for overlap, and the
same fix was already sitting **blocked** as an OpenCode card. It merged nine hours later; my card's
PR became a superseded duplicate whose rebase would have *cut* gate coverage (2 script checks in
place of a 48-file leg), so the duplicate was not merely redundant but harmful.

Rules: query **every** status including `blocked` and `review` before creating anything; match on the
**defect**, not the wording ("docs PRs pay the whole roster" vs "prose diffs must narrow to the
scripts leg" is the same bug phrased by two authors); and when a blocked card already owns the
surface, park yours behind it or comment the overlap on both — never run two implementations of one
rule in parallel.

Corollary for gate/selection rules specifically: a queued PR that changes *which* checks run must have
its base re-checked at merge time, not just at review time. If main grew a competing rule meanwhile,
merging the union can silently reduce coverage.

## Re-minting an operator-archived card

When an operator archives a set of cards and says "one card per concern", those ids
are permanently off-limits: neither your own fan-out nor the auto-decomposer may
re-create the concern under a fresh id.

- Before creating any card, scan live card titles for collision with the archived set.
  A `difflib.get_close_matches` pass over titles (cutoff ~0.72) is cheap and catches
  the wording variants that an id-based search always misses.
- If a live card's body names an archived id as its parent or source, rewrite that
  reference to the LIVE card that actually carries the surface today, and state on the
  card that the archived id is off-limits. Otherwise the worker resurrects it.
- Verify archived state per id with `show` (a `list` call excludes archived rows by
  default, so "not in list" is not evidence of anything).

## A `done` card with an unmerged PR cannot be pushed back through review

When a worker completes its card, its review run closes. Afterwards both reviewer
verbs refuse:

- `request-changes <id> <reason>` → `cannot request changes for <id>: task is not in
  an active review run`
- `reopen-review <id>` → `cannot reopen <id> (not in review?)`

So if you discover after completion that the PR is unmerged or red, there is no verb
that returns the card to the implementer. The route is the LEFT-item pattern: create a
new card carrying the remaining change, assignee = the original implementer, parent =
the completed card, and specify that it continues **on the worker's existing branch**
so the open PR updates rather than a second PR opening. Then state on both the old card
and the new one that the PR is unmerged, because "done" on the board is now a lie about
integration state and only your comment corrects it.

## Measure from the ref, never from the primary checkout

The repo's primary worktree can sit on `main` (or `dev`) and still be **tens of commits
behind** `origin/main`. Grepping it as "ground truth for main" produces confident,
precise, wrong findings — measured 2026-11-23, where a stale primary at `cf6ef9c4`
(41+ commits behind) showed a pre-#635 `orch_message` gate and made a correct PR look
like it was shipping a false model-facing claim.

Rules:

- For "what does main say", read `git show origin/main:<path>` or a detached worktree at
  the SHA. `git fetch` first; never treat the working tree as the reference.
- Before concluding a PR's premise is false, test it against the PR's **base** and against
  the merged commit that supposedly established it (`git merge-base --is-ancestor <sha>
  origin/main`, plus `gh pr view <n> --json state,mergedAt`). A commit found via `--all`
  may be off-main; a file you grepped may be a stale tree.
- When your measurement contradicts a merged PR's own commit message, suspect your
  measurement first: the commit message and the PR state corroborate each other, your
  grep is corroborated by nothing.

## Proving a named artifact does not exist is the hardest claim to earn

Before writing "that file / fixture / test does not exist" into a card, a disposition, or a PR comment,
search by **name fragment across the whole tree**, not by the path shape you assumed:

```sh
git ls-tree -r --name-only origin/main | grep -i <name-fragment>
git grep -l -i <name-fragment> origin/main | head
```

Measured: a card built around a named fixture was nearly archived with "the named fixture does not
exist" because the search looked for `test_<name>.py`. The artifact was a `.txt` under
`scripts/tests/fixtures/`. Fixtures, docs, audit reports and generated data do not follow test-file
naming. This is the false-negative mirror of the word-boundary error below: a loose pattern makes a
false positive, an over-specific path makes a false absence, and both end up as confident text on a
card. Absence claims are the worse of the two, because nothing re-tests them afterwards — and an
**archived** card is exactly where an unverified non-existence statement becomes permanent. If you did
publish one, post an addendum that corrects it and re-verify the other facts in the same comment, since
the first error rightly casts doubt on the rest.

## Merge order when two PRs edit one file

The expensive side is whichever PR needs the bigger gate. Land the big-gated PR first and
re-measure the small one on top of it: one plugin leg costs minutes, a full 12-leg suite
costs a quarter hour and invalidates a run already in flight. State the order on both
cards so neither worker rebases on their own initiative.

## A review request for a PR you already merged

The waker re-delivers `review_requested` events, so one can arrive hours after the merge that
discharged it. Check the event timestamp before doing any work — `show <id>` prints it with an
age. If it predates your merge, the correct action is nothing (or a nudge, below).

Containment test: `git merge-base --is-ancestor origin/<branch> origin/main`. If the branch was
removed by `--delete-branch` on merge, git answers `unknown revision` — that is **expected** and
means the branch is gone, not that work is missing. Confirm the deliverable instead with
`git ls-tree -r --name-only origin/main | grep <file>`.

The real hazard is a **new** run on the same card (compare run ids: the request may come from
run 2063 while run 2082 is heartbeating now). A fresh worker in a stale tree can re-push a
deleted branch and open a duplicate PR for landed work, spending a queue slot and a full gate
run. Comment the state explicitly — files are on main, branch deleted, the event is stale, close
the card, and if you think scope remains, say so in one comment instead of producing work.

## A `kanban_create` that omits `board=` silently creates a phantom

Two cards cost real work this way: both returned `{"ok": true, "task_id": "t_xxxx"}` and then
answered `no such task` / `unknown task(s)` from `kanban_comment` **and** from
`hermes kanban --board hscc show` — because with no explicit board, create resolves the *`current`
symlink* board, which is not necessarily the project you are orchestrating. Symptom to recognise:
the id never appears in `kanban_list` for your board, and every later reference to it fails.

Rule: pass `board` explicitly on **every** kanban call including create, then verify the id with
`show` before linking, commenting, or citing it in a PR. `ok: true` is an acknowledgement, not an
existence proof — and a re-cut card must say it is a re-cut, with the corrected scope, rather than
silently duplicating the phantom's premise.

## `comment` has no `--body-file`, so a reasoned archive can land with no reasoning

`hermes kanban comment <id> --body-file f.md` fails with `unrecognized arguments: --body-file`.
That is survivable on its own — what is not survivable is batching it with the state transition:
`comment --body-file … ; archive <id>` archives the card successfully even though the comment
never posted, so the card lands in the archive with **no explanation attached**, which is worse
than no archive at all because the next reader cannot distinguish a reasoned disposition from a
crash. This bit twice in one session (a superseded-flake archive and a superseded-fixture archive).

Hand long bodies to the `kanban_comment` **tool** (stores ~5KB; the CLI truncates near 150 chars),
and post the comment **before** the transition so a failure is visible while the card is still in
front of you. If the card is already archived, the fix is still to post the reasoning — say in it
that it is an addendum, and re-verify the facts before repeating them, because an archived card is
where unverified claims go to become permanent.

## A merged PR means the card is done — close it in the same breath

Standing rule (operator 2026-11-23, after the dispatcher's `active_pr` respawn guard looped on
this exact state): **a card whose PR is merged gets completed the moment the merge is verified.**
Leaving it `ready` / `todo` / `blocked` / `running` with a merged PR is not untidiness — it is
the input to a dispatcher loop: one card burned **286 dispatcher ticks** before an operator
closed it by hand, and a second instance followed the same day.

Run the sweep in **both** directions. The inverse — a card marked `done` while its PR is still
`OPEN` — is the same defect mirrored, and it is the nastier one because the board now asserts work
is shipped when the tree says otherwise. Check it with the PR's own state plus `git cat-file -e
origin/main:<path>` (absence there is proof the deliverable is unlanded, whatever the card says),
and when you find it, name the downstream consequence: a release note that claims an unmerged
feature is the real damage, not the tidy board.

Why the loop happens: the hermes guard keys off "a GitHub PR URL appears in a recent comment"
(`kanban_db_dispatch.py:1655` returns `"active_pr"`) and performs **no PR-state lookup**. A merged
PR still matches the pattern forever, so the card is skipped every tick and never resolved. Our
side cannot fix the guard, but it can refuse to create the state.

So, after any merge — yours or a worker's:

1. Verify the merge on GitHub (`gh pr view <n> --json state,mergeCommit`), not from the summary.
2. Complete the card with the landed sha in the summary.
3. Only if a live worker still owns the card do you hold — and then say in a comment that the
   merge is verified and the close is the worker's, so the state is deliberate, not forgotten.

### Sweeping for the state

Compare merged PRs against non-terminal cards; branch-name guessing alone misses things. Oc cards
use `wt/oc-t_<hex>` — keep the `t_`, it is part of the id — and some workers branch `fix/...` or
`docs/...`, so match on the card id appearing in a merged PR's head ref:

```sh
gh pr list --repo <repo> --state merged --limit 60 --json number,headRefName \
  --jq '.[] | "\(.number) \(.headRefName)"' > /tmp/merged.txt
for st in ready todo running; do
  hermes kanban --board <board> list --status $st | grep -oE 't_[0-9a-f]{8}'
done | sort -u | while read -r id; do
  grep -qE "(wt/oc-)?$id($|-)" /tmp/merged.txt && echo "  $id open over a merged PR"
done
```

`hermes kanban list` takes no `--limit` (the flag belongs to the tool, not the CLI) — passing it
fails the call and, in a loop that ignores errors, silently scans **zero** cards and reports a
clean board. The same false green arrives from the other direction when you pipe into a parser
that swallows an unexpected shape and prints a count of 0: a wrong `--json` field guess answers
"0 ready cards" with complete confidence. Never let a zero-scan justify a duplicate create — dump
the raw head (`| head -c 200`) the first time you use any new list invocation, and make the parser
say out loud when it received something it could not read.

Repeat `--status` flags do **not** OR. `list --status ready --status todo --status running` behaves as
if only the **last** flag were passed, so a `ready` card you just created is silently excluded and you
are one inference away from declaring your own create a phantom. Measured: a card that `show` confirmed
live (`status=ready`, correct assignee, full body) was missing from a three-`--status` list, and the
only honest reading was "my list call filtered it out", not "the card does not exist". Query **one**
status per call (`for st in ready todo running; do list --status $st; done`) and settle any existence
question with `show <id>` rather than with any list output.

## A rule added to a generated prompt must be checked for WHICH profiles receive it

Presence in a generator constant is not presence in a rendered profile. Ops text is usually
selected by an either/or chain (`ops = _MAIN_OPS` / `elif ...: ops = _PROJECT_ORCH_OPS` / else
`_WORKER_OPS`), so a rule appended to one constant reaches exactly one family of profiles and is
never composed into the others.

After any "add rule N to the orchestrator prompt" card, verify reachability before accepting it:

1. `git show <ref>:<generator>.py | grep -nE "<CONSTANT>"` — a constant referenced only at its own
   definition and one `elif` is not shared with anyone.
2. Read the dispatch branch that assigns it, and state which roles render it. If the actor the
   rule exists to constrain is not in that set, the card is under-specified — say so.
3. Distrust presence-only tests (string containment asserts). They pass when the text is present
   but unreachable, which is precisely this failure. The strong pin deletes the rule text on a copy
   and asserts the test reddens.

When the gap comes from how the card was written rather than what the worker did, record it as a
note on the PR — not a new card — and name the one-line extension so the next hand can take it.

## Short identifiers need word boundaries

Fourth loose-pattern error in one day, same failure each time: `grep active_pr` "proved"
`hscc-cluster/workflow.py` owned the dispatcher guard. It matched inside **`active_profile`**.
The guard is hermes upstream; our repo mentions `active_pr` only in tests that pin hermes. Use
`\b<id>\b`, or search the import/qualified call, before letting a grep carry a claim.

## Registering a tool can redden a gate owned by a different directory

A registry-coverage gate that parametrizes over every registered tool
(`test_every_registered_tool_is_named_or_deliberately_excluded[tool]`) lives in one
directory and asserts about a file in another. A PR that only registers a new tool and
never touches the asserted file turns it red — so the leg that fails is not the leg the
diff touches, and a per-directory local run of the changed area will miss it.

When a PR registers anything new (tool, plugin, CLI verb, skill), run on the **union
tree** the leg owning the coverage gate, not just the legs the diff touches. CI here is
advisory and does mark such a leg `completed/failure` with an `a test leg is red` annotation,
so read the annotations before merging anything whose diff adds a registration.

