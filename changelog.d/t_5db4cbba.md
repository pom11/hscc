kind: Fixed
task: t_5db4cbba

- **An untrapped `SIGPIPE` killed a leg pack and left the legs in flight burning
  CPU on the host that runs the fleet control plane.** `t_b94a2b39` built the
  fatal-signal trap family and deliberately left `SIGPIPE` out, on two claims: that
  it "measured never to leak the dir", and that trapping it in a script that pipes
  through `head`/`tr` changes error paths nobody had evidence for. Both are now
  measured (`docs/audits/sigpipe-orphan-legs_t_5db4cbba.md`), and the first is
  wrong: against the shipped runner, `kill -PIPE` orphaned **2.0 legs** on average
  (3/3 trials) and left the leg-log dir behind in **1/3** trials of the
  early-closing-reader posture. The enumeration the second claim needed turns out
  to be small: `run_tests.sh` has exactly **three** real pipes, all `$(ps … | tr …)`
  — no `| head`, no `| wc -l`, no `| grep`, and no pipe whose writer is a shell
  builtin, which is the only way a closed reader can raise `SIGPIPE` in the shell
  rather than in a child. So "a script that pipes through `head`/`tr`" describes the
  *caller's* pipeline (`run_tests.sh | head -40`), not the script's interior.
  **Ruling:** route `PIPE` through `_on_fatal_signal PIPE 141`, beside `HUP 129` and
  `QUIT 131`. The regression that fear was guarding against does not exist: under
  the shipped bash 3.2.57 every pipe shape that occurs in the runner is rc 0 with a
  `PIPE` trap and rc 0 without it, a `| head` inside a command substitution never
  reaches the parent shell in either posture, and the one shape that *does* change —
  the runner's own stdout being a closed pipe — was **already** fatal at rc 141
  before any leg launched. The trap cannot turn a benign truncation into a
  teardown, because there was no benign truncation to turn; it only adds the reap
  and the `$LEG_TMP` removal the pack was already owed. It re-raises with the
  default disposition, so `128+N` stays byte-identical and a dispatcher or the
  merge queue reads **141 exactly as before** — no rc assertion is relaxed, and the
  129/131/141 codes are now pinned as a table rather than left to a re-raise nobody
  re-checks. Measured after: **0.0** mean orphans in both postures (from 2.0 and
  1.0). The handler's own banner — four `echo`s into the very pipe that closed — is
  measured safe too: bash reports `echo: write error: Broken pipe` on stderr and
  keeps going, so the reap and the cleanup still run.
---
kind: Verified
task: t_5db4cbba

- Red-before is the orphan count, not a theory, and it needed a fix of its own to
  be trustworthy: the first `head_live` run reported **0.0** orphans against the
  unfixed runner, which would have "disproved" the card. It was a **vacuous zero** —
  with every leg the same length the runner's next write to stdout falls exactly
  when those legs have just finished, so nothing was in flight to orphan. Staggering
  the plan (`SIGPROBE_PLAN='hscc-commands:2:0 sparkrun-hermes:30:0 memori_byodb:30:0'`)
  makes the runner write to a closed pipe *while the other two are still running*
  and yields the real **1.0**. Green-after: `external` 2.0 → 0.0, `head_live` 1.0 →
  0.0, rc 141 unchanged, and the SIGPIPE banner printed in 3/3 trials.
  `scripts/tests/test_run_tests_leg_tmp_lifecycle.py` **14 passed** (was 11): the
  parametrised fatal-signal table gains `PIPE`/141 and asserts
  `pack.orphaned_legs() == []`, plus two structural pins — the trap family with its
  per-signal `128+N` codes, and the measured safety property itself (no pipe in the
  runner may pair a shell writer with an early-closing consumer). Teeth, by mutation
  with the tree re-verified after each: M0 no-op control `1 passed`; deleting the
  `PIPE` trap, retargeting its fallback to 130, installing it for the wrong signal,
  and inserting `echo x | head -1` into the runner each turn a **named** test red.
  The last mutant is the one that keeps the safety pin from quietly stopping
  existing.
