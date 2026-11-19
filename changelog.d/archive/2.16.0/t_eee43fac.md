kind: Added
task: t_eee43fac

* `hscc cluster history` prints what the history route serves, from a terminal:
  `hscc cluster history [--window 15m|1h|6h|24h|7d] [--tier 0|1|2 [--since TS]
  [--until TS]] [--node <host|all>] [--metrics a,b] [--json]`. Before this the
  stored series had no CLI verb at all, so an operator looking at a trend that
  seemed wrong had no way to print the rows it was drawn from. The flag surface
  is `GET /v1/cluster/history`'s, decided in t_447cc7fc: `--window` and `--tier`
  are mutually exclusive, `--since`/`--until` need `--tier`, and neither axis
  defaults differently from the route. `--host` does not exist — `--node` is the
  one machine filter, because `host_rollup.host` and `engine_rollup.node` are two
  column names for one machine.
* The verb reads the recorder's store directly (`host_rows` / `engine_rows` /
  `tier0_points`) — never over HTTP, never through `metrics_history.get_sampler()`,
  which constructs a Sampler and would spawn a second `sparkrun cluster monitor`
  and a second roll-up watermark writer. It dispatches before the cluster engine
  loads, so reading history that is already on disk needs neither sparkrun nor the
  engine. `hscc-api/tests/test_cluster_history_cli_parity.py` drives the route and
  the CLI on one fixture state and compares them row for row.
* A human table of temperature or power shows the peak beside the average it is
  the peak of (`gpu_temp_c` then `gpu_temp_c_max`, named by the store's own
  `MAX_KEYS`), field names are sparkrun's `MonitorSample` names verbatim, and a
  value a node never reported prints as `—`, never as `0` — `--json` keeps `null`.
  Emptiness is stated rather than implied: `--tier 0` from a CLI process says the
  2 s ring lives in the sampling process, and a recorder that has not written its
  store yet says so, instead of printing a zero-row table that reads as an idle fleet.

---
kind: Docs
task: t_eee43fac

* The `cluster` group help lists `history` on the same 80-column budget the
  group-help gate enforces, its usage block prints the two flag axes, and the
  README "Common tasks" table gains the row that points an operator at it:
  `look back at the stored series, not just the present`.
