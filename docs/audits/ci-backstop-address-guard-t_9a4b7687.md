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
> also WRITES TEXT to the log. The guarantee now comes from four things this
> file does instead: every diagnostic is a fixed string plus the exception TYPE
> (never `str(exc)`, which can quote the address); the guard's import runs under
> `catch_warnings()`, `redirect_stdout/stderr` **and fd-1/2→/dev/null** (§10c —
> the first two rebind objects, only the descriptor layer stops `os.write(2,…)`);
> `main()` catches `BaseException`, not just `Exception`, so no CPython traceback
> (whose frame text IS guard source) escapes; and a loaded `FORBIDDEN` that is
> not a compiled regex is rejected rather than allowed to raise an
> `AttributeError` traceback into the log.

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

**That last clause was itself a false absolute — corrected by review round 2
(§10c):** `except Exception` does not catch `SystemExit`/`BaseException`, and an
uncaught one prints a CPython traceback whose frame text IS the guard's source
line. The docstring here and in the redactor both repeated it until round 2.

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

## 10b. Resume after the operator's history rewrite (run 955)

Run 953 was SIGTERM'd by the dispatcher while the operator paused the card for
an authorized history rewrite (force-push re-SHA'd `main`). Resuming surfaced
three things worth recording:

1. **A poisoned index survived the kill.** The worktree carried a *staged,
   uncommitted* merge resolution left by the dead run — and its content
   **reverted the operator's scrub**, restoring pre-rewrite text in the ledger
   and in `ios-app/docs/templatedetailview-audit.md`. No `MERGE_HEAD` existed:
   orphaned index state, not a merge in progress, so nothing but a
   `git status --porcelain` at resume would ever have shown it. Had it been
   committed (e.g. by an auto-commit step or a careless `commit -a`), the
   branch would have resurrected masked-range text the rewrite deliberately
   removed — and `address_guard --staged` returns **rc=0 on that content**,
   because a masked range (`192.168.88.x`) is not a concrete address the
   detector matches. The control did not catch this; noticing did. Rule
   learned: after any reclaimed run, inspect the index before doing anything.
2. **The branch had merged the pre-rewrite main.** Local tip `3b8c7dd9` merged
   `685eeb9e` (rewritten lineage), so content was already scrubbed-clean; the
   remote branch ref `a620967c` still pointed at pre-rewrite ancestry and had
   to give way (force-push of the local tip, not the reverse). `git diff` of
   the two trees = exactly the two scrubbed doc files.
3. **Landing math re-checked against the *new* main** (`06746d40`) with
   `git merge-tree`: 0 conflict markers; the DIRS hotspot resolves to the
   union `… memori_byodb scripts .github/scripts` (t_95d864fc's token + this
   card's, disjoint as designed); the ledger keeps main's 05:46 tick and its
   verification section (branch adds, never drops, ledger lines post-merge);
   scrub intact. A clean `git merge origin/main` then produced `26f51c1d` —
   the exact tip the round-2 gate re-stamps below (ledger-only delta of 30
   lines, so the fast legs at the previous tip stay meaningful, but the
   authoritative stamp is taken at the tip that will actually merge).

## 10c. Review round 2 — the same class again, and what "sealed" had missed

Reviewer verdict at `5fc58b4a`: REQUEST CHANGES again, with both round-1 items
verified genuinely landed (their own probes clean, 41/41 mine green on both
interpreters, their own full-suite legs at the tip). The block: two publish
channels in the SAME input class that my round-1 fix had not closed — and my
round-1 audit text had repeated the overclaim (`"no traceback can escape
main()"`, `"captured into a buffer that is discarded, never echoed"`).

**Reproduced before touching code**, with my own harness (`orch_r3/probe_r3.py`
in the task workspace — not the reviewer's, per their instruction to
re-implement), driving the SHIPPING step script parsed verbatim from the YAML
plus the redactor alone, stub guards carrying `_addr`-assembled values, output
scanned with the shipped detector's own `FORBIDDEN`: **10 leaks on py3.11.16 and
10 on py3.13.7** at `5fc58b4a`.

1. **BaseException out of `main()`.** `raise BaseException('fatal at <addr>')`
   and `sys.exit('cannot run with <addr>')` at guard module level walk past
   `except Exception`; CPython prints the traceback, and the frame text IS the
   guard source — value in the log twice (both step redactions), rc=3. The
   `sys.exit` form is the plausible one: locally it shows the author only the
   message line, never the CI traceback.
2. **fd-level writes.** `os.write(2, b'<addr>')` at import, functional guard,
   ADVISORY posture: value in the log, **job GREEN rc=0** — the probe7 shape
   round 1 escalated for, arriving through a different door. `redirect_stdout`/
   `redirect_stderr` rebind the *objects*; fd 1/2 still point at the step's
   inherited pipes. My own extra probe (a warning from a thread the guard spawns
   at import) was caught by the existing layers — the descriptor write is the
   real hole.

**Fix** (4cbdd51b + 3332f372, same files as before): `except BaseException`
around `load_pattern()` with the same fixed-string, type-name-only diagnostic;
`os.dup` + `dup2`/dev-null over fds 1/2 for the duration of `exec_module`, real
descriptors handed back in `finally` — restore path deliberately NOT a fresh
`io.open(1, ...)` (that orphans the interpreter's std wrapper, whose GC can
close fd 1) and the trailing flush guarded so a guard that closed our streams
cannot abort the restore (the reviewer's non-blocking note). The two
python-level layers stay INSIDE the fd layer — three layers, defense in depth.
(Superseded by 10e: all three were in-process windows and all three are gone —
the import now happens in a child process.)

**My fix differs from the reviewer's prototype on purpose** at that restore
point (`sys.stdout, sys.stderr = saved` instead of reopening fd 1/2); the
prototype was evidence, not to be copied.

**Mutation-tested my own tests this round** (the reviewer caught a vacuous
mutation on themselves in round 2; I checked mine): M1 drop `dup2` -> 3 failed
(exactly the fd tests); M2 back to `except Exception` -> 2 failed (exactly the
new ones); M3 drop BOTH python-level layers -> 1 failed, not the 2 the reviewer
measured pre-fd — expected, since the fd layer now also covers the bare-mode
warning case (redundancy, their words: "fine for a fail-closed control"); M4
drop the `isinstance` gate -> **SURVIVED at first**, so I hardened
`test_nonpattern_forbidden_fails_closed_without_traceback` to pin the
diagnosis name (`CANNOT LOAD PATTERN [TypeError]` vs the mutant's
`UNEXPECTED ERROR [AttributeError]`) and it is now caught. A surviving mutant
is a test that doesn't test — worth saying when it happens on your own work.

## 10d. Pause-induced gate legs must be stamped as such

(Run 953/955 note, kept from the routing comment so re-review does not read it
red: the round-3 p313 leg `RUN_TESTS_RC=141` at 06:14:41 was the dispatcher
SIGTERM of the operator's pause, not a test failure — rc=141/143 class already
guarded via t_6bb29d46. Round 4's two clean legs at `5fc58b4a` supersede it;
the round-5 legs at this round's tip supersede those.)

## 10e. Review round 3 — the fd layer was a WINDOW; the fix is a process boundary

Reviewer round 3 (contract lens, run 962) reproduced four more publish channels in
the SAME input class at `d4fa0db8`, py3.11.16 + py3.13.7, through the shipping step
script parsed verbatim from the YAML **and** the redactor CLI alone, with the
advisory job GREEN while the value reached the public log. I re-reproduced them
myself before touching code, with my own harness rather than the reviewer's
(`orch_r4/probe_r4.py`: 12/12 leaking at `d4fa0db8` on both interpreters):

| # | Input (guard module body) | Why the round-2 fd window misses it |
|---|---|---|
| C1 | `atexit.register(...)` writing to stderr, fd 1 or fd 2 | the handler runs at interpreter exit — AFTER `finally` handed fd 1/2 back |
| C2 | non-daemon `threading.Thread` started at import, writes at +150 ms | joined by the interpreter at exit, after the window |
| C3 | fd-table scan `for fd in range(3,64): os.write(fd, ...)` | the window's OWN `os.dup(1)`/`os.dup(2)` saved descriptors sit live in the fd table for the whole import — 4 hits |
| C4 | `dup2` a saved descriptor back over fd 1, rebind `sys.stdout`, print | the window defends fd 1/2 but hands the module live copies of them |

**The pattern across three rounds is the finding.** Round 1 added fixed-string
diagnostics, round 2 added `except BaseException` plus `catch_warnings` + stream
redirects plus a `dup`/`dup2`-over-`/dev/null` window. Each was individually
correct and each was bypassed from *inside* the same process, because every
in-process silencing layer is a time window and guard code can register work that
outlives the window. The conclusion the reviewer drew and I agree with: **in-process
containment of untrusted import-time code has no closed boundary.** So the fix is
not a fourth layer — it removes the premise.

**TRUST BOUNDARY (stated explicitly, as the reviewer asked; rewritten by round 4**
**— t_38fd345f — see `ci-backstop-child-authorship-pycache-t_38fd345f.md` for the**
**reproduction and the tests that pin this):** *the redactor does not execute the
guard's module code in the process that owns the job log, and the child's verdict
alone cannot steer redaction.* The parent derives `FORBIDDEN` TWICE, independently,
and the two must AGREE by compiled equality (`.pattern` and `.flags`): once from the
guard's SOURCE BYTES with `ast` (zero execution: only a module-level
`FORBIDDEN = re.compile(<str literal>[, <int literal>])` is accepted — node-shape
gates, so an f-string, a concat, a name or an attribute all fail closed), and once
from a child (`subprocess.Popen([sys.executable, "-E", "-s", "-W", "ignore", "-c",
...], stdin=DEVNULL, stdout=PIPE, stderr=DEVNULL, close_fds=True,
start_new_session=True)`) that RE-READS the guard file itself and refuses to emit
any verdict unless its bytes hash (sha256) to the digest the parent pinned in argv;
it then `compile()`s exactly those bytes — there is no import machinery on that
path, so a committed unchecked-hash `.pyc` cannot shadow it, and the parent outright
refuses to redact while one sits on the guard's cache name. The protocol remains a
one-line `OK\t<b64 pattern>\t<flags>` or `ERR\t<TypeName>`, validated strictly:
base64 `validate=True`, pattern length bounded, flags a bounded int literal (never
`eval`), exception names matched against `[A-Za-z_][A-Za-z0-9_]{0,63}` — a charset
in which a real address is not expressible (no digits, no dots). **No byte read from
the child is ever echoed.** No pickle: unpickling guard-derived bytes would execute
guard code in the parent, i.e. reintroduce the exact thing being fixed. Whatever the
guard body does — `print`, `os.write` to any descriptor, atexit dumps, threads, fd
scans, `dup2` games, forks, or writing a forged `OK` line and `os._exit(0)` itself —
it lands on the pipe or `/dev/null`, never on the step; and a verdict the guard
authorizes for itself is refused by agreement, because what the redactor masks with
is the SOURCE-DERIVED pattern.

**Protocol contract, pinned both ways rather than left ambiguous** (the reviewer's
correction comment asked for this to be decided and stated, not left to the suite).
Both are safe for the same reason — a child byte is never printed:

  * junk terminated by `\n` is its own line, is not a verdict, is dropped, and the
    single well-formed `OK` is honoured → an otherwise-healthy guard that merely
    chatters stays **advisory-green** (existing tests keep passing unmodified);
  * junk with NO trailing newline merges into the verdict line (`fd1 <addr>
    OK\t...`), so no line is well-formed → **fail-closed rc=3**.

Named in the module docstring and pinned by
`test_child_junk_terminated_with_newline_is_dropped_and_verdict_honoured` /
`test_unterminated_junk_merged_into_the_verdict_line_fails_closed`.

**Out of scope, named so round 4 does not re-open it:** a guard whose `FORBIDDEN`
has been *weakened* (narrowed, or replaced by one matching nothing) still redacts
"correctly" and the job still passes. That is a guard-INTEGRITY attack, not a
publish channel — the value never leaves the process and nothing is printed.
Detection correctness is the parent's probe battery and
`scripts/tests/test_address_guard.py`; this file's guarantee is narrower and is the
one that matters for a public log: a real address never reaches the log, on any
path, including the failure paths.

**Reviewer's non-blocking note taken:** every wait on the child is bounded —
`selectors` read loop with ONE deadline and a byte cap, then pipe close (further
child writes die on EPIPE), then `killpg`, then a second, shorter `wait`. So a
grandchild holding the pipe cannot hang the step to the job timeout. **Measured, not
asserted** (`orch_r4/measure_slowfail.py`: a guard that emits nothing and spawns a
helper holding the verdict pipe open): `PatternUnavailable [TimeoutError]` after
**30.00 s** — the step invokes the redactor twice, so the worst case a job can pay
is ~60 s against GitHub's 6 h default job timeout. Fail-slow, bounded, never a hang.

### A new attack surface that THIS design introduced: cwd shadowing

Checking my own boundary claims turned up something the reviewer's rounds did not
cover, and it was **created by this fix**, not pre-existing: the child runs
`python -c`, and with `-c` CPython puts the **current directory** at `sys.path[0]`.
In CI that directory is the checkout root, so a committed `base64.py` at the repo
root would satisfy the child's own `import base64` and run its module body inside
the child. Measured on py3.11.16 and py3.13.7: `sys.path[0]` is `''` under `-c`,
and a repo-root `base64.py` wins the import. (Script mode puts the *script's*
directory there instead — also attacker-influenced, but the child uses `-c`.)

Why it matters here specifically: the shadow does not need to leak anything. A
committed `base64.py` that emits `OK\t<base64 of one real address>\t0` would make
the parent redact with a **single-address pattern**, silently un-redacting every
other value — the weakened-`FORBIDDEN` trick arriving from the other side of the
boundary, where it *is* a publish channel rather than an integrity question. The
pre-fix in-process redactor could not be attacked this way, because its
`sys.path[0]` was the script's own directory.

My first fix was wrong and caught before it shipped: I added the `-P` flag (PEP 674
safe path) to the child argv. `-P` needs Python >= 3.11, and this repo's floor is
**3.10** (`hscc-cli/pyproject.toml`, `hscc-project/pyproject.toml`, both
`requires-python = ">=3.10"`); an unsupported flag exits the child 2 before it can
emit a verdict, which takes the whole guard down on a supported interpreter — a
fail-closed that is really a breakage, and the draft audit text asserted a 3.11
floor that does not exist. Closed instead inside `CHILD_SOURCE`: `sys.path` is
sanitised (drop `''` and `'.'`) *before* importing anything resolvable from it.
Works on every version, including 3.10. Pinned twice: the load-bearing one is a
real end-to-end attack (`test_a_repo_root_module_cannot_shadow_the_childs_stdlib_imports`
— the shadow emits a forged single-address verdict; the assertion is that BOTH
addresses still get scrubbed, so a winning shadow prints `REAL_TAILNET` in the
clear) and a structural pin asserting the sanitisation happens inside the child and
precedes the imports it protects. Mutation `M11_no_safe_path` deletes the
sanitising line and is killed by both.

### Direct pins for the parent's own payload handling

The end-to-end route cannot reach a single-`OK` line whose **payload** is hostile
without adding a second verdict line — which dies at the exactly-one check before
the payload guard runs. Same lesson as the base64 case: test the guard you mean. So
`_parse_verdict` gets direct pins for uncompilable pattern text, the flags field
(`0x200`, a 20-digit int, `-1`, `__import__('os')`, `[1,2]`, `True`, `None`, and
`len(b'')` — the discriminator, because `eval` returns `0` for it and
`ast.literal_eval` refuses it; without that case the `eval` mutant **survived** my
first run), an oversized pattern, and a verdict with no trailing newline (accepted
deliberately; distinct from the merged-junk case, where junk *precedes* the verdict
on the same line).

### Two bugs in MY OWN first cut of this fix

Both found by tests I wrote for this round, reported because a clean handoff would
hide the information:

1. **A deadlock in the containment code itself.** My first version read the pipe on
   a helper thread and then closed it from the main thread. `BufferedReader.close()`
   blocks on the lock the blocked reader is holding — and the deadlock happens
   BEFORE the kill, so the step hangs forever. Measured: `load_pattern()` never
   returned (py3.11.16, instrumented in `orch_r4/`). Replaced with a `selectors`
   loop over an unbuffered handle (`bufsize=0`), no helper thread at all. Note the
   irony worth keeping: a control written to stop a *slow* failure became one.
2. **The group kill was a silent no-op.** `os.getpgid(proc.pid)` in the `finally`
   block runs after the child has usually exited, so it raises ESRCH, my
   `except OSError` fell through to `proc.kill()`, and the spawned grandchild
   survived on the runner (instrumented: pgid lookup → `OSError:3`, grandchild
   `STILL ALIVE`). The pgid is now captured while the child is alive and passed in.
   Pinned twice: `test_bounded_child_read_leaves_no_surviving_grandchild` and
   mutation `M5b_pgid_not_passed`.

### Mutation battery for my own round-4 tests (`orch_r4/mutate_r4.py`)

Nine mutants, **all killed, none surviving** — but two survived the first run and
one "died" for the wrong reason, so the honest record:

| Mutant | Result | Note |
|---|---|---|
| M1 revert to in-process `exec_module` | KILLED | pid proof + AST audit + probe cases |
| M2 drop the PARSE-side `TYPE_NAME` filter | survived first, then KILLED | survived because the constructor applies the same filter (defence in depth, same shape as round 2's M3). A second filter is invisible to a single-layer test: pinned with two `ERR` lines, where dropping the parse filter loses the real diagnosis. |
| M2c drop the CONSTRUCTOR-side `TYPE_NAME` gate | KILLED | the other half of that redundancy, pinned on its own |
| M3 accept multiple `OK` lines | KILLED | forged-extra-`OK` test |
| M4 swallow the timeout | KILLED | hang test expects `PatternUnavailable` |
| M5 `killpg` → `proc.kill()` | KILLED | grandchild survives |
| M5b don't pass the captured pgid | KILLED | **the real bug I shipped first** |
| M6 `base64` without `validate=True` | survived first, then KILLED | my e2e version died at the exactly-one-`OK` check BEFORE base64 ran, so it passed against the mutant. Re-pinned as a direct unit test on `_parse_verdict` (clean payload accepted, URL-safe-spliced payload rejected). |
| M8 drop the flags bound | KILLED | parametrised flags test |
| M9 drop the pattern-size bound | KILLED | oversized-pattern test |
| M10 `eval` instead of `ast.literal_eval` | survived first, then KILLED | needed the `len(b'')` discriminator: `eval` returns a valid int where literal_eval refuses. |
| M11 delete the child's sys.path sanitisation | KILLED | the cwd-shadow attack test + the structural sanitisation pin |
| M7 echo the child's raw bytes on parse failure | KILLED | the round-3 rule itself |

Final run: **13 mutants, all killed, survivors: none** — after two rounds where
three of them (M2, M6, M10) survived and had to be re-pinned properly.

Two lessons restated, both already in this audit and both re-earned: a mutant that
"fails" on a `SyntaxError` is not a killed mutant (my M2 pattern initially ate a
closing paren), and an end-to-end test that reaches a different guard first proves
nothing about the guard you meant to test (M6 — and the same class as the `GUARD`
constant bug I hit in the hang test, where `monkeypatch.chdir` does not redirect a
module-level path constant).

### Verification at this round's tip

`orch_r4/probe_r4.py` 0/12 leaks (both interpreters, step + alone);
`orch_r4/probe_r4_alone.py` 0/12 on **Python 3.14.8** as well (that interpreter has
no pyyaml, so the step-leg harness cannot run there; this half is the redactor CLI
alone, bare and flagged — the stricter of the two, since it relies on no
interpreter flags), and it also proves the child boundary is not version-specific
while `subprocess`+`selectors` behaviour could in principle be;
the reviewer's own `probe_r3_reviewer.py` 0 leaks, `probe_r3_fdscan.py` 0 leaks and
round-2 `probe_r2.py` all-ok, **re-run unmodified against my implementation** on
py3.11.16 and py3.13.7; card file 90 collected (57 test functions) / `.github/scripts`
leg 100, both interpreters; `address_guard --tracked` rc=0; guard's own 25 detector
tests green. Behavioural proof the boundary is real, not just the source text: the
stub records the pid that imported it and the test asserts it is NOT the redactor's
pid.

## 11. Files

| File | Purpose |
|---|---|
| `.github/workflows/address-guard.yml` | the job; enforcement posture documented as an operator variable |
| `.github/scripts/redact_guard_report.py` | public-log-safe redactor; extracts the guard's pattern from a CHILD process (never executes guard code in the log-owning process); fails closed AND publishes nothing on the way down |
| `.github/scripts/tests/test_redact_guard_report.py` | 90 collected (57 functions): end-to-end job legs, structural workflow assertions, verbatim step-script decision states, fail-closed publish channels (each layer pinned on its own), the round-3 deferred-write channels, and the child-boundary pins |
| `scripts/run_tests.sh` | `DIRS` += `.github/scripts` |
| `changelog.d/t_9a4b7687.md` | fragment (kind: Security) |
