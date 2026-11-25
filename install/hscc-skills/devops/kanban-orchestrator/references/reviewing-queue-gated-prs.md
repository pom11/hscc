# Reviewing a PR the merge queue gates

Applies whenever the project's merge queue is enabled (`hscc merge-queue status` shows
`enabled`) — which is the normal state for hscc.

Two sections carry the discriminators that decide verdicts, and are worth reading first
because each has already prevented a wrong accusation on this board:

* **"Do not push to a PR the queue is holding"** — the head-moved discriminator, which
  separates a genuine foreign push from the queue's own claim-time rebase seen through a
  stale GitHub read.
* **"A new doctor row's seam includes a guard in another package"** — which test actually
  gates a new doctor row, and why row *enumeration* there is insulation rather than
  corroboration.

## What review consists of

1. Read the diff (and the fix commits separately from any `Merge remote-tracking branch
   'origin/main'` commit — diffing across a branch's own merge inflates the stat and can make
   a 3-file fix look like a 1179-insertion blast radius).
2. Hygiene grep on added lines (addresses, secrets, tokens).
3. Targeted tests **for the seam the PR touches**, in a throwaway worktree unioned onto
   `origin/main`. One plugin leg, or a `-k` selection, is the right size.

Do **not** run `scripts/run_tests.sh` (the full 12-leg suite) to review a queue-on PR. GitHub
CI plus the queue's own gate own the full suite on the pushed head. Running it locally in
parallel doubles Mac load, slows every gate, and feeds the load-flake class tracked as
t_20d1af69 — which is the very flake that bounces gates and creates the rebase churn.

Measured cost of one full local run: ~10-15 min wall and the daemon leg alone is ~5000 tests.
A targeted leg is 0.3-50s. There is no review question the full suite answers that CI does not.

Also do not run "one last full suite" on main HEAD after a burst of merges just to feel sure.
That is the same violation wearing a different hat; CI on the head answers it.

## The queue's gate is another agent's process

The queue gate runs `bash scripts/run_tests.sh` from `~/.hscc/merge-queue/<project>/...`, with
children like `pytest -q -rs "$HOME/.hscc/merge-queue/<project>/hscc-api/tests"`. Never kill it.

When clearing your own leftovers, identify by cwd containing `~/.hscc/merge-queue` and exclude
those PIDs. And never kill-by-pattern in a shell whose own command line contains the pattern —
measured: `grep -E "maincheck|rev68|u684|union"` inside a `bash -c` wrapper matched the wrapper's
own argv, so the loop killed its own shell (exit `-15`) before reaching the intended targets.

## Merging

- Enqueue: `hscc merge-queue add <project> <pr>`. Never rebase or hand-merge a queue-on PR,
  and specifically never hand-merge to "unstick" a stuck queue.
- `mergeable=UNKNOWN` parks PRs in `waiting`; the queue livelocks (drains only on an empty
  queue). The manual drain is the operator's, each sweep. Report the stall; do not resolve it
  by merging around it.
- Rebase detection for a pull-out you own: `git merge-base --is-ancestor origin/main origin/<branch>`
  is the test. True = rebased, safe to re-queue. False = still OpenCode's to rebase; leave it.

## "Is it wired in?" — call-site checks need word boundaries

`git grep -l <module_name>` is not a call-site check for a short module name. Measured: searching
`workspace_clean` returned `kanban_live.py`, which merely contains the identifier
`workspace_cleanup_deferred_shared` (an outcome-kind string), and its `execute(` hits were
`conn.execute(` SQL calls. That produced a confident, wrong "this is not dead code" claim.

For a real answer, search the import and the qualified call: `import <mod>` / `from <mod> import`
/ `<mod>\.` with word boundaries, then read the matched line. "Only its own tests reference it"
is a meaningful statement about landing risk, so get it right before saying it.

Same class as the citation-resolver and the stale-worktree errors: a measurement whose pattern is
looser than the claim it supports.

## A job's NAME is not the failing step — read the step before naming the failure

CI groups several steps under one job, and the job label describes its theme, not the step that
exited 1. Measured: a job labelled `address-guard` failed on a *different* step in the same job —
docs-index lint (`readme_lint --strict`) reporting `[DOC-UNLISTED]` for a newly added audit file
that was never listed in the docs index. The diff had no address in it at all.

Before naming any failure class, get the failing step line:

```sh
gh run list --repo <o>/<r> --branch <branch> --limit 6 --json databaseId,headSha,conclusion \
  --jq '.[] | "\(.databaseId) \(.headSha[0:8]) \(.conclusion) \(.name)"'
gh run view <run_id> --repo <o>/<r> --log-failed | grep -iE "FAILED |Error:|error:|\[DOC-" | head
```

This matters most for leak claims: saying "the address guard found a live host" implies an
operator security action, so it must come from the guard's own output line, never from the job
title. If your hygiene grep is clean and the guard is red, the guard is right and your pattern is
narrower than its rule set — go read the log instead of trusting your scan.

Hygiene grep nuance: loopback (`127.0.0.1`) and `10.0.0.x` placeholders in added lines are
benign; the guard targets real/tailnet hosts. Strip known-benign forms before reporting a hit.
Home paths need the **user** checked, not the shape. `address_guard` admits only its
sanctioned fixture and CI users plus the documented `/Users/<operator>` placeholder, so a
plausible-looking home path naming any other user still reds the gate even though it is
nobody's real home. Read the matched user against the guard's allow-list and name it in the
verdict, rather than reporting "0 occurrences of the operator's home" — true and irrelevant.
A playbook shipped into this repo must scrub to the placeholder before it is committed, not
after the gate reddens it.

For the **gate decision** use the head's own check runs, not the workflow list — `gh run list
--branch` can report `success` for the newest runs while the PR head still carries a `failure`
from another workflow/run set:

```sh
H=$(gh pr view <n> --repo <o>/<r> --json headRefOid --jq .headRefOid)
gh api "repos/<o>/<r>/commits/$H/check-runs" \
  --jq '.check_runs[] | select(.name|startswith("leg ")) | "\(.name):\(.conclusion)"' | sort -u
```

## Queue status fields are history, not your PR's state

`hscc merge-queue status` prints `queued=N, waiting=N, in-flight=<pr>, last=<verb>`. `last=merged`
says the **previous transition** merged something — it does not say your PR merged, and
`in-flight` names whichever PR is being gated right now. Reading `last=merged` as "my PR landed"
makes you report work as shipped when it is still open.

Landing is proven only by the PR and the tree:

```sh
gh pr view <n> --repo <o>/<r> --json state,mergedAt,mergeCommit
git cat-file -e origin/main:<path/the/module.py> && echo ON MAIN
```

A red PR parked in the queue only burns gate cycles: `hscc merge-queue remove <project> <pr>`,
fix (or hand the fix to the live worker), then re-add. Pull-outs you own are yours to re-queue.

**Absence from the FIFO is not evidence of a drop.** A PR can be missing from `queue` because it
merged, because it is parked in `waiting`, because somebody removed it, or because it was never
added — and those have opposite meanings. Measured: I read `#725` absent from the FIFO, concluded it
was "unlandable, dropped", and published that in a PR comment and a card comment; it had **merged 14
minutes earlier**. One `gh pr view <n> --json state,mergedAt` settles it before you write a sentence
about work that "failed to land", and the false version is expensive — it tells a worker their work
is stranded, and it can trigger someone else to re-open or re-file work that is already on `main`.

The inverse failure is real too: a green, approved PR can sit outside the FIFO with no error logged
anywhere (observed twice with the same PR in one day). So the sweep is **two** queries, not one — PR
state *and* queue membership — and "green but unqueued" is fixed by re-adding, not by inference from
silence.

**Re-adding is a write, so check the PR is still `OPEN` before you add it.** The absence trap has a mirror: a *landed* PR can still look like it needs queueing, and `merge-queue add` will happily take it. Measured: I reviewed a PR, confirmed CI green, added it — and it had **merged before my add**, so I re-queued a zombie that would then burn a full 3600 s gate on a commit that is already on `main`. One `gh pr view <n> --json state,mergedAt` before every `add` costs nothing and the mistake costs an hour of gate.

The tell is a git error, not a queue error: a merged PR's branch is deleted on merge, so `git diff origin/main...origin/wt/<id>` (or a rebase) fails with **`fatal: bad revision`**. Read that as "this PR merged", not "the ref is broken" — and note it is the same command that *succeeded* minutes earlier, which is why it looks impossible. A `bad revision` on a branch you expected to exist is landing evidence.

Sweep it, do not just fix the one: on each pass, test **every** FIFO entry's PR state and `merge-queue remove` any that are not `OPEN`. A merged entry is invisible to `merge-queue status` and to the log, and it holds a slot.

**But a deliberate pull looks identical to an accidental drop, so check for a HOLD before you re-add.** A PR the orchestrator pulled on purpose — because its head carries a defect whose fix lives on another branch, or because it is one carrier in an absorption — is green, approved, complete-looking and absent from the FIFO: indistinguishable from "silently fell out" by membership alone. Re-adding it ships the defect. Before any re-add, read the PR's own comments for `HOLD` / "do not re-queue", and check whether a card exists that ordered the pull. The gate to lift it is mechanical: the fix commit must be an ancestor of **that PR's own head** (`git merge-base --is-ancestor <fix> origin/<branch>`), not merely "the fix is pushed somewhere" or "the card is done". Measured the same day: I pulled the defect-carrying PR myself, and my own "green but unqueued" rule was the thing that would have put it back.

When you do check membership by grepping the state JSON, grep the **lists**, not the file. Counting
occurrences of `"pr": <n>` also counts the `last` transition record, so a removal that fully succeeded
reads back as "still queued: 1". Parse `d['queue']` and `d['waiting']` and ask whether `<n>` is *in*
those lists — the same `last` field that misleads the CLI summary misleads a naive `grep | wc -l`.

## A claim that is not being processed: state file vs live process

`in-flight=<pr>` sitting there for ages proves nothing on its own — a gate legitimately runs a ~10-minute
leg. Separate running from stuck in one pass:

```sh
python3 -c "import json;d=json.load(open('$HOME/.hscc/merge-queue-state.json'))['projects']['<p>'];print(d['in_flight'])"
pgrep -f 'merge-queue.*pytest'                        # is a gate process alive at all?
grep -E 'merge-queue <p>' ~/.hscc/daemon.log | tail -5
```

A **stuck** claim reads: `stage: gating` with an old `ci_since`, **no** gate process, no new daemon line,
and — decisively — GitHub showing every check **completed with zero failures** on the `pushed_sha` the
queue itself pushed. Green CI plus an idle queue means the resume path is broken, not that CI is slow.
`in_flight` and each `waiting` entry carry a **`last_error`** field; that is where a recurring bug's
fingerprint lives, and reading it beats reasoning from the log's silence. Corroborate against the
deployed code, not `main`: a fix can be merged and the live bug still live, so `grep -c <the fix token>`
inside the deployed plugin file plus its mtime is what says whether the running daemon has it.

What you may and may not do about it: `remove`/`re-add` are yours; hand-merging to "unstick" it is not;
and **restarting the daemon is not yours while cards are running** — worker processes are grandchildren of
the gateway and `kickstart -k` takes them down mid-card. Report the stuck claim with the state-file
fields, the green CI, the `last_error`, and the card that owns the fix. Note too that such a claim often
clears by itself: before escalating, say how long it has actually been held and whether it recovered,
or you will have described a permanent block that was a 35-minute stall.

**Log silence during a gate is normal, not starvation.** One tick carries one in-flight claim, so while a
gate runs the daemon is *not* re-polling the `waiting` list: a PR that goes green mid-gate stays in
`waiting` with no new log line until the gate finishes. `in_flight.stage == "gating"` plus "the last log
line predates the gate" reads as serialization — say that, not "the runner is starved".

And when the log says `checks not green` while GitHub says `mergeStateStatus: CLEAN`, **read the
predicate in the queue module before inventing a cause** (`_checks_verdict`, `_truthy_check`,
`_required_for_info`) — and apply the same reading to a cause **inherited from a card's own blocked
reason or a worker's summary**, which is a self-report like any other. A queue behaviour described as
"fires once and is never retried" may be documented in the code as terminal *by design*; confirming
the mechanism in the source before you act on an inherited diagnosis is one `git grep -n -B2 -A6
<the_symbol> origin/main -- <the/module.py>` and it has twice stopped a wrong card here. `SKIPPED` and `NEUTRAL` count as **green**, the branch-protection probe is
consulted only when the rollup is *empty*, and the poll that logged the verdict may simply predate the
checks finishing. "Runner contention" and "the queue is stuck" are both cheap to say and both wrong about
20 minutes of healthy waiting — the answer is the head's `check-runs` compared against the timestamp of
the queue's last poll for that PR. An absent leg is not automatically a defect either: `select` omits a
leg whose package the diff cannot reach, and if the predicate accepts the rollup as-is, absence is fine.

## When a gate reds a PR whose diff cannot touch the failing test

A docs-only or purely additive PR failing a timing/process test is almost never its own fault.
Establish it with two cheap, independent signals before believing the gate:

1. **Cross-PR corroboration** — did an *unrelated* PR fail the *same* test within minutes? Two
   diffs that cannot reach the code (a `SKILL.md` fold, a new additive module) tripping the same
   assertion is a load/timing flake, not two simultaneous defects in unrelated code.
2. **Single-file run on current main** in a throwaway worktree (`pytest <that_one_file> -q`).
   Main green + PR red on the same file points at the environment, and it costs seconds — it is a
   targeted run, so it does not violate the no-full-suite rule.

If both hold: re-queue, and post the evidence in a PR comment (the gate command the queue logged,
the assertion, the corroboration, the single-file result, and the card that owns the flake class).
Never hand-merge past a red gate on the strength of the flake theory, and never tell the worker
their engine is broken when the evidence says otherwise — that sends someone to fix nothing.

**A third signal, and the only one that needs no local execution** (use it when scratch-worktree runs
are not available to you — CI and the gate own execution anyway, so you are not entitled to it):
compare the gate's verdict against **GitHub's own leg of the same name on the same head**.

```sh
gh api "repos/<o>/<r>/commits/$(git rev-parse origin/<head-ref>)/check-runs?per_page=50" \
  --jq '."check_runs"[]|[.name,.status,(.conclusion//"-")]|@tsv' | grep -i '<leg-name>'
```

GitHub `completed success` on the very leg the gate answered `pytest exit 1` for is a definitive A/B:
same commit, same leg, opposite verdicts, so the difference is the local gate environment. (A leg name
appearing twice with *different* conclusions is a superseded run from an earlier push — not a
contradiction, and not a second failure.)

**A red gate hands the entry back and nothing re-adds it.** The PR leaves the FIFO with no error
logged anywhere, so an unowned PR just stops being processed — the "green but unqueued" trap again,
this time caused by the red. Re-arm once you hold a discriminator, and enforce a **ceiling of two
reproductions**: the same leg plus the same symptom twice means write it up (real keep-alive bug or
gate-environment fault), not a third re-arm — a third spends a full gate cycle to learn nothing new.

Before blaming the PR at all, check whether the failing test is **main-resident and untouched by this
diff** — `git ls-tree --name-only origin/main -- <path>` and `git diff --name-only origin/main...origin/<head-ref>`.
A wall-clock/cadence assertion that lives on main (`expected >=2 lines in 3s at 1s cadence`) red-gates
whichever PR's diff happens to claim that leg, and then gets blamed on that PR. If an open PR already
rewrites that test file, it is covered — say so on that PR rather than filing a flake card.

## A completeness/coverage gate must be mutation-tested before you trust a green run

A gate of the form "every registered X is documented / registered / listed" passes for two opposite
reasons: coverage is complete, **or the enumeration found nothing to check**. An empty allowlist plus
"N passed" cannot distinguish them, and these gates are cheap to write vacuously. Mutate it:

1. Baseline in a scratch worktree: `pytest <the_gate_file> -q` → record the count.
2. Delete the documented entry for **one** real item, from a copy — never a worker's branch.
3. Re-run. The gate must **fail**, and must **name the item you removed** in the failure text. A gate
   that fails without naming it is checking something else.
4. Count the in-test register (`UNDOCUMENTED_ROUTES`-style allowlists). Entries > 0 means the gate has a
   grandfather clause; entries == 0 with a mutation failure is a genuine guard.

Measured here: removing the two `docs/API.md` lines naming `/v1/triggers` turned 9 passed into
**3 failed**, with the route named 5 times across three tests — the strongest possible result, because
the key-set test also failed, which means the gate catches documented-shape drift and not just
missing headings.

**Trap that fakes a pass:** deleting a URL-ish string with `sed "/$R/d"` — the slashes in `/v1/triggers`
collide with the delimiter, `sed` dies with `invalid command code v`, nothing is deleted, and the gate
then passes exactly as it did before. `sed` failing does not make your mutation run invalid *looking*
invalid; assert the mutation landed (`grep -c` the token after editing) before believing the result.
Use python for path-shaped patterns.

## After a fence/predicate fix: a residual grep count is not a leak until every hit is classified

When a PR replaces a wrong idiom with a shared predicate (a test-suite fence, a "may I touch live state"
guard, an env check), the review question is **coverage of the class**, and a residual count from
`git grep -c '<old idiom>' <ref> -- <pkg>/` answers nothing by itself. A count of 6 can be perfect work
or a half-done fix, so classify **every** hit and put the classification in the review verdict:

* the shared helper's own implementation — it *must* still contain the idiom;
* a docstring or comment **explaining** why the old idiom was wrong (a good fix multiplies these);
* a legitimate self-check assertion inside a test file;
* shipped-code call-sites still using it — the only real residuals, and a leak *only* if nothing owns
  them. Check the card body / LEFT list: "not touched here, carded as children: `<id>`, `<id>`" is
  correct scoping, not an omission — verify those child cards exist and are `running` before re-carding
  the same work or calling the fix partial. Declining to touch another subsystem's module because its
  import shape is a separate ruling is the right boundary.

The same discipline applies to hygiene greps, in the opposite direction: **print the matching lines,
never just the count.** `192.0.2.x` is TEST-NET-1 (mandated for cluster/serving fixtures here) and
`127.0.0.1` is loopback, so "hits = 4" invites a re-litigation that "4 hits — TEST-NET-1 and loopback
fixtures, `address_guard.py --tracked` rc 0" ends. Your grep is a screening net; the repo's guard is the
authority, and the two are expected to disagree.

## Deciding merge order when several PRs rewrite the same file

Two or three open PRs touching one file is dangerous here because the gate merges queued entries into a
single **union worktree** — the second one comes back as a `conflict` drop (it cost `PR #410` its slot).
Merge order is the orchestrator's call, not a rebase card, and never a second rebase card. Decide which
PR lands, then *prove* the others are safe to drop:

* **Identical blob** (strongest): `git rev-parse <a>:<path>` vs `<b>:<path>` — equal means one PR carries
  the other verbatim, so the duplicate closes as superseded (`#696`/`#720` resolved this way).
* **Test-name containment** (blobs differ because one rewrote further): extract
  `grep -oE "def test_[a-z0-9_]+"` from each side, `comm -23` the sorted lists. Zero missing names **plus**
  no production files in the smaller PR **plus** its `changelog.d/` fragment carried by the larger →
  genuine superset, safe to close (`#705`/`#717` vs `#724`: 16/16 and 13/13).

Blob-difference alone proves nothing either way, and a PR carrying another card's changelog fragment is a
hint, not evidence — verify the assertions. Then remove the losers from the queue **before** enqueuing the
winner (a drained `waiting` entry re-enters the union and re-creates the conflict), comment the absorption
evidence on each closed PR, and leave the cards alone — a worker that marked its own card done does not
get a comment from you.

**A supersession is not finished when the losers close: the carrier has to actually land.** Containment
proof plus closed losers whose work now lives only in an **unmerged** carrier means the surface is
stranded — nothing on `main` carries it, and the small PRs can no longer be re-opened as a fallback. So
after any absorption, check the carrier's own state (`gh pr view <n> --json state,mergedAt`) and treat
getting it merged as part of the absorption, not a separate wish. The same rule governs *disposition by
attribution*: before archiving a tracking card because its class is "owned by PR X / card Y", confirm X is
merged or Y has landed — otherwise write on the record that the class currently has **no landed owner**.
Attributing a live defect to an unmerged fix and then closing the tracker is precisely how a class that
keeps red-gating gates ends up with nobody watching it.

**Naming the failing test, then attributing a red leg.** `gh api repos/<r>/actions/jobs/<id>/logs`
returns **0 bytes** for a workflow leg here, and check-run *annotations* are useless (`Process
completed with exit code 1`, `a test leg is red`). What works: `gh run view <run_id> --repo <r>
--log-failed`, then grep for the assertion to get a line number and `sed -n` backwards until
`_____ TestClass.test_name _____`. `grep "^FAILED "` never matches — every log line is prefixed
`jobname\tStepname\t<timestamp>Z `.

Before calling a red leg "the known flake", A/B it: same test **file**, same interpreter, fresh
`HSCC_HOME` per run, 3x on `origin/main` and 3x on the PR head. `main` 3/3 plus branch 3/3 means
intermittent and you may enqueue. A deterministic branch failure means the PR broke it — **even when
the PR never touches that file**, which is the case that fools you: a change in a shared path
(logging, stdio, env) can break an untouched timing test. A matching assertion message
(`assert None is not None`) is a string, not an attribution.

**Reproduce the smallest thing that can fail.** When CI names `path/to/test_x.py:479`, run **that
file** on the ref — `pytest path/to/test_x.py -q` costs seconds. Running the whole leg repeatedly to
characterise a flake is self-defeating: one leg here cost 4m20s per run (5048 tests), so three runs
added ~13 minutes of load on the very machine whose contention causes the timing flake, and can
*create* the failure being chased. Escalate from file → leg only if the file result is ambiguous,
and never to the full suite — the queue gate owns that.

## The other half: a red leg that IS the PR's fault, and is one line deep

The corollary of the section above. Before concluding a red `scripts` leg is a code defect, get
the assertion — the single most common cause is changelog **fragment format**, which touches no
code at all:

```
changelog_fragments.FragmentError: t_xxxx.md: first line must be a 'kind:' header, got '### Verified'
```

`changelog.d/<task>.md` entries must open with a machine header — `kind: Fixed` / `Added` /
`Changed` / `Verified` (the closed `KIND_ORDER` vocabulary) and optionally `task: <id>` — then the
body verbatim, entries split by a line containing exactly `---`. **The `### Section` heading is
synthesised at sync time, so an authored `### Fixed` as line 1 is the classic failure**, and the
worker meant that kind all along. Verify with the repo's own guard rather than trusting the edit:
`python3 scripts/changelog_fragments.py check` → `OK … N fragment entries consistent`, rc 0.
(`check`, not `lint` — there is no `lint` subcommand.)

### Fixing a finished worker's one-line metadata error yourself

A card whose owning run has ended, with its PR red on a one-line metadata defect, is the one case
where the orchestrator edits instead of routing. What keeps it defensible:

1. Touch only the metadata file — `git status --short` must list exactly one path.
2. Prove it with the project's own guard *and* the previously-failing targeted selection.
3. **Fast-forward push of one commit — never `-f`, never rewrite history** — and say so in the
   PR, because an orchestrator commit on a worker's branch needs a visible reason attached.
4. Do not open a card for it. That is the meta-card churn the operator ruled out; cards are for
   defects in behaviour, not missing header lines.
5. This buys no permission to merge. The queue still owns the merge.

## A fix folded in by MERGE COMMIT is not durable — the queue's rebase drops it

The queue rebases every claim with a real `git rebase origin/<base>` (`_rebase_onto()`), and a plain
rebase **drops merge commits** while replaying their non-merge ancestors as patches. So when a worker
folds another branch's fix in via `git merge`, the conflict resolution exists *only* in the merge
commit and the gate re-creates the conflict from scratch. Measured twice on the same file in one day:
first on #729 (worker's warning, later confirmed), then on #754 where the fold landed as
`Merge 'add807b3' …` — clean, all 153 tests passing, and the queue's own step failed with
`Could not apply add807b3...`, conflicts in the module and its test file.

Two consequences for how you gate:

1. **`git merge-base --is-ancestor <fix> <branch>` is necessary but NOT sufficient.** It is satisfied
   by a merge fold, and it is *also* false for a correctly linearized branch (the patch replays under
   a new sha). It proves nothing either way. Gate on content and durability instead:
   `git log --oneline origin/main..HEAD --merges` must be empty; the fix must be identifiable by
   distinctive tokens and test *names*; and you must replay the queue's step yourself in a scratch
   worktree — `git -c rebase.updateRefs=false rebase origin/main` must exit 0. Redirect it inside the
   worktree (`>./pf.log`), never an absolute path: a write to `/pf.log` fails with `Read-only file
   system` and the `rc=1` you then read is the shell's plumbing, not git's — an rc=1 with an *empty* log
   means suspect your own command before the branch.
2. **A mid-rebase worktree lies about tests.** After the failed rebase my seam run printed
   `ERROR … 1 error during collection`, which is an artifact of conflict markers in the tree, not a
   test verdict. Re-run the suite on the *clean* head (`git worktree add --detach <dir> <sha>` before
   any rebase) or you will report a green branch as broken. Always separate "this head is correct"
   from "this head survives the queue's operation" — they are different questions and both must be
   answered.

If the branch must be linearized, that rewrites the PR head: `--force-with-lease` pinned to the sha
you observed, stated plainly on the PR. Never rewrite `main`.

**The mirror case is more dangerous than the fold, and it wears a green badge: merging `main` *into* the
branch to clear a `CONFLICTING` status.** That flip makes GitHub report `MERGEABLE / CLEAN`, because its
three-way merge sees the merge commits — while the gate's rebase drops those commits and replays the
branch's non-merge ancestors, which now include a **sibling PR's stale commits** (that feature as it
stood before main moved under it). Measured: `behind=2`, 2 merge commits, and a diff-vs-main spanning
modules a docs-titled PR never claimed, with the carried test file disagreeing with main by ~1000 lines.
Enqueue that and either it conflict-drops, burning an attempt, or it **lands a superseded copy of
another lane's feature** — after which the reviewed PR carrying the correct version can never merge,
because the same code is in its way. Check what would actually land before queueing:

```sh
git rev-list --reverse --no-merges <first-carried-sha>..origin/<head-ref>
git diff --numstat <carried-sha> origin/main -- <the contended file>   # how stale is stale
```

**Carried commits force the merge order, and the carrier goes second.** If PR B's head contains PR A's
commit, whichever lands first ships the other's work; the loser then has nothing left to merge and sits
open forever. Queue A, land it, then re-verify B — B's replay set should have shrunk to B's own commits
(the already-landed one is dropped by patch-id). Never "queue both and trust the FIFO order".

## Before re-adding: can the DEPLOYED daemon gate this diff class?

A green PR whose diff is prose-only (`docs/`, `changelog.d/`, `*.md`, app skill files) costs the gate
nothing extra **only if the running daemon has the narrowing rule**. If the deployed copy predates it,
its selector claims no leg for a prose diff, the zero case falls through to the whole roster, and the
PR is guaranteed to run the full suite against the gate ceiling. Measured: a 2-file skills-docs PR
(`changelog.d/…` + one `SKILL.md`, no test file at all) logged `gate timed out after 3600s` and landed
in `waiting` having changed nothing — an hour of fleet gate for zero information.

So check the deployed file, not main, before re-adding a prose PR:

```sh
grep -c NARROW_TO_SCRIPTS_LEG ~/.hermes/plugins/<pkg>/merge_queue.py   # 0 = narrowing not deployed
```

If it is 0, **leave the PR parked** and say so on it — re-adding is not neutral, it spends the ceiling.
Re-adding prose PRs while a fix for this is merged-but-undeployed is the one case where "green but
unqueued → re-add" is actively wrong. This is also why `merge-queue status` can read as a stalled
queue for an hour on a PR that cannot break anything.

## Before enqueuing: is main itself green?

The queue gate runs the whole suite against a union of the PR head and **current main**, so a red
main turns every gate red and silently spends a `MAX_ERROR_ATTEMPTS` unit per PR. Check the tip
before adding anything — one API call, no checkout:

```sh
git fetch origin --quiet
gh api "repos/<r>/commits/$(git rev-parse origin/main)/check-runs" \
  --jq '.check_runs[] | select(.name|startswith("leg ")) | "\(.name):\(.conclusion)"' | sort -u
```

If any leg is `failure`, **stop enqueuing** until you know what it is; already-queued PRs are
burning attempts while you read this. Say so plainly — pausing the queue is the operator's lever,
not yours.

**Green main is one of three pre-flight checks. The other two cost one command each and are what catch
the enqueues that can never work — and `merge-queue add` prints `queued` for every one of them, so the
queue will not tell you:**

```sh
gh pr view <n> --repo <o>/<r> --json state,mergedAt,baseRefName --jq '[.state,.mergedAt,.baseRefName]|@tsv'
git rev-list --count --merges origin/main..origin/<head-ref>       # must be 0
git rev-list --reverse --no-merges <upstream>..origin/<head-ref>    # what the gate will replay
```

* **`baseRefName` must be the queue's base.** The queue *refuses* a PR whose base is a sibling branch —
  the daemon logs `PR refused: PR targets 'wt/<parent>', not this queue's base 'main'` — so a stacked PR
  is unlandable however green it is, and several of them can sit being refused for a whole day while
  everyone assumes the queue is dropping them. Workers open stacked PRs routinely when their branch
  builds on a sibling PR's predicate (they need a symbol that only exists there), and GitHub's
  `MERGEABLE` says nothing about the base. Retarget after the parent lands — `gh pr edit <n> --base main`
  — then add. A refusal is also why re-adding is futile: it will be refused again, identically.
  **Refusal is terminal by design**, stated in the queue module itself: a wrong-base PR "is not
  transient, so retrying (and auto-enqueue re-adding it) can never make it mergeable here." So never
  card "make refusals retry" — it would re-refuse forever and is aimed at the wrong axis; the remedy
  is always retargeting. Which retarget depends on one test:

  ```sh
  git merge-base --is-ancestor origin/<parent-head-ref> origin/<this-head-ref> && echo CONTAINS_PARENT
  ```

  - **Not an ancestor** → the branch only needed a symbol the parent *already delivered to main*.
    Plain `gh pr edit <n> --base main` is enough.
  - **An ancestor** → the PR's diff literally contains the parent's commits, so flipping the base
    gives a fat PR that re-lands the parent under a foreign title. Wait for the parent to merge, then
    `git rebase --onto origin/main <parent-head>` (verify `git log --oneline <parent-head>..HEAD`
    prints exactly this card's commits first) and publish a **`-rebased` successor branch** with
    `supersedes #N`, closing the old PR so there is one review surface.
* **No merge commits, and know the replay set** — see the merge-commit section above. `MERGEABLE` is
  GitHub's three-way view *with* the merge commits present; the gate rebases and drops them.

**Before reacting to a red tip, test whether it is red in a way that matters.** The cheapest
discriminator is a rerun of the failed leg *on the merged commit itself*: same commit, opposite
conclusion is a definitive flake proof, and it costs one API call instead of tearing up the queue.

```sh
gh api -X POST "repos/<r>/actions/runs/<run_id>/rerun-failed-jobs"
gh api "repos/<r>/actions/runs/<run_id>" --jq '{attempt:.run_attempt,status:.status,conclusion:.conclusion}'
```

`gh run rerun <id> --failed` prints **nothing** on success, and a follow-up API call then answers
`This workflow is already running` — so confirm with `run_attempt` + `status` rather than inferring
from silence. Pair the rerun with the structural argument, which is often conclusive by itself: if the
merged commit is docs/changelog only with **zero deletions** (`git show --stat --format="" <sha>`), it
cannot have broken a timing or stdio test. A green rerun means no revert, no re-merge, and leave the
queued PRs where they are — pulling a dozen correctly-queued PRs because the tip's leg flaked spends
their queue position to fix nothing.

## A new doctor row's seam includes a guard in another package

**Check the right seam.** For a row that reads host state, the load-bearing question is
what it **writes** and what it **execs** — not whether row counts move. The guard spawns
the real door (`python3 hscc-bootstrap/doctor.py --json`) on every suite run, so the new
row executes inside it. Mechanical checks before arming:

1. `git show <ref>:<row module> | grep -nE "open\(|\.write\(|mkdir|touch|shutil|subprocess"`
   → expect read-only modes only (`open(path, "rb")`); any write path is a red review.
2. The fence must refuse **before** any exec, and the row's canary should pin exactly that
   ("under `PYTEST_CURRENT_TEST`, `_run` is never called"), not merely that a refusal-shaped
   row appears. Otherwise the read-only guard catches a leaked probe only by accident, on a
   host where the file happens to exist. The fence is `doctor_rows.under_pytest()`
   (`hscc-bootstrap/doctor_rows.py`) — read it before writing a new row's guard. It keys on the
   `PYTEST_CURRENT_TEST` marker rather than `"pytest" in sys.modules` precisely because that
   marker is **inherited by the spawned child**: the module-local test reads as though it had
   fired only in the process that imported pytest, and the door the agent tool relays is a
   fresh interpreter where pytest was never imported. In the registry, `under_pytest()` is what
   selects a row's `refusal` hook instead of its `loader` — so a row that ignores the fence is
   the leak, and the guard is only as wide as the narrowest row's own fence.
3. Row *enumeration* in `test_doctor_tool.py` is the **weak** signal: it builds synthetic
   rows via `_row()`/`_payload()` and never enumerates `doctor_rows`, so a new row usually
   needs no change there. That is insulation, not corroboration — report it as such and do
   not demand a diff just to have one.

Also ask what the row *actually* measures. A row reading a plist on disk reports what the job
**would be created as**, not what launchd has loaded — keep its wording to "installed job",
never "running job" — and prefer sources proven to carry the value (`launchctl print` has
**no** process-type line; measured).

`hscc-bootstrap/doctor*.py` changes are judged by `hscc-cluster/tests/test_doctor_tool.py`, notably
`TestReadOnly::test_the_read_writes_nothing_anywhere`. Running the row's own test file, the row
registry, and even `-k doctor` inside `hscc-bootstrap` all pass while that guard is broken, because
it lives in a different top-level directory. For any new doctor row, the seam is:

```sh
pytest hscc-cluster/tests/test_doctor_tool.py -q      # cross-directory read-only guard
HSCC_TEST_LEGS=hscc-cluster bash scripts/run_tests.sh # the leg that actually gates it
```

The failure mode is subtle and worth recognising on sight: `a read wrote into the sandbox:
['.hscc/daemon.log']`. A row that reads correctly can still **log**, and logging is a write —
importing `hscc_daemon.autodown` (or anything whose module path emits a WARN through
`daemon_ops.log`) writes `daemon.log` under `HSCC_HOME`. `mode=ro` on the databases is not enough;
the whole read path must be silent. Pass an injected no-op logger, or touch only module constants.


Pull the gate's own verdict from the daemon log rather than guessing it:

```sh
grep -E "merge.?queue" ~/.hscc/daemon.log | grep "<pr>" | tail -5
```

## A reviewer profile that ends without pushing

A reviewer profile that ends without pushing gets gate+push by the orchestrator, and then the
PR is **re-queued**, not hand-merged. The merge belongs to the queue.

## A measurement of zero is a claim; prove the probe can hit

Two vacuous readings landed in one review, and both were caught only because a *contradicting* number was
visible elsewhere in the same output. Treat an empty/zero result as unproven until the probe has a positive
control:

- `hscc merge-queue list --json` returns `{"ok":…, "projects":{<slug>:{queue, waiting, in_flight, queue_len,…}}}`.
  Reading `queue` off the **top level** yields `[]`, so "PR is not in the queue" and `queued=0 waiting=0` print
  with total confidence against a real 12/4. Index `projects.<slug>` (or cross-check `queue_len`, or just read
  `hscc merge-queue status`'s plain `queued=/waiting=/in-flight=` line) before ever reporting membership.
- A count grep like `[0-9]+ -> [0-9]+` silently misses `147 -> **153**`, because markdown bold sits between the
  arrow and the digits. "The changelog count regressed" from an empty grep is a pattern bug until you have read
  the line itself.
- `gh`'s `--jq` fails *inside* the expression — `--jq '.'.$f` gives `unexpected token "state"`, and
  `(.*body|.[0:200])` gives `function not defined: body/0` — and on some argument errors `gh` prints **usage
  text** where the value should be. Ask for one shape per call (`.state`, or
  `.comments[] | .author.login + " :: " + .body`), and treat a line of help text in the output as an argument
  error rather than as the PR's contents.
- `map(select(.conclusion=="failure"))` fed a *streamed* `."check_runs"[]` dies with `expected an object but
  got array`. If you skim past that line you enqueue on a vacuous `failures=0`. Wrap the stream
  (`[.check_runs[] | …]`) or index the envelope in python.
- To inspect a whitespace-only delta, `cat -A` is GNU-only and dies on macOS with `illegal option`; use
  `sed -n l`, which works on both. A `--stat` of `2 --` with no lines under `grep -E '^[+-][^+-]'` is two
  blank lines, not lost code.
- Before calling a large registration count "rot", run the probe that would actually make it a defect:
  `git worktree prune -n` reported **0 stale of 448** entries, so the alarming number was live directories,
  not dead registrations, and no card was warranted. A count is evidence only once you know what its
  defect-signalling value would be.

## A PR head can move between two of your own tool calls

Re-`git ls-remote origin <head-ref>` immediately before any verdict, comment or merge decision that depends on a
head sha. Another lane — a second session of the same orchestrator profile, or a worker's own cleanup phase — can
advance the ref while you are mid-review, and every actor on the repo pushes under one git identity, so the
advance is **not attributable** from your side. Say so plainly ("advanced by another lane, verified by me")
rather than claiming the push.

If the ref already sits at the sha you were about to push, **do not push again**: a second force push over it
rewrites a head that is already correct. Instead re-run every gate against the new head, and use the tree as the
oracle — `git diff --quiet <reviewed> <new>` empty plus equal `rev-parse <c>^{tree}` proves the reviewed content
is the landed content, which retires the code re-review entirely.

## A PR family: base on main, never on a chain tip

A stack of PRs on branch tips is cheap to create and expensive to land, and every
rung re-conflicts each time `main` moves. Rule for any family (same defect class,
one unlock PR):

- **New PRs branch from `main`, or at most from the single unlock PR's tip.** Never
  from a sibling's tip. Depth is what makes a landing cascade long and turns each
  `main` move into N rebases; if a family reaches three rungs, stop and land the
  base first.
- Measure depth before queueing: `git rev-list --count origin/main..HEAD` against
  the PR's own commit count. If it exceeds what the PR owns, it carries other
  lanes' work.
-  **Measure the head ref, never the base ref.** On a stacked PR `baseRefName` and
  `headRefName` differ *by design*, so every ownership, depth, `merge-tree` and
  own-delta command pointed at the base silently describes the **parent branch** —
  it can print "this PR owns zero of its own commits" for a PR that owns two, and
  that number looks like a finding about the PR rather than about your command.
  Resolve it once and use it everywhere:
  `H=$(gh pr view <n> --json headRefName --jq .headRefName)` → `origin/$H`.
  Never shorten that name with `cut`/`head` to fit an output line: a truncated ref
  makes every `rev-list`, `merge-tree` and `shortstat` return **empty**, and an empty
  shape against a nonexistent ref reads exactly like an empty PR. Empty output means
  re-derive the ref before reporting anything about the PR.
- **A child's own commit may import a file only the parent creates.** Then `conf=0`
  and a clean retarget are both true and the head is still not landable: it imports a
  module `main` does not have. Check before retargeting —
  `git cat-file -e origin/main:<path>` or `git grep -c '<new symbol>' origin/main`.
  When the symbol is missing the blocker is an **ordering constraint, not work**:
  park the card as `scheduled` (never `blocked` — its parent is done, so nothing would
  ever wake it) with the retarget recipe in the reason.
- `mergeable=MERGEABLE` and `mergeStateStatus=CLEAN` prove nothing about the gate,
  which **rebases** per claim: a rebase drops merge commits and replays their
  non-merge ancestors. List them — `git rev-list --reverse --no-merges
  origin/main..HEAD` — and test each with `git merge-base --is-ancestor <c>
  origin/main`. Anything NOT in main that the PR does not own lands inside it under
  an unrelated title. A `Merge ... (stacked on #N)` commit is the tell.
- Foreign-but-live carried commits are not automatically wrong — they may be the
  only surviving copy (check whether the closed parent's content really is on main:
  `git grep -c '<new symbol>' origin/main`). Pull the PR from the FIFO and require
  a split, or an explicit note plus a changelog fragment, so merge surface matches
  merge content.

## The gate budget must exceed the suite's real wall time

If `merge_queue.timeout_s` is smaller than `scripts/run_tests.sh`'s wall time, the
PR times out, returns to the FIFO with **attempts unchanged**, and is re-claimed
within about a minute — an unbounded starvation loop that starves the FIFO head and
never trips `MAX_ERROR_ATTEMPTS`. Evidence shape: repeated `gate timed out after
Ns` lines for one PR while `attempts` stays `0`. A silent in-flight entry is NOT
proof of a dead claim: a gate with captured output writes nothing until it finishes,
so decide with the budget clock, not with file mtimes. Do not pull a PR while its
gate is live — a mid-gate removal wedges the queue until the deferral fix lands.
Raising the budget or breaking the loop deliberately is an operator decision, not a
queue shuffle.

## A card waiting on a PR merge belongs in `scheduled`, not `blocked`

`blocked` is a human-attention state, and a card whose only remaining dependency is a
PR merge needs no human decision — so leaving it blocked turns finished work into a
fake ops item that never clears. Check the parent first: if the card's parent is
already `done`, there is no parent-finishes-promotes path, and `blocked` is a dead end.

Park it as `scheduled` instead, carrying `SCHEDULED_UNTIL=<ISO8601>` in the reason plus
the wake-time actions (merged → complete citing the squash sha on main; still queued →
check queue health, change nothing; gate red → the handback names the statement to fix),
and name who wakes it — you. `kanban_schedule` **does not create a timer**: it parks the
card and makes it non-dispatchable until an orchestrator unblocks it, so a scheduled card
with no owner is as stranded as a blocked one. Set the wake to a realistic estimate (one
gate cycle × the PR's FIFO position) rather than a hopeful soon.

A worker that blocks with a correct "dependency, not work" diagnosis is right about the
substance and wrong about the column; move the card and say why on it, and do not re-open
the finished run. When the retry cap has tripped (`GAVE UP` then `BLOCKED`), the crash
loop has already stopped on its own — do not treat the crash as a second problem to chase.

## A burst of unprocessed wakers: one consolidated read, not N answers

When several red-sweep "green + CLEAN + enqueued" notices have queued up unprocessed, do
not reconcile them one at a time — each costs a round trip against a moving tree, and the
premise of the first is stale by the time you reach the last. Take one read that walks
every named PR (state, base, merge count, behind, `merge-tree` conflict count, hygiene)
plus the queue's `in_flight`/FIFO/waiting in a single pass, then act on the handful that
actually differ from what the notices claim. Most closures are legitimate supersessions
whose successors are already in the FIFO; those need one sentence, not a review. Look for
the ones that do not: a **closed** PR reported as enqueued, a PR that is `MERGEABLE` but
base-not-`main`, and a PR whose commit count exceeds what its card owns.

## Rebasing the unlock PR orphans the SHA every downstream branch carries

When a stacked family hangs off one unlock PR, rebasing that PR (to fix `behind`, or
to resolve a conflict) does **not** move its children. The old commit stays an
ancestor of every branch built on it while becoming an orphan: not on `main`, not on
the rebased branch. Measured on one evening: `66d3cf59` orphaned by #761's rebase and
still carried by five branches. When the unlock PR then lands under the new SHA, each
downstream rebase replays the orphan onto content main already has — usually dropped
by patch-id, sometimes a conflict, occasionally a duplicate landing.

So verify a rebase **by own-delta, never by ancestry**: `git diff --name-only
origin/main...HEAD` must list only the files that card owns, and the file set is the
claim. If a rebase conflicts on the unlock PR's files, that is evidence the two SHAs
diverged — stop, report, re-derive from `main`; do not reconcile two versions of
another lane's fix by hand. Re-check any hardcoded `--onto <anchor>` immediately
before using it (`git log --oneline <anchor>..HEAD` should print exactly the commits
the card owns); an anchor verified an hour ago may have moved.

## Do not push to a PR the queue is holding

A push to a queued PR's branch discards its gate cycle. The ledger says so:
`PR #N head moved from the pushed <sha>` then `PR #N -> waiting`. The entry
self-heals only after CI re-runs on the new head (~40-48 min for a `hscc_daemon`
leg), so each push costs the single runner a full cycle, and `attempts` stays
unchanged so nothing surfaces it.

**Do not blame a lane until you have run the discriminator** (fixed by #778 after
this was got wrong twice in one evening): compare the sha named in the WARN with
the PR's current head —
`gh pr view <N> --json headRefOid`.

- **Equal** → the release was **spurious**: the queue force-pushed its own rebase
  (`PR #760 pushed 1055f33e; CI pending, holding the claim` is that path) and
  `gh pr view` was still serving the pre-push head. The queue rebases PRs itself,
  so `behind` clearing with a byte-identical diff is the tell. Do not tell a lane
  it pushed.
- **A third sha** (neither pushed nor pre-push) → a genuine out-of-band push, and
  releasing the claim is correct.

Tell the lane: rebase **before** `merge-queue add`; if the head must move after
queueing, land a successor branch with `supersedes #N` and close the old PR.

## A gate speedup that defaults off is not a speedup

When a PR adds parallelism or a fast path to the runner, check who turns it on
before crediting the measurement. `HSCC_TEST_PARALLEL` defaulting to serial, with
`merge_queue.py` holding 0 references to it, leaves the gate exactly as slow as
before however good the measured 1.96x is. Correct for a green main — ship the
mechanism opt-in with its measurements rather than on by default and red — but
report it as a mechanism, not a shortened gate, or the next large PR gets queued
believing the wait halved. The same check applies to the digest: if legs can
interleave into one stream, `_LEG_FAILURE_RES` (`^✗\s`) and `_TEST_FAILURE_RE`
(`^(?:FAILED|ERROR)\s`) still match while losing which leg a `FAILED` belongs to.

## An add/add resolution: declare the expected total first, or a green count hides a deleted side

When two lanes each append tests to the same file (the usual shape for a hot test module), each branch alone
reports `base + N` and is **count-identical** to the other. Resolving their add/add by keeping one side then
prints a green, plausible, wrong number: `160 passed` can mean "both sides kept" or "one lane deleted
entirely". The count alone cannot tell you; only arithmetic declared in advance can.

Before reviewing or hand-merging such a resolution, write down `base + N + M` from measurements you took
yourself (`--collect-only` on `origin/main`, plus each lane's added case count) and require exactly that in
the result. Two lanes each adding 7 cases to a 150-test file means **164** together; a merge reporting 157
has deleted a lane, and that number is green, plausible and wrong — invisible to any count-only check. The
queue gate that would catch it never runs for a hand-merged PR, so the arithmetic is the only guard.

Resolve add/add mechanically rather than by hand-editing, and prove **both** directions of losslessness:

```sh
# during the rebase, from the three stages git leaves behind (ours = the new base)
git show :1:<file> > base.F; git show :2:<file> > ours.F; git show :3:<file> > theirs.F
git merge-file -p --union ours.F base.F theirs.F > resolved.F
diff ours.F resolved.F | grep -c '^<'                              # MUST be 0: nothing of the base side dropped
grep -oE '^(def|class) [A-Za-z_]+' resolved.F | sort | uniq -d     # MUST be empty: a shadowed test passes while testing nothing
```

A scripted union resolver can look symmetric and still drop the base side's helper changes, which is why the
0-deletions check runs against *ours* and the duplicate check runs against the whole file.

Then prove the rebase preserved the **reviewed contribution** by comparing contribution against contribution
across the two bases — not by counting deletions against main:

```sh
git diff <oldbase>..<oldhead> -- <file> | grep -E '^[+-][^+-]' > c_old.txt
git diff <newbase>..<newhead> -- <file> | grep -E '^[+-][^+-]' > c_new.txt
diff -q c_old.txt c_new.txt && echo PRESERVED
```

**Never gate on "zero deletions versus the new main."** A reviewed contribution legitimately rewrites a line
(replacing a comprehension line, say), so that gate fails on correct work — and a gate that blocks correct
work teaches you to override gates. Deletions are only meaningful relative to the same base on both sides.

## A wording card's pin must be mutation-tested, and the dodge to look for is widening the allow-list

For a card whose deliverable is prompt/workflow text pinned by a test, the failure mode is a pin satisfied by
widening a token allow-list rather than by changing the text. Check `git show <own-commit> -- <pin-test>` for
added allow-list lines (**expect zero**), then mutate the *reason* clause in the source — not the keyword — and
require the pin to go red. Assert the anchor occurs once and that the mutation landed before trusting the
result. `2 failed, 94 passed` on the mutation with `96 passed` after restore is the evidence that the clause is
held rather than merely present; without it a wording card ships decoration.

## A card's PR premise goes stale — find the successor PR before you rebase or advance

A card naming a head sha describes the world as of when it was written. Before rebasing, advancing or
enqueuing on that premise, re-read the PR and look for a successor:

```sh
gh pr view <pr> --json state,stateReason,closedAt,mergedAt
git ls-remote origin | grep -E '<branch>|<branch>-rebased|rebase/.*<pr>'   # sibling refs = another lane's rebase
```

The mechanism that makes this bite: an unattended lane that must rewrite a head hits the force-push guard, so
it pushes the rebased chain to a **new ref** — and from there it either posts the lease-pinned advance for the
orchestrator **or opens a second PR and closes the first to keep one review surface**. Both are correct worker
behaviour, both leave the original card reading "head `<sha>`, enqueued", and only one of them is landable.

If a successor exists, verify **that** head on your own instruments and land it; retire your duplicate in
writing on the successor rather than quietly restoring the old head — a second force-push on a branch that
belongs to a closed PR buys nothing and hides the duplicate work. Landing the other lane's surface instead of
defending your own is the correct move when both are green: check which is open, which is conflict-free, and
whether the two differ by anything but whitespace (`git diff <theirs> <yours> --stat` with no content lines
under the `+/-` grep means blank lines only).

Symptom worth recognising: a **closed** PR still holding a FIFO slot. That is not a new queue defect to card —
it means the *running* daemon predates the rule rejecting merged/closed entries before any gate, so the fix is
on `main` and inert. Same evidence family as the OPEN-before-add check above: membership is daemon state, PR
state is truth, and a lingering closed entry belongs in the deploy case you are already making.

**A `done` card with zero comments is not evidence-free — read the body, not just the thread.** Workers
put the DONE/LEFT report in the card body's report block, and it is the cheapest place to learn the PR's
base branch, whether the branch depends on a sibling PR for a symbol, and the disposition of what was
*not* done. Grep the body for the scope words (`DONE`, `LEFT`, `Not touched`, the file names you are
worried about) before declaring a closure unverifiable — a 0-comment card plus a `worker_result` event
carrying `verified_cards` is a full handoff. Pair the body with PR state, which decides whether `done`
was legitimate at all: a card closed `done` while its PR is still `OPEN` is a premature closure. Review
and land the PR; do not re-open the card and do not comment on it as though the worker still owed
something.

**The same distrust applies to a card's claim about *where* its driver runs.** A blocked body reading
"[opencode] running in tmux `oc:<id>`" is a self-report about a live process: verify it by process
and log — `pgrep -fl <task_id>` plus the tee'd `~/fleet-check/opencode-runs/<id>.log` — because the
named pane may not exist at all (measured: no tmux server on the host) and a missing pane is not a
dead lane. Settle liveness before concluding a parked card is stranded or touching a queue you do
not own.

## A review verdict must separate your evidence from the author's

Close every review with one line naming **which numbers you generated and which you did not**. A
measurement quoted into your comment reads as corroborated to the next reader whether or not you ran
it, and that is exactly the assumption a later merge decision is made on: write *those are your
numbers, uncorroborated by me*, and name the gap's cause in one clause when you could not execute
locally. A review built from diff + hygiene + a read of the decision path is a legitimate review and
needs no apology — it just must not imply it also ran the tests. The converse error is worse: adopting
the author's characterisation of *why* something failed (a defect class, a queue behaviour) without
reading the source, which is how an inherited diagnosis becomes a card aimed at the wrong axis.
