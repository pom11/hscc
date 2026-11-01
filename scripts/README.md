# HSCC cron watchdogs

Pure shell scripts (no LLM) that monitor the HSCC cluster + Mac dispatcher host. Each follows the watchdog pattern: silent when healthy, output only when action is taken or a problem is detected. Registered through Hermes' built-in cron with `--no-agent` so they cost zero tokens.

## Scripts

| Script | Purpose |
|---|---|
| `hscc_proxy_watchdog.sh` | Probe `localhost:4000` (sparkrun LiteLLM proxy). If unreachable, restart via `sparkrun proxy start --cluster hscc --port 4000`. Covers the stale-PID regression where the proxy dies and sparkrun doesn't auto-restart it. **Manual one-shot only — do not schedule it**; the daemon's `proxy` stream owns this supervision (see *Retiring the superseded watchdog cron jobs*). |
| `hscc_worker_health.sh` | Probe all 4 vLLM endpoints (`100.64.0.1/.2/.3/.4:8000`). Verifies each serves the expected model id; reports unreachable hosts and model-id mismatches. |
| `hscc_cluster_digest.sh` | Periodic summary: container count per host, endpoint health, proxy state, per-job uptime. Output is saved to the job's run history (`hermes cron list`); set `--deliver` to a real gateway target to push it to a channel. |
| `hscc_nas_watchdog.sh` | NAS health: ping the QNAP NAS (`100.64.0.x`), check Mac `/Volumes/NAS` mount listability. Falls back to project docs for remediation. |

## Install

Scripts go under `~/.hermes/scripts/` so Hermes can find them via `--script`.

### Via bootstrap (recommended)

Bootstrap copies these automatically as part of the install flow:

```bash
~/dev/hscc/hscc-bootstrap/bootstrap.sh
```

Look for the *Install: operator watchdog scripts* stage. User-added scripts in
the runtime dir are preserved (only `hscc_*.sh` names are touched), and the
previous version of each script is backed up to `<name>.bak-<timestamp>` before
overwrite. Pass `--no-backup` to skip the backup step.

### Manual install (no bootstrap)

```bash
mkdir -p ~/.hermes/scripts
cp scripts/hscc_*.sh ~/.hermes/scripts/
chmod +x ~/.hermes/scripts/hscc_*.sh
```

Then register the cron jobs (Hermes gateway must be running so the cron ticker picks them up):

```bash
hermes -p default cron create 'every 10m'  --name 'hscc-worker-health'   --no-agent --script hscc_worker_health.sh

# Cluster digest — a periodic summary; its stdout is saved to the job's run
# history (visible in `hermes cron list` / `hermes cron runs`). For the digest
# to be pushed to a live channel, set --deliver to a real target (e.g.
# `bot-chat` to inject into a local profile's Bot Chat, or a gateway platform
# home channel like `--deliver discord`). There is no "desktop" delivery
# target — `--deliver desktop` is accepted but silently delivers nowhere.
hermes -p default cron create 'every 2h'   --name 'hscc-cluster-digest'  --no-agent --script hscc_cluster_digest.sh
```

Verify with `hermes -p default cron list`.

**Do NOT create `hscc-proxy-watchdog` or `hscc-nas-watchdog` jobs.** The two
`cron create` lines for them that used to sit in this block are removed, not
just commented: the daemon already supervises both subjects (see the next
section), and a cron copy of the proxy probe actively fights the daemon's own
relaunch logic.

## Retiring the superseded watchdog cron jobs

`hscc-proxy-watchdog` (every 5m) and `hscc-nas-watchdog` (every 240m) are
superseded by daemon check streams and must be **deleted**, not left paused. A
paused job still shows as live inventory, still appears in every `cron list`
that an operator or an auditor reads, and stays one `cron resume` away from
fighting the daemon again. The repo side is already retired: no `cron create`
command anywhere on main can bring either one back — the last two such lines
were removed by t_048fe42e (GAP-B-5), right above.

### Ownership: who supervises what

| subject | cron entry (paused since 2026-06-20) | who owns it now |
|---|---|---|
| sparkrun LiteLLM proxy :4000 | `hscc-proxy-watchdog`, `every 5m`, job `42fa8a0b8c0c` | daemon `proxy` stream: `PERIODIC_INTERVALS["proxy"] = 60` (`hscc_daemon/daemon_ops.py:34`) → `check_proxy` (`hscc_daemon/health.py:1326`), which relaunches via `sparkrun proxy start` behind a durable 300 s cooldown and writes the state `hscc verify` reads |
| NAS reachability + Mac mount | `hscc-nas-watchdog`, `every 240m`, job `06c8e22d00f1` | daemon `nas` stream: `PERIODIC_INTERVALS["nas"] = 900` (`hscc_daemon/daemon_ops.py:34`) → `check_nas` (`hscc_daemon/health.py:1810`), alerting through the `nas-down` trigger rule (`hscc-bootstrap/triggers.default.json:60`) |

The two scripts stay in the tree on purpose: they remain legitimate one-shot,
manual probes (and `hscc_nas_watchdog.sh` runs with no daemon at all). What is
retired is their *scheduling*.

### Retire commands (operator-side)

These are the exact, verified commands (2026-11-01) — run them, do not
paraphrase them. **The entries are Hermes cron jobs in the `default` profile's
store, not OS crontab lines**: `crontab -l` on the dispatcher host returns `no
crontab for desac`, so checking the system crontab proves nothing. (Audit B
filed this gap as "cron entries"; the table above records the accurate store.)

```bash
# 1. Look before you delete — expect BOTH jobs, [paused], Script: as listed:
hermes -p default cron list | grep -A3 'hscc-proxy-watchdog\|hscc-nas-watchdog'

# 2. Delete the two stale jobs (job ids from the live `cron list` above):
hermes -p default cron remove 42fa8a0b8c0c     # hscc-proxy-watchdog
hermes -p default cron remove 06c8e22d00f1     # hscc-nas-watchdog

# 3. Verify they are gone — expect NO output:
hermes -p default cron list --all | grep 'watchdog' ; echo "rc=$? (1 = gone)"
```

If your store shows different job ids, get them from step 1 (`cron remove`
takes only a job id); never paste an id remembered from another machine.

Do NOT remove `hscc-worker-health` / `hscc-cluster-digest` here — GAP-B-5 names
only the two watchdogs, and those jobs are out of scope. If you decide they too
are redundant, that is a separate call (and GAP-B-4 asks for a staleness probe
before anything else is retired on vibes).

## Address guard (`address_guard.py`) — runs on every commit

`address_guard.py` is the repo's **single** implementation of "does this content
carry a real operator LAN or tailnet address". This is a PUBLIC repo, and real
addresses have reached tracked files twice — most recently a ledger tick that
recorded a verbatim `mount_nfs` command and reached `origin/main` on a **docs-only
commit**. The pytest gate could see that leak but never ran for it, so the same
detector now also runs at commit time.

| caller | scope | what it reads |
|---|---|---|
| `.githooks/pre-commit` | `--staged` | the **index** (staged blobs) |
| `hscc_daemon/tests/test_no_real_addresses_committed.py` | `scan_tracked()` | every tracked file on disk |

Both import the same module — there is no second regex to drift. `.githooks` is
committed and `core.hooksPath` points at it (plain `.git/hooks` is not cloned, so
it cannot be the mechanism); `hscc-bootstrap/install_hooks.py` sets it, into the
COMMON git config, so every linked worktree is armed by one install.

Placeholders are the documented convention and are allowed: `10.0.0.x` for a LAN
node, `100.64.0.1` for a tailnet host (`100.64.0.0/24` is the sanctioned fixture
block). A blocked commit prints the offending `file:line` and both placeholders.

```bash
python3 scripts/address_guard.py --staged            # what the hook checks
python3 scripts/address_guard.py --tracked           # whole tracked tree
```

Exit codes: `0` clean, `1` real address found, `2` the guard could not run (the
hook treats `2` as a block too — a guard that cannot run must not wave a commit
through). Do not `git commit --no-verify` past it on a docs file: that is exactly
how the 2026-10-08 leak shipped.

## Tap bump (`bump_formula.py`) — the release step that edits Homebrew

`pom11/homebrew-hscc` installs hscc from the GitHub **release tarball**, so its
`Formula/hscc.rb` pins three facts about a release: the archive `url`, its
`sha256`, and (implicitly) the version. Until t_a946834f all three were
hand-edited at release time — the step that gets forgotten, after which the tap
keeps serving the previous release while `CHANGELOG.md` and GitHub's `latest`
say otherwise, and the only symptom is a user silently installing an old build.

```bash
python3 scripts/bump_formula.py --tag v2.13.0 --dry-run   # read-path only
python3 scripts/bump_formula.py --tag v2.13.0             # commit + push the tap
```

Run it from the release card / orchestrator gate, right after the tag is pushed.
Nothing is typed in and nothing is trusted:

| computed | how |
|---|---|
| `url` | `https://github.com/pom11/hscc/archive/refs/tags/<tag>.tar.gz` |
| `sha256` | downloaded and hashed, in a temp file, every run |
| version | tag minus `v`, cross-checked against the `VERSION` **inside** the tarball |

There is deliberately no `--sha256` flag: Homebrew's only integrity check for the
tarball *is* that digest, so a value copied from a release body or typed from
memory is a value that can be wrong. The `VERSION` cross-check is not belt-and-
braces either — the tap's `test do` block asserts the installed CLI prints the
formula's version, while `hscc --version` prints the file, so a tag cut without
bumping `VERSION` installs fine and then fails `brew test hscc` on every machine.
This catches it where the bytes are.

Two gates run before any tap commit, and either one restores the formula
byte-for-byte: a Ruby syntax check on the rewritten formula (a formula that does
not parse breaks `brew tap` for every user of the tap, not just for hscc), and the
tap's **vendored** `address_guard.py --tracked` — the same file the tap's own CI
runs, imported or executed rather than reimplemented, so this script carries no
address pattern of its own.

Only the release-pinned lines move. The formula's `resource` blocks pin PyPI
(PyYAML, rich, psutil and rich's transitives), not hscc, so they are not this
release's facts; the rewrite asserts every changed line sits before the first
`resource "` and that the number of `sha256` lines did not change. It also never
*adds* a `version` line: the tap's formula has none (Homebrew infers it from the
url) and `brew audit --strict` treats a redundant one as noise.

Idempotent by design — re-running for the version already pinned writes nothing
and exits `0` with `up to date`, after re-hashing the archive (so a right version
with a stale digest is still corrected rather than waved through). `up to date` is
a claim about the *public* tap, not about the file in front of the tool, so it is
only made after the checkout has been fast-forwarded onto `origin` and found to
carry no unpushed commit. A bump commit whose push died gets published by the next
run (`published the unpushed commit(s) that carry it`); a pin that exists only as
an uncommitted hand edit, a checkout that has diverged from `origin`, and unpushed
commits touching files besides the formula are all refused — the tool publishes
its own bump and nothing else's work. Exit codes match `address_guard.py`:
`0` bumped or up to date, `1` a gate or git step
rejected it, `2` malformed tag / unreachable tarball / no ruby / no guard — never
a silent pass. Env overrides for a release host that differs from the operator's:
`HSCC_TAP_DIR`, `HSCC_TAP_CACHE_DIR`, `HSCC_RELEASE_REPO`, `HSCC_TAP_REPO`,
`HSCC_ADDRESS_GUARD_PY`, `HSCC_RUBY_BIN`.

`.github/workflows/bump-homebrew-tap.yml.disabled` is the *future* transport: the
same script on `on: release: published`, which needs a cross-repo write token the
operator must create. It ships inert (`.disabled` extension, plus a
`TAP_BUMP_ENABLED` repo-variable gate) — do not create the secret and do not
rename the file while the release card still runs the script locally, or every
release grows a red X.

## README lint (`readme_lint.py`) — R7 gate (audit §3)

`readme_lint.py` enforces the two rules `docs/audits/README_REVIEW_2026-10-15.md`
§3 defines for this repo's README set, plus the docs/ index-coverage rule
(t_c2ec721f):

1. **badge == VERSION** — every shields.io badge whose label asserts a
   version (`version`/`v`/`release`) must carry the repo version (root
   `pyproject.toml` `[project] version` if present, else the `VERSION` file
   — what `hscc --help` prints). The audit's motivating bug was exactly
   this drift.
2. **quoted commands exist** — every backticked command in a README must
   exist on the real surface: `hscc …` verbs parse through the CLI-help
   audit's own `ExampleParser` oracle (no second enumeration to stale),
   external CLIs are allow-listed strings, repo scripts must exist.
4. **docs/ index coverage** (`DOC-UNLISTED`) — every file tracked under
   `docs/` (`git ls-files docs`) must be referenced by a backtick-quoted
   path in `docs/README.md`, the index whose own preamble promises exactly
   this. Directory-heading bullets (entries ending in `/`) are not file
   entries (b8f05170's counting convention); the claim decayed twice with
   no gate (3 short, then 10), which is why the rule is a finding, not a
   note. The leg runs whenever `docs/README.md` exists under `--repo`, so
   `--files` cannot silence it. (Numbered 4: the audit's rule (3) is
   flightdeck naming — §3.1's ledger, deliberately not this lint's.)

Fenced blocks are out of scope (covered by the CLI-help audit's (C) pass);
every skip reason is counted in the report, so the check surface cannot
quietly shrink. Like the CLI-help audit, it NEVER executes a quoted command.

```bash
python3 scripts/readme_lint.py --strict          # the pytest gate's contract
python3 scripts/readme_lint.py --json out.json   # machine report
```

Exit codes match `address_guard.py`: `0` clean, `1` a finding, `2` could
not run (never a silent pass). The pytest gate is
`scripts/tests/test_readme_lint.py` (runs in the `scripts` leg of
`run_tests.sh`); `scripts/tests/fixtures/readme_lint/{clean,broken}/` pin
both directions — a wrong badge, a fake command, a deleted script and an
UNLISTED docs file (plus a fence-only reference that must not count) all
MUST fail the lint.

## Customization

Each script is self-contained shell + hardcoded host IPs / model ids. Cluster topology assumptions live at the top of each script — edit there if your cluster differs.

## Why `--no-agent`

These are pure probes; no semantic interpretation needed. Routing through the LLM would burn orchestrator tokens (whatever model currently fronts the orchestrator head) every tick for no added value. Operator slash commands like `/cluster` and `/orch-restart` remain agent-mediated since they're interactive and need confirmation flows.

## Runtime-dependency update loop

`dep_pr_watcher.py` closes an automated loop that keeps the cluster's runtime
dependencies (hermes-agent, sparkrun) current, gated by human review:

1. **`.github/workflows/check-runtime-deps.yml`** (GitHub Actions, daily) checks
   each upstream repo's latest release against `hscc-bootstrap/runtime-versions.json`.
   On a bump it opens/updates a PR — label `needs-cluster-check`, repo owner as
   reviewer — carrying a cluster-verification checklist.
2. **`dep_pr_watcher.py`** (Hermes cron, daily) polls for those PRs and creates
   one idempotent kanban card per PR (`idempotency_key = dep-check-pr-<n>`), so a
   worker verifies the bump end-to-end. Silent when nothing is pending.
3. A worker upgrades the runtime, re-bootstraps, runs the suites, and reports on
   the card. The human reviewer merges the PR (bumping the lock) once satisfied.

Install the cluster side (installs the poller to `~/.hermes/scripts/` and
registers the Hermes cron job, idempotently):

```
scripts/install_dep_watcher.sh            # daily 08:00 by default
scripts/install_dep_watcher.sh --uninstall
```

The GitHub Action runs from the default branch once merged; the poller/cron is
independent and needs no network path from GitHub to the cluster.
