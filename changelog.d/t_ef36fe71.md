kind: Fixed
task: t_ef36fe71

- **`hscc verify`'s `api_routes` row called a healthy API dead.** In the
  installed keg the row read `route(s) the app calls did not answer: Traceback
  (most recent call last):` while the API was listening and its log was clean.
  Two faults stacked: the sweep is run by script path, so its own directory was
  the only thing on `sys.path` and `from hscc_daemon import cli_theme` raised
  `ModuleNotFoundError`; and `check_api_routes` shelled out to a bare
  `python3`, which on the keg has no `rich` at all. The traceback exited 1 and
  exit 1 meant "a route did not answer" — the checker's own crash was
  indistinguishable from the outage the row exists to catch. The sweep now
  bootstraps `sys.path` itself (the sanctioned `verify_chat_roundtrip.py`
  pattern), `verify` uses its own interpreter, and a sweep that produced no
  route report is UNVERIFIED — never a dead-route verdict. A missing client
  source, missing `curl` or unavailable renderer exits a new code 3 ("could not
  run"). The row also stops printing a traceback's useless first line: the
  exception itself is on the last.
---
kind: Verified
order: 1

- `hscc_daemon/tests/test_api_route_sweep_standalone.py`: **14** new cases —
  the operator's manual repro (bare script path, cwd `/`, scrubbed `HOME`, fake
  keg in tmp) plus every classification path through `check_api_routes`.
  Mutation-checked: removing the `sys.path` bootstrap turns
  `test_bare_script_run_can_import_the_real_theme` red.
- Live-keg reproduction of both faults before the fix, and of the fixed sweep
  after: `--json` on a closed loopback port reports its routes, the human view
  without `rich` says `cannot render`, and neither emits a traceback.
