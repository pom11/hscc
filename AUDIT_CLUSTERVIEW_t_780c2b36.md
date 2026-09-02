# ClusterView Screen Audit — t_780c2b36

Scope: `ios-app/Sources/HSCC/Views/ClusterView.swift` — the fleet hub / operator's main read on fleet health.

Live API (read-only, derived via `hscc api status`): address **REDACTED** (tailnet host, per address guard), port 8788. Token from `~/.hscc/api-token`.

## Data models / endpoints (file:line)

- `clusterStatus()` → `GET /v1/cluster/status` — HSCCClient.swift:371
- `clusterHosts()` → `GET /v1/cluster/hosts` — HSCCClient.swift:381
- `EndpointPath.clusterStatus = "/v1/cluster/status"` — HSCCClient.swift:85
- `ClusterStatusResponse { workloads, idle_hosts, total_hosts, speak }` — SharedModels.swift:33
- `ClusterHostsResponse { hosts, saved_clusters, live_status, speak }` — Models.swift:58
- `TopologyPair` / `TopologyNode` / `NodeState` — SharedModels.swift:269-307

## Live fetch evidence (2026-09-02, read-only)

### GET /v1/cluster/status (via curl, bearer token)
```json
{
  "workloads": [ 2 entries {name, tp:"2", pp:"1", container_id} ],
  "idle_hosts": [ 4 strings — node_N + LAN ip + "Up 4/5 hours" + image ],
  "total_hosts": 4,
  "speak": "4 hosts up. 2 workloads running, 4 idle."
}
```
### GET /v1/cluster/hosts
```json
{
  "hosts": [ 5 dicts {id,name,ip,role,ssh_user} — gx10-gateway(244), gx10-worker-1(246), gx10-worker-2(247), gx10-worker-3(248), nas(249) ],
  "saved_clusters": {success,returncode,output},
  "live_status": {success,returncode,output},
  "speak": "5 hosts registered."
}
```
Route sweep: both `/v1/cluster/status` and `/v1/cluster/hosts` return 200 + parseable JSON (api_route_sweep.py, all swept routes answered).

## Findings
(work in progress — see section-by-section below)
