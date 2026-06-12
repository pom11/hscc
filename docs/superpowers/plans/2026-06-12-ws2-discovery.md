# WS2 — Dynamic Cluster Discovery — Implementation Plan

**Spec:** `../specs/2026-06-12-hscc-hardening-and-orchestrator-design.md` §WS2, D8.
**Status:** building (autonomous).

## Goal
One discovery module = single source of truth. Live resource map (VRAM, GPU, power-draw idle, health), auto-adopt, precedence live→cache→fail-loud. Every consumer (ops/heal/daemon/commands/templates) reads it.

## Two real sources (today)
- `sparkrun cluster list --json` → `[{name, hosts:[ip,...], user, cache_dir, default}]`. Flat hosts; **first host = gateway** (detect.py convention); `cache_dir` = NAS mount.
- `~/.hscc/cluster.json` → `{gateway:{ip,name,sshUser,id}, workers:[...], nasDevices:[{ip}]}`. Richer per-node metadata.
- Consumed independently by `clusterlib._resolve_topology()` and `hscc_daemon/serving.py:resolve_cluster_config()` — the duplication WS2 removes.

## New module: `hscc-cluster/discovery.py`
```python
@dataclass
class Node:
    ip: str; role: str            # "gateway" | "worker" | "nas"
    name: str = ""; ssh_user: str = "spark"; id: str = ""
    # capability (probed live, cached w/ TTL; None when not probed):
    gpu_model: str|None=None; vram_total_gb: float|None=None
    vram_free_gb: float|None=None; power_draw_w: float|None=None
    idle: bool|None=None; reachable: bool|None=None; vllm_healthy: bool|None=None

@dataclass
class ClusterTopology:
    orchestrator: Node; workers: list[Node]; nas: Node|None
    proxy_port: int = 4000; source: str = "live"   # "live"|"cache"

class DiscoveryError(RuntimeError): ...

def discover(*, refresh=False, probe=False) -> ClusterTopology:
    # 1. live: sparkrun cluster list --json  → build topology (first host=gateway,
    #    rest=workers, cache_dir→nas). Enrich names/ssh/id from cluster.json if present.
    #    Write merged result back to cluster.json (cache).
    # 2. cache: read cluster.json {gateway,workers,nasDevices}.
    # 3. neither → raise DiscoveryError (NO silent 192.0.2.x).
    # probe=True → fill capability via nvidia-smi over ssh (idle from power draw).
```

### Pure, testable seams
- `parse_sparkrun_clusters(raw) -> dict|None` (reuse detect.parse_clusters shape).
- `topology_from_sparkrun(parsed, cluster_json_enrich) -> ClusterTopology`.
- `topology_from_cluster_json(d) -> ClusterTopology`.
- `parse_nvidia_smi(csv_text) -> {gpu_model,vram_total_gb,vram_free_gb,power_draw_w}` — parses `nvidia-smi --query-gpu=name,memory.total,memory.free,power.draw --format=csv,noheader,nounits`.
- `classify_idle(power_w, threshold=IDLE_WATTS=15.0) -> bool` — power-draw based, NOT util%.
- `to_cluster_json(topo) -> dict` (cache writer).

### Capability probe (live, flagged for real run)
`_probe_node(node)` → `ssh_cmd(ip, "nvidia-smi --query-gpu=... --format=csv,noheader,nounits")` → `parse_nvidia_smi`. `vllm_healthy` via `curl :8000/v1/models`. Wrapped so `discover(probe=False)` (default) skips it — cheap. TTL cache in `~/.hscc/discovery_cache.json`.

### Auto-adopt
Live `hosts` is authoritative for membership: a host present live but absent from cache → adopted; absent live but in cache → dropped. Enrichment (names) is best-effort.

## clusterlib integration (back-comhpat, fail-loud)
- `clusterlib.HEAD/NODES/NAS_HOST` become module-level via `discover()` instead of `_resolve_topology()`.
- Remove the silent `192.0.2.x` fallback: if discovery fails, `HEAD/NODES/NAS_HOST` resolve via a `_safe_discover()` that logs loud + raises on use OR (to not break import) sets a sentinel that ops surface as an error. **Decision:** keep import side-effect-free — `discover()` is lazy; `HEAD/NODES/NAS_HOST` become module `__getattr__` properties that call discover() and raise DiscoveryError if unresolvable. (No fake IPs ever.)

## Tools
- `discovery_status` read-only handler (full map + source) — register in `__init__.py`.
- `cluster_status` (ops.py) gains free-VRAM + source when probe available.

## Tests (`tests/test_discovery.py`)
- parse_sparkrun_clusters from captured fixture; topology_from_sparkrun (first=gateway, cache_dir→nas); topology_from_cluster_json; reconcile/enrich; auto-adopt new host; drop removed host; fail-loud when both absent (raises DiscoveryError, asserts NO 192.0.2.x); parse_nvidia_smi from captured csv; classify_idle (12W idle, 60W busy — not util); cache round-trip.

## Acceptance
- `discover()` returns real topology from live sparkrun; `source` correct; no `192.0.2.x` anywhere; new tools work; existing 123 cluster tests still green (clusterlib shim).
