# Doctor row registry — decomposing the `run_doctor()` row pile-up

Card: t_32a06db9 (hotspot backlog, flagged twice; caused the PR #173 vs #175
conflict resolved by rebase card t_3f805c4f).

## The hotspot, measured

`hscc-bootstrap/doctor.py` is ~2260 lines. A feature that wanted a doctor row had
to edit **three** places that all sat in the same two hunks:

1. a row adapter appended after the previous one (before this card: orch-waker,
   cron-watchers, supervision, kanban-enforce, budget-hook — 5 adapters, ~400 lines);
2. a seam keyword appended to `run_doctor()`'s signature (5 keywords, each landing
   on the last lines of the signature);
3. one entry with its explanatory comment appended to the `checks` list — the
   comment block above each call was longer than the call itself.

Any two features touching doctor collided in all three hunks. #173 vs #175 did
exactly that (rebase card t_3f805c4f). While this card was in flight, #175 merged
and collided with **this branch** in the same three places — the strongest
possible confirmation of the premise.

## Design: one registry entry per feature row

The three edits collapse into **one entry** in one module-level list:

```python
FEATURE_CHECK_ROWS = [
    doctor_rows.FeatureCheckRow(
        key="orch_waker", seam_kwarg="_orch_waker_check",
        row_fn="_orch_waker_check_row"),
    ...
]
```

and `run_doctor()` builds the rows with one loop:

```python
checks += doctor_rows.render_rows(
    sys.modules[__name__], FEATURE_CHECK_ROWS, seams,
    hscc_dir=os.environ.get("HSCC_DIR"), hermes_home=home)
```

Three details are load-bearing, each because of something in the existing tests:

- **Hooks are named as strings and resolved on the doctor module per call.**
  `test_doctor_kanban_enforce.py` swaps `doctor._kanban_enforce_check_row` to pin
  the `hscc_dir` forwarding *at the call site*, and the waker/watcher suites swap
  `doctor._load_orch_waker` / `_load_cron_watchers`. A registry that captured
  function objects at import time would silently turn every one of those patches
  into a no-op — the tests would pass while proving nothing.
- **Seams arrive through `**feature_rows` and are validated against the
  registry.** The five pre-existing keywords stay explicit for compatibility, but
  a new row never touches the signature again, and a typo'd keyword is a
  `TypeError` rather than a silently ignored seam that quietly downgrades a row to
  its refusal verdict.
- **Each row declares the context it reads** (`row_kwargs`): `hscc_dir` for the
  daemon-state rows, `hermes_home` for the budget-hook row (it reads the
  *deployed* plugin tree, not `~/.hscc`). One-size-fits-all forwarding would
  `TypeError` at bootstrap time on any row whose function takes the other one.

## Why the row logic stays in `doctor.py` (option B rejected)

The card allowed either a registry or `doctor_<feature>.py` modules. The registry
is the smaller move **and** the existing row tests would break under option B:
they patch private helpers on the `doctor` module object —

- `test_doctor_orch_waker.py:131,158` → `monkeypatch.setattr(doctor, "_load_orch_waker", ...)`
- `test_doctor_cron_watchers.py:144,171` → `monkeypatch.setattr(doctor, "_load_cron_watchers", ...)`
- `test_doctor_kanban_enforce.py:166` → swaps the **row function itself**
- and they call `doctor._orch_waker_check_row(...)` / `doctor._kanban_enforce_check_row(...)`
  directly to pin the five-field copy contract at the bridge.

Splitting the adapters out would force every one of those tests to change — exactly
what the acceptance criterion forbids ("green UNCHANGED or with only seam-plumbing
adjustments"). The registry keeps every name the tests touch where the tests look.

## Semantics that did not move (each one verified against a test)

- **Row order / position**: the five feature rows sit after models-served and
  before the fleet block, in registry order — same as the five literal entries.
- **Fence**: `injected seam → in-suite refusal → real check`, evaluated per call.
  An uninjected row inside pytest is an ERROR naming the seam; never a silent
  pass, never a read of `~/.hscc` or the deployed plugin tree.
- **`hscc_dir`** read from `os.environ.get("HSCC_DIR")` at build time (not import
  time) and forwarded to the real `doctor_check(hscc_dir=...)` — pinned at the
  call site by `test_doctor_kanban_enforce.py:169`.
- **UNVERIFIABLE row** for a feature module that cannot be imported: fallback
  name, `ok=False`, `fatal=False`, "not importable" + "re-run bootstrap".
- **Five-field adapter** (`name/ok/detail/fix/fatal`) — invented fields dropped,
  so `doctor --json` keeps one shape.

## Verification

- `tests/test_doctor_row_registry.py` — 17 tests. Pins registry self-consistency
  (every named hook exists — the one thing a string-named registry gives up
  versus a direct call), seam/signature agreement, "a new row needs no change to
  `run_doctor()`", unknown-seam rejection, per-row context selection, and the
  generic build path (refusal / unverifiable / `hscc_dir` pass-through /
  five-field copy / `fallback_name` constant resolution).
- The `fallback_name` case was found by a non-pytest smoke run, not by the suite:
  the generic path passed the *attribute name* to the adapter, so a feature verdict
  dict without a `name` would print `_PROBE_CHECK_NAME` to a human. Every suite test
  used a feature that returned its own name, so the fallback was uncovered — exactly
  the hazard a string-named registry invites (an identifier that was checked by the
  interpreter at import becomes unchecked data).
- Mutation-proven (each mutation restored immediately after, verified by diff):
  pinning `render_rows` to the known rows fails the "new row needs no change"
  tests; `if False:` on the unknown-seam guard fails the rejection test; dropping
  `hermes_home=home` from the render call fails the budget-row forwarding test.
- The five feature suites — `test_doctor_orch_waker`, `test_doctor_cron_watchers`,
  `test_doctor_supervision`, `test_doctor_kanban_enforce`,
  `test_doctor_budget_hook` — pass **unchanged** (0 edits to any of them).

## Status

- [x] design decided (registry, not per-feature modules — reasons above)
- [x] `doctor_rows.py`: `FeatureCheckRow`, `build_row`, `render_rows`, `adapt`
- [x] five rows registered (`orch_waker`, `cron_watchers`, `supervision`,
      `kanban_enforce`, `budget_hook` — the last folded in when #175 merged)
- [x] `tests/test_doctor_row_registry.py`
- [x] changelog fragment + `docs/README.md` index entry
- [ ] `bash scripts/run_tests.sh` ALL GREEN + `scripts/address_guard.py --tracked`
      rc 0 — see the card comment for the measured result
