# Orchestrator reconcile loop (report-back)

Closes the report-back loop so completed worker tasks actually reach an
*interpreting* orchestrator turn — not just a raw outbound telegram.

## Why

Worker completion writes `task_events`; the coordinator's `notify-subscribe`
makes the gateway `_kanban_notifier_watcher` do a raw `adapter.send(chat_id,…)`.
That is **outbound only** — the human sees a telegram but no orchestrator turn
interprets or continues. Patching Hermes core (to forge an internal
`MessageEvent`) is out of scope, so the loop is closed with a Hermes **cron job**
that spawns an interpreting agent reading kanban (the source of truth).

## Pieces

| File | Runtime home | Role |
|------|--------------|------|
| `hscc_orch_tick.py` | `~/.hermes/scripts/` | Detector. Scans every live `hscc-*` kanban board directly (source of truth — catches ad-hoc cards too), enriches with `~/.hscc/bridge.json` (worker host / worktree / hscc id) when a card came through `dispatch-task`. Emits only NEW terminal cards (done/review/blocked) deduped via `~/.hscc/.orch_tick_ack.json`. Empty stdout → agent stays `[SILENT]`. First run seeds baseline silently. |
| `orch-tick.prompt.txt` | (in cron job) | Agent prompt: brief each task; honor the AUTONOMY flag; may flip its own gate. |
| `orch-tick.cron.json` | (reference) | Sanitized snapshot of the live cron job in `~/.hermes/cron/jobs.json`. |
| `install-orch-tick.sh` | — | Idempotent installer: detector → `~/.hermes/scripts/`, registers the cron job, seeds the autonomy gate. |

## Autonomy gate

`~/.hscc/autonomy` (`on`/`off`, default `off`). Read by the detector and the
coordinator `autonomy [on|off]` subcommand.

- `off` — tick agent **summarizes and waits** for a human go-ahead (no mutation).
- `on` — tick agent **auto-dispatches** the next step (green-check / merge /
  remove / dispatch via the coordinator CLI).

Flip it from either side:

```
hscc-agent-coordinator autonomy on|off   # human OR orchestrator; no arg = show
echo on > ~/.hscc/autonomy                # plain-file fallback
```

The cron prompt tells the tick agent it may flip its own gate and must say so.

## Install

```
DELIVER=telegram:<chat_id> ./install-orch-tick.sh
```

Idempotent: skips the cron create if `hscc-orch-tick` already exists, and only
seeds the autonomy gate if unset.

## Not version-controlled (runtime state)

`~/.hscc/autonomy` (gate) and `~/.hscc/.orch_tick_ack.json` (dedup cursor) are
state, not source — recreated on demand by the installer / detector.
