# False-fail health streams (DGX / gateway / NAS) — root-cause & evidence report

Task t_3fe0cd05. Branch wt/false-fail-streams (off dev). Worktree
/Users/desac/dev/hscc/.worktrees/t_3fe0cd05.

Observed 2026-09-09: dgx FAIL, gateway FAIL (ok=False job=True vllm=False
mux=True "all 40 multiplex profile(s) served"), nas FAIL ("NAS probe timed out
after 5s — stale/wedged mount?"), watchdog permanently BLOCKED
("3 consecutive failures in 10min: DGX=FAIL GW=FAIL", Restarts: 122).

Each stream investigated with the leading hypothesis "the check is wrong"
(since the card believed the cluster healthy). Verdict per stream below.

=======================================================================
1. DGX  — CHECK WAS WRONG (fixed)
=======================================================================
Root cause: sparkrun 0.3.6 REMOVED `query_cluster_status`.
hscc_daemon/health.py's _SPARKRUN_STATUS_SCRIPT still imported it:
    from sparkrun.core.cluster_manager import ClusterManager, query_cluster_status
Reproduced under sparkrun's own interpreter (/tmp/repro_status_old.py):
    ImportError: cannot import name 'query_cluster_status' from
    'sparkrun.core.cluster_manager' (/Users/desac/sparkrun/src/sparkrun/core/cluster_manager.py)
So the structured path ALWAYS raised, the check fell back to text-parsing
`sparkrun status`, and DGX reported ok=False regardless of the operator's
(real, separate) PATH fix. The daemon log confirmed the structured path was
failing every tick:
    [WARN] structured sparkrun status unavailable — falling back to
    `sparkrun status` text-parse (venv_py='/Users/desac/sparkrun/.venv-sparkrun-py313/bin/python')

Fix (committed 8a41036): ported _SPARKRUN_STATUS_SCRIPT to the replacement API
(verified live under sparkrun's interpreter, /tmp/repro_status_new.py):
    snapshot = api.status(hosts=hosts)
    result = classify_cluster_status(snapshot, cache_dir=..., host_list=hosts)
    print(json.dumps(result.to_dict()))
Live output: total_containers=6, errors={}, idle_hosts=[], host_count=4 —
cluster healthy. _workloads_from_cluster_status reads group/entry["meta"],
present in the new to_dict(); unchanged.

No host/IP hardcoded — the script resolves hosts via
resolve_hosts(None, None, None, mgr, config.default_hosts), preserving the
old behaviour for an empty host list (prints {"host_list": []}, exit 0).

=======================================================================
2. GATEWAY vllm=False  — CHECK WAS CORRECT (real problem; auto-heal broken)
=======================================================================
What the vllm sub-probe tests (hscc_daemon/health.py:411):
    vllm = http_check(serving.VLLM_HEALTH_URL, timeout=5)
serving.VLLM_HEALTH_URL = http://{PRIMARY_NODE}:{VLLM_PORT}/health
(serving.py) — a GET to the vLLM engine's /health endpoint.

Evidence the endpoint is CORRECT (not a wrong-endpoint false positive):
- vLLM serves /health; when the engine is up it answers 200. Current daemon
  state (post-recovery) proves it: ~/.hscc/state/gateway.json
      "ok": true, "vllm_healthy": true, "multiplex_ok": true
  where vllm_healthy=true is produced by http_check(/health) succeeding.
  /health and /v1/models are served by the same engine and both require
  engine readiness — /health answering 200 now, and only now, confirms the
  probe targets a real liveness endpoint.

Evidence vllm=False on 09-09 was a GENUINE backend-down, not a probe bug:
The watchdog tried to auto-restart vLLM repeatedly and every attempt failed
for ~9.5 hours (auto-restart #19..#58+ across 04:06–13:39) with the SAME error:
    [WARN] vLLM stop failed: Command failed: [Errno 2] No such file or
    directory: 'sparkrun'
(rotated log daemon.log.1.gz, e.g. lines 3868501-3868504, 3868812-3868815,
3869124-3869127, ...). The daemon's launchd PATH lacked ~/.local/bin (where
sparkrun lives), so the auto-heal could not restart the backend. vLLM stayed
down for hours BECAUSE the auto-heal was broken — the check was correctly
reporting a real outage the whole time.

Why "all 40 multiplex profile(s) served" looked healthy but was not
corroboration: _check_multiplex_profiles (health.py:336-404) compares the
config.yaml multiplex_profiles roster against the gateway's OWN
gateway_state.json served_profiles list. It does NOT probe the network at
all — it reflects the gateway supervisor's belief, which is stale during a
backend outage. It is not independent evidence the backend answers; the card's
"/v1/models 200" observation during the window is not corroborated by any
independent probe in the logs I could find, and is inconsistent with a
backend that could not be restarted for 9.5h.

Conclusion: gateway check is correct. The real problem on 09-09 was a vLLM
backend outage that the watchdog could NOT recover because `sparkrun` was not
on the daemon's PATH (same broken-PATH root cause as the DGX fallback). The
operator's plist PATH fix + manual TP-pair relaunch is what recovered it.
The check now correctly reports healthy. No code change made.

=======================================================================
3. NAS  — CHECK WAS CORRECT (real, intermittent NFS readdir stall)
=======================================================================
Determined whether the probe is heavier than a stat, or 5s too tight.

The probe (_probe_mount, health.py:923-960) does more than a stat: after the
stat/statvfs (disk_usage) checks it enumerates a subdirectory:
    hf_items = [d for d in os.listdir(hf_dir) ...]   (health.py:956)
where hf_dir = /Volumes/NAS/huggingface. That readdir is run inside the
timeout-bound daemon thread (_run_bounded, health.py:880) with
NAS_PROBE_TIMEOUT=5s (health.py:47).

Evidence it is NOT "5s too tight" — the readdir genuinely never returns when
it stalls. Repeated bounded probes from a cold process (8/8 iterations,
/tmp/probe_nas_repeat.py, p313 python):
    st_dev=os.stat_result(... st_dev=436207697 ...)   # stat returns instantly
    hf=True                                          # isdir returns instantly
    hf_items=EXC InterruptedError                    # listdir blocked >5s, thread abandoned
Every iteration: stat/statvfs/isdir all <ms; os.listdir(huggingface) blocked
the full 5s bound and the abandoned thread was interrupted. Direct shell
checks also hang:
    gtimeout 8 ls /Volumes/NAS            -> exit 124 (root readdir hangs)
    gtimeout 6 ls /Volumes/NAS/huggingface -> exit 124
    gtimeout 6 ls /Volumes/NAS/hub         -> exit 124
while stat/statvfs on the same paths return instantly.

Mount parameters explain the hang (nfsstat -m): NFSv4.0 over tcp,
    hard,nointr,timeo=10,acdirmax=60
`hard` + `timeo=10` (100s retransmit) means a READDIR the server never answers
is retried forever — no readdir timeout short enough would "fix" a slow read;
when it stalls it never returns.

Intermittent at the server side: the daemon's own NAS checks (long-running
process, warm NFS client) logged ok=True at 04:14:42 and 04:29:42 — same
readdir completed <5s those ticks — while cold processes hit the stall ~every
time. acdirmax=60 rules out the daemon being served purely from client cache
across the 15-min gap, so the server is intermittently answering readdir.
The 5s bound is correct: it catches the stall when it happens and never hangs
the daemon, exactly as designed (health.py:739-749).

Conclusion: NAS check is correct; it is reporting a REAL, intermittently
stalling NFS readdir on /Volumes/NAS (NFS export at a LAN node — path :/models,
nfs, NFSv4.0, hard mount). This is an infrastructure problem (NFS server /
export health), not a check bug. 5s timeout NOT raised — raising it would only
delay detection of a stall that never resolves.

=======================================================================
Full-suite result
=======================================================================
HSCC_TEST_PY=/Users/desac/miniconda3/envs/p313/bin/python bash scripts/run_tests.sh
  ✓ hscc-bootstrap
  ✓ hscc-commands
  ✗ hscc-roles (pytest exit 1)   <-- PRE-EXISTING, see below
  ✓ hscc-cluster
  ✓ hscc-project
  ✓ hscc_daemon
  ✓ sparkrun-hermes
  ✓ hscc-api
hscc_daemon/tests/ alone: 1053 passed.

The sole failure is hscc-roles/tests/test_generator.py::test_every_hscc_owned_
worker_profile_has_role_spec: FileNotFoundError:
/Users/desac/.hermes/profiles/devops-engineer/profiles — a missing profiles
dir in the devops-engineer profile. PRE-EXISTING: identical failure in the
prior full-suite run /tmp/full_tests2.log (Sep 9, before this branch's commit).
Unrelated to the health checks; my commit touches only hscc_daemon/health.py.

=======================================================================
What I did NOT do (and why)
=======================================================================
- Did NOT raise the NAS timeout: the stall is a hard readdir hang, not a slow
  read — raising it would only delay detection. Real infra issue, documented.
- Did NOT change the gateway check: it correctly reports a real backend
  outage. The fix was the operator's plist PATH change (already done) + the
  DGX structured-status port (which restores reliable workload detection and
  powers the auto-heal).
- Did NOT fix `hscc cluster stop <container_id>` silent no-op (recorded in the
  comment thread as a second bug in the same area): out of scope for this
  card's three health streams. Recorded here so it is not lost; recommend a
  follow-up card.
- Did NOT run hscc doctor / bootstrap.sh against live runtime, did not use
  Telegram, did not mutate live state in any test.
- No host/IP hardcoded; test_no_real_addresses_committed.py passes.

## Commits (branch wt/false-fail-streams, off dev)
- 8a41036 deps: port sparkrun status check off removed query_cluster_status (t_3fe0cd05)
(this audit report is a second commit on the same branch)
