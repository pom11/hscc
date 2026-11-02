kind: Fixed
task: t_b82ad0fe

- **The httpx keg guard could report GREEN over a blind keg venv (GAP-D-1).**
  `hscc_daemon/tests/test_kanban_monitor_deps.py` looped the installed kegs and
  handled a pre-pin keg with `verdicts.append("…skip…") + continue`, so the
  `assert verdicts` at the end held and pytest reported `5 passed` — on this
  host, over the 2.14.0 keg whose shipped `libexec/hscc/requirements.txt` has
  no httpx at all. The leg is now one test PER KEG
  (`@pytest.mark.parametrize` over a collection-time `_keg_params()`), so every
  reason a keg goes unprobed is a real `pytest.skip()` with its reason, and
  pytest reports `4 passed, 1 skipped` instead of a pass. `verdicts` is gone;
  nothing can append its way past an assertion any more.
- **`run_tests.sh` now runs each leg with `-rs`.** A skip that is real but
  silent is still invisible to the next reader; the canonical runner prints the
  skip reason so "the guard never got a tap" cannot be mistaken for "the tap
  agrees".
---
kind: Added
task: t_b82ad0fe

- **Tap-vs-SOT dependency drift guard** —
  `scripts/tests/test_formula_dep_drift.py`. `Formula/hscc.rb` builds its
  libexec venv with `venv.pip_install resources` from hand-written resource
  blocks and NEVER reads `requirements.txt`, so every pin added here is inert
  for brew installs until the tap carries the matching resource — which is
  exactly how `httpx==0.28.1` (PR #113) shipped missing from the 2.14.0
  formula. The guard compares the two, name-by-name (PEP 503 canonicalised), on
  presence, exact `==` pins and range satisfaction, plus resource-block
  structure, and skips with the paths it tried when the formula is unreachable
  (it never clones — a guard that needs the network to satisfy itself is not a
  guard). Non-vacuity is pinned: fabricated formulas that drop the httpx
  resource or pin it at the wrong version MUST be reported.
---
kind: Verified
order: 1
task: t_b82ad0fe

- `hscc_daemon/tests/test_kanban_monitor_deps.py`: same **5** collected cases
  (`pytest --collect-only` names the keg leg by keg id —
  `test_brew_keg_venv_imports_the_board_read_chain[2.14.0]`); on this host the
  run now reports `4 passed, 1 skipped` with
  `SKIPPED … ships /opt/homebrew/Cellar/hscc/2.14.0/libexec/hscc/requirements.txt
  without an httpx pin …` where the whole file previously reported `5 passed`
  over a keg venv that has no httpx installed.
- `scripts/tests/test_formula_dep_drift.py`: **8** new cases, green against the
  operator tap (`origin/main` carries `resource "httpx"` since tap commit
  `9676297`).
- End-to-end mutation probe (scratch, not committed): the real test function
  FAILS with `MISSING: 'httpx' … INERT for every brew install` against a copy
  of the real formula with the httpx block removed, PASSES on the real
  formula, and `SKIPPED` visibly lists all three candidate paths when
  `HSCC_TAP_DIR` / `DEFAULT_TAP_DIR` / `CACHE_ROOT` are redirected to empty
  dirs.
