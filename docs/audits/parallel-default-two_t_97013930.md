# `HSCC_TEST_PARALLEL`: the default moves 1 → 2, and what that contract change cost

Card: t_97013930. Parent: t_4fc35c1f (PR #780, stacked on #776 / t_6c2123f9).
Tree under measurement: `wt/t_97013930`. The narrowed gate ran at `1d276dbc` (the
flip itself); the two consecutive full-roster runs ran at `aa55c7a8`, which is the
flip plus the one correction its own roster run asked for (§7). The only later
commit is the one that writes these numbers into this file.

Host: Mac Studio, 14 cores, macOS. Every run here carries `/usr/sbin/taskpolicy
-c utility` — the queue's own QoS prefix (`merge_queue.GATE_QOS_CLASS=utility`,
`_gate_shell_argv`), so these are gate postures, not bare-shell postures. Nothing
here set `HSCC_TEST_PARALLEL`: every row is the **default** posture.

## 1. What the card claimed it needed, and what was actually missing

The precondition (a green full-roster concurrent run) was met and measured by the
parent: 12/12 at `p=2`, 713 s wall against 1396 s of summed leg work = **1.96×**,
load 3.5 (`docs/audits/packed-roster-reds_t_4fc35c1f.md` §4). Three things were
still missing, and all three are contract, not green:

1. a *measured* wall clock for the full roster at the new default and for the
   narrowed `hscc_daemon scripts` gate at it — the `579 s → ~340 s` in the parent
   audit is explicitly derived from per-leg durations and had never been run;
2. two consecutive green full-roster runs, not one;
3. the `WHY THE DEFAULT IS 1 AND NOT 2` header rewritten against the new posture.

All three are done: §3 for the clocks, §3 for the pair, and the runner header
(`scripts/run_tests.sh`, `WHY THE DEFAULT IS 2 — WHAT MOVED IT, AND WHAT WOULD MOVE
IT BACK`) for the prose.

## 2. The two readers the flip is not allowed to surprise

The header's claim was that the serial stream is what two readers were measured
against. Both were checked before the flip, and both pins now run at the DEFAULT
posture as well as an asked-for width.

**`during suite: $CURRENT_SUITE` (SIGTERM forensics, t_6bb29d46).** With a pack it
names the SET of legs in flight. Checked for parsers, not argued: `grep -rn
"during suite"` lands the writer (`scripts/run_tests.sh:458` and `:486`), the pin
itself (`scripts/tests/test_run_tests_leg_parallelism.py`),
`scripts/audits/rc143_guard_selftest.sh:27` (its own echo of the shape, for the
self-test), and the prose recipe in
`docs/audits/rc143-suite-reaper-t_6bb29d46.md` — **no code path parses the line**.
The recipe is a human forensics procedure and reads the line as a clue, not a
field, so a set is as readable as a single name; what would have been a real break
is the default naming one arbitrary suite while two are burning, and
`test_sigterm_names_the_live_legs_and_leaves_none_behind` now asserts the SET at
unset as well as at an explicit 2 (and that no leg survives the kill).

**merge_queue's digest anchors (`^✗ <leg>`, `^FAILED <path>::<test>`).** This is
the one that reads the stream, and it reads it *at the default*: the queue's gate
command is `HSCC_TEST_LEGS="…" bash scripts/run_tests.sh`
(`merge_queue._selected_leg_gate`) — it sets legs, never a width. Per-repo
processing is serialised under `flock` (`merge_queue._repo_lock`) and gates run one
after another (`for command in gates`), so the queue runs ONE `run_tests.sh` at a
time and the default of 2 cannot fan out into 2 × N legs from the queue's side.
`test_red_leg_digest_matches_the_serial_shape` compares the packed digest to the
serial digest through the real `merge_queue._failure_lines`, and is now
parametrised `asked-for-3` / `the-default`, i.e. the posture the queue actually
runs is the one under test.

## 3. Measured wall clocks (not derived)

| what | posture | wall | summed leg work | result |
|---|---|---|---|---|
| narrowed `hscc_daemon scripts` gate | unset → pack of 2, `taskpolicy -c utility`, load 3.49 | **330 s** | — | `ALL GREEN`, `✓ hscc_daemon` (330 s), `✓ scripts` (268 s) |
| full roster, run C | same | **708 s** | 1385 s (**1.96×**) | `ALL GREEN`, 12/12 `rc=0`, load 2.2/3.6/3.9 |
| full roster, run D, immediately after C | same | **712 s** | 1391 s (**1.95×**) | `ALL GREEN`, 12/12 `rc=0`, load 3.0/3.3/3.5 |
| full roster, run A (at `1d276dbc`) | same | 718 s | — | 11/12, `✗ scripts` — §7: my own pin, not the pack |

Serial reference for the gate pair, from the parent's per-leg durations: `hscc_daemon`
331 s + `scripts` 248 s = **579 s**. Measured packed: **330 s → 1.75×**. The
parent's `~340 s` was arithmetic on those two numbers; it is replaced by the 330 s
above, and the arithmetic turned out to be a fair guess for once.

**Why the gate lands exactly on its longest leg, and why that is the whole win.**
In the 330 s gate, `scripts` (268 s) disappeared entirely inside `hscc_daemon`
(330 s) — the wall equals the longer leg to the second. So a two-leg gate at 2 buys
"the shorter leg is free", and no further width can buy anything at all: with two
legs and two slots, a third slot has nothing to hold. That is the measured form of
§4's argument, and it is also the rollback trigger (b) in §5 made concrete — if
`hscc_daemon` ever stops being the leg that carries the others, the gate's speedup
shrinks toward 1.0× on its own.

## 4. Why 2 and not 3

1.96× at 2 on 14 cores, and the headroom for a third slot is unmeasured. The
roster's two long legs (`hscc-api` 449 s, `hscc_daemon` 331 s) set the makespan
floor: with 12 legs and one 449 s leg, a third slot shortens the queue, not the
floor. Measured again on this card's run C: the pack ended at 708 s with `hscc-api`
finishing at `pack=687s` — the pack's makespan is that leg plus the tail, and a
third slot would have had to shorten *it* to matter. This host also runs the fleet
control plane (daemon, gateway, dispatcher), so a wider default buys an unmeasured
speedup with control-plane latency. Raise it by measuring per leg count and per
host.

**The speedup is recycling, not just concurrency** — and the trace is the proof,
because the parent's pre-recycling loop measured 1.0× for the same roster. From run
C: `sparkrun-hermes elapsed=3s rc=0 pack=238s` — launched into the slot
`hscc-bootstrap` (233 s) vacated, done 3 s later, 470 s before the pack ended;
`memori_byodb elapsed=3s pack=391s`, `hscc-project elapsed=6s pack=69s`,
`hscc-commands elapsed=0s pack=1s`. A leg's `pack=` is how long it sat in the pack
overall while its own `elapsed=` was its work: the gap is a recycled slot. Same
shape in t_4fc35c1f's run (`sparkrun-hermes elapsed=4s pack=241s`, `memori_byodb`
4 s at `pack=408s`, `hscc-project` 7 s at `pack=72s`).

## 5. What would move it back to 1

Written into the runner's header too, so the next reader does not have to find this
file. One red leg is not the trigger — a packed red needs the same root-cause work a
serial red needs and t_4fc35c1f is the precedent (three reds, none of them the
pack). The triggers are: (a) a failure that reproduces serial and not packed; (b)
the narrowed gate's measured wall clock no longer beating serial by a clear margin
— see §3's longest-leg note, this trigger can arrive by the legs drifting, with no
code change; (c) a stream reader that breaks on a packed shape and cannot be fixed
at its own end — after re-reading the digest pin, because that pin is what makes
(c) hard to reach. Reverting is a one-line default plus the pins.

## 6. The `scripts` open item

The parent's run reported `scripts` `1 failed of 702` whose failure line was never
captured, and it did not reproduce in two of the parent's packed runs. This card
added three more packed samples at the new default, all green, none carrying a
`FAILED` line: inside the gate `705 passed, 4 skipped in 267.88s`; run C `705
passed, 4 skipped in 261.55s`; run D `705 passed, 4 skipped in 259.60s`. Five
packed samples, no reproduction, and still no failure line to reason about.

Verdict, stated as what it is: **unresolved, not absorbed** — same as the parent
left it. The count moved 702 → 709 items only because this card added pins to the
`scripts` leg; the item that failed in the parent's run is unidentified, so nothing
here proves it was a flake. If it recurs, the `FAILED path::test` line is now
recoverable (t_4fc35c1f fixed the path-named leg log that made the `.github/scripts`
leg unseeable), and it goes here verbatim.

## 7. Pins added or re-argued (this card)

* `test_the_default_is_a_pack_of_two_and_is_the_explicit_two_stream_exactly` — the
  re-argument: unset, empty and explicit `2` emit the same stream (per-run nonces
  folded by `_fold_stream`, narrowly and anchored per line).
* `test_the_serial_posture_is_one_line_away_and_adds_nothing_else` — `1` and `0`
  keep peak 1, interleaved banner+output, no tmp dir, exactly one self-reporting
  line.
* `test_a_single_leg_selection_stays_serial_and_silent_at_the_default` — CI's
  one-leg-per-job shape stays serial AND silent, which is why no workflow change is
  needed and none was made.
* digest + SIGTERM pins parametrised to cover the default posture.
* **and the one the roster caught** (`aa55c7a8`): the identity pin above compared
  the whole log, including the live `parallel: done leg=… elapsed=… rc=… pack=…`
  lines, whose ORDER is the scheduler's, not the stream's — so it reddened the real
  `scripts` leg on run A (`718 s`, 11/12 green) while the pack itself was fine. The
  fold now reduces those lines to one token each (count preserved, so a lost line
  still fails) and compares what they mean separately through `_completions`,
  `(leg, rc)` pairs sorted. Live progress and the reader-facing digest are different
  objects; a byte-identity pin has to know which one it is pinning.

## 8. Mutation probe: does the digest pin still bite?

Run against `wt/t_97013930` in a scratch worktree, only the `-k digest` selection.

* `cat "${P_LOG[$P_I]}"` → `grep -v "^FAILED" < "${P_LOG[$P_I]}"` (the class that
  matters: a `FAILED` line that never reaches the digest — the interleaved-dots
  shape in miniature): **`2 failed, 1 passed`** — both `asked-for-3` and
  `the-default` redden. The pin bites.
* `cat` → `sed "s/^FAILED/ FAILED/"` (indentation only): **`3 passed`** — survives,
  correctly: `merge_queue._failure_lines` strips leading indentation before
  matching, so it is not a reader-visible difference. A pin that reddened on that
  would be pinning pytest's formatting, not the contract.
