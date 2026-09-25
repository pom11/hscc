# PART 2 README REVIEW (5/5) — hscc_daemon + scripts + ios-app + templates + install

Card: t_6b51ef5b
Assignee: worker
Date: 2026-09-25

## Scope

Task: verify TRUE after telegram removal, main-only cutover, Rich CLI. Verify every
documented command by RUNNING it where CLI-runnable. Fix or DELETE.

Prior cards already covered:
- t_d90de5b6 (card 3): root, hscc-cli, install READMEs
- t_d20fb383 (card 2): memori, memori_byodb, sparkrun-hermes
- t_8ac9e87c (card 4): hscc-bootstrap, hscc-cluster (incl. templates/README), hscc-roles, hscc-api, hscc-commands

Remaining for this card (5/5): hscc_daemon/README.md, scripts/README.md, ios-app/README.md.
hscc-cluster isn't covered above and install/templates already handled; confirm no residuals.

## Status

- [ ] hscc_daemon/README.md — verified
- [ ] scripts/README.md — verified
- [ ] ios-app/README.md — verified
- [ ] merge to main + push

## Findings

### hscc_daemon/README.md — SEVERELY STALE, REWRITTEN
The old README described the pre-modularisation v1 daemon. Multiple claims are
FALSE against the current tree (verified by reading the code + running the CLI):

- Command surface was `hscc_daemon start/stop/status/alerts/check`. The real
  surface is the MERGED `hscc` CLI (hscc_daemon/hscc.py:main()). Verified by
  running `hscc status` / `hscc check all` / `hscc help` (themed Rich, exit 0).
  There is NO `alerts` command (DAEMON_COMMANDS, hscc.py:944-948) — FALSE.
- `hscc_daemon start --dry-run` documented; `start` takes no args (cli.py
  cmd_start) — no --dry-run for start; the only --dry-run exists on
  `hscc cluster up/down`. FALSE.
- Configuration section showed `~/.hscc/daemon/config.json` with handlers
  vllm/gateway/container/nas. NO config.json exists anywhere in hscc_daemon/ —
  checks are `health.py` functions (check_dgx/gateway/local/heartbeat/nas/
  idle/workers) config via env vars; 0 grep hits for config.json. FALSE.
- File Layout showed `daemon.py` + `handlers/{base,vllm,container,gateway,nas}.py`
  and `~/.hscc/daemon/{config.json,status.json,alerts.jsonl,daemon.pid}`.
  None exist. Real: hscc.py + submodules (serving/state/util/health/lifecycle/
  trigger/desktop/daemon_ops/install/cli) and state under `~/.hscc/state/
  <stream>.json` + `~/.hscc/daemon.pid` + `~/.hscc/daemon.log`. FALSE.
- "No dependencies beyond Python 3.9 stdlib" — FALSE. cli_theme.py imports
  rich.* at top level (hard dep), and hscc.py imports cli_theme at top. The
  daemon needs Rich.
- Health Status Categories (healthy/unhealthy/unknown) replaced by real model:
  each stream's <stream>.json ok/blocked -> status shows OK/FAIL/BLOCKED/never
  (cli.py _status_for).

Rewrote README: real module table, real `hscc` command surface, real state file
table, per-stream intervals, worker auto-heal / orchestrator-wedge behaviour,
safety guarantees (debounce, cooldown, per-probe timeouts), troubleshooting.

Verified by RUNNING: `hscc help`, `hscc status` (themed status panel + stream
table: dgx/gateway/local/heartbeat/nas/watchdog/triggers/engine_wedge/dispatcher,
exit 0), `hscc check all` (themed, exit 0). Intro auto-heal claims cross-checked
against health.py (HSCC_WORKER_AUTOHEAL_DEBOUNCE=3, force-recreate via
`template apply <applied> --confirm --force-recreate`, orchestrator NOT
auto-restarted, /cluster-restart registered in hscc-commands register()).
Startup self-clean of .corrupt-*/.stale + .bak.* cap verified (daemon_ops.py:134).

### scripts/README.md — LAN addresses scrubbed + `--deliver desktop` fix
- All 4 documented watchdog scripts exist and RUN (verified): hscc_proxy_watchdog.sh
  (silent = proxy healthy, exit 0), hscc_nas_watchdog.sh (reports the currently-down
  NAS per design), hscc_worker_health.sh (reports 4 unreachable endpoints — cluster
  autodowned), hscc_cluster_digest.sh (digest output). No syntax errors.
- LAN-address scrub (REPO PUBLIC): README repeatedly repeated real fleet IPs
  `.244/.246/.247/.248:8000`, `.249`, `.244`. Replaced with the documented
  placeholder `100.64.0.1/.2/.3/.4` / `100.64.0.x` / "orchestrator head".
  (The actual shell scripts still hardcode real 10.0.0.x IPs — those are the
  live deployed probes; out of README scope. FLAGGED as follow-up.)
- `--deliver desktop` = SILENT NO-OP (code-verified). `desktop` is not in
  cron.scheduler_delivery._KNOWN_DELIVERY_PLATFORMS (telegram/discord/slack/
  signal/matrix/mattermost/...), no plugin registers a cron_deliver_env_var for
  it, so _resolve_single_delivery_target returns None → behaves exactly like
  `--deliver local`. Verified empirically: _resolve_delivery_targets({'deliver':
  'desktop'}) == [] (same as 'local'/'origin' for these script jobs). The CLI
  ACCEPTS `--deliver desktop` at create time (tested: created+removed a paused
  job) but it delivers NOWHERE.
  FIX: removed `--deliver desktop` from the hscc-cluster-digest command (default
  = output saved to job run history, visible via `hermes cron list`/`runs`), and
  documented that a real delivery requires `--deliver <platform>` / `bot-chat`.
  NOTE: escalate_watcher_run.py + the root README escalation line use the SAME
  `--deliver desktop` convention (card t_d90de5b6 blessed it) → same silent
  no-op. FLAG as follow-up (not in this card's README scope).
- dep_pr_watcher section verified: .github/workflows/check-runtime-deps.yml exists,
  hscc-bootstrap/runtime-versions.json exists, install_dep_watcher.sh exists with
  `--uninstall` + daily 08:00 default (SCHEDULE="0 8 * * *"). Accurate.
- hermes cron create syntax verified (`--name/--no-agent/--script/--deliver` + the
  valid-target list from hermes_cli/subcommands/cron.py).

### ios-app/README.md — port claims FIXED, LAN addr scrubbed
- "Default port is 8788 (8787 is taken by another service)" FALSE: the API code
  default is DEFAULT_PORT=8787 (hscc-api/api_server.py:44); this deployment
  overrides to 8788 via ~/.hscc/api.json (bind=tailscale). "8787 taken by another
  service" is inaccurate — 8787 is simply the code default. Fixed all 4 port
  mentions to say: this deployment runs 8788 (override in ~/.hscc/api.json), code
  default 8787, match the app to `hscc api status`.
- Scrub: `gateway (`.244`)` → `gateway (the orchestrator head)` (real LAN
  address). No other real addresses in ios README (100.x/100.x.y.z are generic
  Tailscale placeholders, correct as-is).
- `hscc api start --tailscale` verified real (api_cli.py:133-134); token at
  ~/.hscc/api-token mode 0600 verified; 0.0.0.0 refused by design verified
  (api_server.py:239-244); api status currently shows still running.
- Endpoint claims (project/orchestrator chat/template/autodown/kanban/etc)
  previously verified against live API (README's own End-to-end review, 2026-08-27);
  hscc-api routes confirmed present by card t_8ac9e87c. No cross-cutting route
  removal postdates that. No telegram references anywhere in ios README.


