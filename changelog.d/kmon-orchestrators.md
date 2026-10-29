kind: Added
task: kmon-orchestrators

- **`hscc kanban monitor` shows the running orchestrators and fleet-wide
  token use.** A new "orchestrators (N running)" section lists every live
  `hermes -p <profile> chat` REPL with its session title, last activity and
  lifetime tokens (kanban workers and pane shell wrappers are skipped). The
  header now shows 24 h and 7 d totals for workers + orchestrators with the
  split. Worker and cron sessions are windowed by `started_at`; long-lived
  REPL sessions only store lifetime totals, so their window use comes from a
  small ledger the monitor keeps (`~/.hscc/state/kmon_orch_tokens.json`,
  5-min samples, 8 days; a deleted session never makes a window negative),
  shown as "history since …" until it covers 7 days. `--json` carries
  `tokens` and `orchestrators`; `tokens_24h` is unchanged.
