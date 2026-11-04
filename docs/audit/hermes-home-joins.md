# HERMES_HOME join audit — root-intent vs profile-intent

Task: t_8893ab89 (report-only; zero code changes).
Audited revision: `c5ba992671e54e035458665f0faf92ea91595116` (origin/main,
post-c5ba9926 — DO step 4 satisfied: the hermes_python ladder and the
bootstrap.sh PYBIN ladder are both present and cross-checked below).

Method: `git grep -n "HERMES_HOME"` over every file type (.py, .sh,
.plist.template, .template, yaml, md), plus one-hop trace of every variable
that flows from it — `hermes_home`, `home`, `_hh`, `HERMES_DIR`, `PLUGINS`,
`HERMES_SCRIPTS`, `HERMES_PLUGINS`, `SCRIPTS_RUNTIME`, `_HERMES_AGENT_PATH`,
module constants (`rolelib.HERMES_HOME`, `kanban_live.HERMES_HOME`,
`cluster_template.HERMES_HOME`, `onboard.HERMES_HOME`) and `$HERMES_HOME` in
shell — to its JOIN SITE. Every JOIN SITE is classified; grep hits that are
pure prose/comments/docs/test-fixtures are excluded and counted in "coverage".

## Classification rules (as specified by the card)

- **ROOT-intent** — the joined path lives at the Hermes ROOT
  (`<root>/hermes-agent`, root `config.yaml`/`SOUL.md` when the writer intends
  the root home, root `skills`/`templates` install targets, kanban board state).
- **PROFILE-intent** — things Hermes itself reads/writes per-profile
  (`get_hermes_home()/plugins`, `get_hermes_home()/skills`,
  `<profile>/hooks`, `<profile>/scripts`, profile `config.yaml`, profile
  `state.db`, `memori.json`). A raw join is **CORRECT** here — explicitly a
  finding, not a defect.
- **AMBIGUOUS** — same line behaves differently by caller (daemon vs profile
  session vs installer); both behaviors named.

Verdict column: **OK** = intent matches the join; **MISCLASSIFIED** = ROOT
intent joined to the raw env home (the shipped class bug); **AMBIG** =
context-dependent; **WATCH** = correct today, one plausible change breaks it.

## Findings table

All line numbers verified at `c5ba9926`. "Ladder?" = whether
`hscc_daemon/hermes_python.hermes_venv_python`, `doctor.hermes_root_from_home`,
`rolelib._hermes_root` or `backup_util._hermes_root` already covers the site.

### A. The class-bug sites (ROOT intent joined to raw $HERMES_HOME)

| # | file:line | suffix joined | intent | verdict | ladder? | note |
|---|-----------|---------------|--------|---------|---------|------|
| 1 | hscc-bootstrap/ensure_review_feature.py:24-26 | `hermes-agent` | ROOT | MISCLASSIFIED | no | `HERMES_DIR` module constant. From a profile session the dir is absent → `_feature_state` returns `skipped_missing`, `ok=True` (L86-87) → bootstrap prints "ok kanban review feature: skipped_missing" while the PR #43425 feature is NEVER applied. Silent feature loss, boot-time-relevant. |
| 2 | hscc-bootstrap/apply_patches.py:101-102 | `hermes-agent` | ROOT | MISCLASSIFIED | no | `_hh` used only for `--check` without `--target`. From a profile session the check reports `{not found}` → `ok:false`, exit 1. bootstrap.sh:510 swallows it (`2>/dev/null` + grep) and warns "already patched / upstream drifted" — WRONG diagnosis; the real cause is the resolver. The `--target` path (L113) is caller-supplied and fine. |
| 3 | hscc-bootstrap/bootstrap.sh:509 (plan text :507) | `hermes-agent` | ROOT | MISCLASSIFIED | no | `HERMES_DIR="$HERMES_HOME/hermes-agent"` — the exact c5ba9926 sibling of the fixed PYBIN, left raw. From a profile session `--target <profile>/hermes-agent` → apply fails → warn; combined with #2 the whole patch step is a no-op with a misleading message. Bootstrap is always run by a human at a terminal, but they may well be inside a profile session (`hermes`-spawned shell). |
| 4 | hscc-bootstrap/enable_plugins.py:1231-1233 | `config.yaml` | ROOT (installer) | MISCLASSIFIED/AMBIG | no | `__main__` (bootstrap's only caller; bootstrap.sh:533). Intent is "wire the install's config". Profile session → reads/writes `<profile>/config.yaml` and the root config stays unseeded (the goal-judge sweep at L1210 itself resolves the root correctly via `_hermes_root(config_path)`). |
| 5 | hscc-bootstrap/install_soul.py:267-269 | `SOUL.md`, `config.yaml` | ROOT (installer) | MISCLASSIFIED/AMBIG | no | `__main__`. Same vector as #4: from a profile session the HSCC SOUL block lands in `<profile>/SOUL.md` and the personality in `<profile>/config.yaml`; the root home is skipped. |

### B. Correct-as-is PROFILE-intent (raw joins that MUST stay raw)

| # | file:line | suffix joined | intent | verdict | note |
|---|-----------|---------------|--------|---------|------|
| 6 | hscc-bootstrap/bootstrap.sh:18 (`PLUGINS`) | `plugins` | PROFILE | OK | Deliberate (comment L31-32): Hermes scans `get_hermes_home()/plugins`; widening it would make the deployed payload undiscoverable — c5ba9926 re-affirmed. |
| 7 | hscc-bootstrap/enable_plugins.py:54-57 (`HOOKS_DIR`, `CLUSTER_GUARD_DST`) | `hooks`, `cluster-guard.py` | PROFILE | OK (WATCH) | Bound at import from env — correct for bootstrap (which exports HERMES_HOME); the t_9462260b audit grounds per-profile hooks. Fragile: a profile-session import re-binds the constants (conftest `_CLUSTER_GUARD_SANDBOX` seam exists for exactly this). |
| 8 | hscc-bootstrap/install_payload.py:616 | `plugins` | PROFILE | OK | `PLUGINS` env (exported by bootstrap L18) else `~/.hermes/plugins`. |
| 9 | hscc-bootstrap/install_payload.py:478-500 (`_named_profile_plugins_dirs`) | `profiles/<n>/plugins` | PROFILE | OK | Derived from the passed `plugins_dir`, and deliberately no-ops when that dir is itself profile-scoped — the good pattern. |
| 10 | hscc-bootstrap/skill_guard.py:239/253 (`config.yaml`, `skills`), 406, 459-460 (`profiles`), 512-518 (marker), 536 | per-home scan | PROFILE-per-arg | OK | `hermes_home` is always an EXPLICIT ARG (root or profile), never ambient env; grounding comments at L434 cite `tools/skills_tool._skill_search_dirs`. |
| 11 | hscc-bootstrap/doctor.py:1556-1600 (`_budget_plugin_dirs`) | `plugins` | PROFILE | OK | t_ba14f77f fix: a profile home contributes ONLY `<profile>/plugins` (no root rescue). WATCH: L1587 trusts `PLUGINS` env first — a stray `PLUGINS` env in a caller's shell steers the row. |
| 12 | hscc-cluster/budget_watch.py:150-154 | `kanban/boards` (+root fallback) | root-state | OK | Tries the raw home then the profiles-grandparent root — the dual-probe idiom done right. |
| 13 | hscc-cluster/orch_message.py:231-236 | (leaf-name extract) | PROFILE | OK | Reads only the profile NAME out of the home, never a file join. |
| 14 | hscc_daemon/kanban_live.py:241-244 (`_state_db_path`) | `state.db`, `profiles/<p>/state.db` | arg-intent | OK | `hermes_home` is a caller arg; default `kanban_live.HERMES_HOME` (L59) is the DEFAULT constant `~/.hermes`, NOT the env — must never be changed into an env read. |
| 15 | hscc_daemon/hermes_python.py:57-155 (ladder) | `hermes-agent/venv/bin` | ROOT | OK | THE ladder: exported home → profiles-grandparent → `~/.hermes` → running venv; fail-closed None. |
| 16 | hscc_daemon/api_launchd.py:144-176 | (via ladder) | ROOT | OK | Fixed by c5ba9926; refusal lists every path searched. |
| 17 | hscc-bootstrap/bootstrap.sh:33-53 (PYBIN ladder) | `hermes-agent/venv/bin` | ROOT | OK | Fixed by c5ba9926; drift-guarded by hscc-bootstrap/tests/test_bootstrap_venv_ladder.py. |
| 18 | hscc-bootstrap/doctor.py:99-137 / 140-151 (`hermes_root_from_home`, `hermes_install_dir`) | `hermes-agent` | ROOT | OK | The ladder equivalent; `hermes_agent` join via candidates + existence test. |
| 19 | hscc-bootstrap/doctor.py:299-318 (`_hermes_ok`) | `hermes-agent` | ROOT | OK | Resolves through the ladder; fatal only when NO candidate carries an install. |
| 20 | hscc-bootstrap/doctor.py:154-169 (`_skills_home`) | (walk-up) | ROOT-for-audit | OK | One-level walk-up, deliberately NO `~/.hermes` fallback (hermeticity note in docstring). |
| 21 | hscc-bootstrap/doctor.py:173-251 (`_skill_findings`) | per-home `skills` | PROFILE-per-home | OK | Scans root + every profile home SEPARATELY (profile sessions see only their own roots — grounded comment L193-196). |
| 22 | hscc-bootstrap/doctor.py:778-781 (`_check_models_served`) | `config.yaml` | PROFILE | OK (AMBIG by design) | Worker mode resolves the worker's own config — correct for "what will THIS home serve". |
| 23 | hscc-bootstrap/backup_util.py:306-316, 351-360 | sweep roots + `profiles/<n>` | ROOT | OK | `_hermes_root` ladder + per-profile walk. |
| 24 | hscc-bootstrap/enable_plugins.py:682-722, 1210-1211 (goal-judge sweep) | `config.yaml`, `profiles/<n>/config.yaml` | ROOT | OK | L696 walks up from env (named use; documented). L1210 sweeps from `config_path`, deliberately NOT ambient env (t_267f9d88 class) — good. |
| 25 | hscc-roles/rolelib.py:11-35 | `profiles` | ROOT | OK | `_hermes_root` ladder; module constant `rolelib.HERMES_HOME` is the ROOT, documented at L30-33. |
| 26 | hscc-roles/role_soul_check.py:610 | profiles scan | ROOT | OK | `rolelib.PROFILES_DIR`; docstring L602 states the worker-env case is handled. |
| 27 | hscc-api/routes_memory.py:73-114 | `profiles/<p>/memories` | ROOT (profiles tree) | OK | t_7a16aed0 fix: resolver anchors on `get_default_hermes_root`, fallback hardcodes `~/.hermes/profiles`, explicitly NOT `$HERMES_HOME` (leaked env = the original bug). |
| 28 | hscc-daemon-adjacent: scripts/recover_worker_work.py:52-60, 199-201 | `kanban/boards/<b>/kanban.db` | ROOT (board state) | OK | Explicitly avoids ambient env ("even in a dispatched worker"); uses `HERMES_KANBAN_DB` env when set, else `~/.hermes` + `HERMES_KANBAN_HOME` respected elsewhere. |
| 29 | hscc_daemon/orch_waker.py:116, 699-709 | `profiles/<name>` | ROOT | OK | `DEFAULT_HERMES_HOME="~/.hermes"` constant — never the env. |
| 30 | memori/_paths.py:36-59 | `plugins/memori`, `memori.json` | PROFILE | OK | Mirrors Hermes precedence exactly: explicit arg → `get_hermes_home()` → `$HERMES_HOME` → platform default. |
| 31 | memori_byodb/__init__.py:258-277 + _client.py:74 | `memori_byodb.json`, `memori_byodb.db` | SHARED/ROOT (intentional) | OK | Cross-profile shared DB with explicit profile→default fallback chain, documented (2026-06-13 capture cutoff). |

### C. Ambiguous (invocation-dependent) — named both behaviors

| # | file:line | suffix | verdict | behavior under installer/root session vs profile session |
|---|-----------|--------|---------|----------------------------------------------------------|
| 32 | hscc-api/routes_profile.py:58-60 | `profiles` | AMBIG | API server with unset env (plain launchd unit — api_launchd writes HOME+PATH only): lists `~/.hermes/profiles` = correct. API spawned from a profile session / `env HERMES_HOME=…`: lists `<profile>/profiles` = double-nested, EMPTY — the GOAL_LEDGER provision-check double-nest class, still live on this route. `hermes profile list` itself anchors on the default root, so the API disagrees with the CLI. |
| 33 | hscc-api/routes_profile_editor.py:51-53 (+240-241) | `profiles` | AMBIG | Identical vector and identical divergence, plus the editor WRITES profile config.yaml through it (L240 `_resolve_profile_dir`). |
| 34 | hscc-api/routes_profile_editor.py:79-83 | `skills` | AMBIG | Scans env-joined `skills` as the "GLOBAL" dir: unset env → `~/.hermes/skills` (the real global); profile env → `<profile>/skills`, i.e. the global half of the preload picker silently disappears. |
| 35 | install/hscc-skills/hscc-model-onboard/scripts/onboard.py:41-43, 207-212, 317-323 | `config.yaml`, `profiles/<n>/config.yaml` | AMBIG | Run from a plain shell: snapshots/writes the root config + every profile config (intended). Run from a profile session: `PROFILES_DIR=<profile>/profiles` doesn't exist → per-profile `model.default` mirroring SKIPS every profile (silent partial onboard), and the "root" config it edits is the profile's. |
| 36 | hscc-skills/hscc.py:31, 50, 54, 56 | `skills`, `templates` (+fallback source) | AMBIG | `install-skills` target is the raw env home: installer session (unset env) → root `~/.hermes/skills` (the `configs/hscc-config.yaml.template:38` contract); profile session → `<profile>/skills` (per-profile skill dir — defensible). The L50 fallback source `HERMES_HOME/skills` is the same path it copies INTO when source discovery fails (self-copy risk, mitigated in practice because the repo layout always resolves). |
| 37 | hscc-bootstrap/install_review_skill.py:107-109 (`default_home`) + 135-160 (`skill_dirs`) | walk root + `profiles/*` | AMBIG (minor) | `default_home()` returns the raw env home, then `skill_dirs` walks `home/profiles`: profile env → walks `<profile>/profiles` (non-existent) → seeds ONLY the profile itself; root env → seeds root + every profile (the documented fan-out intent). |
| 38 | hscc_daemon/hscc.py:960-961 | `plugins/hscc-bootstrap/bootstrap.sh` | AMBIG (benign) | Candidate list order: source-root → … → env-joined plugins. Profile session finds the profile's deployed copy (works); unset env finds the root deployed copy (works). A profile that has no local copy falls through to the same path via `~/.hermes` in other candidates — fail-open OK. |
| 39 | scripts/install_dep_watcher.sh:24-25 + scripts/install_escalate_watcher.sh:25-26 | `scripts` | OK but fragile (AMBIG) | t_3aadd2c0 fixed the hardcode: the install dir and the `hermes cron create` binding both resolve from the SAME env → consistent in every invocation mode. Residual fragility: HERMES_HOME is load-bearing for BOTH sides; any future caller that sets it for one side only recreates the "17 consecutive failed runs" class. The `HERMES`/`HERMES_BIN` venv joins two lines below are hardcoded `~/.hermes` (ROOT-intent done safely). |

### D. Watch items (correct today, one change away)

| # | file:line | suffix | note |
|---|-----------|--------|------|
| 40 | hscc-cluster/cluster_template.py:34, 38, 42 | `config.yaml`, `profiles` | Deliberately IGNORES `$HERMES_HOME` (constant + `HSCC_PROFILES_DIR`). Correct: t_267f9d88 proved env-joining this module made `cluster apply` rewrite the ambient home's config — the comment "never ambient HERMES_HOME" is the contract; adding env support later would recreate the poisoning family. |
| 41 | hscc-project/flightdeck/commands/qa.py:68-79 (`qa_home`), commands/standup.py:380-386 | flightdeck state root | AMBIG-namespace: repurposes `HERMES_HOME` as the flightdeck sandbox root. Under the dispatcher (env = profile dir) flightdeck state SILENTLY migrates to `<profile>/…` instead of `~/.flightdeck`; documented as test-sandbox-only. Consider a dedicated `FLIGHTDECK_HOME` env. |
| 42 | hscc_daemon/launchd-setup.sh:47-56, systemd-setup.sh:47-56 | venv python | ROOT-intent ladder that does NOT consult `$HERMES_HOME` (uses `HSCC_PYBIN` → `~/.hermes/hermes-agent/venv/bin/python` → PATH …). Safe as-is; note it is a THIRD copy of the ladder rule — hermes_python would be the canonical source. |
| 43 | hscc_daemon/cron_watchers.py, plist templates (com.hermes.hscc_daemon.plist.template:21/47/51) | `plugins/hscc_daemon`, PATH, PYTHONPATH | Boot-time unit paths hardcoded `__HOME__/.hermes/...` (ROOT) — correct: units run with no HERMES_HOME; substituting the installer's env home would bind the daemon to a profile. `__HOME__` substitution is the right seam. |
| 44 | hscc-bootstrap/doctor.py:149-151, 309-310 | `hermes-agent` (failure detail) | Ladder-guarded; the raw join fires only when NO candidate carries an install, purely to name a path in the error. Harmless. |
| 45 | hscc-roles/hscc.py:270-277 (embedded loader), :498 | `HERMES_HOME=<profile>` set deliberately; `skills` source | The subprocess SETS env to a profile dir so Hermes' `get_config_path()` targets the profile — a correct, documented PROFILE-intent use (fresh subprocess, no parent poison). L498 source = `rolelib.HERMES_HOME/skills` (root skills source — ROOT-intent via ladder). OK. |
| 46 | hscc-project/flightdeck/core/project_lifecycle.py:600-679 | `hermes-agent/venv/bin` | ROOT-intent; candidates = raw env home THEN `Path.home()/.hermes` THEN PATH. Lacks the profiles-grandparent rung, but unlike api_launchd every candidate is VERIFIED by a `import hermes_state, hermes_cli.profiles` probe, so from a profile session it lands on `~/.hermes` (3rd) or PATH — no dead-end, no wrong interpreter. Not a ladder consumer (stdlib-only `hermes_python` cannot do the probing); noted for drift only. |

## Coverage (no join sites)

- `hscc-api/gateway_bridge.py:217`, `gateway_driver.py:164`,
  `routes_orchestrator.py:883`, `gateway_bridge` prose — documentation only;
  `?profile=` → server-side env mapping happens inside the gateway, no join.
- `memori/install.py` — thin delegation to `memori/_paths.py` (#30).
- `hscc_daemon/orch_waker.py` board enumeration — board DB discovery goes
  through hermes' own `kanban_db.kanban_home()` (`HERMES_KANBAN_HOME`), GAP-C-1.
- Tests, conftests, `tests_gateway/` probes, `docs/audits/demo_memory_regen.py`,
  `scripts/audits/ws_relay_final_verify.sh`, `scripts/repro_worker_toolset.py` —
  fixtures/scripts that SET env to throwaway or dispatcher homes (intended),
  not production join sites. `_pvb_tmp_home` litter already self-cleaned
  (t_a52ab9f4).
- `_archive/**`, `ios-app/docs/**`, CHANGELOG/changelog.d — prose/historical.

## Cross-check of the two fixed sites (DO step 4)

- `hscc_daemon/api_launchd.recommend_api_python` → delegates entirely to
  `hermes_python.hermes_venv_python()` (api_launchd.py:159); the old raw join is
  gone; refusal uses `hermes_python.describe_search()`. Covered by
  `hscc_daemon/tests/test_hermes_python.py` (19 cases) + the profile-scoped
  regression in `test_api_launchd.py`.
- `bootstrap.sh` PYBIN ladder (bootstrap.sh:33-53) walks exported home →
  profiles-grandparent → `~/.hermes` → PATH, drift-pinned by
  `hscc-bootstrap/tests/test_bootstrap_venv_ladder.py`.
- Confirmed stale doc references: `bootstrap._api_python` still appears in no
  code (`git grep _api_python` → only prose), as c5ba9926 stated.

## MISCLASSIFIED / AMBIGUOUS list (actionable, by severity)

Severity = what breaks and when. "profile session" = the dispatched-worker /
any-`hermes`-spawned-shell normal mode.

1. **ensure_review_feature.py:24-25 — HIGH (silent feature loss).** From any
   profile session the whole kanban-review re-apply step reports
   `skipped_missing`/ok and does nothing: on a fresh or drifted install the
   PR #43425 feature silently never returns, and bootstrap shows a green-ish
   line. Minimal fix: resolve `HERMES_DIR` through a root ladder
   (`hermes_root_from_home`-style candidate walk for `hermes-agent`); report
   `skipped_missing` only when NO candidate carries the checkout.
2. **bootstrap.sh:509 + apply_patches.py:101-102 — HIGH (patch step
   silently no-ops + wrong diagnosis).** `apply_patches --check` without
   `--target` checks `<profile>/hermes-agent` (never exists) →
   bootstrap.sh:510 warns "already patched / drifted", skips `--target`
   apply — misleading the operator forever about why hermes patches are
   "not applicable". Minimal fix: same ladder in `apply_patches --check`
   default; `HERMES_DIR` in bootstrap.sh derived from the resolved
   `PYBIN`'s grandparent-of-venv (it is already ladder-resolved two lines
   above), or drop the env join and pass `--target` unconditionally.
3. **enable_plugins.py:1231 + install_soul.py:267 — MEDIUM (installer
   writes land in the wrong tree).** Run from a profile session, bootstrap
   wires the PROFILE's config/SOUL instead of the root install's — the root
   config.yaml (the general/`default` home, and the file the doctor's
   models-served + wiring rows check for the root) stays unseeded — the
   goal-judge sweep at enable_plugins.py:1210 DOES walk a profile path up to
   the root correctly, so per-profile configs still get wired; it is the
   ROOT config.yaml itself that is skipped. Minimal fix: resolve the home
   with the profiles-grandparent rule at `__main__` (document: installer
   acts on the ROOT home; use `--home` for an intentional profile wire).
4. **routes_profile.py:59 + routes_profile_editor.py:52/80 — MEDIUM
   (API-vs-CLI divergence, empty/double-nested profile lists).** If the API
   process ever carries a profile-scoped `HERMES_HOME` (spawned from a
   session, launched under one, or a future plist that adds the env — the
   current api plist sets only HOME/PATH, which is the accidental guard),
   `GET /v1/profiles` and the whole editor surface read
   `<profile>/profiles` = empty; the editor's path-traversal jail roots on
   the same dir. Minimal fix: mirror routes_memory.py — resolve via
   `hermes_cli.profiles` / `get_default_hermes_root`, ignore ambient env.
5. **onboard.py:41-43 — MEDIUM (silent partial model onboarding).** From a
   profile session `PROFILES_DIR` doesn't exist → the per-profile
   `model.default` mirror (L317-323) skips EVERY profile without any error.
   Minimal fix: root ladder on the home (or reuse `rolelib._hermes_root`),
   and report per-profile results as applied/skipped-with-reason.
6. **install_review_skill.py:109 — LOW (fan-out collapses).** `default_home`
   raw env → `--skill all` from a profile seeds only that profile instead of
   root+all profiles. Minimal fix: walk a profile-scoped env home up one
   level before `skill_dirs` (or reuse `discover_profiles` on the root).
7. **hscc-skills/hscc.py install-skills — LOW-AMBIG (document the choice).**
   Root vs profile skills dir depends purely on the invoking env; add a
   printed line naming the resolved destination and which mode is active, so
   a profile-session install is never mistaken for a fleet-wide one.
8. **flightdeck qa.py:74 / standup.py:385 — LOW-AMBIG (namespace
   hijack).** `HERMES_HOME` doubles as the flightdeck sandbox root, so worker
   runs silently relocate flightdeck state under the profile. Minimal fix:
   prefer `FLIGHTDECK_HOME`, fall back to `HERMES_HOME` for test compat.
9. **Watch (no action needed now):** enable_plugins.py:54-57 (import-time
   env binding — keep the conftest sandbox seam); cluster_template.py
   (do NOT add HERMES_HOME support); launchd-setup/systemd-setup third
   ladder copy (canonicalize to hermes_python someday); kanban_live.py:59
   default constant (must remain a constant); doctor.py:1587 `PLUGINS`
   env trust.

## Summary counts

- 46 classified join sites: **5 MISCLASSIFIED/AMBIG-broken** (items 1-3,
  4-5 in the actionable list), 8 AMBIGUOUS, 8 WATCH, 25 explicit
  correct-as-is (PROFILE-intent raw joins + ladder-covered ROOT joins).
- The three shipped-October members of the class (api_launchd, bootstrap
  PYBIN, profile-config poisoning) are confirmed fixed at this revision.
- The remaining un-fixed members are the three bootstrap `hermes-agent`
  joins (ensure_review_feature, apply_patches `--check`, bootstrap.sh:509)
  and the two bootstrap `__main__` config joins — same rule, same
  profiles-grandparent fix, no ladder consumer yet.
