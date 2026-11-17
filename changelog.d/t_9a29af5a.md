kind: Fixed
task: t_9a29af5a

- **`ios-app/scripts/model_decode_check/main.swift` — the banner stops calling
  its check count a fixture count, because the docs now cite that line as the
  authority for how many are green.** `c.passed` increments once per CHECK: a
  fixture decoded against two models adds two, and a `checkFails` fixture adds
  one by *throwing*. The pass line printed that number as
  `89/89 fixtures decoded against the real models`, while the directory holds
  55 `*.json` files — measured on this branch's own run (`89` `OK` lines,
  `ls scripts/model_decode_check/fixtures/*.json | wc -l` → `55`). The failure
  line had the same unit bug in the other direction: `N fixture(s) did not
  decode` for a count of failed checks. It now prints
  `ALL DECODE CHECKS PASSED — N/N checks (over F committed fixtures) decoded
  against the real models` and
  `DECODE FAILURES — N check(s) did not pass against the real models`. `F` is
  not typed: `DecodeCheck` records the distinct names it actually opened inside
  `load` — the one function every fixture byte passes through — so the fixture
  number is derived from the run, exactly like the check number, and a card that
  adds or retires a fixture changes both without editing a sentence. That is the
  whole point of the card this one follows: t_74b0c758 deleted every transcribed
  count out of `ios-app/README.md` and this harness's `fixtures/README.md` and
  pointed the prose at this line instead, so handing the docs a number whose
  unit was wrong just moved the rot somewhere more authoritative. The exit codes
  are untouched (0 on all-pass, 1 on any failure) — the exit code is the guard,
  the words only report. Verified both paths against the real binary rather than
  by reading the string: the green run prints
  `ALL DECODE CHECKS PASSED — 89/89 checks (over 55 committed fixtures) decoded
  against the real models` and exits 0; a scratch copy of the fixtures with
  `cards.json` truncated and `cron_list.json` given `{"jobs": 7}` prints
  `DECODE FAILURES — 3 check(s) did not pass against the real models` over two
  broken files and exits 1 — three failed checks over two fixtures, which is the
  distinction the old wording erased. No decode assertion, fixture, or exit-code
  logic changed; the only other edit is the one place in `fixtures/README.md`
  that quoted the old wording verbatim, which now quotes the new shape with its
  `N`/`F` placeholders (still no number). `grep -rn "ALL DECODE CHECKS PASSED"`
  re-checked before landing: the remaining hits are dated point-in-time records
  (`ios-app/docs/audits/`, `E2E-API-REVIEW-2026-08-27.md`, archived changelog and
  `CHANGELOG.md` lines), which are evidence of what those runs printed and were
  left alone.
