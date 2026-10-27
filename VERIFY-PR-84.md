# Verification report — PR #84 runtime dep bump (sparkrun v0.4.1 -> v0.4.2)

Task: t_0bf45bc2 (@backend-engineer)
PR: https://github.com/pom11/hscc/pull/84
Scope: **VERIFY ONLY — PR left OPEN, not merged.**
Run date: 2026-10-27 (EET)
Result: **COMPATIBLE — ship-ready. Zero regressions.** Two incompatibilities found and handled (§7).

Addresses redacted throughout per the public-repo rule: LAN nodes appear as
`10.0.0.x`; no tailnet host, token, or key is recorded anywhere in this file.

## 1. What the PR moves

Exactly one file, one line — `hscc-bootstrap/runtime-versions.json`:

| dependency | from | to |
|---|---|---|
| `sparkrun` | v0.4.1 | **v0.4.2** |

`hermes-agent` stays pinned at `v2026.9.24` and was **not** touched, so `hermes
update` is deliberately out of scope here (and the repo's own runbook forbids an
agent self-upgrading the runtime it and its siblings run on —
`install/hscc-skills/hscc/references/runtime-bump-verification.md:123`).
Branch `deps/runtime-bump`, base `main`, 1 commit (a01a256a), MERGEABLE.

## 2. Recovery point (established BEFORE anything live was touched)

Pushed, so durable rather than local-only:

| repo | ref | points at |
|---|---|---|
| hscc (origin) | `recover/pr84-pre-hscc-20261027-080719` | `4871c0b1` (2.13.1) |
| hscc (origin) | `backup/pr84-pre-hscc-20261027-080719` | `4871c0b1` |
| sparkrun (fork) | `recover/pr84-pre-sparkrun-20261027-080719` | `1aaa6479` — live fork tip |
| sparkrun (fork) | `backup/pr84-pre-fork-20261027-080719` | `1aaa6479` |

Deeper rollback lines left untouched per the runbook: `~/dev/sparkrun-live`
(v0.4.0, `.venv-sparkrun-040`) and `pre-rebase-v040-safety` (v0.3.10).

On-disk: `~/.hscc/_recovery-pr84/` — `recovery-manifest.txt` (incl. rollback
commands), `runtime-versions.json.pre`, `verify_pre_bump.json`, plus the
pre-bump `litellm_config.yaml`, `sparkrun_balancer.py`, and the v0.4.1 editable
dist-info + `.pth` (so the metadata refresh in §7.2 is itself reversible).

Baseline re-confirmed with real work, not a version check: proxy answered,
`chat_roundtrip` returned text from 3 serving units, `project_chat_ready` 15/15.

## 3. The bump is a rebase, not an install (fork-branch rule honoured)

The cluster runs **our fork branch**, not upstream `main`:
`~/dev/sparkrun-041-verify` on `feat/proxy-least-in-flight-041`, editable into
`~/sparkrun/.venv-sparkrun-041` (`~/.local/bin/sparkrun` -> that venv).

```
git -C ~/dev/sparkrun-041-verify fetch origin tag v0.4.2     # v0.4.2 = 96f2621a
git -C ~/dev/sparkrun-041-verify rebase v0.4.2               # conflict-free
```

Before: `v0.4.1-1-g1aaa6479`  ->  After: `v0.4.2-1-g39f64273`.

Post-bump invariants, all checked:

- `rev-parse --abbrev-ref HEAD` == `feat/proxy-least-in-flight-041` — **branch left checked out** (§rule 1)
- `router_settings` still read in `src/sparkrun/plugins/litellm/engine.py:901`
  (`_configured_router_settings` -> `ProxyConfig.gateway_settings`, :906) and
  still emitted by `build_litellm_config` (:230) — and `git diff` proves the
  whole `plugins/litellm/` + `proxy/` tree is **byte-identical** across the bump
- Never `git checkout main`, never reinstalled from upstream (§rule 2)
- Upstream did **not** touch `tests/test_benchmark_host_identity.py`, so the
  fork's macOS-spawn fix is still needed and still rebases cleanly (v0.4.2 still
  carries the unpicklable `lambda` at that file's line 376)

**Needs the human: the fork branch tip moved, and syncing the remote is a
force-push** (a rebase rewrites history), which a worker must not do
unattended. The rebased tip is `39f64273` local; `fork/feat/proxy-least-in-flight-041`
is still `1aaa6479`. Verified lease command for the reviewer:

```
git -C ~/dev/sparkrun-041-verify push --force-with-lease=feat/proxy-least-in-flight-041:1aaa647962cece897a541fd4346b275b61842516 fork feat/proxy-least-in-flight-041
```

The live cluster is already running the rebased branch; pushing only makes the
fork remote match what is deployed. Nothing waits on it for verification.

## 4. Static compatibility read (the 38-commit delta)

Read the actual range, not the release notes. v0.4.1..v0.4.2 = 38 commits,
113 files, +13130/-524. Concentrated in **rdma-test, TUI/monitor, model-cache**
and — the one that matters for us — a **rewrite of the sparkrun plugin
subsystem** (`plugin_defaults.py` new; `plugin_manager`, `plugin_lifecycle`,
`plugin_validation`, `plugin_policy`, `api/plugins.py` all modified).

Against every sparkrun symbol HSCC actually imports:

| symbol | still present at v0.4.2 | module touched by the bump |
|---|---|---|
| `ClusterManager` | yes | +3 lines (additive `lease_timeout_s`) |
| `SparkrunConfig`, `get_config_root` | yes | +1 line (additive) |
| `resolve_hosts` | yes | no |
| `build_ssh_kwargs`, `run_remote_script` | yes | no (`orchestration/` diffs are distribution/executors/job_metadata/rdma) |
| `ProxyConfig` | yes | **no** |
| `api.status(hosts, ssh_kwargs=…)` | yes, signature identical | **no** |

No dependency changes at all: `pyproject.toml` and `uv.lock` differ from v0.4.1
only in the version string. `sparkrun-hermes/plugin.yaml` declares no minimum
sparkrun version, and `dep_bump_guard.py` encodes branch-name invariants, not a
version floor — so nothing in the stack gates on the version string.

## 5. Re-bootstrap

`~/dev/hscc/hscc-bootstrap/bootstrap.sh --yes` (run against the shared
`~/.hermes`, not a profile dir; `--dry-run` reviewed first, no deletion path in
the script, backups on by default): exit 0. Plugin payload copied, skills, role
profiles, orchestrators, config wiring (41 profiles), trigger rules, daemon
installed + restarted. Warnings were all pre-existing/expected: patches already
applied, kanban review feature skipped on a dirty hermes tree (the 2 local
carried commits), `serving.json` kept, REPLs outside tmux.

`runtime-versions.json` is a lock **record** read by the dep-watcher/CI, not by
bootstrap — so the bootstrap does not consume the bump; it redeploys HSCC on top
of it. Committed in this worktree to make the verified pairing reproducible.

## 6. Test suites — 6539 passed, 1 failed (pre-existing), 3 skipped

Via the canonical `scripts/run_tests.sh` (one pytest process per dir; never
combine dirs in one invocation):

| suite | result |
|---|---|
| hscc-bootstrap | 467 passed |
| hscc-commands | 69 passed |
| hscc-roles | 134 passed, **1 failed** |
| hscc-cluster | 666 passed, 1 skipped |
| hscc-project | 1420 passed |
| hscc_daemon | 2431 passed |
| **sparkrun-hermes** | **12 passed** |
| hscc-api | 938 passed, 1 skipped |
| memori_byodb | 11 passed |
| scripts | 262 passed, 1 skipped |
| .github/scripts | 129 passed |

The one failure is `test_every_hscc_owned_worker_profile_has_role_spec` —
`ecofire-pontaje-engineer` has no role spec. **Environmental and pre-existing**:
it fails identically on pristine main on this operator mac (live profile added
outside HSCC), so it is not attributable to the bump.

`sparkrun-hermes` is the suite most exposed to v0.4.2's plugin rewrite and it is
green, including its sparkrun-fence assertions (live CLI resolved to
`~/sparkrun/.venv-sparkrun-041/bin/sparkrun`, 0 unexpected live calls).

## 7. Live verification — zero regressions

### 7.1 `hscc verify`: 13/13 checks byte-identical pre vs post

Diffed the two `hscc verify --json` payloads check-by-check:
**REGRESSIONS: NONE. IMPROVEMENTS: NONE.** 3 checks fail identically **before
and after**, all environmental and sparkrun-independent:

- `multiplex` — `profiles not served: ecofire-bc, .deleted`. `profiles/ecofire-bc`
  holds only a stray `workspaces/` dir (no profile config) and `profiles/.deleted/`
  is a leftover probe dir (`probe-20261026-*.orch`) being scanned as a profile id.
- `daemon_streams` — watchdog `Degraded — gateway not reachable`, auto_restart_count=270.
- `api_routes` — a route the app calls raises.

Recording this baseline first is what makes "no regression" a measured claim
rather than an assumption.

### 7.2 Incompatibility #1 found and fixed: stale editable-install version

After the rebase, `sparkrun --version` still reported **0.4.1** while the code
was v0.4.2. Cause: `sparkrun/__init__.py:6` resolves `__version__` via
`importlib.metadata.version("sparkrun")`, i.e. the venv's `dist-info` — and a
rebase changes no metadata, so `sparkrun-0.4.1.dist-info` stayed behind.

Not cosmetic in practice: `docs/audits/sparkrun_proxy_venv_cutover.sh:119,148`
gates a rollback on `sparkrun --version` matching, so a stale string is a live
trap for the next cutover. Fixed with a metadata-only refresh — same venv, same
editable path, same branch, no dependency movement (safe precisely because the
lock files are otherwise identical):

```
VIRTUAL_ENV=~/sparkrun/.venv-sparkrun-041 uv pip install --no-deps -e .
# - sparkrun==0.4.1 (from file:///Users/desac/dev/sparkrun-041-verify)
# + sparkrun==0.4.2 (from file:///Users/desac/dev/sparkrun-041-verify)
```

Verified after: `sparkrun --version` -> `0.4.2`; branch invariant still holds;
`.pth` still points at `~/dev/sparkrun-041-verify/src`; proxy still healthy.
Old dist-info + `.pth` archived in the recovery dir.

### 7.3 Proxy :4000 + balancer — verified WITHOUT a restart

A proxy restart would have cut my own inference route (`worker-model` is served
by :4000), so I proved the balancer survives without one:

- `sparkrun proxy sync` against v0.4.2 code: "already in sync", and both
  `litellm_config.yaml` and `sparkrun_balancer.py` came back **md5-identical**.
- `hscc verify` `proxy_router_settings` quiet; daemon `proxy.json` stream
  `router_drift_warn: false`, 3 models served, aliases covered.
- Generator parity proven directly against the live v0.4.2 tree: rendered the
  config through the engine's own `_build_desired_config()` and compared to the
  live file — `model_list`, `litellm_settings`, `router_settings` all identical;
  `callbacks: [sparkrun_balancer.handler]` present; `routing_strategy:
  simple-shuffle` (the EXPECTED healthy value while the balancer is live, not
  drift); `sparkrun_balancer.py` next to the config.
- Real chat round-trip on :4000 -> `chat.completion` from `worker-model`,
  `system_fingerprint: vllm-…`, live through the balancer.
- `engine_wedge` stream: **3/3 streaming ok**, none wedged/down/busy.

### 7.4 Daemon streams + slash commands

Daemon restarted by bootstrap, streams written continuously (dgx, dispatcher,
engine_wedge, gateway, heartbeat, idle, local, nas, proxy, triggers), `heartbeat
ok: true`, fleet 7/7 idle, `dgx ok: true`, `engine_wedge ok: true`.

Deployed operator slash commands (`~/.hermes/plugins/hscc-commands`) registered
all 12 handlers; invoked the two read-only ones in-process the way the gateway
does: `/status` (topology + live worker power) and `/cluster` (3 nodes, serving
model, aliases) both returned real content.

### 7.5 Incompatibility #2 found: bootstrap left the daemon UNSUPERVISED

`bootstrap.sh` -> `restart_daemon.sh` restarts via `hscc stop`/`hscc start`, and
`using_console_script()` (restart_daemon.sh:93-96) prefers whatever `hscc` is on
PATH. On this host that is the **brew keg** (`/opt/homebrew/bin/hscc` -> Cellar
2.13.1, keg venv **python 3.13**), so the restart:

1. ran **keg code**, not the freshly-copied `~/.hermes/plugins` payload — the
   opposite of the restart step's stated purpose ("so freshly-installed code is
   ALWAYS loaded");
2. left launchd with **no PID** for `com.hermes.hscc_daemon`, and
   `KeepAlive.SuccessfulExit=false` means a clean exit is never respawned — the
   fleet was running an unsupervised daemon.

Reported as `daemon restarted (pid 40108 -> 62395)` and exit 0, so bootstrap
looks clean while supervision is gone. This is a pre-existing bug in the
bootstrap/brew co-install path (the same shadowing class as the shadow-warn
work), not a v0.4.2 incompatibility — but it bit during this bump's re-bootstrap,
so it is recorded here.

I restored supervision the sanctioned way (`hscc stop`, releasing the
single-instance lock, then `launchctl start com.hermes.hscc_daemon`). Now:
launchd `PID = 64827`, running `~/.hermes/hermes-agent/venv/bin/python
~/.hermes/plugins/hscc_daemon/hscc.py start-daemon` — the pre-incident
configuration. Follow-up card filed (§8).

### 7.6 A transient `chat_roundtrip` blip, correctly attributed

One post-bump run reported `model on 10.0.0.x:8002 did not answer
(closed_early)`. The `engine_wedge` stream minutes later showed that unit ok and
`3/3 streaming ok`; two further verify runs then passed `chat_roundtrip`. So it
was a transient backend blip, **not** a regression — recorded so nobody chases
it as bump damage.

## 8. Verdict

**COMPATIBLE. Nothing blocks merge** except the reviewer's force-push of the
rebased fork branch (§3) and their own judgement on merging, which the card
reserves for them. The proxy/balancer surface HSCC depends on is byte-identical
across the bump, every imported symbol survives, all suites pass except one
known-environmental failure, and live behaviour is identical to the v0.4.1
baseline.

Two things to carry forward (filed as cards, not fixed here — out of this
task's scope):

1. `restart_daemon.sh` must restart the **launchd-supervised** unit (or pin the
   hermes-venv interpreter) instead of any keg `hscc` on PATH, and must fail
   loudly when supervision is lost (§7.5).
2. `hscc verify multiplex` false-fails on `ecofire-bc` + `profiles/.deleted/` —
   a stale probe dir is being read as a profile id (§7.1).

PR left **OPEN** with the checklist updated and evidence posted as a PR comment.
Nothing merged; no merge button touched.
