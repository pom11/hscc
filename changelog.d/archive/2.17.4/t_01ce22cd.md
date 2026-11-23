kind: Fixed
task: t_01ce22cd

* **`hscc escalate`'s help no longer claims the verb never mutates (F6).**
  `POST /v1/escalate` performs the escalation — reassign a repeatedly-failing
  card to the strong tier, or alert a human — while its CLI twin advertised
  "Show pending failure escalations (dry-run, no mutations)". Read as a property
  of the *verb*, that sentence is false of the route, and the surface that
  misleads is the one an agent actually reads (the audit's own §B matrix row for
  the verb names the gap: "no confirm gate named in the verb"). `dry-run` is a
  false friend here besides: everywhere else in this CLI `--dry-run` is a flag on
  a *mutating* verb (`cluster up|down`, `node pause|resume`, `orch inject`), never
  a property of a read. The route already carries the confirm contract the other
  mutating routes carry (`hscc-api/routes_ops.py:501` `_require_confirm` → 409
  `confirm_required`, the same gate as `/v1/triggers/run` and
  `/v1/cluster/{up,down}`, in place since the endpoint was added in `67fc6de7`),
  so the disagreement was the wording — and the wording is what changed.
  `COMMAND_HELP["escalate"]`, the single source both `hscc help escalate` and
  `hscc escalate --help` print, is now "Preview pending escalations — this verb
  writes nothing. / POST /v1/escalate is the mutating half: confirm-gated, / it
  reassigns a failing card or alerts a human."; the `hscc --help` entry is
  "Preview escalations — POST /v1/escalate mutates". Naming the route inside a
  verb's help is this CLI's existing disambiguation shape (`COMMAND_HELP["orch"]`
  already points at `POST /v1/orchestrator/chat`). No behaviour change: the verb
  still injects no-op `_reassign`/`_notify` (`hscc_daemon/hscc.py:2531`) and
  writes nothing.
---
kind: Verified
task: t_01ce22cd

* new `hscc-api/tests/test_escalate_surface_contract.py` (1 item) pins both
  surfaces' semantics in ONE test, because a per-surface test can stay green
  while the pair disagrees — and the disagreement is the finding. CLAIM: the
  verb's help names `POST /v1/escalate`, names its confirm gate, carries no
  unqualified "no mutations", and the `hscc --help` entry names the route too.
  BEHAVIOUR: `hscc escalate` and `GET /v1/escalate` each inject substitute
  `_reassign`/`_notify` (asserted distinct from the watcher's real
  `_default_reassign`/`_default_notify`, then called for real against a relocated
  HOME that must gain no file), while `POST /v1/escalate` injects nothing — so the
  acting defaults bind — and is refused without `"confirm": true` by an `ApiError`
  equal to the one `handle_cluster_down` raises (one contract, not a private
  gate), the refusal landing before the watcher runs. Head: `1 passed`.
* the pin bites — five mutations applied to a detached copy worktree, each run
  against that one test: F6 wording restored (`rc=1`), the CLI verb stops
  injecting substitutes (`rc=1`), the POST's `_require_confirm` deleted (`rc=1`),
  the POST injects no-ops so it stops mutating (`rc=1`), the GET stops injecting
  (`rc=1`); the same copy untouched: `rc=0`.
* the help text has a coupled mirror, and it bit: `hscc --help` is also stored in
  `scripts/tests/fixtures/hscc_help_landing.txt`, and
  `scripts/tests/test_site_landing.py::test_fixture_is_the_repos_own_help`
  compares that fixture against the repo's OWN rendered help — this card's
  wording change reddened it (`1 failed, 66 passed, 2 skipped`) until the fixture
  was refreshed from the repo's own render the way the test renders it (sandboxed
  HOME, `PYTHONPATH`=repo, COLUMNS=80, NO_COLOR, TERM=dumb, HSCC_THEME=dark, ANSI
  stripped). The fixture delta is exactly the escalate entry (the old 56-col
  description pushed the pair to 79 cols, so it rendered as usage-alone + hanging
  description; the new 47-col one fits one line), and
  `scripts/gen_site_commands.py --help-file … --write` then reported
  `site/index.html is up to date (46 commands, hscc 2.17.3)` — the page lists
  command names, not descriptions. After the refresh:
  `test_site_landing.py` + `test_cli_help_surface.py` → `67 passed, 2 skipped`.
* the surfaces the diff touches, re-run on the merged tree: `hscc_daemon/tests`
  `test_toplevel_cli_help_f5.py` (60 items — it carries the verbatim pin of the
  one-line entry) + `test_cli_h_shortflag.py` + `test_cli_rich_handlers.py` →
  `195 passed`; `hscc-api/tests` `test_escalate_surface_contract.py` +
  `test_routes_ops.py` → `34 passed`.
* leg totals, attributed honestly (one pytest process per leg dir, the runner's
  interpreter and env, measured at `14e2e7af`): `hscc_daemon/tests`
  `2 failed, 4952 passed, 1 skipped`; `hscc-api/tests` `3 failed, 1341 passed,
  1 skipped`. All five reds are SIGINT / cooperative-stop ids in
  `test_api_cli_stop_sidecar.py`, `test_api_server_stop_signal.py` and
  `test_api_server_stop_bound.py` — three files this diff does not touch and which
  are byte-identical to `origin/main` (`git diff --name-only
  origin/main...HEAD` lists only the two source files and the new test). Each of
  the three passes in isolation on this tree (`12 passed`, `13 passed`,
  `8 passed`), and a pristine `origin/main` worktree run of
  `test_api_cli_stop_sidecar.py` is red without this diff too (`1 failed,
  11 passed in 184.88s` — the 180 s stall signature, on an id other than the two
  that reddened the leg). The lane is stall-flaky on a loaded host; nothing in it
  reads the escalate surface.
* post-merge (head `1b49951c`, host idle, one pytest process per leg dir):
  `hscc_daemon/tests` **`4954 passed, 1 skipped`, exit 0** — both pre-merge reds
  came back green. `hscc-api/tests` `1 failed, 1360 passed, 1 skipped`; the single
  red is
  `test_gateway_serve_watchdog.py::test_watchdog_buries_its_child_when_the_parent_is_sigkilled`,
  the SIGKILL-liveness test that landed with `t_ed2f1abe`, and it reddens without
  this diff too: the same selection six times over two trees — pristine
  `origin/main` `3 passed / 3 passed / 1 failed [poison]`, this tree
  `1 failed [poison] / 3 passed / 3 passed` — with the whole-leg red a third
  parametrization again.
