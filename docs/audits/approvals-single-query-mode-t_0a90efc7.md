# t_0a90efc7 — bootstrap: wire approvals.single_query_mode so workers can execute code

Status: DONE
Branch: wt/bootstrap-sqm
Commits:
- 50bf32e  bootstrap: wire approvals.single_query_mode so dispatched workers can execute code
- 4769db4  bootstrap tests: cover approvals.single_query_mode both directions
- 905d142  bootstrap tests: include wired approvals key in the doctor fresh-config fixture

## Root cause (confirmed)
Dispatched kanban workers run `hermes -c ... -p <profile>` in single-query
(-q) mode. Hermes defaults `approvals.single_query_mode` to **deny**
(tools/approval.py:3552), so execute_code is denied for every worker — and by
definition no human exists to approve in -q mode. Hermes' own error text names
the remedy: "set approvals.single_query_mode: approve in config.yaml".

## Fix
Added `_ensure_approvals` to `hscc-bootstrap/enable_plugins.py:533`, run from
`enable()` (line 749). It defaults the key to `approve` (module constant
`SINGLE_QUERY_MODE` = "approve", line 143), but ONLY sets it when the key is
ABSENT or not a valid value (`approve`/`deny`). A deliberate operator-set
`deny` is preserved untouched — mirroring the preserve-the-operator semantics
of `delegation.max_concurrent_children` (the block added previously).

ALSO completed the wiring the earlier partial edit missed: `changed_approvals`
now participates in the write-if-changed guard (line 761) and in the returned
result dict (line 778), so an approvals-only reconcile actually persists to
disk and is reported.

## Tests
- hscc-bootstrap/tests/test_enable_plugins.py: 78 passed (includes two new
  tests, both directions).
- hscc-bootstrap/tests/test_doctor.py: 3 affected tests pass after updating
  the fresh-config fixture to include the wired approvals key (it now is a
  true no-op again; the reconcile union been correctly reports the approvals
  fix as drift on any config lacking it).

## Evidence
- `pytest hscc-bootstrap/tests/test_enable_plugins.py` → "78 passed in 271.44s"
- `pytest hscc-bootstrap/tests/test_doctor.py -k "fix_noop_when_fresh or no_config_path"` → "3 passed in 15.55s"

## What I did NOT do (and why)
- Did NOT edit the operator's live ~/.hermes/config.yaml. The operator already
  set this key by hand. The change is to the bootstrap CODE only, so a FRESH
  install gets it automatically.
- Did NOT run `hscc doctor` or bootstrap.sh against the live runtime — they
  mutate ~/.hermes/config.yaml. All tests use temp paths.
- Did NOT touch Telegram / no mutating POSTs.
- No real host/IP/token committed anywhere (public repo).
