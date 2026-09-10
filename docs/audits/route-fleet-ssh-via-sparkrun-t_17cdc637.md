# Route hscc-cluster fleet SSH through sparkrun — t_17cdc637

Status: IMPLEMENTED and COMMITTED (`247fa93`)

## The directive

HSCC must rely on sparkrun for the fleet and must not reimplement what it
owns. Three sites previously shelled out to raw `ssh` against fleet nodes:

| Site | Old command | What it needs from the node |
|------|-------------|------------------------------|
| `discovery.py` `_probe_node` | `ssh -o BatchMode=yes -o ConnectTimeout=8 user@host nvidia-smi --query-gpu=name,memory.total,memory.free,power.draw --format=csv,noheader,nounits` | Per-node GPU capability: model name, total VRAM (MiB), free VRAM (MiB), power draw (W) → drives `gpu_model`, `vram_total_gb`, `vram_free_gb`, `power_draw_w`, and `idle` (power-draw-based, since util% reads ~96% even when idle on GB10). |
| `discovery.py` `nas_status` | `ssh ... ls /mnt/nas >/dev/null 2>&1 && echo ok \|\| echo fail` | Whether the NAS mount is present/reachable from a worker: one probe, never a fan-out. |
| `clusterlib.py` `ssh_cmd` | `run_cmd(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", f"spark@{host}", command])` | Generic remote command; used by `discovery`, `heal.py` (docker health, nas diagnose) and `debug.py`. |

## The sanctioned sparkrun path (inspected, not guessed)

`sanctioned` in `hscc_daemon/health.py` runs a script under sparkrun's OWN
interpreter (`_sparkrun_venv_python()`, resolved from the `sparkrun` CLI
binary's shebang) and drives sparkrun's structured API.

I inspected the real sparkrun package source under
`/Users/desac/sparkrun/src/sparkrun/` and its live venv. Facts:

- `sparkrun.core.cluster_status.ClusterStatus` / `HostOccupancy.gpus`
  (`GpuOccupancy(used_memory_gb, used_util_fraction, ...)`) carries only
  *used* memory / utilization — NOT model name, total VRAM, free VRAM, or
  power draw. So `query_cluster_status`'s `to_dict()` does **not** carry the
  facts `_probe_node` needs, and anyhow `query_cluster_status` was removed in
  sparkrun 0.3.6 (import breaks).
- `sparkrun.orchestration.ssh.run_remote_script(host, script, **ssh_kwargs,
  timeout=...)` — sparkrun's OWN remote-execution primitive. It builds the ssh
  command (`bash -s`, stdin-piped) from `build_ssh_kwargs`, which carries
  sparkrun's configured `ssh_user`, `ssh_key`, and `ssh_options`, and returns
  a structured `RemoteResult(returncode, stdout, stderr)`.

So the right route is not `query_cluster_status` (wrong facts, removed API)
but **`run_remote_script` driven by `build_ssh_kwargs`** — sparkrun's own
remote transport. This is exactly "the better-suited API" the task invited us
to use rather than guess.

## What changed

New `hscc-cluster/sparkrun_bridge.py`:

- `_sparkrun_venv_python()` — resolves sparkrun's interpreter from the CLI
  shebang (same seam as `health.py`).
- `remote_cmd(host, command, timeout)` — runs a small script under sparkrun's
  own venv python that resolves `SparkrunConfig` → `build_ssh_kwargs` and
  drives `run_remote_script`, emitting structured JSON. Returns the same
  `{ok, stdout, stderr, code}` dict the old transport produced, so every
  consumer keeps working unchanged. Fails open (never fabricates output).
  The command is passed as a JSON literal and piped as a bash script — no
  local shell-quoting drift.
- `ssh_cmd(...)` — back-compat alias forwarding to `remote_cmd`.

Sites rewired:

1. `discovery._probe_node` → `sparkrun_bridge.remote_cmd(node.ip, q, timeout=15)`.
   The `sshUser@host` threading is gone; sparkrun owns the user.
2. `discovery.nas_status` → `sparkrun_bridge.remote_cmd(probe_node, "ls /mnt/nas...")`.
3. `clusterlib.ssh_cmd` → `sparkrun_bridge.remote_cmd(host, command, timeout)`.
   Because `ssh_cmd` is the shared transport, `heal.py` and `debug.py` (both
   call `ssh_cmd`) are fixed at the same time. Dead `SSH_USER = "spark"`
   constant removed.

## Why this is the right call

- sparkrun owns host resolution + ssh kwargs. The bridge uses sparkrun's OWN
  `build_ssh_kwargs` (user, key, options) and `run_remote_script`, so it cannot
  drift from what sparkrun is configured to do. The live probe confirmed it
  resolves to `/Users/desac/sparkrun/.venv-sparkrun-py313/bin/python`.
- It does not introduce a second transport: it *reuses* sparkrun's.
- `query_cluster_status` was the wrong target (removed in 0.3.6, and its
  `to_dict()` never carried GPU model/VRAM/power anyway) — using it would have
  reproduced `t_3fe0cd05`'s ImportError-in-silence failure mode.

## Tests

- Tests must not ssh anywhere → they inject a fake at the sparkrun seam:
  `monkeypatch.setattr(d.sparkrun_bridge, "remote_cmd", ...)`. The nvidia-smi
  probe test now asserts the parsed fields (gpu_model/vram/power) flow through
  from the sparkrun seam, and that no `ssh` ever reaches `_run`.
- hscc-cluster suite: **384 passed, 14 skipped.**
- Full 8-dir suite: all green except `hscc-roles`, which fails for a
  **pre-existing, unrelated** environment reason
  (`rolelib.PROFILES_DIR` = `/Users/desac/.hermes/profiles/devops-engineer/profiles`
  does not exist on this node). My commit touches only hscc-cluster files.
  `hscc_daemon`, `sparkrun-hermes`, `hscc-api` all pass.
- Bridge probed live against the real sparkrun venv against the documented
  placeholder — fails open cleanly, returns the structured shape, no real host
  touched, no fabricated output.

## Out of scope / not touched

- `hscc_daemon/health.py:527` (`docker info`) and `:541` (local hscc-postgres)
  — explicitly exempt (legitimately local to the daemon host).
- `hscc_daemon/health.py:1101` auto-heal container-age — belongs to t_cd6a895a.
- `hscc_daemon/health.py`'s own `_SPARKRUN_STATUS_SCRIPT` — it still imports
  the removed `query_cluster_status` and falls back to text-parse; that is
  `t_3fe0cd05`'s concern and was handled separately. Not in this card's scope.

## Verification

```
hscc-cluster/tests/  : 384 passed, 14 skipped
full suite (8 dirs)  : ✓ bootstrap ✓ commands ✗ roles(pre-existing) ✓ cluster
                       ✓ project ✓ daemon ✓ sparkrun-hermes ✓ api
bridge live probe    : resolves sparkrun venv, drives run_remote_script,
                       fails open, never fabricates output
```
