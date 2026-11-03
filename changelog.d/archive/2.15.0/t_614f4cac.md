kind: Fixed
task: t_614f4cac

- **A merely SLOW node can no longer trip `FLEET VISIBILITY LOST`.** The daemon's
  caps on the sparkrun status shell-out sat *inside* the operation they wrapped, so
  the alarm fired with no fault anywhere — the Mac idle, the network fine, sparkrun
  behaving exactly as configured. `api.status` → `query_status_for_cluster` walks the
  host-scope executors **sequentially** (docker, then local — `orchestration/executor.py:170`)
  and each round fans out with `timeout=ssh_kwargs.get("timeout", 15)`; because
  `build_ssh_kwargs` emits no `timeout` key, the sweep's own worst case was
  2 rounds × 15 s = **30 s against a 25 s daemon cap**, and the legacy
  `sparkrun status` text-parse leg ran under a **10 s** cap — below a *single* round.
  Both legs were observed spending their whole budget (structured 9/14 ≥ 22.5 s,
  max 157.85 s; legacy 13/13 ≥ 9 s) while a healthy identical sweep measures 1.23 s.
  The fix is one rule — **the cap must sit above the operation it wraps** — applied in
  both halves: the structured script now passes `ssh_kwargs["timeout"]` so the sweep
  self-limits to 10 s/host (worst case 20 s, the same thing sparkrun's own CLI does
  for its completion sweep), and every sweep call site — both status legs in
  `health.py` (DGX check, worker-created map, launch-guard status) plus
  `recover.py`'s identity fetch — shares one named cap,
  `health.SPARKRUN_STATUS_TIMEOUT` = **40 s**, which clears even the legacy leg's
  30 s (that CLI exposes no per-host timeout flag) plus interpreter start-up.
  10 s/host is ~8× the measured healthy per-host cost and equals sparkrun's own SSH
  `ConnectTimeout`, so a host that cannot connect inside it is unreachable, not slow.
  Tick cadence is unchanged and no headroom is paid in the normal case (healthy
  sweep 1.5 s). Env overrides `HSCC_SPARKRUN_STATUS_TIMEOUT` /
  `HSCC_SPARKRUN_SWEEP_HOST_TIMEOUT` for operators; the last-known-good fleet
  visibility signal is deliberately NOT here (it is the parented follow-up).
kind: Verified
task: t_614f4cac

- `hscc_daemon/tests/test_sparkrun_resolution.py`: 28 → **35** cases. New
  `TestSweepBudgetAlignment` drives the **real** `util.run_cmd` timeout branch — a
  `subprocess.run` stand-in raises `TimeoutExpired` exactly when the caller's cap is
  below what the sweep needs — so the acceptance case is decided by the code path, not
  by a constant: a **20 s sweep (a merely slow node) yields `ok=True` and
  `fleet_visibility: "ok"` on BOTH legs**, with no `VISIBILITY` WARN. A **negative
  control** puts the old 10 s cap back and asserts the same slow sweep *does* publish
  `degraded` + the alarm, so the suite cannot go green on a reverted constant. Also
  pinned: the budget arithmetic (`SPARKRUN_STATUS_TIMEOUT > rounds × per-host timeout`,
  with the concrete 10 s/40 s and the sequential-two-round premise that makes it
  2), the `ssh_kwargs['timeout']` self-limit inside the script, that the script
  (now an f-string) still **compiles** — a dropped `\n` would otherwise surface only
  as an every-tick `ImportError` at daemon runtime — and an `ast`-based guard that no
  sweep call site re-hardcodes a numeric cap, which is exactly how 25 s and 10 s
  drifted inside the sweep's budget.
- `bash scripts/run_tests.sh` — one pytest process per plugin dir, all green;
  `python3 scripts/address_guard.py --tracked` clean;
  `python3 scripts/changelog_fragments.py check` clean.
