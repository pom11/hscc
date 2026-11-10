# Is the wake-dedupe stopgap deletable yet? No — and here is the row that says so

Card: `t_3367cb04` (gates + stopgap-deletion reachability), on `wt/t_3367cb04`
branched from `ae162a7f` (the head of hscc PR #275, `t_2cd692f0`'s orch-waker
half). Verification card: it changes no production line. `hscc_daemon/orch_waker.py`
is untouched by this branch — see the "hotspot" note at the bottom before
adding a third suppression channel to `tick()`.

## Verdict

`gateway_wake_cursors()` and `gateway_already_delivered()` **stay**. Deleting them
today removes the only cross-emitter dedupe that works on a deployed system, and
the suite says so in the only language that counts: the deletion is red, and the
red is a **doubled wake**, not a missing import.

`docs/UPSTREAM-kanban-wake-once-gate.md:201` makes the deletion the acceptance
test for hermes-agent#152206 §3.1 ("if the stopgap cannot be deleted cleanly, the
API is wrong"), and §9.3 lists it as the post-ship hscc step. That is the goal,
not the present. The claim table's *API* is sufficient — that question is already
answered yes, and answered by a test that survives the deletion (below). What is
missing is a **row**: on any board this waker can open, in any Hermes deployed
today, `kanban_wake_claims` does not exist, so the claim channel suppresses
nothing and the cursor ledger is the only evidence channel left.

## What was measured

All runs are the canonical leg (one pytest process, the runner's env guards, the
dispatcher's board pins removed), on this host, `PY=~/.hermes/hermes-agent/venv/bin/python`.

| what | result |
|---|---|
| baseline, `ae162a7f`: `test_orch_waker.py` + `_dedupe.py` + `_wake_claim.py` | **87 passed** |
| **deletion probe** — same three files, stopgap + wiring + `wake_ledger` seam removed (129 lines) in a throwaway detached worktree at `ae162a7f` | **7 failed, 80 passed** |
| new tripwire file, on `ae162a7f` | **5 passed** |
| new tripwire file, on the deletion probe | **2 failed, 3 passed** — exactly the two load-bearing pins fail; the handover case still passes |

The deletion probe removed `gateway_wake_cursors`, `gateway_already_delivered`,
`_suppress_gateway_delivered`, the `wake_ledger` parameter of `tick()` and its
caller in `run_tick`, and the delivery-block call — i.e. the stopgap and every
seam into it. The seven reds split cleanly:

* **the guarantee itself (4)** — `test_one_event_gateway_first_wakes_the_orchestrator_once`
  (`the waker re-woke a card the gateway delivered`),
  `test_one_event_waker_first_wakes_the_orchestrator_once`,
  `test_two_events_same_task_same_second_are_not_collapsed`
  (`the gateway-delivered event must not ride along`),
  `test_replay_after_restart_still_wakes_once`;
* **the seam (3)** — `test_unreadable_ledger_fails_open`
  (`tick() got an unexpected keyword argument 'wake_ledger'`),
  `test_suppression_is_reported_not_silent` (`KeyError: 'suppressed'` — the
  suppression never happened at all, so this is a fifth real-behaviour red),
  `test_gateway_wake_cursors_reads_only_what_it_needs` (`AttributeError`).

That is `docs/UPSTREAM-kanban-wake-once-gate.md` §9.3's "six required cases"
plus the direct cursor reader. A "green deletion" is therefore reachable only by
deleting those six pins with it — a rollback of `t_ff520296`, whose whole reason
for existing was a measured double wake (one `completed` row, event id 93871,
delivered twice). Deleting a guarantee is not the same operation as deleting dead
code, and a card that reports "tests still pass after the deletion" got there by
removing the tests that could say otherwise.

## Why it is structural, not incidental

Verified on this host rather than inherited from the parent handoffs:

1. **The deployed Hermes has no §3.1.** `~/.hermes/hermes-agent` at
   `045881137d` (branch `hscc-v0.23.0`): **0** occurrences of `def try_claim_wake`
   and **0** of `kanban_wake_claims`. So `_load_wake_claim_lib()` → `None` →
   `BoardWakeClaim.suppressions()` returns the **empty set for every board**. The
   adapter is not broken — failing open towards delivering is the correct posture
   — it simply has nothing to know.
2. **Upstream has no §3.1 either.** `origin/main` of hermes-agent: **0** and **0**.
   §3.1 exists only as the unmerged branch `feat/wake-once-claim-board-db`; the
   notifier half exists only as the unmerged `feat/wake-once-claim-gateway`. Nobody
   writes a row, so even a Hermes that had the table would find it empty.
3. **The existing dedupe suite passes on the cursor alone.** `test_orch_waker_dedupe.py`
   contains **0** occurrences of `wake_claim` — all ~40 of those tests run with the
   default `BoardWakeClaim()` and a board with no claims table, i.e. with the claim
   channel inert. Today's greenness of that file *is* the stopgap's greenness.

Point 3 is the one a future card is most likely to misread: the claim tests in
`test_orch_waker_wake_claim.py` stayed green through the deletion because they
inject their own claim layer. Green there means "the adapter honours a row somebody
put there". It does not mean a row will be there.

`hscc_daemon/tests/test_orch_waker_stopgap_load_bearing.py` pins all three facts —
including the second half of point 1 host-independently (the default claim layer
suppresses nothing on a pre-§3.1 board even on a machine that *does* have a §3.1
checkout, because suppression needs a row) and the handover case
(`test_a_claim_row_alone_is_enough`), which deliberately uses no `wake_ledger`
seam and no subscription row so that it keeps passing on both trees: the day rows
exist, the claim channel alone holds the line. The predicate for deletion is
"rows exist", never "the claim API is insufficient".

## When the deletion becomes reachable, in order

1. §3.1 merged upstream **and** the notifier's claim wiring merged — both are
   branch-only today (`feat/wake-once-claim-board-db`, `feat/wake-once-claim-gateway`).
2. A Hermes **release** containing both, **deployed** on every host that runs the
   gateway notifier, and hscc's supported floor raised to it. This waker resolves
   hermes at runtime through `autodown._load_kanban_db_or_default()`, so "merged"
   is not the test — the installed build is.
3. Boards written by an older hermes get the table at `connect()`, so a board the
   notifier has opened since the upgrade is fine; a board nobody has opened since
   is not, and this waker opens boards read-only with raw sqlite (it never
   migrates anything, by design).
4. Then: delete the two functions and `_suppress_gateway_delivered` + the
   `wake_ledger` seam, retarget `test_orch_waker_dedupe.py`'s six cases at the
   public claim API (the shape in `test_a_claim_row_alone_is_enough`), drop the
   `suppressed=` stopgap wording from the stream stamp, and delete
   `test_orch_waker_stopgap_load_bearing.py`'s two load-bearing pins — those two
   tests exist only to be deleted, and the two claim-only tests in the same file
   must survive. If any of that is not clean, the API is wrong, which was the point.

One residual to keep honest, because it is the only case where the cursor is
*stronger* than the claim even after step 2: a wake that was delivered but whose
`confirm_wake` never landed leaves an unconfirmed lease, and an unconfirmed lease
stops suppressing at `_WAKE_CLAIM_LEASE_S` (120 s), where the cursor — advanced by
the same notifier — suppresses indefinitely. hermes' own docstring calls that
trade deliberate ("the cost is a doubled wake, never a dropped one"), so it does
not block the deletion; it does mean the post-deletion system can double a wake in
a case today's cannot. If the product ever decides it cannot, that is a
`confirm`-retry conversation upstream, not a reason to keep this file's cursor.

## Merge state — what is actually live

Do not describe the cross-emitter dedupe as active. It needs both halves and
neither has shipped:

* hscc `PR #275` (`t_2cd692f0`, the waker half): `OPEN`, GitHub `MERGEABLE`,
  head `ae162a7f`, base `main`. `git diff --name-status origin/main...ae162a7f`
  re-verified here: four paths, one of them a stray `db.sqlite` added by the
  middle commit `8dc18bba`. Verified to be the **empty blob** (`e69de29b…`,
  `git cat-file -s` = 0 bytes — no table, no content, no address in it), so it is
  hygiene rather than exposure, but it must not ship; a fixup card owns it.
  Nothing here touches that PR's branch.
* hermes §3.1 board-DB half: `pom11/hermes-agent feat/wake-once-claim-board-db`
  @ `1f3da154f1` — unmerged, no PR filed (deliberate: no maintainer signal on
  hermes-agent#152206).
* hermes notifier half: `pom11/hermes-agent feat/wake-once-claim-gateway`
  @ `926650a1fc`, stacked on §A `feat/route-wake-dedupe-token` @ `f1fc4b69c3` —
  unmerged, no PR.

Live behaviour on the fleet today is therefore exactly what it was before this
work: **cursor-ledger dedupe only**. §A's gate token, §3.1's claim table and the
notifier's claim wiring are all inert until deployed.

## Two things a cleanup must not "simplify" away

1. **A claim this emitter holds is fan-out, not suppression.** Two subscriptions in
   one profile are two destinations and must both wake; the notifier decides that
   with the read-only `wake_claim_holder()` (`t_2f780536`, forced by a real
   regression in `tests/gateway/test_kanban_notifier_apiserver_wake.py`). A
   single-key "both emitters suppress on any claim" reading of the table is a bug,
   not a simplification.
2. **This waker needs no holder filter only by construction** — it takes its claim
   strictly between the last gate and the keystroke, so it never re-reads a claim it
   holds before deciding to send. That equivalence holds only while **one claim
   means one wake per `(profile, task, event)`**. If the notifier ever moves to
   per-destination claims, `suppressions` here must gain the same holder filter or a
   claim taken for destination A silently mutes this REPL wake. (`t_7c5d2669` carries
   this for the reviewer.)

Reviewer's base note (from `t_7c5d2669`, repeated so it is not rediscovered):
diff the notifier branch from `5634047ecf`, not from §3.1's head `1f3da154` — the
latter folds §A's two dedupe test files in and reports 12 files/+1610 instead of
the real 3 files/+701/-12.

## Gates run for this card

*hermes, `feat/wake-once-claim-gateway` @ `926650a1fc`, judged against **the same
base §A uses** — `86b9622f01`, the `merge-base` both branches share with upstream
`origin/main` (`c33bf7cec7` at `NousResearch/hermes-agent`). `--base origin/main`
on a stacked branch reports FILE_LINES against files these commits never touched,
so `t_2f780536`'s note is right: the merge-base is the only honest base.*

- `python3 scripts/check --base 86b9622f01` → **15 checks, ok** (`CHECK_EXIT=0`).
  Code-health ratchet: 13 files vs the base, 0 blocking / 0 advisory. The four
  `BLE001` waivers in the wake-claim path are allowed *with their reasons printed*
  — deliberate catch-alls, and `t_7c5d2669` carries the ask that claim-path errors
  fail toward **deliver** in a test, not in a waiver comment.
- `scripts/run_tests.sh` (canonical per-file-isolated runner, all of `tests/`) →
  **7399 files, 73393 passed, 235 failed, 11 errors, 2083 skipped** in 1920 s on 14
  workers; 78 files failed (3 at import/collection only, because `keyring` and
  `fal_client` are not installed in this checkout's test env) and 7 more were flaky
  and passed on retry.
- **That is not a clean sheet, and it is not the branch's.** Two independent
  comparisons at `86b9622f01` in a throwaway detached worktree:
  1. every one of the 85 failing files re-run at the base → **82 of the 85 fail at
     the base too**, and the base batch fails *more* of them (493 distinct ids vs
     the branch run's 232) on the same file list — the load signature of this
     suite's sqlite/store-retry class, not a code difference;
  2. the six ids that failed on the branch and not in that base batch
     (`test_local_component_jobs.py`, `test_sessions_held_store_gate.py` ×4,
     `test_repair_unavailable_health.py`) re-run as whole files on an idle machine
     fail as an **identical 7-test set at both heads** (base reports 2 FAILED + 5
     ERROR, head reports the same 7 FAILED). Zero branch-introduced failures.
- None of the failures touches the seam this branch changes: no
  `tests/gateway/test_kanban_notifier*` file appears in the 85. The notifier
  neighbourhood itself was measured by `t_2f780536` (129 files, 865 passed, 1
  failure documented pre-existing at the base by `t_c3c7acae`) and by §3.1's own
  card; it was not re-run here.

*hscc, `wt/t_3367cb04` (branched from PR #275's head `ae162a7f`, pushed to
`pom11/hscc`):*

- the three orch-waker files (`test_orch_waker.py`, `_dedupe.py`,
  `_wake_claim.py`) → **87 passed** — the baseline the deletion probe is measured
  against;
- `hscc_daemon/tests/test_orch_waker_stopgap_load_bearing.py` (new here) →
  **5 passed** on this branch, **2 failed / 3 passed** on the deletion probe;
- `hscc_daemon/tests` leg → **exit 0**, not one `F`/`E` in the dot stream (the
  double `-q` suppressed pytest's count line — exit status is the evidence);
- `scripts/tests` leg (which owns `test_readme_lint.py`, the rule the new audit doc
  must not break) → **exit 0**;
- `scripts/changelog_fragments.py check` → OK. No fragment added on purpose: this
  branch ships a document and a test, no user-visible behaviour, so any `kind:`
  would misdescribe it;
- `scripts/readme_lint.py --repo . --strict` under the venv interpreter →
  `docs-index: 203/203`, **findings: 0**. The first commit tripped `DOC-UNLISTED`
  (202/203); `docs/README.md` closes it — the same trap
  `docs/audits/orch-waker-import-leak_t_2be3b04e.md` records.

## Hotspot

`t_2cd692f0` flagged `tick()`'s delivery block: it now carries two suppression
channels (cursor ledger, then wake claim) plus the take/confirm/release trio
around the keystroke in one ~80-line stretch. This card adds no line there. A
third channel in that block should be decomposed first, not appended.
