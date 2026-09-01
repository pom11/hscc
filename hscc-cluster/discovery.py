"""Single source of truth for live cluster topology (WS2 / D8).

Replaces the two independent topology readers (clusterlib._resolve_topology and
hscc_daemon.serving.resolve_cluster_config) with one module.

Precedence: live `sparkrun cluster list --json` → cached ~/.hscc/cluster.json →
DiscoveryError. There is **no silent fake-IP fallback** — a misconfigured
machine fails loudly instead of SSHing documentation addresses.

Topology is a live *resource map*: with probe=True each node is enriched with
VRAM / GPU model / power-draw-based idle (the real GB10 idle signal — util%
reads ~96% even when idle) / vLLM health.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any

CLUSTER_JSON = os.path.expanduser("~/.hscc/cluster.json")
DISCOVERY_CACHE = os.path.expanduser("~/.hscc/discovery_cache.json")
SERVING_JSON = os.path.expanduser("~/.hscc/serving.json")
SPARKRUN = "sparkrun"
SSH_USER_DEFAULT = "spark"
DEFAULT_PROXY_PORT = 4000

# A GB10 draws ~11-15 W idle, 35-80 W serving. util% is misleading (~96% idle),
# so idle is classified by power draw, not utilization.
IDLE_WATTS = 15.0


class DiscoveryError(RuntimeError):
    """Raised when topology cannot be resolved from live OR cache (no fake IPs)."""


@dataclass
class Node:
    ip: str
    role: str  # "gateway" | "worker" | "nas"
    name: str = ""
    ssh_user: str = SSH_USER_DEFAULT
    id: str = ""
    # capability (None until probed)
    gpu_model: Optional[str] = None
    vram_total_gb: Optional[float] = None
    vram_free_gb: Optional[float] = None
    power_draw_w: Optional[float] = None
    idle: Optional[bool] = None
    reachable: Optional[bool] = None
    vllm_healthy: Optional[bool] = None


@dataclass
class ClusterTopology:
    orchestrator: Node
    workers: List[Node] = field(default_factory=list)
    nas: Optional[Node] = None
    proxy_port: int = DEFAULT_PROXY_PORT
    source: str = "live"  # "live" | "cache"
    name: str = ""

    @property
    def all_nodes(self) -> List[Node]:
        ns = [self.orchestrator, *self.workers]
        if self.nas:
            ns.append(self.nas)
        return ns

    @property
    def worker_ips(self) -> List[str]:
        return [w.ip for w in self.workers]


# ── Pure parsers (testable, no I/O) ─────────────────────────────────────────

def parse_sparkrun_clusters(raw: str) -> Optional[dict]:
    """Parse `sparkrun cluster list --json` → the default (or only) cluster dict,
    or None. Shape: [{name, hosts:[...], user, cache_dir, default}]."""
    try:
        clusters = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(clusters, list) or not clusters:
        return None
    return next((c for c in clusters if c.get("default")), clusters[0])


def topology_from_sparkrun(parsed: dict, enrich: Optional[dict] = None) -> ClusterTopology:
    """Build topology from a parsed sparkrun cluster. First host = gateway
    (the detect.py convention), the rest = workers, cache_dir = NAS mount.
    ``enrich`` is an optional cluster.json dict for names/ssh_user/ids."""
    hosts = list(parsed.get("hosts") or [])
    if not hosts:
        raise DiscoveryError("sparkrun cluster has no hosts")
    user = parsed.get("user") or SSH_USER_DEFAULT
    name = parsed.get("name") or ""

    # index enrichment metadata by ip
    meta: Dict[str, dict] = {}
    if enrich:
        g = enrich.get("gateway") or {}
        if g.get("ip"):
            meta[g["ip"]] = g
        for w in (enrich.get("workers") or []):
            if w.get("ip"):
                meta[w["ip"]] = w

    def _mk(ip: str, role: str) -> Node:
        m = meta.get(ip, {})
        return Node(ip=ip, role=role,
                    name=m.get("name", ""),
                    ssh_user=m.get("sshUser") or user,
                    id=m.get("id", ""))

    orch = _mk(hosts[0], "gateway")
    workers = [_mk(ip, "worker") for ip in hosts[1:]]

    nas = None
    cache_dir = (parsed.get("cache_dir") or "").strip()
    # NAS ip: prefer cluster.json nasDevices; cache_dir alone is a mount path.
    nas_ip = None
    if enrich:
        nd = enrich.get("nasDevices") or enrich.get("nas_devices") or []
        if nd and nd[0].get("ip"):
            nas_ip = nd[0]["ip"]
    if nas_ip:
        nas = Node(ip=nas_ip, role="nas", name="nas")
    return ClusterTopology(orchestrator=orch, workers=workers, nas=nas,
                           source="live", name=name)


def topology_from_cluster_json(d: dict) -> ClusterTopology:
    """Build topology from a ~/.hscc/cluster.json dict
    {gateway:{ip,...}, workers:[{ip,...}], nasDevices:[{ip}]}."""
    g = d.get("gateway") or {}
    if not g.get("ip"):
        raise DiscoveryError("cluster.json has no gateway.ip")

    def _mk(m: dict, role: str) -> Node:
        return Node(ip=m["ip"], role=role, name=m.get("name", ""),
                    ssh_user=m.get("sshUser") or SSH_USER_DEFAULT,
                    id=m.get("id", ""))

    orch = _mk(g, "gateway")
    workers = [_mk(w, "worker") for w in (d.get("workers") or []) if w.get("ip")]
    nas = None
    nd = d.get("nasDevices") or d.get("nas_devices") or []
    if nd and nd[0].get("ip"):
        nas = Node(ip=nd[0]["ip"], role="nas", name=nd[0].get("name", "nas"))
    return ClusterTopology(orchestrator=orch, workers=workers, nas=nas,
                           source="cache", name=d.get("name", ""))


def to_cluster_json(topo: ClusterTopology) -> dict:
    """Serialize topology to the ~/.hscc/cluster.json cache shape."""
    def _n(node: Node) -> dict:
        return {"ip": node.ip, "name": node.name, "sshUser": node.ssh_user,
                "id": node.id, "role": node.role}
    out = {"name": topo.name, "gateway": _n(topo.orchestrator),
           "workers": [_n(w) for w in topo.workers]}
    if topo.nas:
        out["nasDevices"] = [{"ip": topo.nas.ip, "name": topo.nas.name}]
    return out


def parse_nvidia_smi(csv_text: str) -> dict:
    """Parse one line of
    `nvidia-smi --query-gpu=name,memory.total,memory.free,power.draw
     --format=csv,noheader,nounits` (MiB, W). Returns {} on failure."""
    line = (csv_text or "").strip().splitlines()
    if not line:
        return {}
    parts = [p.strip() for p in line[0].split(",")]
    if len(parts) < 4:
        return {}
    out: Dict[str, Any] = {"gpu_model": parts[0] or None}
    try:
        out["vram_total_gb"] = round(float(parts[1]) / 1024, 2)
    except (ValueError, TypeError):
        out["vram_total_gb"] = None
    try:
        out["vram_free_gb"] = round(float(parts[2]) / 1024, 2)
    except (ValueError, TypeError):
        out["vram_free_gb"] = None
    try:
        out["power_draw_w"] = round(float(parts[3]), 1)
    except (ValueError, TypeError):
        out["power_draw_w"] = None
    return out


def classify_idle(power_w: Optional[float], threshold: float = IDLE_WATTS) -> Optional[bool]:
    """Idle iff power draw below threshold. None when power unknown. Uses power
    draw, NOT GPU util% (which reads ~96% even when idle on GB10)."""
    if power_w is None:
        return None
    return power_w < threshold


# ── I/O boundary ────────────────────────────────────────────────────────────

def _run(args, timeout=20) -> dict:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {"ok": r.returncode == 0, "stdout": r.stdout, "stderr": r.stderr}
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        return {"ok": False, "stdout": "", "stderr": str(e)}


def _read_cluster_json() -> Optional[dict]:
    try:
        with open(CLUSTER_JSON) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_cache(topo: ClusterTopology) -> None:
    try:
        os.makedirs(os.path.dirname(CLUSTER_JSON), exist_ok=True)
        tmp = CLUSTER_JSON + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(to_cluster_json(topo), fh, indent=2)
        # cluster.json holds node addresses. This plugin is loaded into Hermes
        # agent processes whose umask we do not control, so a process-level
        # umask (as the daemon, CLI and cron watcher each set) cannot reach
        # here — set the mode explicitly on the temp file before the atomic
        # rename so the published file is never world-readable, even briefly.
        os.chmod(tmp, 0o600)
        os.replace(tmp, CLUSTER_JSON)
    except OSError:
        pass  # cache write is best-effort; never fail discovery on it


def _probe_node(node: Node) -> None:
    """Fill capability fields in place via ssh nvidia-smi + vLLM health."""
    q = ("nvidia-smi --query-gpu=name,memory.total,memory.free,power.draw "
         "--format=csv,noheader,nounits")
    r = _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
              f"{node.ssh_user}@{node.ip}", q], timeout=15)
    node.reachable = r["ok"]
    if r["ok"]:
        caps = parse_nvidia_smi(r["stdout"])
        node.gpu_model = caps.get("gpu_model")
        node.vram_total_gb = caps.get("vram_total_gb")
        node.vram_free_gb = caps.get("vram_free_gb")
        node.power_draw_w = caps.get("power_draw_w")
        node.idle = classify_idle(node.power_draw_w)
    # vLLM health: probe the ports this node actually serves (from serving.json),
    # falling back to 8000 if no serving info is available.
    ports_to_probe = [8000]  # default fallback
    try:
        with open(SERVING_JSON) as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for unit in data.get("units", []):
                if node.ip in (unit.get("nodes") or []):
                    port = unit.get("port")
                    if port and port != 8000:
                        ports_to_probe.append(port)
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
        pass  # no serving info — stick with fallback 8000

    vllm_ok = False
    for port in ports_to_probe:
        h = _run(["curl", "-s", "--max-time", "6",
                  f"http://{node.ip}:{port}/v1/models"], timeout=10)
        if h["ok"] and h["stdout"].strip():
            vllm_ok = True
            break
    node.vllm_healthy = vllm_ok


def discover(*, refresh: bool = False, probe: bool = False) -> ClusterTopology:
    """Resolve topology. live → cache → DiscoveryError. probe=True enriches
    each node with VRAM/power/health over ssh (slower, real-cluster only)."""
    enrich = _read_cluster_json()
    topo: Optional[ClusterTopology] = None

    # 1. live
    r = _run([SPARKRUN, "cluster", "list", "--json"])
    if r["ok"]:
        parsed = parse_sparkrun_clusters(r["stdout"])
        if parsed:
            topo = topology_from_sparkrun(parsed, enrich=enrich)
            _write_cache(topo)  # refresh the cache from live

    # 2. cache
    if topo is None and enrich:
        try:
            topo = topology_from_cluster_json(enrich)
        except DiscoveryError:
            topo = None

    # 3. fail loud — never invent IPs
    if topo is None:
        raise DiscoveryError(
            "Cannot resolve cluster topology: `sparkrun cluster list` failed "
            "and ~/.hscc/cluster.json is absent/unparseable. Configure a "
            "sparkrun cluster (`sparkrun cluster add ...`) first.")

    if probe:
        for node in [topo.orchestrator, *topo.workers]:
            _probe_node(node)

    return topo


def nas_status(args=None, **kwargs) -> dict:
    """Read-only NAS health: discovery's NAS node + a mount probe from one worker.

    Honors the staging constraint (NAS 10G/SATA caps parallel pulls) — this is a
    single lightweight probe, never a fan-out. Returns {ok, nas, mounted, detail}.
    """
    try:
        topo = discover()
    except DiscoveryError as e:
        return {"ok": False, "error": str(e)}
    if not topo.nas:
        return {"ok": True, "nas": None, "note": "no NAS configured (optional)"}
    probe_node = topo.workers[0].ip if topo.workers else topo.orchestrator.ip
    r = _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
              f"{topo.orchestrator.ssh_user}@{probe_node}",
              "ls /mnt/nas >/dev/null 2>&1 && echo ok || echo fail"], timeout=15)
    mounted = r["ok"] and "ok" in (r["stdout"] or "")
    return {"ok": True, "nas": topo.nas.ip, "probe_node": probe_node,
            "mounted": mounted,
            "detail": "mounted" if mounted else "not mounted / unreachable"}


def discovery_status(args=None, **kwargs) -> dict:
    """Read-only tool: full topology map + source. probe=true for live caps."""
    do_probe = bool((args or {}).get("probe"))
    try:
        topo = discover(probe=do_probe)
    except DiscoveryError as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "source": topo.source,
        "name": topo.name,
        "proxy_port": topo.proxy_port,
        "orchestrator": asdict(topo.orchestrator),
        "workers": [asdict(w) for w in topo.workers],
        "nas": asdict(topo.nas) if topo.nas else None,
    }
