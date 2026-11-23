kind: Fixed
task: t_3b3bddb0

* **A red gate now names the test that broke.** The handback a gate failure
  leaves on the card carried only the last 40 lines of the gate's output
  (`_tail(text, MAX_GATE_TAIL_LINES)`), and 40 lines of a 12-leg run cannot
  contain a failure. `scripts/run_tests.sh` runs its legs in sequence, so the
  `FAILED path::test - reason` row printed by the leg that went red sits
  ~60 lines above the end of the run: the legs that passed *after* it, and the
  stderr the API appends at the very end, are what filled the window. Measured
  on main 2026-11-23 — PR #673 bounced at 10:40 UTC, and its handback named no
  test at all; the whole red evidence was `✗ hscc-api (pytest exit 1)`, which
  says a leg failed and nothing about which test. Telling a flake from a real
  break then cost a manual `gh pr checks` plus a crawl of a 22-minute Actions
  log, from a comment whose stated job was to make that decision.
  It was not a truncated write either: `hermes kanban comment` stores the body
  it is handed (`--max-len` is opt-in and the queue never passes it), and the PR
  comment carried the same text — the loss was in what was *selected*, not in
  what was *sent*.
  A non-passing leg now goes through `_gate_report`, which reads the FULL output
  and puts `_failure_lines` of it — the verbatim `FAILED`/`ERROR` rows, the
  `✗ <leg> (pytest exit N)` summary row and the per-leg `N failed, M passed`
  count line — ahead of the tail. Verbatim is the requirement: a paraphrase is
  what the summary row already said. The leg rows lead the digest and may never
  lose their place to a flood of `FAILED` lines, because they are printed LAST
  by the runner and are therefore a print-order cap's first casualty. The digest
  is capped (`MAX_FAILURE_LINES` distinct lines, `MAX_FAILURE_LINE_CHARS` per
  line, with the hidden count stated) so a branch whose fixture blew up cannot
  turn a card comment into a log file. A passing leg is untouched: it still gets
  the plain `_tail`, byte for byte.
* **The red-gate comment states which attempt bounced.** `Attempt: N`, 1-based
  — `entry["attempts"]` counts retries *already spent* (0 on a first pass), so
  the handback adds one rather than telling the author their first try was
  attempt zero. The evidence also moved above the resolve script: the comment
  used to bury the failure below six lines of rebase/force-push boilerplate.

---
kind: Verified
task: t_3b3bddb0

* `hscc_daemon/tests/test_merge_queue.py` — 6 new cases, 98 passed under
  `~/.hermes/hermes-agent/venv/bin/python`. The regression case feeds `_run_gate`
  a `run_tests.sh`-shaped output in which everything printed *after* the failing
  leg is filler — deliberately >40 lines, so the case fails without the digest
  instead of passing by luck of a short suite — and asserts the verbatim
  `FAILED hscc-api/tests/test_ws_relay.py::test_writer_restarts_after_kill`
  line, the `✗ hscc-api (pytest exit 1)` row, and that the tail is still there
  (the digest is added, never substituted). A second case drives the same output
  through a real `tick()` and asserts the CARD comment alone carries test name +
  leg + `Attempt: 1`, above the resolve boilerplate, and that the PR comment
  carries the same text rather than a thinner copy.
* Proven through the real argv too, not only a fake `Popen`: a `sh` script that
  prints the same 12-leg shape and exits 1 is run by `_run_gate` for real
  (milliseconds, no repo, no network), because a fake `Popen` returns exactly
  the strings the test wrote and can never show that real `communicate()`
  output reaches the digest.
* Guard cases pin what did **not** change: a green leg's output is asserted
  `== mq._tail(text)` byte for byte, so the fix cannot quietly reformat every
  passing run's evidence; and the matcher tests pin the refusals — a dot
  progress line, a `✓` row and prose that merely mentions `FAILED` mid-line may
  not enter the digest. Capping is pinned as stated, not silent: beyond
  `MAX_FAILURE_LINES` the digest ends with the count it hid, a duplicate
  short-summary row appears once, and a 5 KB failure reason is shortened without
  losing the `path::test` id.
