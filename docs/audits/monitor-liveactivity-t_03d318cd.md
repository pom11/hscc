# HSCC Monitor Live Activity — t_03d318cd — work notes

STATUS: in progress
BRANCH: wt/t_03d318cd
ASSIGNEE: ios-engineer

## Task
Build the NEW "HSCC Monitor" Live Activity:
- New extension target HSCCLiveActivityMonitor + MonitorLiveActivity.swift (ActivityConfiguration + Dynamic Island + Lock Screen).
- App-side feeder: poll GET /v1/cluster/monitor + GET /v1/daemon/host at refreshMinutes cadence, update() the activity. BGAppRefreshTask when backgrounded; poll while foregrounded. Honor settings isActive/refreshMinutes/showSections.
- Rendering: reuse Theme, ok/warn/bad colouring, per-node meters, staleDate.

## Constraints
- Live Activities are push-only. All updates flow from app process polling + update(). Force-quit -> freezes; set staleDate + document.
- Update via ActivityKit instance API (Activity.request/update/end), LiveActivityManager pattern.
- Battery budget: respect refresh cadence.
- Do NOT touch wake activity or its attributes (LiveActivityManager.swift is sibling — minimal/non-breaking only).
- Do NOT modify backend route.
- Scope: ONLY monitor Live Activity (not monitor widget).

## Wire shapes
### /v1/daemon/host (confirmed from routes_ops.py handle_daemon_host + card)
{ hostname, platform, arch, uptime_seconds, cpu:{count,percent,load_avg:[..]},
  memory:{total_gb,used_gb,percent}, disk:{total_gb,used_gb,percent},
  processes, daemon_running, daemon_uptime_seconds, speak }

### /v1/cluster/monitor (LIVE-pinned by fetch 2026-09-28)
{ success:bool, output:string, json:{
    timestamp:float,
    hosts:[{ host:string, error:null|string,
             sample:{ cpu_usage_pct, cpu_load_1m, cpu_temp_c,
                      mem_used_pct, mem_available_mb, mem_total_mb, mem_used_mb,
                      gpu_name, gpu_util_pct, gpu_mem_used_pct, gpu_mem_total_mb,
                      gpu_mem_used_mb, gpu_temp_c, gpu_power_w, ... : ALL STRINGS },
             workloads:[{cluster_id, recipe, runtime, ranks_on_host, containers:[...]}],
             used_slots:int, free_slots:int }],
  }, speak }
NOTE: per-node sample values are STRINGS ("42.5") -> decoder must parse to Double/Int.

### /v1/daemon/host (LIVE-pinned)
{ hostname, platform, arch (strings), uptime_seconds:int,
  cpu:{count:int, percent:float, load_avg:[float]},
  memory:{total_gb, used_gb, percent:float},
  disk:{total_gb, used_gb, percent:float},
  processes:int, daemon_running:bool, daemon_uptime_seconds:int, speak }

## Deliverables
1. Shared: MonitorActivityAttributes + ContentState (models) — app-side + extension agree.
2. MonitorLiveActivity.swift (new HSCCLiveActivityMonitor target).
3. App-side MonitorActivityDriver (poll + update + BGAppRefresh) + sweep on launch + end on settings off.
4. project.yml new target + all four targets in scheme.
5. Verify: build_check.sh, headless decode/render harness, install_payload, merge to main.

## VERIFICATION (running)
- build_check.sh: all 5 targets 0 err / 0 warn (commit 8bf5560).
- check_sources: 86 Swift files in sync. check_theme: CLEAN.
- monitor_activity_check harness PASSES against LIVE API (2026-09-28):
  4 nodes decoded (.244/.246/.247/.248) with CPU/RAM/GPU + daemon-host
  (macOS arm64, CPU/RAM/Disk %, daemonRunning=true). Hostname scrubbed here.
- DEVICE-TARGET BUILD: xcodebuild Release generic/platform=iOS
  CODE_SIGNING_ALLOWED=NO → **BUILD SUCCEEDED**. All 5 targets compile+link+
  embed+validate. HSCCLiveActivityMonitor.appex embedded in HSCC.app/PlugIns
  (bundle com.hscc.ios.liveactivity.monitor, display "HSCC Monitor", arm64
  executable, 1265 symbols — monitor Swift linked).
- Reused app's existing ClusterMonitorResponse/NodeSample from Models.swift
  (already present); added only DaemonHostResponse (new route) + typed
  monitorSnapshot()/daemonHost() client methods in a HSCCClient extension.
- MonitorActivityAttributes protocol-stripped in harness only (macOS can't
  declare ActivityAttributes); committed source keeps the conformance.

## Honest limits
- No physical device attached; sim render blocked (revoked cert) per t_b6a8c450.
  On-device compile+link verified via build_check; xcodebuild device build next.
- BGAppRefresh background execution is OS-scheduled and can't be exercised from
  a headless host — implemented + registered; documented as best-effort.
