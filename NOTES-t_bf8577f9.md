# t_bf8577f9 — F5: `/cluster-down` slash vs the `hscc cluster down` engine

Worktree: `wt/t_bf8577f9` @ `origin/main` `cedfeb93`.
All addresses below are REDACTED to `192.0.2.x` (TEST-NET-1); the live fleet hosts
never leave this machine.

---

## STEP 1 — the dry-run side-by-side (safety gate, read-only)

**Nothing here stopped, started or reconfigured anything.** The measurement ran in
one process with:

* a **fake `sparkrun`** on `PATH[0]`, and a **tripwire** around `subprocess.run`
  that RAISES on any argv not resolving (realpath) to that exact fake;
* `hscc_daemon.post_boot.clear_expected_up` **replaced by a recorder**, with the
  real `~/.hscc/expected-up.json` stat'd before and after (unchanged, printed
  below);
* the engine driven only through `--dry-run` and fake-CLI paths.

Probe: `probe_step1.py` (scratch), log `step1.log`. Verbatim output:

```
==============================================================================
SAFETY PROOF
==============================================================================
PATH[0]                    : <scratch>/fakebin
which sparkrun             : <scratch>/fakebin/sparkrun
realpath(which) == fake    : True
real CLI realpath          : absent  (NOT on PATH[0])
subprocess.run             : tripwired to the fake sparkrun only
post_boot.clear_expected_up: STUBBED (recorder, no disk write)
marker before              : size=1161 mtime_ns=1794210032780252155
engine module              : <wt>/hscc-cluster/hscc.py
slash module               : <wt>/hscc-commands/__init__.py

==============================================================================
A. ENGINE — `hscc cluster down --dry-run`  (cmd_cluster_down(dry_run=True))
==============================================================================
[stdout]
  | Will run: sparkrun stop --all --cluster hscc
  |   --dry-run: not executing
[returned dict]
  {"dry_run": true, "command": ["sparkrun", "stop", "--all", "--cluster", "hscc"], "issued": []}
[sparkrun calls issued     : NONE]
[marker writes             : NONE]

==============================================================================
B. SLASH — `/cluster-down` plan path  (cmd_cluster_down('') — unconfirmed)
==============================================================================
[returned text]
  | ⚠️ *Confirm cluster down*
  |
  |   • orchestrator 192.0.2.1
  |   • worker 192.0.2.3
  |   • worker 192.0.2.4
  |   • worker 192.0.2.2
  |
  | Stops all vLLM units (sparkrun stop --all) on each node, parallel. Hosts stay up. Workflow: /cluster-down → /cluster-docker-prune → /cluster-restart.
  | Run `/cluster-down confirm` to execute.
[stdout                      : (none)]
[sparkrun calls issued       : NONE]
[marker writes               : NONE]

==============================================================================
C. WHAT EACH PATH ACTUALLY ISSUES (fake sparkrun; nothing real execs)
==============================================================================
--- engine (`hscc cluster down`) ---
  | Will run: sparkrun stop --all --cluster hscc
  | fleet marked deliberately down — the daemon will not relaunch workers, the proxy or the notifier until `hscc cluster up`
  [dict] {"success": true, "returncode": 0}
  [sparkrun argv issued] ['stop --all --cluster hscc']
  [marker written]       ['hscc cluster down']
--- slash  (`/cluster-down confirm`) ---
  [sparkrun argv issued] ['stop --all --hosts 192.0.2.1', 'stop --all --hosts 192.0.2.3',
                          'stop --all --hosts 192.0.2.4', 'stop --all --hosts 192.0.2.2']
  [marker written]       ['/cluster-down']

==============================================================================
D. IDENTICAL sparkrun OUTPUT → does each surface reach the SAME verdict?
==============================================================================

### fake sparkrun mode = clean            (rc 0, "Stopped 4 job(s)…")
[engine] dict            : success=True  rc=0  benign_no_workloads=None  benign_note=None
[slash ] lines           : ✅ 192.0.2.1: stopped / ✅ … / (stay-down line) / "All units stopped."

### fake sparkrun mode = already          (rc 1, per-host JobNotFound, no real failure)
[engine] dict            : success=False rc=1 benign_no_workloads=True
                          benign_note='nothing to stop — the fleet was already down (sparkrun exited nonzero on an already-down host)'
[slash ] lines           : ○ 192.0.2.x: nothing running ×4  + stay-down + "All units stopped."

### fake sparkrun mode = mixed            (rc 1, JobNotFound AND `could not query …: ssh: Permission denied`)
[engine] dict            : success=False rc=1 benign_no_workloads=None benign_note=None   → renders ERROR
[slash ] lines           : ○ 192.0.2.x: nothing running ×4  + stay-down + "All units stopped."   ← HIDES the failure

### fake sparkrun mode = none             (rc 0, "No sparkrun containers running.")
[engine] dict            : success=True  rc=0  benign_no_workloads=None
[slash ] lines           : ○ 192.0.2.x: nothing running ×4  + "All units stopped."

==============================================================================
SAFETY PROOF (after)
==============================================================================
marker after               : size=1161 mtime_ns=1794210032780252155
marker UNCHANGED           : True
marker writes attempted    : 9 recorder hits, 0 disk writes (stubbed)
```

### The diff, per the card's axes (hosts / models / marker)

| Axis | engine `hscc cluster down` | slash `/cluster-down` | Same? |
|---|---|---|---|
| stop command | ONE `sparkrun stop --all --cluster hscc` from `serving.fleet_down_cmd()` (`hscc_daemon/serving.py:496`) | FOUR `sparkrun stop --all --hosts <node>`, **no `--cluster`** (`hscc-commands/__init__.py:512-514`) | **NO** |
| host set | sparkrun's `hscc` cluster (everything in it) | `serving.json` unit nodes only (`cmdlib.read_units`) | **NO** — a workload on a node absent from serving.json is stopped by the engine and MISSED by the slash, which then prints "All units stopped." |
| timeout | `_fleet_op_timeout_seconds()` (generous) | 90 s per node | **NO** |
| plan surface | `--dry-run` prints the exact argv, touches nothing | no dry-run at all; the confirm preview never names the command | **NO** |
| models | not named | not named | same (neither reports them) |
| post-boot marker | `clear_expected_up("hscc cluster down")` | `clear_expected_up("/cluster-down")` | **YES — the audit's "misses the marker clear" is STALE**: both clear it. Landed in `4dc5599d` (t_3a0bdca9 / PR #256), released in 2.15.6, i.e. *before* the 2026-11-23 audit; the auditor read `:467-510` (the stop loop) and not the fail-soft claim at `:520`. Reason TEXT differs, and `verify.py:437-444` documents that the text is never tested, so that is provenance, not a safety property. |
| benign classification | `_benign_no_workloads_note` = already-down markers **AND** a `_REAL_FAILURE_MARKERS` denylist (`hscc-cluster/hscc.py:531-582`) | `_was_already_down` = markers only, **no denylist** (`hscc-commands/__init__.py:440-461`) | **NO — measured above** (`mode = mixed`): the engine renders ERROR, the slash renders `○ nothing running` ×4 + "All units stopped." |

### Verdict, and what it forces

The card's rule: the standalone path may stay **only if** the plan comparison shows
the *same verdict*; otherwise delegate to `cmd_cluster_down(dry_run=…)`.
Mode `mixed` is a same-input / different-verdict pair, and the host set differs
structurally. **The slash must delegate to the engine.** The "works when the LLM is
wedged" constraint is not a reason to keep the loop: the engine is an in-process
Python call whose only external dependency is `sparkrun` on PATH — no LLM, no
gateway round-trip. What delegation *does* cost is the slash's own per-node emoji
rendering, which is replaced by sparkrun's own per-host output (the tool's words,
not a second interpretation of them).

---

## STEP 2 — the fix (decision, written before the code)

1. **Engine** (`hscc-cluster/hscc.py`): `cmd_cluster_down(dry_run=False,
   reason="hscc cluster down")` — a keyword-with-default so every existing caller
   and the marker wording stay byte-identical. The result dict gains the ADDITIVE
   `stay_down_marker` key (`{"cleared", "reason", "error"}`), the same pattern as
   `benign_no_workloads` / `benign_note`: `success` keeps meaning "sparkrun exited 0".
2. **Slash** (`hscc-commands/__init__.py`): delete the second implementation
   (per-node `sparkrun stop` loop, `_ALREADY_DOWN_MARKERS`, `_was_already_down`,
   `_claim_stay_down`, `_post_boot_module`) and delegate through the EXISTING
   sanctioned seam `cmdlib._import_cluster_module("hscc")` (cmdlib.py:166). Confirm
   preview prints the exact fleet command from `serving.fleet_down_cmd()`; a real
   run renders the engine's verdict, sparkrun's own output and the marker outcome.
   If the engine module cannot be loaded the verb **refuses** and names
   `hscc cluster down` — it never silently falls back to a different fleet-down.
3. Tests: 192.0.2.x fakes + fake sparkrun on PATH (the suite's autouse fence stays
   armed); pins for delegation-argv, no-self-stop, verdict parity, refusal.
4. `hscc-commands/README.md`: the audit's S2 sentence, updated to the new fact.
5. Changelog fragment `changelog.d/t_bf8577f9.md`.
