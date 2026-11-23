kind: Docs
task: t_28cc4de5

* `hscc kanban monitor --help` now names its route parity: `GET
  /v1/kanban/running` answers this same read from the same
  `kanban_live.collect(registered_only=True)` pass, for a caller with no TTY
  (agent-surface audit F8, `docs/audits/2026-11-23-agent-surface.md:158` — the
  parity existed under a different name and no surface said so, so a reader at
  the verb could not learn the route exists and a reader at the route could not
  learn which verb it mirrors). The pointer is stated on BOTH ends: the verb's
  help names the path, and `routes_kanban.handle_kanban_running`'s docstring
  names the verb back, so neither side can go silent again. Nothing was added to
  the surface — no agent tool and no mutation route (F8's disposition says do not
  invent one): the kill panel stays the separate confirm-gated `POST
  /v1/kanban/task/{id}/kill`, and `kanban pause/resume/paused/enforce` stay
  CLI-only per decision D6.
---
kind: Verified
task: t_28cc4de5

* `hscc-api/tests/test_kanban_monitor_route_pointer.py`: 9 tests green, and they
  bite — measured by mutation on a throwaway `--detach` worktree at the commit,
  tree restored clean afterwards: removing the pointer from the verb help reddens
  2 CLI-end tests, rewriting the handler's summary line to drop the verb name
  reddens 1 route-end test, and registering a `POST` on `/v1/kanban/running`
  reddens the GET-only test; the unmutated control is 9 green. (The verb name is
  pinned on the docstring's SUMMARY line precisely because the old body prose
  already mentioned `hscc kanban monitor --board` and F8 still happened — a
  pairing buried in prose nobody opens is not a pointer.)
* The naming is not allowed to be decorative: the shared-backing test drives
  both doors — `kanban_cli._cmd_monitor --json` and the route handler — against
  one spied `kanban_live.collect` and requires the identical call set
  `board=<slug>, registered_only=True`, and a second test requires the handler to
  reach collect through `_backing_collect` exactly once (a second hand-rolled
  board read is how a parity pointer becomes a second source of truth, audit
  F2's shape). Row-for-row equality stays `test_widget_route_parity_cards.py`'s
  job and was not re-proved here.
* No live anything: no board opened, no process enumerated (`_backing_list_running`
  is stubbed), no host contacted; the leg's autouse `_isolate_hscc` fixture keeps
  the real `~/.hscc` out of it.
