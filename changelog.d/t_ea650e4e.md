kind: Added
task: t_ea650e4e

* the dashboard pair now has HTTP reads: `GET /v1/main/status` and
  `GET /v1/orch/waker/status` (audit §2.6, decision D9). Each route answers one
  CLI verb's question by calling the ONE function that verb renders from —
  `main_cli.status()` (identity through `hscc-roles/main_orch.py`, pane through
  `repl_inject`) and `orch_waker.waker_status()` — so the CLI and the route
  cannot drift, and each handler adds nothing to the payload but `speak`. Reads
  only: the handlers take no body, no confirm flag and no `profile`, and the
  waker's enabling verbs stay CLI-only.
---
kind: Verified
task: t_ea650e4e

* `hscc-api/tests/test_routes_ops_main_waker.py` — 28 items, collected and
  green at `5621b61f`; the whole `hscc-api` leg in one process is
  `1429 passed, 1 skipped, 1 xfailed, 1 failed` in 461 s at the same sha. The
  one red is `test_api_server_stop_signal.py::test_the_event_shape_is_the_hazard[SIGINT]`,
  whose own docstring says it measures the interpreter "with no hscc code
  involved"; the same file is `15 passed` standalone at this head and on
  pristine `origin/main` (`f608ad06`), so it is a load-sensitive premise pin and
  not a regression from this diff.
* Read-only is measured, not asserted: the waker's own config and `state/`
  files are byte-for-byte identical (size, mtime_ns, sha256) across a real
  request against a sandboxed root, with `set_enabled`/`waker_tick` patched to
  tripwires. The main answer is read from a name stored through
  `main_orch.store_name` at a sandboxed `HSCC_DIR`, so a route that grew its own
  resolver would answer the default instead.
* Mutation probe at `5621b61f`: dropping one key from the main payload inside
  the handler reddens
  `test_main_status_200_passes_the_cli_payload_verbatim` (`KeyError: 'detail'`)
  and nothing else, so the pass-through gate bites.
* `scripts/changelog_fragments.py check` rc 0, `scripts/readme_lint.py` prints
  `findings: 0`, `scripts/tests/test_changelog_fragments.py` 49 passed — all at
  `5621b61f`.
