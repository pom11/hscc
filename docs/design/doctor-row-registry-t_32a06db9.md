# Doctor row registry — decomposing the `run_doctor()` row pile-up

Card: t_32a06db9 (hotspot backlog, flagged twice; caused the PR #173 vs #175 conflict
resolved by rebase card t_3f805c4f).

## The hotspot, measured on main @ 5d12aecc

`hscc-bootstrap/doctor.py` is 2263 lines. A feature that wants a doctor row must
edit **three** places that all sit in the same two hunks:

1. a row adapter appended after `_supervision_check_row` (`doctor.py:1426-1547` today:
   orch-waker, cron-watchers, supervision, kanban-enforce — 4 adapters, ~320 lines);
2. a seam keyword in `run_doctor()`'s signature (`doctor.py:1550-1556`:
   `_orch_waker_check`, `_cron_watchers_check`, `_supervision_check`,
   `_kanban_enforce_check` all appended on the last lines);
3. one entry with its explanatory comment in the `checks` list
   (`doctor.py:1583-1658`) — the comment block above each call is longer than the call.

Any two features touching doctor therefore collide in all three hunks. #173 and #175
did exactly that.

## Design: one registry entry per feature row

The three edits collapse into **one registry entry** in one module-level list, and a
new feature adds to it in exactly one place.

```python
FEATURE_CHECK_ROWS: List[FeatureCheckRow] = [
    FeatureCheckRow("orch_waker", "orch-waker (orchestrator waker)",
                    "_orch_waker_check", _load_orch_waker,
                    "hscc_daemon/orch_waker.py", _refused_orch_waker_check),
    ...
]
```

`run_doctor()` keeps its explicit keyword seams (tests inject them today and must keep
working) but the list is built by a loop over the registry:

```python
for feature_row in FEATURE_CHECK_ROWS:
    checks.append(feature_row.build(
        seams.get(feature_row.seam_kwarg), hscc_dir=os.environ.get("HSCC_DIR")))
```

The five field adapter that every row repeats verbatim moves to
`FeatureCheckRow._adapt()`, shared with the node-recovery / role-SOUL /
notifier-mask / GPU-clock blocks.

### Why the row logic stays in `doctor.py` (option B rejected)

The card allows either a registry or `doctor_<feature>.py` modules. The registry is the
smaller move **and** the existing row tests would break under option B: they patch
private helpers on the `doctor` module object —

- `test_doctor_orch_waker.py:131,158` → `monkeypatch.setattr(doctor, "_load_orch_waker", ...)`
- `test_doctor_cron_watchers.py:144,171` → `monkeypatch.setattr(doctor, "_load_cron_watchers", ...)`
- `test_doctor_kanban_enforce.py` → the same shape for `_load_kanban_pause`
- and they call `doctor._orch_waker_check_row(...)` / `doctor._kanban_enforce_check_row(...)`
  directly to pin the five-field copy contract.

Splitting the adapters out would force every one of those tests to change — exactly
what the acceptance criterion forbids ("green UNCHANGED or with only seam-plumbing
adjustments"). The registry keeps every name the tests touch where the tests look.

### Semantics that must not move (checklist, verified against main)

- **Row order**: the four feature rows stay in the same relative position inside the
  core `checks` list (after models-served, before the fleet block). The fleet/skipped
  tail (node-recovery, role-SOUL, notifier-mask, GPU clock cap) stays where it is.
- **Fence**: `if injected is not None / elif "pytest" in sys.modules / else real check`
  — evaluated at row-build time, exactly as today. Uninjected in pytest ⇒ ERROR naming
  the seam; never a silent pass, never a read of `~/.hscc`.
- **`hscc_dir`**: taken from `os.environ.get("HSCC_DIR")` at build time per row (not at
  import time) and forwarded to the real `doctor_check(hscc_dir=...)` — pinned at the
  call site by `test_doctor_kanban_enforce.py:168`.
- **Unverifiable row**: a feature module that cannot be imported prints the fallback
  name and `fatal=False`, "not importable" + "re-run bootstrap".
- **Five-field adapter**: `name/ok/detail/fix/fatal` only; invented fields dropped.
- **`run_doctor()` signature**: keeps the four explicit keywords plus `**_feature_rows`
  for future rows, so a new row never touches the signature again.

## Status

- [x] design decided
- [ ] registry + shared adapter in `doctor.py`
- [ ] four rows registered
- [ ] new-row suite `tests/test_doctor_row_registry.py`
- [ ] `scripts/run_tests.sh` ALL GREEN + `scripts/address_guard.py --tracked` rc 0
