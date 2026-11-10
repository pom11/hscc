kind: Fixed
task: t_b62da70c

- **The "is this a planted fake `hermes_cli`?" rule has one definition again.**
  PR #270 (`t_2be3b04e`) stopped one test file's planted fake from deciding what
  a later file's board enumeration saw, and left the rule with **three** owners:
  conftest's `_is_planted_path` / `_hermes_cli_poison_reason`,
  `test_autodown.TestLoadKanbanDb._evict_planted_hermes_cli` (matched its own
  `tmp_path` *or* a substring of the pytest tmp-dir marker) and
  `test_board_pin._evict_planted_hermes_cli` (matched that marker alone). Three
  definitions of one hazard is how this class kept coming back — `t_b014c2b8`
  hit it from the other direction in `8e2283a7` — and the copies were already
  diverging: `test_board_pin`'s non-vacuity test built its stub under `/tmp`,
  which its own substring matched and conftest's temp-root rule did **not**, so
  the file was testing a rule nothing else in the suite agrees with.
  The predicate now lives in `hscc_daemon/tests/hermes_cli_plant.py` (moved
  verbatim: `is_planted_path`, `hermes_cli_names`, `poison_reason`,
  `purge_planted`, plus `evict_planted` which hands back what it removed so a
  borrower can restore it) and all four consumers import it. conftest keeps the
  autouse fixture and rebinds the shared objects under its historic private
  names, so `test_hermes_cli_import_isolation`'s `cf._…` probes are unchanged —
  and no test imports conftest to reach the rule, because a second import of
  conftest re-runs its module-level sparkrun-fence loader and swaps the live
  fence out from under the running suite. Each site keeps what is genuinely its
  own: `test_autodown` keeps the class-scoped finalizer (its own plant),
  `test_board_pin` keeps the borrow-and-`finally`-restore for `real_root`, and
  the nested probe file B keeps its own canary — now asked through the shared
  predicate instead of a re-derived `TEMP_ROOTS`.
  A fix that only moves text is not closed, so
  `hscc_daemon/tests/test_planted_hermes_cli_single_source.py` sweeps the
  package and fails if any tracked file re-`def`s a predicate function, carries
  the tmp-dir-marker or private-temp-root literal, or handles a plant without
  importing the owner; it also pins that conftest's `_…` names are *the same
  objects* as the module's, and that importing the module in a fresh interpreter
  changes neither `sys.path` nor the `hermes_cli`/fence cache. Guard proven
  non-vacuous by mutation probes (second `def` and marker literal each caught by
  the named check, unmutated control green). Behaviour is preserved where it was
  intentional; the one deliberate narrowing is that a path which merely contains
  the marker substring no longer counts as a plant — it was a false positive, and
  `hscc-cluster/tests/sparkrun_fence.py::_under_pytest_tmp` needs its *stricter*
  two-marker rule for a different question, so it stays out of scope (recorded in
  the guard's docstring so the next reader does not "unify" it).
