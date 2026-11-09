kind: Fixed
task: t_c2b5fb13

- **`hscc check post_boot` told an operator `no expected_up marker` when the
  marker file was sitting right there.** Gate 2 of `hscc_daemon/post_boot.py`
  reads "empty `units`" as one branch and until now chose between exactly two
  sentences: the stay-down one (when the `deliberate_down` record is present)
  and `no expected_up marker (fleet not fleet-upped, or brought down on
  purpose) — nothing to do` for everything else. #263 (t_9c437f03 step 1b,
  merged as `c186eb4a`) created a third state that branch had never described:
  an empty wake plan now calls
  `record_expected_up([], reason="autodown wake: empty plan (nothing to
  start)")`, which pops the stay-down and writes `units: []` — so the file
  EXISTS, there is NO stay-down record, and the second sentence is
  doubly wrong: it denies a file that exists, and it drops the marker's own
  `reason`, the one breadcrumb telling the operator that a wake tried and could
  not describe the fleet (`serving.json` missing/corrupt).
  `check_post_boot_relaunch` copies `res["message"]` into the stream verbatim,
  so that sentence is literally what the operator reads — and it was
  indistinguishable from the case where the fleet was genuinely never
  fleet-upped.

- **One verdict, three sentences.** Gate 2 now prints which state it found:
  record present → the existing `fleet deliberately down since <set_at>
  (<reason>) — nothing to do`; **marker file present and readable with an
  empty footprint** → `expected_up marker present with an EMPTY footprint
  (nothing is expected up and nobody holds a standing stay-down order; reason:
  "…"; cleared_at: …) — nothing to do`, quoting the marker's `reason` and its
  `cleared_at` when the marker carries one; no readable marker (absent,
  corrupt, or not a JSON object) → the old absent-marker sentence, kept
  byte-identical, because an unreadable marker is *absence of evidence* — the
  same thing `load_marker` reads as an empty marker and `deliberately_down()`
  reads as `None`, and the wording that promises nothing.

- **Display only, and pinned as such.** Nothing behavioural moved:
  `result` stays `no-marker` for all three states (it is a member of
  `_STREAM_OK_RESULTS`, `post_boot.py:777` — a new label would read `ok=False`,
  turn `hscc verify` red and fire the trigger engine, i.e. page for a state
  that is not a fault), and the pass stays inert because launch authority needs
  NON-EMPTY `units`. The new `marker_file_readable(path=None)` predicate is
  declared DISPLAY-ONLY in its own docstring and fails safe to `False`.
  Printing the marker's prose is not branching on it, and the PR #243 rule
  (never decide from record presence or a `reason` substring) is pinned from
  both sides: `TestWordingIsNotBehaviour::test_no_units_verdict_is_invariant_
  to_the_reason` runs eight reason strings — including the *verbatim wording of
  the other two states*, an imperative ("relaunch everything now"), the empty
  string and a NUL/emoji — through all three states and asserts the identical
  verdict with zero boot/probe/lock/launch calls, and
  `test_launch_authority_reads_units_never_prose` asserts a NON-EMPTY footprint
  behaves identically whatever `reason` claims, even with a hand-stamped
  `deliberate_down` (a shape no sanctioned writer produces).

- **Verified:** `tests/test_post_boot.py` 47 → 63 (16 new). The third state is
  reached only through the sanctioned writers — `clear_expected_up` then
  `record_expected_up([])`, the exact sequence step 1b performs — never by
  hand-writing a marker shape, and one test guards that the fixture really is
  the third state rather than one of the older two. Focused
  `tests/test_post_boot.py tests/test_autodown.py`: **266 passed** at
  `0ddee987` (63 + 203). Before #263 landed the same command measured **258**
  (63 + 195) and on #263's open tip it measured the same **266**, so the change
  is right in either landing order — its files are disjoint from step 1b's.
  `hscc_daemon` leg of `scripts/run_tests.sh`: **3566 passed, 0 failed** at
  `0ddee987` (3501 at the pre-merge `63e3236c`; the delta is main's own new
  test files, not this diff). 9/9 mutation
  probes caught on a pristine CONTROL green (63 passed), every probe run
  asserting it imported `post_boot` from inside the copy, each mutation reset to
  original on a fresh copy and asserted to have applied before running: the
  empty-footprint branch disabled, the stay-down branch disabled, the `reason`
  breadcrumb dropped, `cleared_at` printed when absent, the presence predicate
  answering True for any parseable JSON, the same predicate raising instead of
  failing safe, gate 2 made to **branch on prose** (caught by the
  wording-invariance test), the verdict label renamed out of
  `_STREAM_OK_RESULTS`, and a straight revert to the old lie.
