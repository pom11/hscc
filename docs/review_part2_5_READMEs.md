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

