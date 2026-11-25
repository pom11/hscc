# t_f508ce51 — harness-failure vs test-failure in the pack (working notes)

Base: `origin/devops/t_692814f5` (cc72fbf7). None of #825/#827/#828 has landed.

## DECISION: a distinct verdict, ON the anchored `✗` row — not an extra diagnostic line

Three measured facts decide it; none of them is taste.

1. **An extra line would be invisible to the only machine that reads this.**
   `hscc_daemon/merge_queue.py:1304-1308` — `_failure_lines` keeps a line ONLY if it
   matches `^(FAILED|ERROR)\s` or `^✗\s` / `^\d+ failed\b` (after `line.strip()`).
   An unanchored "harness failure" note is dropped from every gate handback, so the
   distinction would live only in a full log nobody reads. The distinction has to be
   ON the `✗` row to exist for the queue at all.

2. **Today this class is ADVISORY-GREEN in CI, not merely ambiguous.**
   `.github/scripts/leg_verdict.sh:56` hard-reds `pytest exit (0|[2-9]|[1-9][0-9]+)`,
   i.e. "the harness, not a test, is broken". The class the card is about reports
   `pytest exit 1` — so it falls through to the *test-failure* branch and exits 0 with
   `::warning::` whenever `HSCC_TESTS_ENFORCE` is unset. A leg that never ran does not
   just look like a red test, it looks like an *advisory* red test. The distinct
   machine-readable phrase is what lets that branch be re-routed to a hard red.
   (This is the card's "fails red, never silent" constraint, and the leak is one layer
   further out than the card knew.)

3. **The empty log is semantically, not conveniently, the discriminator.**
   If a leg printed `FAILED path::test` and then its wrapper died before writing `.rc`,
   the FAILED line IS evidence about a test and calling it a harness failure would be
   a lie. "no record AND no output" is exactly "the harness has no evidence about this
   leg", which is the only thing we may claim. A killed leg that flushed output keeps
   `pytest exit 137` and is already hard-red via (2)'s `[2-9]` rule.

## The row

    -  ✗ .github/scripts (pytest exit 1, 0s)
    +  ✗ .github/scripts (no leg output — harness failure, pytest exit 1, 0s)

Keeps: the `  ✗ ` prefix at line start (`^✗\s`), the leg name verbatim, and the literal
`pytest exit N` token inside the parenthetical (so `leg_verdict.sh`'s non-test-code rule
still fires on a 137/143 kill). Adds one phrase. Row length ~66 chars vs
`MAX_FAILURE_LINE_CHARS = 300`, so it cannot be truncated into a different meaning.

## Class is PACK-ONLY, and that is not an oversight

The missing artefact is the per-leg `.rc` record, which only exists in the pack
(`run_tests.sh:916` writes it, `:934` reads it). In serial mode the leg writes straight
to the runner's stdout and its exit code is captured in the same shell as the summary
row — there is no record to lose. So serial keeps `  ✗ <leg> (pytest exit N, Xs)`
byte-for-byte: `test_run_tests_leg_duration.py:52`'s strict `RED_ROW` regex and
`test_summary_rows_keep_the_serial_wording` stay green untouched, and the
packed/serial digest parity test keeps comparing the same failure identity.

## Readers I checked before choosing the wording (all verified, not guessed)

| reader | file:line | effect |
|---|---|---|
| queue digest | `hscc_daemon/merge_queue.py:1304` | matches `^✗\s` after strip → still bucketed as a leg row |
| CI verdict | `.github/scripts/leg_verdict.sh:56` | `pytest exit 1` → **advisory green today**; gains an explicit rule |
| duration pin | `scripts/tests/test_run_tests_leg_duration.py:52` | parses the SERIAL loop only → untouched |
| parity pin | `scripts/tests/test_run_tests_leg_parallelism.py:316,323` | stub legs write records → ordinary red path → untouched |
| digest pin | `scripts/tests/test_run_tests_leg_parallelism.py:299` | compares a plan with no harness-failure leg → untouched |

## Reproduction (measured, not reasoned)

`LEG_TMP` has no env seam (`mktemp -d /tmp/hscc-legs-XXXXXX`, `run_tests.sh:688`) and
the runner is `set -u` without `set -e`, so the card's "point LEG_TMP at a non-writable
dir" option is not reachable from a test. Two instances of the SAME discriminator are
used instead:

* **shipped test**: the leg's wrapper is SIGKILLed before it can write `.rc`
  (stub does `kill -9 $PPID`; the subshell at `run_tests.sh:906-918` is the wrapper and
  the stub is its direct child). Nothing flushed → empty log → no `.rc` → `wait` falls
  back to 137. This is the real-world instance (OOM-killer / SIGKILL) and needs no
  production seam.
* **redirect instance**: re-applying the pre-flatten concat
  `logf="$LEG_TMP/$P_NEXT-$d.log"` reproduces t_692814f5's own mechanism, whose
  redirect fails and whose `wait` status is **1**.

Both instances land on the same two reap-time facts, which is the point: the verdict is
a function of the facts, not of which mechanism destroyed the record.

## The red/before pair, as executed

Against the unmodified runner (`cc72fbf7`), `scripts/tests/test_run_tests_leg_harness_verdict.py`:

    4 failed, 6 passed in 7.84s

The four reds are the verdict, the digest, the live line and the source pin. The actual
pre-fix row the victim leg produced:

    parallel: done leg=hscc-commands elapsed=0s rc=137 pack=0s
      ━━━ hscc-commands ━━━
      ━━━ sparkrun-hermes ━━━
      …
        ✗ hscc-commands (pytest exit 137, 0s)

— an empty block and an ordinary-looking red, exactly the card's shape.

The **6 survivors are the evidence too**: the ordinary test-red, the healthy pack, the
serial row, the killed-after-flushing control, the premise case and the non-zero exit
were already green and had to stay green — they are the controls that must not move, and
they are what shows the four reds are the verdict's verdict rather than a rewrite
artefact.

After the change:

    10 passed   scripts/tests/test_run_tests_leg_harness_verdict.py
    12 passed   .github/scripts/tests/test_leg_verdict.py   (9 pre-existing + 3 new)
    34 passed   test_run_tests_leg_log_path.py + _leg_parallelism.py + _leg_duration.py

## The pair this card asked for, as executed (redirect instance)

The card asked specifically for: *a leg whose log cannot be written yields the new
harness-failure verdict, not the ordinary test-red — and must still exit non-zero.*
Executed by re-applying the pre-flatten `logf=` concat to the shipped runner (scratch
probe, restored; `git status --porcelain` reported CLEAN after the run), packing the
REAL roster leg `.github/scripts` alongside a healthy one:

    --- BROKEN (pre-flatten logf): rc=1
        ✓ hscc-commands (1s)
        ✗ .github/scripts (no leg output — harness failure, pytest exit 1, 0s)
        phrase_present=True   no_such_file_line=True   verdict=FAILURES ABOVE
    --- FIXED (shipped logf): rc=0
        ✓ hscc-commands (1s)
        ✓ .github/scripts (1s)
        phrase_present=False  no_such_file_line=False  verdict=ALL GREEN

Three things worth reading off that pair. The victim leg carries **`pytest exit 1`** —
the exact code that, before this change, made the class indistinguishable from a red
test and that `leg_verdict.sh` still calls a test failure; only the phrase separates
them now. The stray `No such file or directory` line is still in the stream (the fix is
a verdict, not a suppression — the underlying message is left alone). And the healthy
leg in the same pack is unaffected, which is what makes this a per-leg verdict rather
than a pack-level panic.

The probe is scratch, deliberately not committed: it patches a tracked file in place,
and a test that mutates its own checkout cannot be run twice concurrently.

## Where the class leaked, and by how much

`leg_verdict.sh`'s `pytest exit (0|[2-9]|[1-9][0-9]+)` rule is the only thing in CI that
knows "not a test". It fires on a 137/143 kill. It does **not** fire on the redirect
instance, because a failed `>` leaves `wait` reporting 1. So of the two instances above,
the one t_692814f5 actually shipped was classified as an ordinary test failure —
`::warning::` plus exit 0 while `HSCC_TESTS_ENFORCE` is unset. The new rule in
`leg_verdict.sh` closes that, and `test_a_leg_the_harness_could_not_run_is_red_even_in_advisory`
is its red-before: against the old script that log yields rc 0.


## LEFT / risks

- **Base choice.** Branch `devops/t_f508ce51` is cut from `devops/t_692814f5` (PR #828),
  because this card's repro file lives there. None of #825 / #827 / #828 had landed when
  this was written, so "rebase onto whichever has landed" could not be honoured at
  authoring time. My hunks are the reap classification and the summary block
  (`scripts/run_tests.sh`), which is *adjacent to* but not inside `_cleanup_leg_tmp`
  (#825) or `_on_fatal_signal` (#827) — expect a textual conflict in the same region,
  not a semantic one. Re-implementing the cleanup was deliberately not done.
- **A docstring in another lane's hot file.** `hscc_daemon/merge_queue.py:1301` still
  describes the leg row as `` `  ✗ <leg> (pytest exit N)` ``, which now understates the
  parenthetical. Behaviour is unchanged (`^✗\s` still matches), so this is prose only;
  left alone rather than touching a 4.9k-line file four cards are sitting on. Worth a
  one-line follow-up.
- **The phrase is a contract now.** `no leg output` on a `✗` summary row is read by
  `.github/scripts/leg_verdict.sh`. Renaming it means changing the runner, the runner
  test, `leg_verdict.sh` and its test in the same diff — the four are pinned to each
  other, and the source pin in
  `scripts/tests/test_run_tests_leg_harness_verdict.py::test_the_verdict_is_derived_from_the_two_reap_facts_at_one_site`
  fails if a second rendering site appears.

