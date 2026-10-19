# README audit — repo-wide (t_6e1e8443, 2026-10-15)

Deliverable of the docs-lane audit card. **Findings + target-state spec only —
no README edits were made in this card.** The rewrite cards (R1–R7) fan out
from this document.

Base: `main` @ `e2278355` (2.9.1). Lanes read as the help/nav surface they leave
behind (neither merged into main yet):
- CLI-help lane `origin/cli-help-2026-10-15` @ `43aa08ad` (audit gate + F1 +
  2 fixes). F1 tip `ff20706c` (branch `wt/t_20261016_f1_cli_help_f1`): `hscc
  project` help now speaks `hscc project ...`, dead verbs hidden, rendered
  strings reworded to "hscc-project home" (on-disk `~/.flightdeck` unchanged).
- iOS drawer lane `origin/fix/nav-graph-2026-10-15` @ `35c75925`: XCUITest
  nav-graph gate green on an iPhone-16-class simulator (9/9 walk + negative
  self-check), app IA is now **AppShell drawer over one NavigationStack — 17
  drawer rows** (Sidebar.swift), the three-tab TabView is gone.

## Measurements used throughout (don't re-guess these)

- **Suite count (for the "N tests" badge)**: `bash scripts/run_tests.sh` on this
  worktree tip (docs-only change, base e2278355), Hermes venv py3.11,
  `env -u CONDA_*`: **5245 passed, 1 skipped, 11 legs, ALL GREEN**
  (bootstrap 451, commands 69, roles 135, cluster 495, project 1411, daemon
  1477, sparkrun-hermes 12, api 938+1 skip, memori_byodb 11, scripts 120,
  .github/scripts 126). Quote as **"5,000+ tests"** or "**5245 tests**".
- `VERSION` = **2.9.1**; `hscc --help` prints `v2.9.1`.
- **Registry projects** (`~/.flightdeck/registry.yaml`, 13, live):
  `hscc`, `ecofire-bc`, `ecofire-app`, `sphoin`, `soconn`, `flosana`,
  `powerbi`, `efsdriver`, `grid`, `radio`, `pickolo`, `pom`, `pontaje`.
  (Old docs say "twelve"; the CLI audit's REAL EXAMPLE DATA predates `pontaje`.)
- **Templates** (`hscc template list`, live, 22): `1node-orchestrator-only`,
  `2node-coding`, `2node-fast`, `3node-balanced`, `3node-coding`,
  `4node-balanced`, `4node-coding`, `4node-coding-plus-fast`,
  `4node-colocated-dual`, `4node-deepseek-v4-orchestrator`,
  `4node-dsv4-plus-coding`, `4node-dual-dsv4`, `4node-dual-orch`,
  `4node-flash-next-all`, `4node-quality`, `4node-review-heavy`,
  `4node-throughput`, `8node-coding`, `8node-coding-plus-fast`,
  `colocated-two-models`, `hscc-live`, `single-family`.
- **Roles**: 25 spec files in `hscc-roles/roles/` (incl. `worker`,
  `ios-engineer`, `ecofire-bc-engineer` — the roster lists in docs omit the
  last two). "22+ role profiles" remains true.
- **MCP tools**: `hscc-project/flightdeck/mcp_server.py` has **25**
  `@mcp.tool()` decorators (docs say 24).
- **Slash commands**: `hscc-commands/__init__.py` registers exactly the 12 the
  README table lists (`/cluster /status /orch-restart /cluster-restart
  /cluster-reboot /cluster-down /cluster-docker-prune /cluster-apt-upgrade
  /cluster-prune /heal /template /workers-up`).
- **Decode fixtures**: **54** `*.json` under
  `ios-app/scripts/model_decode_check/fixtures/` (docs say 26).
- **Address placeholders sanctioned by repo policy** (`scripts/README.md:76`):
  `10.0.0.x` LAN, `100.64.0.1` tailnet (`100.64.0.0/24` fixture block). All
  doc quotes below use only those.
- All live `hscc`/`hscc project` smoke runs quoted here were executed
  2026-10-16 on this host; mutating commands were parse-only (`--help`) —
  exceptions recorded in §Smoke log.

## 1. Per-file verdict table

Every `*readme*` file in the tree: 25 matched `find -iname '*readme*'`; 3 of
them are prior audit reports (`docs/audits/readme-review-t_0ae232a4.md`,
`readme-review-t_d90de5b6.md`, `docs/review_part2_5_READMEs.md`) — not user
docs, covered in §4, verdict KEEP (provenance). Dates from `git log -1`.
"Verdict" = target action for the fan-out; owners per §5.

| file | lines | last touched | verdict | key evidence (file:line) |
|---|---|---|---|---|
| `README.md` | 343 | e65a12da 2026-10-13 | **REWRITE → <150 lines (R1)** | see §1.1 detail block |
| `ios-app/README.md` | 538 | 3687f2fa 2026-10-14 | **REWRITE (R2)** | status banner `:10-20` "UNBUILT, UNTESTED AT RUNTIME ... never been run" — false on the drawer-lane tip: XCUITest nav-graph walk green on iPhone-16 sim + 12×2 capture_screens + generic iOS build (t_810ab152). IA `:24-31` "three tabs: Projects · Cluster · Settings" — replaced by AppShell drawer, 17 drawer rows (`ContentView.swift:5-6` on `fix/nav-graph-2026-10-15`). `:50` "twelve projects" — registry has 13. `:177-181` "26 committed fixtures 26/26" — 54 fixtures today. |
| `hscc-project/README.md` | 351 | e6622d45 2026-09-25 | **REWRITE (R3)** | 16 `flightdeck` mentions (`:1 :3 :6-14 :70 :144-145 :235 :286-298 :309`). Sample outputs `:69-87` show `flightdeck home: ~/.flightdeck` — F1 reworded these renders to "hscc-project home" (t_5005a067), so the transcript is now wrong vs post-F1 reality. `:287` "24 tools" — code has 25. `:298` registers MCP client as `"flightdeck": {"command": "flightdeck-mcp"}` — the console script exists in `hscc-project/pyproject.toml [project.scripts]` but is not installed by bootstrap; flag as technical-necessity case (§3). |
| `hscc-project/docs/README.md` | 43 | e6622d45 2026-09-25 | **REWRITE (small, R3)** | title `:1` "Flightdeck docs — index"; `:31` "flightdeck MCP"; `:18-19` `~/.flightdeck/*` (technical necessity — those are the on-disk paths). All 12 listed files exist (link check green). |
| `hscc-project/docs/assets/README.md` | 6 | 9926c650 2026-08-19 | **MERGE-INTO-PARENT (R3)** | 6 lines about one `banner.png`; fold into `hscc-project/docs/README.md`; drop "FlightDeck banner" wording (`:3`). Banner file exists (495 KB). |
| `hscc-cli/README.md` | 42 | 6143929f 2026-09-25 | **KEEP (R4 polish)** | entry point `hscc = "hscc_cli:main"` verified in `hscc-cli/pyproject.toml`; venv/shebang rationale matches `hscc-bootstrap/install_cli.py`. No stale claim found. |
| `hscc-cluster/README.md` | 84 | 0a9f7299 2026-09-25 | **REWRITE (R4, short template)** | `:66` "the worker span's primary vLLM on the `.247` node" — bare operator-node id; must become placeholder per policy (`scripts/README.md:76`). Long `Delegation routing / probe-before-write` essay (`:53-80`) is implementation lore — belongs in code docstring or `docs/`, not the plugin front page. Rest accurate (CLI merged form `:42-46`). |
| `hscc-commands/README.md` | 47 | 9db11cb6 2026-09-25 | **KEEP** | command table `:13-26` matches the 12 `register_command(name=...)` sites exactly; confirm-first semantics verified in the same file. |
| `hscc_daemon/README.md` | 124 | 23a65f3b 2026-09-25 | **KEEP + REFRESH (R4)** | module table `:20-34` lists 13 of the 42 real modules in `hscc_daemon/`. Missing entirely: `heal_lease.py`, `heal_breaker.py`, `launch_guard.py` (the HEAL_CASCADE 2026-10-14 trio), `cluster_live.py` (**operator-owned, read-only for docs cards — quote it, never edit**), `kanban_blocked.py`/`kanban_cli.py`, `escalate*.py`, `autodown*.py`, `kill_switch.py`, `qr_code.py`. Safety section `:103-114` predates lease/breaker/guard — the most operationally significant daemon change since the rewrite is undocumented. |
| `hscc-roles/README.md` | 64 | d36d2d02 2026-09-25 | **KEEP + small fix (R4)** | roster `:53-57` omits `ios-engineer` and `ecofire-bc-engineer` (present in `roles/`); `:25 :49` `~/.flightdeck/registry.yaml` = technical necessity (actual on-disk path; keep, reword context per F1's "hscc-project home" convention). CLI list `:9-18` matches `hscc.py` (generate/create/list/validate/autonomy/orch/orch-all — all smoke-ran). |
| `hscc-bootstrap/README.md` | 67 | 0a464d46 2026-10-08 | **KEEP + small fix (R5)** | `:67` "266 tests" — leg count today 451. All stage files in `:48-60` exist; flags verified. |
| `hscc-skills/README.md` | 18 | fbb9b054 2026-06-13 | **KEEP** | commands match `hscc.py`; source-walkup claim matches code. |
| `install/README.md` | 21 | 3ee5a0a2 2026-06-12 | **KEEP** | `install/hscc-skills/` listing matches disk (13 skill dirs incl. `hscc-lessons`). |
| `sparkrun-hermes/README.md` | 11 | 7bb6968e 2026-09-25 | **KEEP** | guarded-passthrough claim matches `__init__.py` (command must start `sparkrun`). |
| `memori/README.md` | 5 | fbb9b054 2026-06-13 | **GROW (R5)** | stub; `register_memory_provider` confirmed in `memori/__init__.py:450`. |
| `memori_byodb/README.md` | 5 | fbb9b054 2026-06-13 | **GROW (R5)** | stub; `register_memory_provider` at `memori_byodb/__init__.py:486`. Claim "NAS-backed store" **unverified** — the provider resolves a local SQLite store; writer must check code and keep/delete the NAS framing. |
| `hscc-cluster/templates/README.md` | 302 | 038bc679 2026-09-25 | **KEEP + REFRESH (R5, templates owner)** | the deep reference; v3 format + validation layers match code. Refresh: library tables `:184-201` list the pre-September generation next to the seven 2026-09 templates — `hscc template list` shows 22 names (full list above); reconcile the two tables + VRAM numbers against `sparkrun show`. `:126 :130 :173 :175` use sanctioned `10.0.0.x` placeholders — OK. |
| `hscc-api/README.md` | 147 | e65a12da 2026-10-13 | **KEEP + fix (R5)** | `:111` "`hscc doctor` (tmux/REPL section)" — **no such verb**: `hscc doctor` prints `Unknown command: doctor`; the tmux/REPL section lives in **`hscc project doctor`** (verified live: "iOS chat reachability (tmux/REPL)" panel). CLI table `:57-61` omits `hscc api install-service` (exists — seen in `hscc api --help`). |
| `scripts/README.md` | 122 | 0a464d46 2026-10-08 | **KEEP + generalize (R5)** | `:96` "currently Qwen3.6-35B-A3B-NVFP4 on the orchestrator head" — volatile per-apply fact (live fleet currently runs qwen3.8-flash-next recipes per `hscc cluster status`); make it model-agnostic. `--deliver desktop` trap correctly documented (`:50-51`). |
| `changelog.d/README.md` | 153 | 8b6f5939 2026-10-06 | **KEEP** | command table `:101-111` matches `changelog_fragments.py` COMMANDS (check/render/sync/list/add/rescue/release); migration script `scripts/migrate_changelog_fragments.py` exists. |
| `docs/README.md` | 17 | b735328c 2026-10-06 | **REWRITE (R6)** | claims verified (superpowers gitignore at `.gitignore:54`; CHANGELOG pointer rule true) but indexes **3 of 155** files under `docs/` (`find docs -type f | wc -l`). Enumeration in §2.3. |
| `ios-app/scripts/model_decode_check/fixtures/README.md` | 120 | bfd596a7 2026-09-27 | **KEEP + count fix (R5)** | `:40` "All 26 files" — **54** JSON fixtures now; capture date `:3` "2026-08-27" predates the t_ca133e7d additions the same file describes. Sanitisation table `:18` is the house convention example — cite it. |

Non-README files the fan-out must NOT touch: `docs/audits/README_REVIEW_2026-10-15.md` (this doc), `docs/audits/CLI_HELP_2026-10-15.md` (CLI lane), `hscc_daemon/cluster_live.py` + `tests/test_cluster_live.py` (operator-owned per orchestrator note).

### 1.1 `README.md` detail (the front page)

Stale/wrong claims, at file:line:

- `:11` version badge **v2.1.1** — actual `VERSION` = 2.9.1. (First regression
  of exactly this badge: t_d90de5b6 fixed 1.17.1→2.1.1 on 2026-09-25; nothing
  keeps it honest. The R7 lint card must pin badge == VERSION.)
- `:13-14` badges "**pure-stdlib** ... no pip dependencies" + "**1000+ tests**".
  Wrong twice: `requirements.txt` declares **PyYAML, rich, psutil** (and
  `hscc_daemon/cli_theme.py:25-29` imports `rich.*` at top level); suite is
  5245. The same claim repeats `:16` ("pure-stdlib Python plugins — no pip
  dependencies"), `:65` ("Python 3 (stdlib only)"), `:335`.
- `:86` "**`hscc doctor`** reports each project's REPL as in-tmux / outside-tmux
  / idle" — `hscc doctor` does not exist (`Unknown command: doctor`); real
  command **`hscc project doctor`** (tmux/REPL section verified live). Same
  error in `hscc-api/README.md:111`.
- `:119` `hscc project --help  # full flightdeck command list under the project verb`
  — caption sells the dead name; the command itself works. Same word at
  `:125` ("formerly the standalone `flightdeck` tool" — historical, acceptable
  inside one sentence if kept, but operator rule kills it) and `:129`.
- `:145-193` cluster topology + serving + template deep-dive duplicates
  `hscc-cluster/templates/README.md` (v3/routing/validation all already there —
  the root block is a lossy copy).
- `:197-234` idle-autodown chapter (~38 lines incl. cron-classification
  essays `:214-216`, messages-while-down `:218-220`, never-do `:222-232`) —
  `docs/design/idle-autodown.md` already exists as the design doc.
- `:238-274` "The fleet" (roles/review/autonomy/escalation) — mid-detail that
  belongs in a docs/ page; root keeps 3 bullets + link.
- `:278-297` plugin table **omits four shipped components**: `hscc-project/`
  (the `hscc project` domain), `hscc-api/` (the HTTP API backing the iOS app),
  `hscc-cli/`, `ios-app/`. Also no `changelog.d/` in Layout `:306-331`.
- Missing entirely (operator list, re-verified): iOS app (only a tmux aside),
  `hscc cluster monitor --live` (new in 2.9.1, PR #32), the HEAL_CASCADE trio
  (heal lease / heal breaker / launch guard), and any "start here" path —
  quickstart `:92-101` never says what to run first after bootstrap beyond
  scattered verbs.
- `:339` `launchctl kickstart -k gui/$UID/ai.hermes.gateway` — a mutating
  system command in a Conventions bullet; fine as doc lore, wrong on the front
  page (moves to `docs/FLEET.md` or `docs/OPS.md` per §2).
- Link check: every relative link in this file resolves (verified mechanically).

## 2. Target-state spec (what the rewrite cards execute)

### 2.1 Root `README.md` (R1) — < ~150 lines

1. **3-line "what this is"** (keep current `:7-9` — it's good), badges fixed:
   version from `VERSION`, "5,000+ tests", **drop the pure-stdlib badge** →
   "pure-Python, 3 pinned deps (PyYAML/rich/psutil)".
2. **One ASCII stack diagram** (Sparks → sparkrun → proxy(:4000) → Hermes
   kanban → workers; daemon watching; `hscc api` → iOS over tailnet), e.g.:

   ```
   DGX Spark nodes (GB10) ── vLLM units ── sparkrun ── worker proxy :4000
        ▲  self-heal (lease+breaker+guard), monitor --live      │
        │                                                       ▼
   hscc daemon (launchd) ── Hermes kanban dispatcher ── role workers (worktrees)
        │                                                       ▲
   hscc api :8787/8788 (bearer token, tailnet) ── iOS app (QR pair)
   ```
3. **Quickstart** (all commands smoke-verified §6): clone → `bootstrap.sh`
   `--yes` → `hscc status` → `hscc template list` / `preview` /
   `apply <name> --confirm` → `hscc project standup` → `hscc api start
   --tailscale` → open the iOS app (one line; details → `ios-app/README.md`).
4. **Common tasks** table (~10 rows): daily digest, review a card, fleet
   health, live monitor (`hscc cluster monitor --live`), templates,
   autodown (5-line summary + link to `docs/design/idle-autodown.md`), API.
5. **Components table** — add `hscc-project`, `hscc-api`, `hscc-cli`,
   `ios-app`, `changelog.d`. Trimmed Layout tree (keep 14 lines, add the 5
   missing dirs).
6. **Chunk destinations (nothing lost):**
   - `:145-193` topology/serving/template deep-dive → **DELETE from root, link
     to** `hscc-cluster/templates/README.md` (content already lives there; R5
     reconciles its two library tables against the live 22-template list) and
     `docs/DESIGN-template-explicit-placement.md` (exists).
   - `:197-234` autodown detail → **append to** `docs/design/idle-autodown.md`
     (operator-facing sections "cron classification", "messages while down",
     "never-do list"); root keeps 5 lines + link.
   - `:238-274` fleet org → **new `docs/FLEET.md`** (roles, SOUL layering,
     review loop, autonomy flag, escalation watcher incl. the `--deliver
     desktop` explanation + `launchctl kickstart` convention line).
   - `:43-59` "Why we built this" + gap table → **new `docs/WHY.md`** (root
     keeps the ELI5 paragraph).
7. **Commands quoted** must be the R7-lint-approved set (§3), using real
   template/registry names from Measurements.

### 2.2 Plugin README template (R4 — the five plugin files)

Max ~45 lines each: what it is (2 lines) / when you touch it / how to test
(exact command) / links. No changelog-style history. `hscc_daemon/README.md`
keeps its state-file + safety-guarantee tables (genuinely useful) but must add
the heal-lease / heal-breaker / launch-guard trio + `cluster_live.py` (one
row each, link `docs/audits/HEAL_CASCADE_2026-10-14.md`); its module table
says "core modules" rather than pretending completeness.

### 2.3 `docs/README.md` (R6)

Real index grouped: **Reference** (`API.md`, `PROJECT-COMMANDS.md`,
`COMPAT-7.30.md`, `hermes-017-new-features.md`) · **Design** (`DESIGN-api.md`,
`DESIGN-template-explicit-placement.md`, `design/*` incl. `idle-autodown.md`,
`IOS_REDESIGN_2026-10-14.md`, `2026-10-02-session-ownership-handoff.md`) ·
**Plans/roadmap** (`ROADMAP.md`, `ROADMAP.draft.md`, `HANDOFF.md`) ·
**Investigations** (`INVESTIGATION-*.md`, `bootstrap-audit-*.md`) ·
**Audits/** (one paragraph: working directory of per-card audit reports —
point at `README_REVIEW_2026-10-15.md`, `CLI_HELP_2026-10-15.md`,
`HEAL_CASCADE_2026-10-14.md`, `IOS_CHAT_REPORT_2026-10-14.md` as the live
ones; do not enumerate the ~100 per-card files, say how to find them) ·
**Examples** (`template-examples/`) · **Verification** (`verification/WS3-*`).
Keep the changelog.d pointer paragraph (it's load-bearing) but fix the
"this deployed copy of docs/" aside (untrue in the repo tree — the repo root
*does* carry `changelog.d/`; the sentence only made sense for the installed
runtime copy).

### 2.4 `ios-app/README.md` (R2)

On top: **5-line quickstart** — `brew install xcodegen`; `cd ios-app &&
scripts/generate.sh` (the file's own `:300` warning stands: **use
`scripts/generate.sh`, NOT bare `xcodegen generate`** — three targets need the
team; operator's "pick team" step is satisfied by the script's auto-detect +
`HSCC_DEVELOPMENT_TEAM=` override); `open HSCC.xcodeproj`; Run; **pair the app
by scanning the QR that `hscc api status` prints** (token is in the QR — treat
like a password).
Status banner `:10-20` rewrite: the app **has** run in CI-style gates — XCUITest
nav-graph walk green on an iPhone-16-class simulator (17/17 drawer rows,
fixture-seeded), generic iOS build green, decode checks green — but Siri
intents / widget / Live Activity **runtime** still need a signed device build,
and release builds are sideload-only. IA section: drawer (AppShell, 17 rows),
not three tabs; projects count 13.
History disposition:
- `:175-235` "End-to-end review" table + "the two fixes this pass made" →
  **move to `ios-app/docs/E2E-API-REVIEW-2026-08-27.md`** (dated evidence,
  still useful), update the fixture count claim (26→54) when moving.
- `:525-538` "Scope of later phases" (B3/B4/B5/C5/C6 strikethroughs) →
  **DELETE** (git has it; `docs/design/IOS_REDESIGN_2026-10-14.md` holds the
  current IA).
- `:237-249` "Honest limits" → KEEP, trimmed to current truth.
- `:366-401` sideload/7-day-cert + `:402-497` tailscale/token/security → KEEP
  (instructions, not history; the `docs/SIDELOAD-TAILSCALE-SECURITY.md`
  duplicate is already linked at `:513` — cross-link, don't delete either).

### 2.5 `hscc-project/README.md` (R3)

Keep the structure (it's a good user doc) but re-title to "**hscc project —
project/kanban control**"; banner image may stay (asset exists). All sample
transcripts re-baselined against the **post-F1 renders** ("hscc-project home",
`hscc project project ...` doubled form where the parser requires it — copy
the canonical examples from `docs/audits/CLI_HELP_2026-10-15.md` §REAL EXAMPLE
DATA + F1 commit `ff20706c`). MCP section: "25 tools"; the client-registration
snippet stays `flightdeck-mcp` **only** because that's the packaged console
script name (`hscc-project/pyproject.toml [project.scripts]`), with a one-line
note; every other "flightdeck" becomes `hscc project` or `hscc-project/`.
The "Why this exists" anecdote (`:315-325`, the 2026-08-09 board-that-lied
story) — cut to 3 lines; the full story already lives in
`hscc-project/docs/INCIDENTS.md`.

## 3. Command inventory (feeds R7 lint + all writers)

Every backticked `hscc*`/`hermes*`/`sparkrun*` command quoted in a README, per
file. Markers: **R** = read-only, run for real during this audit (rc=0 unless
noted); **P** = parse-only (`--help`, never executed); **M** = mutating,
never executed here; **X** = broken today (fix before quoting).

| file | commands |
|---|---|
| `README.md` | R: `hscc status` `verify` `stats` `throughput` `autoscale` `escalate` `template list` `template preview 4node-coding` `template validate 4node-balanced --structural-only` `template status` `cluster status` `cluster hosts` `api status` `help` `help advanced` `help project` `project standup` `project doctor` `kanban blocked` `autodown status` · P: `hscc template apply <name> --confirm` `cluster monitor --live` `cluster down/up --dry-run` `api start [--tailscale]` `autodown enable/wake/cancel/disable` `check <stream>` `triggers` `notify` `start/stop/install/uninstall/plist/log` · P(external): `sparkrun run <recipe>.yaml --cluster <name> --hosts <node-ip> --port 8000 --ensure` `sparkrun cluster list` `sparkrun show` · P(roles): `python3 ~/.hermes/plugins/hscc-roles/hscc.py generate/create/list/autonomy` · **X**: `hscc doctor` (`:86`) |
| `hscc-project/README.md` | R: `hscc project init` `project sync` `standup` `why <card>` `review <card>` `review --queue` `qa` `doctor` · P: all others incl. `ingest` `roadmap adopt` `decompose` `start` `report` `--apply` variants · (post-F1 canonical example forms in the CLI audit doc) |
| `hscc_daemon/README.md` | R: `hscc status` `check [stream]` `watch` `triggers` `log` · P: `start` `stop` `install` `uninstall` `plist` `notify` `start-daemon` |
| `hscc-cluster/templates/README.md` | R: `hscc template list` `template validate 4node-coding` (name exists) · P: `template preview/apply 4node-coding --confirm` · P(external): `sparkrun show`, `hf download` (staging note) |
| `hscc-cli/README.md` | P: `~/.hermes/hermes-agent/venv/bin/python -m pip install ./hscc-cli` |
| `hscc-bootstrap/README.md` | P: `hscc-bootstrap/bootstrap.sh` + flags (`--yes --force --no-backup --skip-*`) |
| `hscc-api/README.md` | R: `hscc api status` · P: `api start [--tailscale --bind --port --no-qr]` `api stop` **missing from table: `api install-service`** · **X**: `hscc doctor` (`:111`) |
| `scripts/README.md` | R: `python3 scripts/address_guard.py --staged/--tracked` · P: `hermes cron create 'every 5m' --name ... --no-agent --script ...` (delivery note already fixed), `scripts/install_dep_watcher.sh [--uninstall]` |
| `changelog.d/README.md` | R: `python3 scripts/changelog_fragments.py list` (ran, "0 entries" — clean tree) · P: `check` `render` `sync` `add` `rescue` `release --version V` |
| `hscc-skills/README.md` | P: `hscc.py install/install-skills/status/uninstall` |
| `ios-app/README.md` | R: `hscc api status` · P: `hscc api start --tailscale` `stop` · P: `brew install xcodegen` `scripts/generate.sh` `xcodebuild -showBuildSettings` `cat ~/.hscc/api-token` (secret — keep the file's "never print" note) |
| `hscc-roles/README.md` | R: `python3 hscc-roles/hscc.py list` `autonomy` (live: "on") · P: `generate` `create` `validate` `orch` `orch-all` |

R7 lint rules: (1) every fenced `sh`/`bash` block + inline `hscc …` command
parses through the CLI audit's argparse oracle — the harness already exists in
`scripts/hscc_cli_help_audit.py` (ExampleParser); extend it to glob
`**/README.md` + `docs/*.md`; external (`sparkrun`/`brew`/`hermes`/`pip`)
commands are allow-listed strings, not parsed by the hscc oracle. (2) badge
version == `VERSION`. (3) zero literal `flightdeck` in user-facing docs
(README.md + `*/README.md` + `docs/README.md`), except the §3.1 ledger paths;
case-insensitive; `--flightdeck`-style flags in code are out of scope. (4) no
command in the X column.

### 3.1 `flightdeck` survival ledger (technical necessity — each case flagged)

Allowed to survive (with rationale) after the wave:
1. `hscc-project/flightdeck/` package path + `flightdeck-mcp` console script —
   the actual importable/package names (`hscc-project/pyproject.toml`).
2. `~/.flightdeck/registry.yaml` / `~/.flightdeck/config.yaml` — real on-disk
   paths (F1's decision: on-disk dir unchanged; docs say "hscc-project home"
   and may show the path once when naming the file).
3. Historical document *titles* cited as provenance: `docs/INVESTIGATION-flightdeck-port.md`,
   `docs/PROJECT-COMMANDS.md`'s mapping table (its purpose is the old↔new
   mapping), prior audit reports in `docs/audits/`.
Everything else (root `:119/:125/:129`, `hscc-project/README.md` 13 more,
`hscc-project/docs/README.md:1/:31`, `assets/README.md:3`) is user-facing
prose and must die. `hscc --help`'s own "(flightdeck)" blurb
(`hscc_daemon/hscc.py:326/386`) is **F5's** work (not in the R-wave).

## 4. Prior-audit reconciliation

| prior finding | where | status today | action |
|---|---|---|---|
| F1: badge 1.17.1→2.1.1 | t_d90de5b6 | **REGRESSED AGAIN** — badge now 2.1.1 vs VERSION 2.9.1 (`README.md:11`) | R1 fixes; R7 lints badge==VERSION permanently |
| F2: escalation = desktop, not Telegram | t_d90de5b6 | **DONE** (`README.md:270-272` matches `escalate_watcher_run.py:7,83-87` — direct desktop notify, `--deliver desktop` correctly called a no-op) | none |
| `hscc template preview` env-fail note | t_d90de5b6 | informational, cluster-dependent | none |
| hscc-project rewrite + 24 MCP tools | t_0ae232a4 | **DONE then drifted** — rewrite landed; tools now **25**; 16 flightdeck mentions remain (by design then, banned now); sample renders stale vs F1 | R3 |
| docs/README + hscc-skills verified | t_0ae232a4 | docs/README still under-indexes docs/ | R6 |
| Follow-ups: telegram in config.example.yaml | t_0ae232a4 | **CLOSED** — `hscc-project/docs/config.example.yaml:9` now states the Telegram surface was removed; `registry.example.yaml:16-19` marks topic fields legacy | none |
| Follow-ups: telegram remnants in CLI help strings | t_0ae232a4 | folded into the CLI lane (F-cards) | CLI lane owns |
| daemon README rewrite | t_6b51ef5b | **DONE then drifted** — accurate 2026-09-25, missing the entire 2026-10-14 heal trio + `cluster_live.py` | R4 |
| scripts/README LAN scrub + `--deliver desktop` | t_6b51ef5b | **DONE** (verified `:76 :50-51`); model-name line `:96` newly stale | R5 generalize |
| ios port fix (8787 default / 8788 override) | t_6b51ef5b | **DONE** and still true (live `hscc api status` shows 8788) | keep |
| t_1c1bf5f1 `--deliver desktop` silent no-op | t_6b51ef5b child | code-side fixed per root README `:272` (direct desktop notify) | none |
| scripts/*.sh hardcode real LAN IPs (public repo) | t_6b51ef5b | **CLOSED 2026-10-19 (operator verdict)** — `address_guard --tracked` rc=0 on current main; no `192.168.*` anywhere in `scripts/*.sh`. Only LAN-shaped literal is `scripts/hscc_cluster_digest.sh:19` (`10.0.0.244/246/247/248`), which is the sanctioned `10.0.0.x` placeholder range — guard-clean by design | none — do not fold into R-cards |
| operator brief claim: "templates/README EMPTY — 0 lines" | this card's brief | **NOT TRUE on this base** — `hscc-cluster/templates/README.md` = 302 lines (last touched 2026-09-25). The genuinely README-less template dirs are `hscc-project/flightdeck/templates/` (prompt md files) and `docs/template-examples/` (2 yaml examples) | R5: if R5 wants a README somewhere, write `docs/template-examples/README.md` (2 yaml files, no index); do not "write" over the existing 302-line reference |

## 5. Sequencing / collision notes

Disjoint-file design; every pair below is parallel-safe:

- **R1** owns `README.md` + new `docs/FLEET.md`, `docs/WHY.md`, and the append
  into `docs/design/idle-autodown.md`. Parent on **F1 + F5** (never quote
  pre-rename help) and on this card. The autodown append touches only
  `docs/design/idle-autodown.md` — no other R-card names it.
- **R2** owns `ios-app/README.md` (+ new `ios-app/docs/E2E-API-REVIEW-2026-08-27.md`).
  Serialize **after the iOS nav lane merges** (t_810ab152 tip defines the drawer
  IA it must describe). Hotspot already flagged on t_706129d8: `ios-app/scripts/`
  is off-limits to R2.
- **R3** owns `hscc-project/**` (its README + docs README + assets README merge).
  Parent on F1 (sample transcripts must match post-F1 renders).
- **R4** owns the five: `hscc-cli`, `hscc-cluster`, `hscc-commands`,
  `hscc_daemon`, `hscc-roles` READMEs.
- **R5** owns the six small: `sparkrun-hermes`, `hscc-cluster/templates/README`
  (refresh — it is NOT empty, see §4), `install/README`, `hscc-skills`,
  `memori`, `memori_byodb`. **Assignment growth this audit recommends** (keep
  with R5 — same disjoint-file set, no other card names these):
  `scripts/README.md` (one-line generalize `:96`), `hscc-api/README.md`
  (2 fixes: `hscc doctor`→`hscc project doctor` at `:111`, add
  `api install-service` to the CLI table), and
  `ios-app/scripts/model_decode_check/fixtures/README.md` (count 26→54) —
  the orchestrator's original R-list orphaned these three.
- **R6** owns `docs/README.md` only; records closure of §4 rows.
- **R7** (code) owns `scripts/tests/test_cli_help_surface.py` +
  `scripts/hscc_cli_help_audit.py` extension. Parent on R1–R6 (needs final
  docs) + F1/F5.

Hotspot watch: none of the R-cards share a file. If the orchestrator merges
R-cards into one PR, order R4/R5 before R1 is irrelevant (disjoint); the only
true serialization is **F5 → R1** (the `hscc --help` blurb R1's common-tasks
table may quote) and **nav lane → R2**.

## 6. Smoke log (real CLI, 2026-10-16, this host; placeholders per policy)

Ran read-only for real, all rc=0: `hscc --help` · `hscc help advanced` ·
`hscc help project` · `hscc status` · `hscc verify` ("All checks passed (1
unverified: api_routes)") · `hscc cluster status/hosts` · `hscc template
list/status` · `hscc template preview 4node-coding` · `hscc template validate
4node-balanced --structural-only` · `hscc api status` (running,
`100.64.0.1:8788` — placeholder) · `hscc kanban --help` · `hscc project
standup` (13 projects) · `hscc project doctor` (tmux/REPL panel) ·
`python3 hscc-roles/hscc.py list` / `autonomy` ·
`python3 scripts/changelog_fragments.py list`.
Parse-only (`--help`, never executed): `cluster monitor --live` ·
`template apply --confirm` (note: missing-name error prints legacy prog name
`cluster-template apply` — CLI-lane cosmetic, noted for F-cards) · `api
start/stop` · `autodown *`.
X (broken, do not quote): `hscc doctor`.
Mutations executed: **none against fleet/daemon/proxy/gateway.** One
recorded operator-state change: `hscc autodown enable` (bare, parse-check
mistake — argparse executes `enable` on `--help`) flipped `idle_minutes`
120→10 at 19:44; **restored immediately** with `hscc autodown enable
--idle-minutes 120` (value of record: 120, per t_d90de5b6). Lesson for R7:
`autodown` subcommands must not run on `--help` — CLI-lane bug, file as
follow-up card.

Full suite on final tip of this card: see Measurements (ALL GREEN, 5245+1 skip,
11 legs) — stamp applies to `e2278355`+docs-only commits; any later base needs
a re-stamp per operator rule.

## 7. Open questions surfaced (not blocking this audit)

1. `autodown enable --help` executes `enable` (see §6) — CLI-lane follow-up.
2. `template apply` missing-arg error leaks prog name `cluster-template` —
   F-card cosmetic.
3. memori_byodb "NAS-backed" claim needs code verification before R5 grows it.
4. scripts/*.sh real-IP exposure (t_6b51ef5b carry-over) — operator decision,
   not docs.
