# CI backstop for the address guard — t_9a4b7687

Status: implemented on `wt/t_9a4b7687`; enforcement posture is an operator
setting, not a code change.

## 1. What was asked, and what the card got wrong

The card asked for an operator decision (advisory vs blocking) *before* a CI job
could be written, and listed acceptance criteria. Two things were wrong with that
framing, both resolved here:

* **The decision did not need to block the work.** Enforcement is now a GitHub
  repository variable (`ADDRESS_GUARD_ENFORCE`), so advisory, blocking-on-PRs and
  blocking-on-main are three settings of one shipped job. Writing the job first
  was strictly better than waiting: the operator flips a variable instead of
  waiting on a code change.
* **One acceptance criterion, taken literally, would have created a leak.** The
  card asks that "a push/PR containing a real-shaped address fails ... with the
  offending `file:line` visible in the job log". `pom11/hscc` is a **public**
  repo and Actions logs are **public**, while `scripts/address_guard.py` prints
  `rel:line: <the matched address>` — its report is designed so a local
  committer can see and fix the value. Echoing it on CI would have published,
  in the job log, the exact class of secret the guard exists to stop, for every
  future leak. See §4 for how this is squared.

## 2. Prerequisite that blocked the job (and is now closed)

The card assumed `scripts/address_guard.py --tracked` "already exists". It
existed only on the approved parent branch `wt/t_ec2c2f95` @ `92470cd8` — the
parent was board-DONE but **not on `main`**, so a workflow calling it would have
failed on a missing file on its first run. This is the recurring stranding
pathology (card DONE, work stranded on `wt/*`).

Landed as orchestrator duty, not as card work: `wt/t_ec2c2f95` merged to
`main` @ `db6bcb9b`, pushed, `92470cd8` verified an ancestor of `origin/main`.
Plain merge, no rebase (the worktree was clean at the approved tip, so
per-commit patch-id equality with the approved commit holds trivially). Verified
before pushing: `address_guard.py --tracked` exit 0 on the merged tree (0.18 s),
58 guard tests green on py3.11.16 and p313.12 at the merge commit.

## 3. Why CI is the right layer (question (b) answered by measurement)

The open question "does the public remote have any CI runner wired at all?" is
answered: **yes**. `check-runtime-deps` has run daily on `ubuntu-latest` and
succeeded every day from 2026-09-29 through 2026-10-08 (10 consecutive
scheduled runs observed), and `GET /repos/pom11/hscc/actions/permissions`
returns `{"enabled": true, "allowed_actions": "all"}`. So no server-side hook or
scheduled history audit is needed as a substitute — the server-side check the
card wanted is available.

`main` was **unprotected** (`404 Branch not protected`), which matters for the
posture decision: a red job alone does not block a merge until the check is made
required.

A second point the card did not make: `main` receives commits through merges
(40/40 of the last 40 commits on `origin/main` are merge commits), so a
PR-triggered job sits on the real merge path, not a decorative one.

## 4. The public-log problem

The guard's report is `rel:line: <address>`. The job needs `file:line` and must
not print the address.

**Rejected: scrubbing by line shape.** My first implementation used
`sed -E 's/^(  [^:]+:[0-9]+): [^ ]+$/\1/'`. It looks reasonable and it is wrong:
the t_ec2c2f95 review probe battery established that paths containing `:` are a
supported evasion the guard must report (`test_path_evasion`-style cases), and on
exactly such a path — an offender line `docs/we:ird.md:3: <LAN host>` — the shape
regex fails to match and the address goes to the public log unscrubbed. A
redactor whose failure mode is "silently stops matching on adversarial input" is
the wrong tool for a security control.

**Chosen: reuse the guard's own pattern.**
`.github/scripts/redact_guard_report.py` imports `FORBIDDEN` from
`scripts/address_guard.py` and substitutes the match with `***`, keeping
`file:line`. Pattern knowledge stays in exactly one place (the one-regex rule the
parent card established), it cannot drift, and it is path-agnostic. It **fails
closed**: if the pattern cannot be loaded it exits 3.

> **Corrected in review round 1 — do not read "fails closed" as "cannot leak".**
> My first version of this section said it "exits 3 WITHOUT echoing anything, so
> an unredacted address can never reach the log on our account". The exit code
> was always right; the claim was wrong, and the reviewer reproduced both
> channels (§10). Exiting non-zero is not the guarantee, because a failure path
> also WRITES TEXT to the log. The guarantee now comes from three things this
> file does instead: every diagnostic is a fixed string plus the exception TYPE
> (never `str(exc)`, which can quote the address); the guard's import runs under
> `catch_warnings()` with its stdout/stderr captured and discarded; and a loaded
> `FORBIDDEN` that is not a compiled regex is rejected rather than allowed to
> raise an `AttributeError` traceback into the log.

Verified behaviour of the real command chain (`docs/ledger.md` holding a real-shaped
NAS host), showing what a public job log would contain:

```
REAL OPERATOR ADDRESS in the tracked tree — this repo is PUBLIC, the commit is blocked.

  docs/ledger.md:3: ***

Scrub them to the documented placeholders:
  LAN node     -> 10.0.0.x
  tailnet host -> 100.64.0.1
```
guard rc=1 · redactor rc=0 · address absent from the log · `file:line` present.

## 5. Fail-closed, and the one place advisory mode must not be advisory

Advisory mode must not be advisory about the guard's own health. `rc=2`
(the guard could not run) exits 2 in **both** postures. This is why the step
computes its exit code itself rather than delegating to
`continue-on-error: vars.ADDRESS_GUARD_ENFORCE != 'true'` — that construct would
also have swallowed `rc=2`, turning a broken guard into a silent green pass,
which is precisely the failure that let the 2026-10-08 leak reach `origin`.

### Evidence on real GitHub Actions (not simulated)

| Run | Branch | State | Result |
|---|---|---|---|
| `37845099684` | `wt/t_9a4b7687` | clean tree | **success**, 8 s |
| `37845289259` | `probe/failclosed-9a4b7687` | `scripts/address_guard.py` deleted | **failure**, 9 s — fail-closed in advisory mode confirmed |
| `37845798597` | `wt/t_9a4b7687` | after rc=2 precedence fix | **success**, 10 s |
| `37846997118` | `wt/t_9a4b7687` | final | **success**, 11 s |

The fail-closed run exposed a **diagnostic-order bug** worth recording: with the
redactor branch checked first, a checkout missing the guard file reported
`exit 3` / "redactor could not load the guard pattern". The outcome was correct
(red), but the message blamed the wrong component — the missing file is the
guard's, and the redactor only fails *because* the guard is gone. Fixed by
testing `rc=2` before `redact_rc`, pinned by
`test_guard_cannot_run_is_reported_before_the_redactor`. The probe branch was
deleted from remote and local after the measurement; it never carried an address.

### The rc=1 branches cannot be shown on CI, and why that is not a gap

Proving "a push containing a real-shaped address goes red" on real CI requires
pushing a real-shaped address — the leak itself, to a public repo. So those
branches are covered by extracting the **actual step script from the YAML at
test time** and running it verbatim against a stub guard (the redactor and the
step logic under test are the shipping ones, not a copy):

* clean tree -> exit 0, no annotation
* leak, `ENFORCE` unset -> **exit 0 + `::warning::`**, `file:line` present, address absent
* leak, `ENFORCE=true` -> **exit 1 + `::error::`**, `file:line` present, address absent
* `rc=2` -> exit 2 in both postures

## 6. Test placement, which was a real hole

`.github/scripts/tests/` already existed (`test_check_runtime_deps.py`, 10
cases) and was collected by **nothing**: not in `run_tests.sh`'s `DIRS`, not in
any workflow step. My 23 cases would have inherited that — a security
control's own tests rotting silently is the same class of bug this card exists
to prevent (a correct check with the wrong trigger point). `DIRS` now includes
`.github/scripts`; the new leg costs ~0.7 s and runs 33 tests.

## 7. Operator decision, reframed

The job ships **advisory** (option a). The three postures the card listed are
settings of the same code:

* **(a) advisory** — current state, no operator action. A leak is a warning
  annotation; the job stays green; nothing blocks.
* **(b) blocking on PRs** — `gh variable set ADDRESS_GUARD_ENFORCE --body true`
  plus requiring the `address-guard / guard` check in branch protection.
* **(c) also enforced on main** — the above plus enforcing it on pushes.

Recommendation, and the reasoning, are in the card comments. The tension the
card raised is real and asymmetric: a red check on `main` is the signal it wants,
but the emitter scrub (`t_56dda2c8`) is not yet verified live, so the orchestrator's
own ledger ticks would turn CI red post-merge and train everyone to ignore the
check — which is worse than advisory. Advisory now costs nothing (the log is
read on every leak-shaped event) and the flip is one command whenever the
emitter is proven.

`concurrency.cancel-in-progress` is `false` deliberately: a **cancelled** check
counts against branch protection, so `true` would have made (b)/(c) flaky. The
job is ~10 s; racing it buys nothing.

## 8. Deliberately not done

* **No git history rewrite.** Pre-scrub addresses remain in pushed history
  (`bbaeaa2f` ledger lines 4217/4220 were the measured recall in the parent
  review). That is the operator's separate call, per the card.
* **No `--all-history` mode.** An earlier comment on this card speculated about
  a scheduled `address_guard.py --all-history` audit; checked at implementation
  time, no such mode exists (`argparse` exposes only `--staged`, `--tracked`,
  `--repo`). If history scanning is wanted it is a new parent-side feature, not
  something to bolt on here.
* **Pattern-class limits unchanged** — the sanctioned `100.64.0.0/24` fixture
  block stays allowed and `10.x` / `172.16.x` stay out of scope, exactly as
  shipped by the parent. The CI job inherits the detector's scope, by design.

## 8b. Gate hygiene: how I produced a false failure

The first full-suite leg ran from the live task worktree while I was still
editing it, and reported `✗ hscc_daemon` —
`test_no_real_operator_address_in_tracked_files` flagged
`docs/audits/ci-backstop-address-guard-t_9a4b7687.md:68`. That was a *correct
verdict about the wrong checkout*: the leg was stamped `commit: 7a7b5241`, but
`scan_tracked` reads files from **disk**, and the audit doc still carried a
literal address in a code sample at that moment (scrubbed two commits later). A
real failure, not a flake, and entirely self-inflicted.

The repo rule already covers this — full-suite verification belongs in a **clean
detached worktree at the target commit** — and I broke it by launching the gate
before the branch was finished. The re-run is stamped with `commit`,
`dirty_files: 0`, interpreter and worktree path so the claim is auditable. Two
lessons worth keeping:

* never start the suite from a worktree that is still being edited;
* the CI backstop's guard test catches *my own* prose leaks, which is the point.

## 9. Reading logs safely — measured, and deliberately carded not shipped here

The guard's report format carries the value, so any surface that publishes the
report publishes the leak. Severity, measured rather than assumed:

* **The shipped `address-guard` job's logs are clean.** Its step redirects both
  streams to files and echoes only redacted output. Measured on run
  `37845289259` (157 lines): scanning the raw `gh run view --log` dump with the
  guard's own detector returned **0 hits**.
* **The one leak that actually happened was a local suite log**, not CI: a
  full-suite leg during this card echoed `docs/audits/…:68: <live LAN host>` into
  a workspace file that was nearly attached to the card. Caught by scanning the
  log with the detector before attaching; the file was then scrubbed in place.
  All four gate logs in this card's workspace scan clean.

So the residual gap is procedural, not an open hole in the shipped path: nobody
has a sanctioned way to read a CI log, and no rule says "scan a log before you
paste, attach, or comment it". A raw `gh run view --log` on a job that ran the
guard *without* stream redirection (a manual dispatch, an ad-hoc step in another
workflow, any pre-existing run) would print the address into scrollback.

A `ci_log.sh` wrapper (600-mode temp dump, print only redacted bytes, keep-with-
warning behind `--keep`) plus the file-argument mode the redactor needs and one
written rule is ~50 lines plus tests — but it is **not this card's scope**, so it
is **`t_914b8db5`** (devops-engineer, child of this card) with the acceptance
criteria and the fake-`gh` test design written out. Whoever builds it: do not
send `gh`'s stderr to `/dev/null` — those bytes cannot be proven to be non-job
content, so they need the same redaction, and swallowing them also hides real
`gh` errors.

## 10. Review round 1 — two publish channels, both reproduced

Reviewer verdict: REQUEST CHANGES. Everything else passed independent
re-execution (all three acceptance criteria, my gate logs, their own full-suite
leg at the tip, all 10 CI runs verified via `gh`, merge-tree 0 markers). One
defect blocked approval, and it was in the one place I had documented as an
absolute — see the correction in §4.

**Channel 1 — `str(exc)`.** The load-failure diagnostic interpolated the
exception message. A guard module that raises at import with an address in the
message published it verbatim while still exiting 3. I reproduced this
independently before touching any code:

```
redact_guard_report: CANNOT LOAD PATTERN — boom at <LAN host>     # rc still 3
```

**Channel 2 — import-time warnings (the escalation, and the one that matters).**
The step ran the redactor with no stderr redirect and no warning filter, and
`exec_module(guard)` runs *inside the redactor process*. Python's default
warning renderer prints the offending SOURCE LINE. So:

- guard FULLY FUNCTIONAL (correct pattern, correct detection, correct rc=1,
  report correctly redacted to `***`), ADVISORY posture;
- one edit: a debug `NOTE` string naming a real-shaped tailnet host inside a
  non-raw literal containing `\d` — a single missing `r` prefix;
- result: the guard's own source line, address included, reached the PUBLIC job
  log on EVERY run while the check reported **GREEN rc=0**.

That last part is what took it from "broken-guard corner case" to the card's
worst input class. CI checkouts have no `__pycache__`, so the module recompiles
each time, and on Python ≥ 3.12 (the runner) invalid-escape is a SyntaxWarning
shown by default. Activation needs a leak-shaped edit to the DETECTOR, which is
exactly what the local hook blocks at commit time — and this card exists
precisely for the hook-bypass set (`--no-verify`, `git apply --cached`, an
un-bootstrapped clone). Dormant on today's tree (measured warning-free on 3.11
and 3.13); live-class by this card's own threat model.

**Fix** (three commits, inside files this card already ships):
`f33b0e40` redactor, `20e53cb1` step, `16113b09` tests. Fixed-string diagnostics
carrying `type(exc).__name__` only; `catch_warnings()` plus captured-and-discarded
stdout/stderr around the guard import; `isinstance(pattern, re.Pattern)` or fail
closed; no traceback can escape `main()`.

**Layer independence, measured rather than assumed.** The step also passes
`-W ignore -E` now, which on its own would suppress channel 2 — so a test that
only used the flagged form would still pass if `catch_warnings()` were deleted.
`test_warning_channel_closed_by_the_redactor_itself` runs the same input as bare
`python3` with no flags to pin the redactor's own silencing. Reverting the
redactor alone fails 5 of the new cases; reverting the workflow alone fails 6.

**Two non-blocking advisories taken as well** (both cheap, both about failing
open): `guard.out` is now piped through the redactor instead of `cat`'ed, and
`ENFORCE` is normalised so `True`/`TRUE`/`1`/`yes` enforce — a variable named
ENFORCE must not go advisory on a capitalisation.

## 11. Files

| File | Purpose |
|---|---|
| `.github/workflows/address-guard.yml` | the job; enforcement posture documented as an operator variable |
| `.github/scripts/redact_guard_report.py` | public-log-safe redactor; imports the guard's pattern; fails closed AND publishes nothing on the way down |
| `.github/scripts/tests/test_redact_guard_report.py` | 51 tests: end-to-end job legs, structural workflow assertions, verbatim step-script decision states, fail-closed publish channels |
| `scripts/run_tests.sh` | `DIRS` += `.github/scripts` |
| `changelog.d/t_9a4b7687.md` | fragment (kind: Security) |
