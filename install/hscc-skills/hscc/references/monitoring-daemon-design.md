# Monitoring Daemon Design — ADHD Session (2026-05-28)

## Design Principles

- Check every **60s** (not 30s)
- **Self-heal**: only restart the orchestrator container automatically
- **Alert on everything else**: notify orchestrator which forwards to user via Telegram
- NOT responsible for agent lifecycle management

## Idea Pool (Clustered)

### 1. Multi-layered Health Checks `[N8 V7 F9]`
- **End-to-end synthetic user journey** — test real request paths, not just pings
- **Behavioral anomaly detection** — pattern recognition on error entropy, request latency distributions, not binary up/down
- **Container runtime socket watching** — bypass HTTP entirely, talk to Docker socket for immediate crash detection
- **Subnet ping sweep** — network-layer heartbeat via fping, catches silent host failures HTTP checks miss

### 2. Self-Healing & Circuit Breakers `[N7 V9 F10]`
- **Correlated-failure gates** — only trip when both process AND container show failure, prevents zombie restart storms
- **Hot-standby orchestrator** — sub-10s failover to spare DGX (rejected: too complex for MVP)
- **Phagocytic cleanup** — when container fails, capture core dumps, memory snapshots, root cause artifacts before killing
- **Controlled "fever response"** — during systemic degradation, throttle non-critical services, concentrate resources on failing subsystem

### 3. Graded Alerting `[N8 V9 F9]`
- **Pain signaling network** — graded telemetry (request queue depth, GC pressure, memory fragmentation) enables triangulation of failure origin
- **Predictive workload rebalancer** — migrate vLLM replicas BEFORE OOM kills cascade
- **NAS I/O latency fingerprinting** — self-calibrated thresholds with automatic local-NVMe staging failover

### 4. Daemon Self-Protection `[N9 V8 F8]`
- **Resource capping with real-time priority** — prevents resource exhaustion from starving the monitor
- **Immutable log shipping** — ship logs to isolated external storage, prevents tampering from hiding failures
- **Local decision-making during network partition** — operate autonomously when management node is unreachable
- **Meta-monitoring** — separate cron job verifies monitor's own PID file mtime < 120s

### 5. Minimal Implementation `[N6 V10 F10]`
- **systemd timer + path unit** — native init system as scheduling backbone, path units trigger on file existence
- **Inotify file watcher** — kernel-native filesystem events instead of polling
- **dmesg/journalctl tailer** — greps kernel ring buffer for OOM kills, catches failures at OS level

## Rejected Ideas (Traps)
- **Hot-standby orchestrator** — adds complexity for marginal benefit; single orchestrator with circuit-breaking is sufficient
- **Full predictive scaling** — over-engineering; reactive monitoring is adequate for current cluster size (4 nodes)
- **AI/ML anomaly detection** — requires training data and adds heavy dependencies

## Recommended Approach

**Multi-layered health + circuit breakers + graded alerting**

1. **Health layer**: synthetic user request test (not just ping), container runtime socket watch, behavioral anomaly detection
2. **Self-heal**: circuit breaker with correlated-failure gates, only auto-restart orchestrator
3. **Alert layer**: graded signals (severity levels), not binary up/down
4. **Self-protection**: resource capping to prevent starvation

## Implementation Order

1. Basic polling loop (60s) with HTTP health checks + container state
2. Circuit breaker logic for orchestrator auto-restart
3. Graded alert system with severity levels
4. Resource capping for daemon self-protection
5. Synthetic user journey testing (replaces simple pings)
6. Behavioral anomaly detection (advanced, post-MVP)
