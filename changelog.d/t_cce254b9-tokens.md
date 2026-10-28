kind: Added
task: t_cce254b9

- **Token counts in both monitors.** `hscc cluster monitor` gains `In` / `Out`
  columns (cumulative prompt / generated tokens per vLLM engine since it
  started, from `vllm:prompt_tokens_total` / `vllm:generation_tokens_total`)
  at every width, summed in the per-model total row. `hscc kanban monitor`
  shows each running card's worker-session tokens (`in/out`, matched by worker
  profile + run claim time against the profile's `state.db`
  `source='kanban'` session) and a 24 h total over every profile's kanban
  sessions; `--json` carries `tok_in` / `tok_out` / `tokens_24h`. Read-only,
  databases opened `mode=ro` and closed every pass.
---
kind: Verified

- `test_kanban_live.py` +3 (session match picks the run's own session and
  ignores non-kanban / >24 h rows, missing state.db renders `-`, render
  format), `test_cluster_live.py` +3 (poller carries counters, narrow table
  shows summed In/Out, `fmt_tokens`); tests never read the real `~/.hermes`.
