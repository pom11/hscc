# Screen audit: AutodownView — prove every element works

Task: t_9b678f46
Assignee: ios-engineer
Target: `ios-app/Sources/HSCC/Views/AutodownView.swift`

## Status snapshot (live API, read-only, fetched this run)

Address derived via `hscc api status` (redacted to placeholder — see note).
Token: ~/.hscc/api-token.

```
GET /v1/autodown/status →
{
  "enabled": true,
  "state": "up",
  "idle_minutes": 120,
  "last_activity_iso": "2026-09-02T22:30:23.062552+00:00",
  "down_since": null,
  "wake_source": "prci",
  "reason": "autodown: reconciled to reality — stalled wake (wake process died) but layer is UP despite state:waking",
  "watchdog_blocked": false,
  "watchdog_intentional": null,
  "kanban_ok": true,
  "kanban_reason": "",
  "blocked_by": "open PR / active CI run on a tracked repo, and kanban work on board 'hscc': t_3359b983 (...), t_4cedf275 (...), t_6060f92b (...), … and 9 more",
  "force_armed": false,
  "force_armed_overrides": [],
  "active_cron_cpu_only": ["hscc-dep-watcher", "hscc-escalate-watcher"],
  "active_cron_model": [],
  "speak": "Autodown armed, idle limit 120 minutes, status up. Blocked by ... prci."
}
```

(work in progress — findings below being appended)
