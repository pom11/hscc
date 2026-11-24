kind: Fixed
task: t_fdbc24f9

- **The `node pause expiry` row's in-suite fence now bites in a child the suite
  spawns.** `hscc_daemon/node_pause_exec.py::_doctor_check` fenced its default-root
  read with `"pytest" in sys.modules` — a process-local test. The caller that reaches
  that default root is a fresh interpreter a suite item exec'd, where pytest was
  never imported, so the fence answered for a process that was never involved.
  Measured from such a child (its own planted `HOME`, so the "live" reads here are the
  planted stand-ins for the operator's): the row read the stamp, the pause overlay and
  the daemon pid file and printed `expiry stream healthy: last pass 0s ago ...` while
  the fence had fired for nothing. The door path was already covered — the
  `node_pause_expiry` registry row is refused by `doctor_rows.build_row` before its
  module is consulted (PR #761/#763); a DIRECT call with no root was not.
- **Ruling, since a daemon module cannot import a bootstrap flat sibling:** one
  predicate per package, each stdlib-only and unable to fail, pinned to agree.
  `hscc_daemon/suite_fence.under_pytest()` is the package's copy — `"pytest" in
  sys.modules` OR the `PYTEST_CURRENT_TEST` marker pytest exports per item and every
  child inherits. `doctor_rows.under_pytest()` was not made to delegate into it:
  that would put a plugin-package import on the fence guarding bootstrap's Stage-1
  gate, which has to answer on a fresh install where `hscc_daemon` is not deployed
  yet, and a guarded fallback there would restate the same expression anyway — no
  single copy bought, and a safety line made failable. The duplication is therefore
  checkable rather than tolerated: one test compares both predicates against the
  expected answer for every combination of the two signals.
---
kind: Verified
order: 1

- `hscc_daemon/tests/test_node_pause_expiry_fence_reaches_children_t_fdbc24f9.py`:
  4 cases, both halves in one file. A probe child loads the row by ordinary `import`
  (not `runpy` — a throwaway namespace would make the counts meaningless, as
  t_0d991e1d measured) and reports WHICH BRANCH it took: a call count on each live
  seam (the stamp read, the overlay read, the daemon-liveness probe) plus the path
  each read was asked about. Marked child: both `doctor_check()` and `_doctor_check()`
  refuse, all three seams at 0 and no path named. Unmarked child: every seam reached
  and the verdict carries the planted stamp's resumed host and the planted pause count,
  so the zeros guard a branch the operator's `hscc doctor` still takes. A
  caller-named-root case pins the reads stayed inside the root it was given (the fence
  is not a blanket skip), and an injected-stamp case pins that an injection does not
  unlock a default-root read. The child's own module constants are asserted to resolve
  under the planted home before any zero is believed.
- Red-before (same tree, pre-fix `_doctor_check` restored, these tests kept): **2 of 4
  fail on behaviour** — the marked child read stamp, overlay and pid and printed a
  healthy verdict, and the injected-stamp case still read the overlay it had no root
  for and printed the same healthy verdict. The other two pass both ways by
  construction: they pin the ambient path and the caller-named-root path, which
  pre-fix code already took.
- `hscc_daemon/tests/test_suite_fence_agrees_with_doctor_rows_t_fdbc24f9.py`: 3 cases.
  The two predicates are compared against the EXPECTED answer for all four
  combinations of `pytest in sys.modules` × the marker (a bare "both agree" would let
  two copies that are both wrong pass), the marker constant is pinned to one spelling,
  and the row is pinned to ask the shared predicate rather than a private copy.
- Nothing here asserts on `.hscc/daemon.log`: #716 (`78be7fe3`) muted these rows'
  borrowed WARN through `_silent_read`, so a file-diffing assertion on that line
  belongs to another lane.
