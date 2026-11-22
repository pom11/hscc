kind: Fixed
task: t_036fedf8

* doctor's human-view tests no longer ask the Rich panel where it broke the line.
  Three more asserts in `hscc-project/tests/test_doctor_command.py` checked a
  phrase against the RAW rendering, and a Rich Panel hard-wraps a long row at the
  width `_theme.make_console` pins for a pipe (200) — so where the break lands is
  set by the length of the data inside the row, which for the memory rows includes
  pytest's `tmp_path`: a machine-state lottery. `cannot read the` (missing
  memori config) now asserts against `_flowed(out)`; the two freshest-`MEMORY.md`
  path checks now assert against a second normaliser `_says()`, because a path is
  ONE token and Rich FOLDS it mid-word (no space inserted), which a word-boundary
  reflow cannot recover. No width change, no skip, no xfail, no deleted case, no
  broadened token: every raw status-marker assert stays raw (a row's first token
  cannot wrap, so it still proves a row was rendered as a row), and the negative
  `memories.db` assert now survives a fold that would have split the name in two —
  a false-green shape it previously had.
---
kind: Verified
task: t_036fedf8

* The audit is a sweep, not an eye-test. `sweep.py` (task scratch) discovers every
  `assert "<phrase>" in <expr>` in `test_doctor_command.py`,
  `test_doctor_declaration.py` and `test_doctor_tmux_repl.py` by AST (including the
  `_says` / `not _says` forms), then runs the REAL test function objects with
  shimmed fixtures across every tmp-root length 48..210 chars, in both postures
  (p313 + sealed HOME, and the workstation venv). Calibrated against t_cc2f23ea's
  measured window: it reproduces the 60/66/120/126-split / 72..114-green table for
  `learning has stopped`. At `94828d99` it reported 3 cases RED at some length;
  at `63aa57d9` it reports `cases whose raw assert can split: 0;
  cases red at some length: 0` in both postures.
* Split windows measured at base (tmp_path chars): `learning has stopped`
  57..68 and 119..127 (t_cc2f23ea's, re-measured — its `_flowed` conversion is
  load-bearing, not decorative); `cannot read the` 119..127 and 151..155;
  `backend-engineer/memories/MEMORY.md` 152..185;
  `profiles/newest/memories/MEMORY.md` 162..194. Real pytest through
  `--basetemp` of controlled length gives 9 red-at-base / green-at-fix pairs and
  the E-lines name exactly those asserts (`:444`, `:682`), never the `rc`.
* Mutation battery 6/6 CAUGHT in BOTH postures (workstation default tmp root and a
  failing tmp length), with a provenance-gated green control first in all 8
  posture/length combinations: clause deleted (`cannot read the`), freshness path
  not printed, path reduced to its basename, ok-row made to name `memories.db`
  (the negative assert fires), path spelling scrambled (proves `_says` is not a
  word-salad acceptor), and `— learning has stopped` rewritten. Copy at an
  absolute path, pristine snapshot eager, reset + byte-compare after every probe,
  anchors asserted `count == 1`, every mutant `compile()`d before pytest,
  `__pycache__` dropped per run, `PYTHONDONTWRITEBYTECODE=1`.
* Leg counts, paired with the sha (`63aa57d9`, base `94828d99`): canonical
  (`HSCC_TEST_PY` default = hermes venv, real HOME) `1475 passed, 0 skipped`;
  CI-shaped (p313 + sealed HOME) `1471 passed, 4 skipped` — the same 1475 items in
  both arms, and the same numbers t_cc2f23ea measured at `52ca25d5`, so nothing was
  added, deleted or silenced. Skips stay the two existing `hermes_gap` reasons
  (`test_mcp_server.py:176` ×1, `test_session_discovery.py:49` ×3).
