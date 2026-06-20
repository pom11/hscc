---
name: data-engineer
description: Data engineering patterns for sphoin dataset generation, including memory-safe batch processing, process dedup guards, and EURJPY crash handling.
version: 2.0.0
tags: [data-engineering, sphoin, dataset-generation, memory-safety]
---

# Data Engineer — Skill (v2.0)

> **CRITICAL: All three rules below are mandatory. Violation = OOM panic risk.**

## 🔒 RULE 1: Memory Estimates — Hard-Coded Table (NEVER Override)

These are **measured peak RSS** values. Workers MUST use these in `--mem-est-gb`:

```python
MEM_ESTIMATE_GB = {
    ('stocks', '1d'):  25,  # measured peak: ~3GB raw but parquet write phase spikes to ~7GB
    ('stocks', '1h'):  25,  # measured peak: ~7GB
    ('forex',  '4h'):  30,  # measured peak: 24-26GB (highest — use alone)
    ('forex',  '1h'):  30,  # estimate, untested — use alone
    ('crypto', '1h'):  10,  # measured peak: ~9GB
    ('crypto', '4h'):  10,  # estimate, untested
}
```

**Before running ANY `generate_dataset_v2.py`:**
1. Read `--family` and `--timeframe` from the command
2. Look up the estimate in the table above
3. Use `--mem-est-gb` EXACTLY matching the table value
4. **NEVER use `--mem-est-gb 2` for forex_4h** (proven wrong: actual 24-26GB)
5. **NEVER use `--no-mem-gate`** (banned — see RULE 3)

**When in doubt:** Use the higher estimate. The gate will serialize execution rather than let processes compete for RAM.

## 🔒 RULE 2: Process Dedup — pgrep Check (ALWAYS)

**Before spawning any `generate_dataset_v2.py` subprocess, use `pgrep` to check for existing instances with the same `--family` and `--timeframe` args.**

### The Check (Simple One-Liner)

```python
import subprocess, sys

family = "YOUR_FAMILY"  # e.g., "forex"
tf = "YOUR_TIMEFRAME"   # e.g., "4h"

existing = subprocess.check_output(
    ["pgrep", "-f", f"generate_dataset_v2.py.*--family {family}.*--timeframe {tf}"],
    text=True
).strip()
if existing:
    sys.exit(f"duplicate gen detected for {family}/{tf} (pid={existing}). abort.")
```

`pgrep` returns empty (exit code 1) if not found, non-empty if found. No `grep -v grep` needed, no `ps aux` parsing.

### Decision Tree

- **pgrep returns empty** → spawn the gen subprocess
- **pgrep returns PIDs** → do NOT spawn. Wait for existing process to finish, or kill it if stuck/zombie
- **pgrep returns a PID that's not in `ps aux`** → zombie, kill it, then spawn fresh

### Why This Is Critical

On 2026-06-17, duplicate `generate_dataset_v2.py` processes were spawned (same family+timeframe args) by:
1. Multiple kanban workers dispatched for the same task
2. Dispatcher re-dispatching "running" tasks whose workers crashed

Each duplicate gen process allocates 8-37GB of RAM. On a 36GB Mac Studio, just 2 duplicates (2 × 25GB) exceed total RAM → kernel OOM panic. The `mem_gate` system only protects against concurrent STARTS — it **cannot** prevent duplicates once both are running.

**Rule: ONE process per family+timeframe combo at any time.**

## 🔒 RULE 3: EURJPY Crash — Explicit Handling Procedure

> **Full diagnostic reference:** `references/eurjp4y-crash.md` — detailed symptom log, probe commands, and fix approaches.

**Problem:** forex_4h gen consistently crashes at EURJPY (tickers [1-2] skip, [3] EURJPY → process dies). 2+ runs confirmed.

**Root causes (prioritized):**
1. **Dukascopy fetcher returning corrupted/too-large dataset** for EURJPY 4h (5x+ rows vs other pairs)
2. **Memory leak in window generation** for large datasets (not just loading, but iterating)
3. **Data quality issue** — malformed rows, NaN values, etc.

### Step-by-Step Fix Procedure

1. **Read the fetcher:**
   ```bash
   # Understand how EURJPY 4h is fetched
   cat ~/dev/sphoin_engine/bin/fetchers/dukascopy.py | grep -A 20 "def fetch"
   ```

2. **Probe the data:**
   ```bash
   /Users/desac/miniconda3/envs/p313/bin/python3 -c "
   import sys
   sys.path.insert(0, '/Users/desac/dev/sphoin_engine/bin')
   from fetchers.dukascopy import fetch
   df = fetch('EURJPY', '4h', start='2010-01-01')
   print(f'rows={len(df)}, memory={df.memory_usage().sum()/1e9:.2f} GB')
   print(f'time range: {df.timestamp.min()} to {df.timestamp.max()}')
   print(f'NaN count: {df.isna().sum().sum()}')
   
   # Compare with a working pair
   df2 = fetch('EURGBP', '4h', start='2010-01-01')
   print(f'EURGBP rows={len(df2)}, memory={df2.memory_usage().sum()/1e9:.2f} GB')
   "
   ```

3. **Diagnose:**
   - If EURJPY has 5x+ rows → **streaming/batched window generator needed**
   - If data has many NaNs → **data cleaning step needed**
   - If memory usage > 20GB → **process in chunks**

4. **Implement fix:**
   - **Option A (streaming):** Rewrite window generation to process data in chunks (not load entire dataset)
   - **Option B (skip):** Log warning, skip EURJPY, continue with remaining pairs
   - **Option C (data fix):** If fetcher returns bad data, add validation/cleanup

5. **Test:**
   - Run gen on EURJPY alone first (use `--limit 3`)
   - Verify no crash, parquet files generated
   - Then run full forex_4h batch

### Quick Workaround (If Fix Takes Time)

If EURJPY is blocking the entire batch:
```bash
# Skip EURJPY temporarily by editing the ticker list or adding a skip logic
# Then run the full batch — EURJPY can be added back later
```

## ✅ Task Completion Checklist

When the gen subprocess finishes successfully:
- [ ] Check the log for "done — X windows + Y labels"
- [ ] Verify parquet files exist in `generated/{family}_{timeframe}/`
- [ ] Verify `cascade_state/v2_generated_{family}{_timeframe}.json` was updated
- [ ] Report: tickers processed, windows/labels, output path

When the gen subprocess fails:
- [ ] Check the log for error messages
- [ ] Note: tickers processed before failure, if any
- [ ] If a specific ticker breaks, note it for potential fix
- [ ] **Do NOT keep retrying** — diagnose root cause first (see EURJPY procedure above)

## ⚠️ Common Pitfalls

1. **Duplicate gen processes:** Always dedup before spawning (RULE 2)
2. **Wrong conda env path:** Use `/Users/desac/miniconda3/envs/p313/bin/python3`, NOT `conda run -n p313`
3. **--no-mem-gate is banned:** Never pass `--no-mem-gate` — this disables the admission gate and can OOM-panic the box
4. **Stale worker PIDs:** The kanban DB worker_pid can be stale — always verify the gen subprocess exists separately
5. **Memory underestimation:** If a gen uses significantly more memory than `--mem-est-gb`, increase the estimate so the gate forces single-process execution
6. **Memory estimates not sticky:** Each kanban worker re-derives from a stale brief. **ALWAYS read from the hardcoded table in this skill.**

## References

- `references/eurjp4y-crash.md` — EURJPY crash symptom log, probe commands, fix approaches
- `references/sphoin-gen-safety.md` — memory gate ops, mem_gate registry location, duplicate detection, conda quirk, known bugs