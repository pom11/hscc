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
