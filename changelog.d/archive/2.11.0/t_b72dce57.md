kind: Fixed
task: t_b72dce57

- **PipelineWatchdog's vLLM auto-restart could race an in-flight heal and
  launch a hollow duplicate of a cluster-up-owned unit.** The
  2026-10-18 01:38 UTC incident (redacted timeline kept as a test fixture):
  engine-wedge recovery held the heal lease while it stopped `orch` and
  relaunched it via `hscc cluster up`; the proxy stream honoured the lease
  and deferred at 01:38:34; 10s later the watchdog ignored the same lease,
  issued a target-less stop (sparkrun: "Error: Must specify TARGET or
  --all" — a no-op the code logged and continued past), and launched a
  SECOND job from the bare recipe whose vllm never started (:8000 was
  already held) — then logged `#270: success`. The hollow container sat
  ~7h. Four gates now hold: (1) `pipeline_watchdog` consults
  `heal_lease.read_lease()` BEFORE any attempt (the same deferral machinery
  the proxy WARN used — no second lock) and does nothing but log + state the
  deferral while held; `restart_vllm` re-checks for its other caller
  (trigger.py); (2) a (node,port) registered in serving.json is
  `hscc cluster up`'s exclusive launch surface — a watchdog-initiated
  restart there is REFUSED with a WARN naming the unit; (3) the stop is
  `sparkrun stop <recipe> --hosts <node>` (new `serving.vllm_stop_cmd`;
  `--all` is never a fallback; no resolvable TARGET ⇒ abort without ever
  issuing the broken argv) and a failed stop ABORTS before any launch;
  (4) `ok` requires the launched unit to actually serve — the engine-wedge
  streaming probe (`health._probe_unit_generation`) gates the lifetime
  counter, the `#N: success` log and the load-grace window, and a streak of
  unverified launches (`WATCHDOG_VERIFY_ALARM_AFTER`) escalates a critical
  NOT-SERVING notification. The stop→verify window itself runs under the
  lease as `vllm-auto-restart` so the other repair paths back off it.
---
kind: Verified
order: 1

- `hscc_daemon/tests/test_lifecycle_watchdog_race_20261018.py`: 19 cases,
  seeded from `fixtures/vllm_auto_restart_race_20261018.log` (the real
  01:36:25–01:39:10 UTC window, verbatim timestamps, LAN hosts redacted to
  `10.0.0.x`). The replay is RED on pre-fix code at the behavioral
  assertion (`restart_calls == [1]` — old code restarted despite the held
  lease) and green on the fix; nine mutation probes (lease gate at both
  layers, stop-abort, registry refusal, rc=0-as-success, counter gating,
  alarm branch, lease-around-mutating-window, target-less stop fallback)
  each turn at least one test red. Negative controls keep ordinary healing
  alive: unregistered target still restarts, lease-free tick still attempts.
