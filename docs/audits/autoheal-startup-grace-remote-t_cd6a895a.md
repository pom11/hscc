# Auto-heal grace is inert — resolve container age through sparkrun (implementation report)

Task t_cd6a895a. Branch wt/autoheal-remote-docker.

## The bug (confirmed)
health.py:1090 `_default_worker_container_created_ts(node, port)` shells to
`docker ps` on the DAEMON HOST (the Mac Studio). The workload containers run on
the DGX nodes (reached over SSH via sparkrun; live fleet hosts redacted to
`10.0.0.x`). The Mac has no such container, so the command returns nothing →
`None` → health.py:1151 `if created_ts is None: return False` (fail-open) →
grace skipped → auto-heal proceeds → force-recreate loop. The
~/.hscc/worker_container_state.json contains only null created_ts entries; the
`(10.0.0.2,8000)` / `(10.0.0.3,8000)` keys are leftover test placeholders.

## Operator directive: use SPARKRUN, never raw docker/ssh
The operator superseded the card's "reuse SSH path" instruction. HSCC talks to
the fleet through sparkrun, which owns host resolution + ssh kwargs. So the fix
must resolve container age from sparkrun's structured status, not docker/ssh.

## Investigation: does sparkrun's status carry a container creation time? (SHOWN, real to_dict())
I inspected the REAL sparkrun status surface (see below). Two findings:

### Finding 1 — health.py's `_SPARKRUN_STATUS_SCRIPT` is BROKEN (this is t_3fe0cd05)
The script (health.py:113-127) imports
`from sparkrun.core.cluster_manager import ClusterManager, query_cluster_status`
but **`query_cluster_status` no longer exists** in sparkrun. Verified live — running
the script under sparkrun's venv python raises:
    ImportError: cannot import name 'query_cluster_status' from
    'sparkrun.core.cluster_manager'
The function was refactored away. The current API is `sparkrun.api.status(hosts,
ssh_kwargs=...)` returning a `ClusterStatus` snapshot, shaped via
`sparkrun.core.cluster_manager.classify_cluster_status(snapshot, cache_dir=...,
host_list=...)` → `ClusterStatusResult.to_dict()`.
This is why the DGX check reports ok=False (t_3fe0cd05): the structured call
fails and it falls back to the text-parse path. Per the operator directive I
record the root cause on t_3fe0cd05, but fixing the script is REQUIRED for this
task anyway (the grace must read the corrected status), so it lands here.

### Finding 2 — per-container creation time is NOT exposed; launch epoch IS
Real `ClusterStatusResult.to_dict()` (sanitized, live fleet, host ids redacted
to placeholders):
    groups: {
      "<cluster_id>": {
        "meta": { ..., "port": 8000, "started_at": 1788961530.72, ... },
        "containers": [ {"host": "10.0.0.x", "role": "node_0",
                         "status": "Up 14 hours", "image": "..."} ]
      }, ...
    }
    solo_entries: [ { "cluster_id": "...", "meta": {..., "port": 8000,
                      "started_at": 1788961729.52, ...},
                      "host": "10.0.0.x", "status": "Up 14 hours", ...} ]
    errors: {}, idle_hosts: [], total_containers: 5, host_count: 4

Concretely:
- `RunningWorkload.started_at` (the field meant to carry a start epoch) is
  **None for every docker container** — confirmed live: all 5 containers show
  started_at=None even though status is "Up 14 hours". The docker executor
  (`orchestration/executors/docker.py:870`) builds `ContainerDetail(name, role,
  status, image)` and never populates started_at. `ContainerDetail` has NO
  created/start field (dict keys: name, role, status, image, executor).
- BUT `groups[*].meta.started_at` and `solo_entries[*].meta.started_at` DO carry
  a launch epoch (epoch-seconds float). It comes from sparkrun's persisted job
  metadata (`orchestration/job_metadata.py:536`, written by `save_job_metadata`
  on every real launch — called from `core/launcher.py:935/1066/1383`). A
  force-recreate (`sparkrun stop --all` + `sparkrun run ... --ensure`)
  re-launches the cluster_id, so `save_job_metadata` REWRITES `started_at =
  time.time()`. Verified in the live dump: the two groups and solo carry distinct
  fresh launch epochs.

Conclusion per the directive (step 2): the "launch epoch" signal IS present and
IS refreshed on recreate, so I key the startup grace on `meta.started_at`
(groups[*].meta / solo_entries[*].meta), mapped to the health key (node, port)
via `containers[*].host` / `solo_entries[*].host` + `meta.port`. No second
transport. The per-container docker `CreatedAt` (true container creation time)
is NOT exposed — noted as an optional sparkrun-side follow-up, not needed here.

## Design decision: fail-CLOSED (bounded), not fail-open
I am changing the fail direction. Today fail-open means "when we cannot tell the
container age, DESTROY a possibly-loading unit". Given a force-recreate costs a
full multi-minute model reload and can loop (the exact incident), I fail CLOSED:
when the age is unknown, I ASSUME the unit is still loading and shield it for the
grace window, so a genuinely dead unit still heals once the window elapses.

Mechanics (durable, across restart): if neither the live sparkrun age nor a
stored created_ts is available, store `created_ts = now_wall` on first sight of
the unknown-age down unit and treat it as a fresh container. The normal grace
logic (`now_wall - created_ts < GRACE`) then shields it for GRACE (default 1200s
= 20 min) across restarts, then expires → heals. If /health comes back OK within
the window, `_record_container_served` marks has_served=True and the unit is
exempt thereafter. A unit that HAS served and then stops is exempt (heals
promptly) — preserved.

Rationale for fail-closed here:
- Cost asymmetry: false-negative (suppress when actually dead) costs at most the
  bounded grace window of downtime, after which it heals anyway. False-positive
  (heal when actually loading) costs a multi-minute reload AND can loop —
  strictly worse, and it is the bug this card exists to kill.
- The unit is already DOWN and has crossed the debounce; suppressing the
  aggressive force-recreate for a bounded window is a safe backstop, not a
  suppression of all healing.
- Bounded by the max suppression window so a genuinely dead unit still recovers.

Exception documented: if a unit HAS a stored served/old created_ts from before
(i.e. sparkrun is temporarily down but we have durable prior knowledge), we use
the stored value rather than assuming fresh — so an established unit with a
stale-but-known age still heals while only a genuinely-unattributable unit gets
the fresh-assumption shield.

## What changed (hscc_daemon/health.py)
1. Corrected `_SPARKRUN_STATUS_SCRIPT` to the current API
   (`sparkrun.api.status` + `classify_cluster_status` → to_dict). Same output
   shape, so `_workloads_from_cluster_status` (the DGX check) keeps working —
   this also fixes t_3fe0cd05's structured-call failure (recorded there).
2. `_default_worker_container_created_ts(node, port)` now resolves via sparkrun:
   builds a `(host, port) -> started_at` map from the corrected to_dict()
   (groups[*].containers[*].host + meta.port → meta.started_at; solo_entries
   likewise) and looks up (node, port). Refreshed with a short TTL cache so the
   down-threshold path doesn't launch a fresh ~25s sweep per unit per check.
   Best-effort with the existing short timeout — a slow/unreachable node never
   stalls the health cycle.
3. Fail-CLOSED in `_suppress_autoheal_for_startup_grace`: unknown live age +
   no stored created_ts → store created_ts=now_wall (assume fresh) and shield
   for GRACE. Bounded + durable across restart.
4. Migrated stale null created_ts entries: on load, entries with
   created_ts=None are dropped/re-derived (they cannot mask a real reading).

## Makes the stopgap removable
HSCC_WORKER_AUTOHEAL_DEBOUNCE=15 / COOLDOWN=30 in the launchd plist was a stopgap
because the shipped grace is inert. With the grace now keyed on real
sparkrun-provided launch age and fail-closed-bounded, the debounce/cooldown
stopgap is no longer needed and can be removed — the startup shield does not
depend on the debounce value.

## Tests
Fake remote-exec seam (module already injects `_worker_container_created_ts`):
- a young never-served REMOTE container (sparkrun-derived started_at young,
  has_served=False) suppresses auto-heal;
- that holds ACROSS a daemon restart (durable created_ts persisted).
No force-recreate against the live fleet (heal action is stubbed as before).

## Evidence / artifacts
- Sanitized live to_dict() dump (see above).
- /tmp/inspect_sparkrun_status.py (corrected status query demo).
- worker_container_state.json before: null created_ts + test placeholders.
