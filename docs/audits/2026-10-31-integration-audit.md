# Integration audit A–F — synthesis (2026-10-31)

Card: t_7c43c0ba (synthesis; last card of the audit set A/B/C/D/E/F).
Audits ran 2026-10-30 → 2026-11-01, read-only, on the operator's Mac (the
daemon host). Fix cards landed 2026-11-01 → 2026-11-02.
This document is the 2.15.0 gate artefact: the release card (t_5ae7284b)
promotes only when this doc is merged **and** shows all six areas GREEN.

## 0. What was audited, when, why

The operator's concern (2026-10-30 23:51): features we built do not use hscc's
own features, and are not hooked into the daemon. Six read-only audits, one per
area, each filing numbered GAPs with `file:line` evidence:

| Area | Question | Audit card | Anchor |
|---|---|---|---|
| A | Are the new verbs (`kanban pause/resume/paused/enforce`, `orch inject`, `orch waker`, `cards monitor`) actually reachable — dispatcher wiring, help surfaces, and the installed CLI? | t_6785616a | origin/main `66c40bbe` |
| B | Who owns every long-running thing (daemon stream vs launchd vs cron vs an ad-hoc script), and which operator stopgaps can be retired? | t_72b03930 | origin/main `66c40bbe` |
| C | Do new modules reuse the canonical seams (repl_inject, kanban_live.collect, autodown board enumeration) or reimplement them? | t_e73d41cf | origin/main `797c8cc9` |
| D | Does the installed brew keg have the dependencies and the code the new verbs need; do the verbs run under the Cellar interpreter? | t_e5841211 | origin/main `797c8cc9` + installed keg 2.14.0 |
| E | Does bootstrap/install clobber operator state (fleet caps), does it restart the daemon, and does it overwrite the live daemon tree? | t_f7d94a76 | origin/main `66c40bbe` |
| F | Does `hscc doctor` / `hscc verify` actually assert each shipped feature, or print a ✓ that asserts nothing? | t_549a21e0 | origin/main `66c40bbe` + installed keg 2.14.0 |

Read the verdicts as of 2026-11-02, not as of the audit anchor: every gap in
§3–§8 carries a status updated with the fix card's receipt. `file:line`
citations are as the auditor recorded them at the anchor above; the tree has
moved (main is at `850b38d1` at the time of writing) and line numbers drift, so
treat a citation as a pointer to code, not a coordinate.

**Status vocabulary used below.** `CLOSED (#N)` = fix merged into main.
`PENDING-RELEASE` = no code left to write; it becomes true when 2.15.0 is cut,
installed, and the daemon restarted (release card t_5ae7284b) — per the
orchestrator's ruling these do not make an area RED. `OPEN` = work still owed.
`UNRESOLVED, needs follow-up` = a parent left it open and no card owns it.

## 1. Verdict summary

Area is GREEN only when it has **zero open gaps**.

| Area | Verdict (2026-11-02) | Gaps | Code-closed | Pending-release | Open |
|---|---|---|---|---|---|
| A CLI reachability | **GREEN-PENDING-RELEASE** | 7 | A-2, A-3, A-4, A-7 | A-1, A-5, A-6 | — |
| B ownership + stopgaps | **GREEN-PENDING-RELEASE** | 6 | B-2, B-4, B-5, B-6 | B-1, B-3 | — |
| C reuse of own seams | **GREEN** | 5 | C-1 … C-5 | — | — |
| D installed keg deps/verbs | **GREEN-PENDING-RELEASE** | 4 | D-1, D-4 | D-2 | D-3 (operator PATH check) |
| E bootstrap/install | **GREEN with one uncarded residual** | 4 | E-1, E-2, E-3, E-4 (half) | — | E-4a (observational, no card) |
| F doctor/verify coverage | **RED** | 8 | F-1, F-2, F-3, F-5, F-6, F-8 | F-7 | **F-4** (fix in PR #173, unmerged) |
| **Overall** | **RED — 1 open code gap** | **34** | **21** | **8** | **3** |

The single thing standing between this doc and an all-green board is GAP-F-4
(PR #173 open, unmerged). Everything else is either merged on main or resolves
by shipping 2.15.0. §9 lists the gate precisely.

## 2. Cross-cutting findings

These recur across areas and are the reason the six audits are worth reading
together.

1. **"Asserts nothing" is not green.** Audit F-2 found `check_daemon_streams`
   counting files with no `ok` and no timestamp (`orch_waker.json` in the
   stopgap's shape, `kanban_paused.json`, `reasoning_policy.json`) inside
   "all N streams healthy". A check that cannot fail is worse than no check:
   it advertises coverage. Fixed in #147 — such files are now named
   `not-checked` and the row goes `UNVERIFIED` (`ok=None`), never ✓. The
   principle is the right filter for the whole fleet: every ✓ must name what
   it compared.
2. **A guard that soft-skips is a guard that is off.** Audit D found
   `test_kanban_monitor_deps.py` printing "5 passed" on this machine while the
   keg leg it exists to run was silently skipped by `verdicts.append(...)` +
   `continue` instead of `pytest.skip()` — the exact drift it was written to
   catch (httpx missing from the keg) was reported as a pass. Fixed in #170:
   unprobed-keg paths are real skips with visible reasons and `run_tests.sh`
   gained `-rs`.
3. **Release drift, not code rot, is the dominant root cause.** A-1, A-5, A-6,
   B-1, B-3, D-2 and F-7 are all one fact: main carries the features, and
   everything installed — brew keg, VERSION, the launchd-loaded plugin copy —
   is 2.14.0 (last release commit `3c384657`, 2026-10-29). Audits A and E
   independently reached the same conclusion; audit E additionally corrected
   the card's premise (see §7).
4. **Two writers on one path, or one path with two owners, is how features die
   silently.** B-2 / t_fa9a2faf: the orch waker's scan state and its daemon
   stream stamp were the *same file*, so every tick wiped the watermark, every
   board re-tail-started, and zero nudges were ever delivered while the tick
   stamp and the doctor row both read healthy. C-1/C-2/C-3/C-5 are the same
   class seen from the reuse side (a second enumeration, a second pane
   matcher, a second per-board task read).
5. **A verifier that cannot run is indistinguishable from a verifier that
   passes.** F-1: `hscc verify` crashed outright on this machine (a
   list-shaped state file hit `data.get('ok')`), so *every* row — including
   the profile→endpoint guard that was actually working — was invisible.
6. **Auditors' own first readings were wrong twice, and the correction is part
   of the record.** See §6 (D-4: `hermes_cli` *does* resolve under the Cellar
   venv; the httpx pin's causal claim *does* reproduce) and §5 (B-5: the stale
   "cron entries" are Hermes cron jobs in the default profile store, not OS
   crontab lines — an operator checking `crontab -l` would conclude the gap is
   closed when it is not).

## 3. Area A — CLI reachability of the new verbs

Audit t_6785616a, read-only, origin/main `66c40bbe`; 36 sandboxed `--help`
probes (redirected HOME, sparkrun/hermes PATH canaries, port tripwire), zero
side effects.

### Findings (condensed from audit A's table)

| Verb | Wiring on main | Installed 2.14.0 |
|---|---|---|
| `hscc kanban pause\|resume\|paused\|enforce` | dispatcher branch + `COMMAND_HELP` + `HELP_TEXT` present; non-mutating `--help` verified | `unknown kanban subcommand` (deployed `kanban_cli.py:34` = `(stale, monitor)`); `kanban pause --help` returns **rc 0 with the `blocked` verb's help** — a successful wrong answer |
| `hscc orch inject` / `orch waker status\|enable\|disable` | present (`hscc.py:1169`, `:418`) | `Unknown command: orch` |
| `hscc cards monitor` | present (`cards_live.py`) | `Unknown command: cards` |
| `orch_message` registration | correct on main: `hscc-cluster/__init__.py:97` `_ORCH_TOOLS` → `:109` `ctx.register_tool`; orch-only gating at `orch_message.py:88` | deployed plugin predates it — the file is not in `~/.hermes/plugins/hscc-cluster/` |
| Help audit gate | `scripts/hscc_cli_help_audit.py:216` hardcoded `('blocked','stale')`; `:707` same set; `grep orch` in the file = 0 | passes today **because** it never probes the new verbs |
| Unknown-command hint | `hscc.py:2276` listed `orch` but omitted `cards` and `autodown` | — |

Area A on main was GREEN as wired; every RED finding was either coverage of the
help-audit gate or release drift.

### GAP-A-n

- **GAP-A-1 — PENDING-RELEASE.** Installed brew hscc is 2.14.0 (`v2.14.0`
  `@382ad53b`); all eight verbs landed after it, so every probe against
  `/opt/homebrew/bin/hscc` returns `Unknown command`. Closes with 2.15.0 +
  `brew upgrade` + daemon restart (t_5ae7284b).
- **GAP-A-2 — CLOSED (#162).** The help audit hardcoded the kanban verb pair
  and never probed `pause/resume/paused/enforce`; now derived from
  `kanban_cli.VALID_SUBCOMMANDS`.
- **GAP-A-3 — CLOSED (#162).** The audit never walked the `orch` group router or
  `cards monitor`; now walks both, enumerating each group from the registry
  that lives with its verbs, plus a `COV` class that cross-checks
  walked-vs-advertised against the live dispatcher. Measured: 159 paths / 309
  probes → 169 / 331 (+20 surfaces, 0 removed), 0 `(D)` findings.
- **GAP-A-4 — CLOSED (#166).** The unknown-command hint was a hand-maintained
  220-char literal; it now joins `TOP_LEVEL_COMMANDS` (`hscc_daemon/hscc.py:625`)
  and lists all 30 dispatched verbs.
- **GAP-A-5 — PENDING-RELEASE.** `orch_message` is registered on main but the
  deployed `~/.hermes/plugins/hscc-cluster/` has no `orch_message.py` (== the
  2.14.0 payload), so served orchestrators cannot call it. Now *visible* rather
  than hidden: #157 makes the `plugins` verify row name
  `orch_message.py, orch_message (deployed payload VERSION 2.14.0)` today.
- **GAP-A-6 — PENDING-RELEASE (+ a release step).** The live launchd daemon tree
  `~/.hermes/plugins/hscc_daemon/` is **split** — `hscc.py` == 2.14.0 (no
  orch/cards branch) while `kanban_cli`/`kanban_blocked`/`kanban_pause` are
  newer (`kanban_cli` == `66c40bbe`); one tree answers `kanban paused` and
  rejects `orch`. Release step added on t_5ae7284b: after install, verify the
  deployed plugin tree is byte-identical to the released payload across **all**
  files before declaring the release good — a partial deploy is what made A-6
  invisible for two days. The detector that would have caught it is now in the
  repo (#154 `check_deployed_vs_released`).
- **GAP-A-7 — CLOSED (#167).** `hscc-cluster/plugin.yaml` declared 13 of 23
  registered tools (10 missing, incl. `orch_message`); reconciled to 23/23 with
  a manifest-vs-registry drift test (`hscc_daemon/tests/test_plugin_manifest_drift.py`,
  16 cases) mutation-probed all three drift ways.

**Verdict A: GREEN-PENDING-RELEASE.** Zero open code gaps; A-1, A-5 and A-6 are
closed by shipping 2.15.0 and installing it.

## 4. Area B — daemon/launchd ownership + operator stopgap ledger

Audit t_72b03930, read-only, origin/main `66c40bbe` + live `launchctl`/`ps`/
`cron list` reads. The structural acceptance claim came back **GREEN**: the
orch waker is a daemon stream via `PERIODIC_INTERVALS` (`daemon_ops.py:50`,
`:729` → `orch_waker.check_orch_waker`), with no launchd job, no cron job and
no loop script; `cards monitor` is an interactive TTY viewer (PPID = zsh), not
a resident daemon.

### Findings (condensed)

| Long-running thing | Owner found at audit time | Verdict |
|---|---|---|
| daemon loop | launchd `com.hermes.hscc_daemon`, KeepAlive, single-instance claim refuses a twin | correct |
| 13 stream threads incl. `orch_waker` (30 s) | `PERIODIC_INTERVALS` → supervised thread; verify reads the same dict as SOT | correct |
| watchdog / trigger loops | hardcoded `wait(30)` / `wait(15)` **inside** `run_daemon_loop`, not `PERIODIC_INTERVALS` keys → verify's per-stream staleness could not see them | GAP-B-6 |
| autodown tick, engine/dispatcher wedge | daemon threads, swallow errors | correct |
| `dep_pr_watcher`, `escalate_watcher` | Hermes cron (active, default profile); **no** staleness/doctor probe watched them | GAP-B-4 |
| `hscc_proxy_watchdog.sh` / `hscc_nas_watchdog.sh` | Hermes cron jobs **paused since 2026-06-20**, superseded by the daemon `proxy`/`nas` streams, still registered; the proxy script's header told the reader to schedule it every 5 m | GAP-B-5 |
| `~/bin/orch-waker` | PID 89452, **PPID 1**, no launchd job, no cron, no restart-on-crash, no liveness probe — an unsupervised orphan owning a fleet function | GAP-B-2/B-3 |
| `hscc cards monitor --live` | operator TTY viewer | correct, no owner needed |

### GAP-B-n

- **GAP-B-1 — PENDING-RELEASE.** The root cause behind every "not yet" in this
  area: main carries the waker stream, `orch inject` and `cards`, while
  VERSION, the brew formula, the installed CLI and the launchd-loaded plugin
  copy are all 2.14.0. Until a >2.14.0 release ships and the daemon restarts,
  the built-ins are code-only and all four stopgaps stay load-bearing.
- **GAP-B-2 — CLOSED (#145, t_fa9a2faf).** `DEFAULT_STATE_PATH` and the daemon
  stream stamp resolved to the same file, so every tick wiped the waker's own
  `{last,pending,sent}` watermarks: every board re-tail-started, zero nudges
  were ever delivered, and both the tick stamp and the doctor row read healthy.
  Fixed by merging the stamp onto the scan state instead of replacing it, plus
  7 tests at the seam every earlier test bypassed (real `state.write_state`,
  default state path, redirected HOME) verified failing-then-passing.
- **GAP-B-3 — PENDING-RELEASE.** The stopgap waker is unowned (PPID 1, no
  supervisor, no liveness probe). The terminal fix is to **delete** it (§9),
  not to daemonize it; it closes with B-1/B-2 and the retirement gate.
- **GAP-B-4 — CLOSED (#149).** Both cron watchers now stamp liveness on every
  pass (idle passes included) through the canonical `state.write_state`;
  `hscc doctor` gained a "cron watchers" row that warns (never fatal) past 3×
  cadence and reports "not configured" cleanly; verify excludes the two streams
  explicitly instead of judging them against the flat daemon window.
- **GAP-B-5 — CLOSED (#151) on the repo side; operator deletion outstanding.**
  The two stale watchdog jobs are Hermes cron jobs in the **default profile's**
  store (`42fa8a0b8c0c` every 5 m, `06c8e22d00f1` every 240 m) — *not* OS
  crontab lines, which is why `crontab -l` proves nothing. The scripts' headers
  now say the daemon owns proxy/NAS supervision and that the scheduling advice
  is withdrawn; the two `cron create` lines are gone from `scripts/README.md`;
  a structural guard test makes the retirement stick (no tracked line may both
  register a cron job and name a retired watchdog). Deletion of the two live
  jobs is operator-side (§9).
- **GAP-B-6 — CLOSED (#152).** `PERIODIC_INTERVALS` now carries `"watchdog": 30`
  and `"triggers": 15` (`hscc_daemon/daemon_ops.py:68`); both loops read their
  cadence from `STREAMS` and print the SOT value at thread start, and their
  existing per-tick stamps are now covered by verify's per-stream staleness
  rule. Neither gets a `CHECKS` row — that would start a second thread ticking
  the same machinery, pinned by a test.

**Verdict B: GREEN-PENDING-RELEASE.** Zero open code gaps; B-1 and B-3 close by
shipping 2.15.0, restarting the daemon and passing the stopgap retirement gate.

## 5. Area C — do the new modules reuse hscc's own seams?

Audit t_e73d41cf, read-only, origin/main `797c8cc9`. Three seams audited:
`hscc-api/repl_inject.py` (injection), `hscc_daemon/kanban_live.collect()`
(board/task reads), `hscc_daemon/autodown._enum_board_names` (board discovery).

### Findings (condensed)

| Seam | Result |
|---|---|
| repl_inject | **clean** — all 10 inject paths (kanban_pause, `hscc orch inject`, orch_message, waker W2, `message send`, routes_ws) reach the canonical module. Repo-wide `grep 'send-keys'`: only `repl_inject.py:362/:365`. `hermes-inject`: references to the retired tool only. |
| kanban_live.collect | consumers compliant (`kanban_cli.py:334`, `:339-341`, `cards_live.py:167`). W1's `scan_board` is **legitimately new** — `collect()` cannot carry payload/run_id/id-cursor, is lossy (1 h / 12 rows), and the waker opens boards read-only. |
| autodown enumeration | compliant across autodown, kanban_live, kill_switch, dispatcher_wedge, flightdeck — **except** the waker's own copy (C-1) and a dead fallback (C-2). |

### GAP-C-n

- **GAP-C-1 (HIGH) — CLOSED (#158).** `orch_waker.py:147-172` ran a second live
  board enumeration. The canonical seam gained a read-only path view
  (`autodown.board_db_paths()`, `hscc_daemon/autodown.py:322`; consumed at
  `hscc_daemon/orch_waker.py:260`); `DEFAULT_BOARDS_DIR` deleted; `scan_board`
  and the takeover schema untouched. A fail-**closed** blind signal
  (`unevaluable` → `waker_tick ok=False` → doctor warn) keeps "hermes
  unreachable" from reading as "no boards".
- **GAP-C-2 — CLOSED (#156).** `kanban_blocked.py:121-129` kept a dead inline
  copy of the enumeration helper's body for a "not merged yet" window that had
  already closed; now a one-line forward to the seam. A mutation probe proved
  the deleted branch was behaviourally unreachable (all 24 pre-existing tests
  green with it restored).
- **GAP-C-3 (MEDIUM) — CLOSED (#163).** `tmux_chat.py` duplicated repl_inject's
  pane matcher and had already diverged (three inline predicates incl.
  `_session_profiles`, plus a copied `_PROFILE_RE`). Now delegates to the
  canonical `pane_chat_command`/`pane_chat_profile`/`profile_flag`; divergence
  reconciled to the canonical rule; `scripts/tests/test_pane_matcher_single_source.py`
  makes the audit's grep structural — the swept fragments may appear only in
  `hscc-api/repl_inject.py` among tracked non-test code.
- **GAP-C-4 — CLOSED (#160).** `orch_message.py:530` hardcoded a `VERB_PREFIX`
  fallback that could drift from the canonical table; the verb is now read off
  the loaded repl module via `_verb_for_mode`, and a source guard fails on any
  quoted verb literal in that file. The pre-parity `_PREFIX` branch was kept
  deliberately: the shipped 2.14.0 payload has no `VERB_PREFIX`, so deleting it
  would have regressed the verb to `None` in production.
- **GAP-C-5 — CLOSED (#164).** `routes_orchestrator.py:473-490` hand-rolled a
  per-board running-task query; it now delegates to `kanban_live.collect()`
  through two injectable seams, with the fail-safe-**busy** posture rebuilt (not
  inherited — `collect()` alone fails open) and an AST guard banning a second
  board read in that module. Live read-only smoke against the real 25-board
  fleet passed.

**Verdict C: GREEN.** All five gaps closed and merged; the repo now has exactly
one pane matcher, one board enumeration, and one per-board task read.

## 6. Area D — installed keg: dependencies and the new verbs

Audit t_e5841211, read-only; compared the installed 2.14.0 keg and the
origin/main tree under the same Cellar interpreter, with a state-file
fingerprint control (64 files, ± a 45 s no-CLI control) proving the `--help`
sweep mutated nothing.

### Findings (condensed)

| Probe | Result |
|---|---|
| Cellar venv inventory | `PyYAML 6.0.3 · rich 15.0.0 · psutil 7.2.2 · markdown-it-py 4.2.0 · mdurl 0.1.2 · pygments 2.21.0 · hscc-cli 2026.6.15 · **httpx ABSENT**` |
| requirements.txt | `httpx==0.28.1` is the **only** violation; everything else satisfies its range |
| Can brew install it? | **No.** The tap formula builds the venv from six hand-written `resource` blocks and never reads `requirements.txt`. |
| Per-verb run under the Cellar interpreter, from main | **16/16 verbs rc 0** on both interpreters — dependency compatibility is fine; the blocker is release + packaging |
| Installed keg | `kanban paused` → rc 1 `unknown kanban subcommand`; `cards`/`orch` → `Unknown command`; `kanban paused --help` → **rc 0 with the `blocked` help** |
| Guard for this exact drift | soft-skipping and reporting "5 passed" (§2.2) |
| Two `hscc` on PATH | `/opt/homebrew/bin/hscc` (3.13.18) shadows `~/.hermes/hermes-agent/venv/bin/hscc` (3.11.16); both print `2.14.0`, capabilities differ, `--version` does not say which answered |

**Self-corrections recorded by the auditor (cite them, they are part of the
evidence).** (a) "hermes_cli is unreachable from the Cellar venv so fleet caps
are silently blank" is **false** — `autodown._load_kanban_db_or_default` puts
the Hermes runtime dir on `sys.path`; caps read `[12, 3]` on both interpreters
through the real path. (b) "no hscc code path reaches httpx, so the pin's
stated reason does not reproduce" is **false** — the chain reaches httpx via
`kanban_db_connect → hermes_state → agent.* → hermes_cli.auth`.

### GAP-D-n

- **GAP-D-1 (HIGH) — CLOSED (#170) on the repo side; operator step outstanding.**
  `httpx==0.28.1` was absent from the keg and uninstallable as the formula is
  written. Fixed: the soft-skip became a real `pytest.skip` with a visible
  reason (this host now reports "4 passed, 1 skipped" naming the 2.14.0 keg,
  where it previously reported 5 passed over a venv with no httpx);
  `run_tests.sh` gained `-rs`; new `scripts/tests/test_formula_dep_drift.py`
  (8 cases) compares `requirements.txt` pins against the tap formula's resource
  list and is non-vacuity-probed against mutations of the real formula.
  Operator step remains: `brew reinstall hscc` once the tap carries the resource
  (the tap already does — the drift guard is green against it today).
- **GAP-D-2 — PENDING-RELEASE.** The keg contains none of the new verbs; they
  are all post-2.14.0. `orch waker status` is therefore **pending-release, not
  pending-W3** — W3 is merged and works, it is simply not shipped. Includes the
  secondary defect: `--help` on a nonexistent verb returning rc 0 with another
  verb's help (same surface as A-2/A-3; the help audit now walks derived verb
  sets from their registries).
- **GAP-D-3 — OPEN (operator step, no code fix proposed).** Two `hscc` entry
  points on PATH, different interpreters, identical version string, different
  capabilities; `--version` does not say which CLI answered. Assigned as an
  operator check on the release card (t_5ae7284b). This is the one area-D item
  that a release alone does not close.
- **GAP-D-4 — CLOSED (#169).** `kanban_live._read_caps()` was call-order
  dependent (`(None, None)` before the loader ran, `(12, 3)` after) and its
  `except Exception: return None, None` made "could not read" indistinguishable
  from "operator set no cap". It now resolves the Hermes agent tree itself and
  records/warns once on a genuine read failure. Measured under the keg venv:
  old first call `(None, None)` → new first call `(12, 3)`. Caps values and
  bootstrap behaviour unchanged.

**Verdict D: GREEN-PENDING-RELEASE.** D-1 and D-4 are merged; D-2 closes with
the release. D-3 stays OPEN as an operator PATH check — flag it explicitly at
release time rather than assuming `brew upgrade` fixed it.

## 7. Area E — bootstrap/install: caps, restart, and the live tree

Audit t_f7d94a76, read-only, origin/main `66c40bbe`; 25/25 citations
re-checked against the pinned tree.

### Findings (condensed)

| Question | Verdict | Evidence |
|---|---|---|
| Does bootstrap revert the operator's fleet caps? | **GREEN** | live `~/.hermes/config.yaml:482-483` = 12/3 == SOT; `enable_plugins.py:276-286` is guarded by `_is_int_like` (`:284`) so int/whole-float/digit-string operator caps survive a run and the 30/10 defaults (`:81-82`) only fill absent/non-numeric keys; pinned by `tests/test_enable_plugins.py:484/:502/:518` |
| Does the install flow restart the daemon, or does the operator? | **GREEN — the flow does** | `bootstrap.sh:663` → `restart_daemon.sh` `restart_via_supervisor` (`:313`) → `launchctl kickstart -k gui/$(id -u)/com.hermes.hscc_daemon`, verifying the supervisor's pid turned over; the kickstart-only rule is code-enforced (rc 4 refusal on an unsupervised twin). **Consequence for area B:** after 2.15.0 + `brew upgrade` + bootstrap, `bootstrap.sh:663` is what loads `orch_waker.py` — no manual kickstart needed. |
| Does anything protect the live `hscc.py`/`kanban_cli.py`/`kanban_blocked.py` from being overwritten? | **RED** | `install_payload.py:115-140` moves the whole `hscc_daemon` dir to backups and copytrees over it; the only filter is `_EXCLUDE` (build artifacts). `grep 'kanban_cli\|kanban_blocked'` in `hscc-bootstrap/` (non-test) = 0 hits. |

**Correction the auditor made to the card's premise (use this reading, not the
card's).** The live trio is *not* an operator kmon build that main would
overwrite: live `hscc.py` is byte-identical to the v2.14.0 keg payload and sits
288 diff lines **behind** main; `kanban_cli.py` == main; `kanban_blocked.py`
differs only in 5 rewrapped help lines. The operator's kmon build is UNDEPLOYED
at `~/.hscc/cards-monitor-src`. So today's exposure is release drift (same root
as A-6), while the overwrite-of-operator-work path is real, unguarded — and
unexercised.

### GAP-E-n

- **GAP-E-1 (HIGH) — CLOSED (#153).** No payload-preservation guard. Generalised
  the autodown.json precedent: `install_payload.py` now content-hashes deployed
  files against the incoming payload before copying, preserves drifted ones into
  `plugins-backups/<entry>.drifted.bak-<ts>/`, flags them per-path
  (`drifted`/`drift_detected`), warns loudly in `bootstrap.sh`, and exits rc 3
  (install succeeded, drift flagged). Never auto-merges; `--force` restores the
  silent overwrite across the root **and** every `profiles/<p>/plugins`.
- **GAP-E-2 (HIGH) — CLOSED (#154).** No deployed-vs-released drift detector:
  `check_plugin_payload`'s repo side is `_repo_root()` — the deployed/keg tree
  itself, never the release — so 288 lines of deployed drift coexisted with
  green gates (this is why audit A's help audit passed on both trees). New
  `check_deployed_vs_released` (`hscc_daemon/verify.py:1819`, wired into
  `run_all`) diffs each deployed payload file's sha256 against the **released
  keg** payload, names drifted/missing/extra files, reports GAP-E-1-preserved
  files as `KNOWN-PRESERVED` without failing, never diffs origin/main, and
  returns `ok=None` when no keg exists. Live proof on this box: `ok=False`
  naming the exact split tree against 2.14.0.
- **GAP-E-3 (LOW) — CLOSED (#155).** `flash-next-all.yaml` declared
  `max_in_progress_per_profile: 2` while operator SOT is 3, so
  `hscc template apply flash-next-all --confirm` would have silently rewritten
  3 → 2. Template raised to 3 and pinned both ways against an explicit
  `OPERATOR_CAPS_SOT` constant, red-then-green proven. The STOP-check was
  evaluated and cleared from the config-backup timeline: the operator raised SOT
  to 3 *after* the template shipped, so 2 was stale, not deliberate.
- **GAP-E-4 (LOW) — half CLOSED (#153), half OPEN and uncarded.** The second
  half (profile-plugin overwrite with no escape hatch) is closed by `--force`
  across every destination. The first half is **UNRESOLVED, needs follow-up**:
  `enable_plugins.py:1050-1055` rewrites `config.yaml` with `yaml.safe_dump`
  whenever anything changes, which drops comments. It is safe today only
  because `config.yaml` has zero comment lines (verified). The orchestrator
  ruled this cite-only, no card; if the operator ever comments `config.yaml` by
  hand it becomes a real data-loss path and needs a card.

**Verdict E: GREEN with one uncarded residual.** E-1, E-2 and E-3 are merged;
E-4a is a named, currently-inert observation, not a blocker.

## 8. Area F — does doctor/verify actually assert the features?

Audit t_549a21e0, read-only, origin/main `66c40bbe` + the installed 2.14.0 keg;
two real `hscc doctor` runs and fixture-controlled `verify` runs.

### Coverage matrix (condensed; 11 features)

| Feature | Coverage found at audit time | Verdict |
|---|---|---|
| kanban pause/enforce | `kanban_paused.json` *read* by `check_daemon_streams` but has no `ok`/timestamp → both assertions skip; `enforce()` had **no scheduler anywhere** (only call: `kanban_cli.py:364`) | GAP-F-2, F-4 |
| autodown PR/CI + kanban interlock | verdict exists only inside `hscc autodown status`; verify merely consumes `classify()` to excuse streams | GAP-F-6 |
| kickstart-only restart (t_61457fbd) | install-time only; nothing at runtime compares `~/.hscc/daemon.pid` against launchd's pid; twin scan counts **loop processes**, not supervision, and is wired only into `hscc status` | GAP-F-5 |
| dispatcher wedge self-heal | detection covered (dispatcher stream), but the row could not run because verify crashed; the cap latch had no row at all | GAP-F-1, F-8 |
| cards monitor | not a doctor subject **by design** — interactive viewer, "silent-off" is not a failure mode | legit, no gap |
| orch waker | doctor row **exists as W3 promised** (`doctor.py:1360` → `orch_waker.doctor_check`); its `○ predates-build` verdict was honest against the 2.14.0 daemon. The verify side was blind: the live state file carried the stopgap schema | GAP-F-2 (mechanism) |
| served-profile→endpoint guard | `check_profile_endpoints` present and **working** (`ok=True`, 122 base_urls / 48 profiles) — but the operator can never see it, because `hscc verify` dies at row 3 | GAP-F-1 (consequence) |
| watchdog/sysctl, snap-mask, GPU cap | real coverage, 4/4 ok on live node state | legit |

### GAP-F-n

- **GAP-F-1 (HIGH, "coverage that never runs") — CLOSED (#147).**
  `hscc verify` crashed outright — `AttributeError: 'list' object has no
  attribute 'get'` on `kmon_orch_tokens.json` at `verify.py:333` — killing the
  whole table, on 2.14.0 **and** on main (not release drift). `check_daemon_streams`
  is now schema-tolerant: non-dict files are reported `unrecognized schema` and
  skipped. Acceptance ran the real `hscc verify --json` against a copy of the
  live 17-file state dir: all 13 rows report (pre-fix: rc 1, zero JSON).
- **GAP-F-2 (MED, "ok while broken") — CLOSED (#147).** Files with no `ok` and
  no timestamp are named `not-checked` and make the row `UNVERIFIED` instead of
  counting green; aside files are appended to *every* verdict path so a failure
  cannot hide them. This is the cross-cutting principle in §2.1.
- **GAP-F-3 (HIGH, worst kind — doctor printed a verdict contradicted by live
  state) — CLOSED (#148, plus #168).** Both real doctor runs printed
  `○ gateway: not running` while launchd pid 74857 ran `gateway run --replace`.
  **The audit's stated cause was understated and the fix card corrected it:**
  the pattern did not merely fail to match — macOS `pgrep` cannot *see* that
  process at all (`pgrep -f .` returned 1210 pids where `ps -axww` listed 1216,
  the invisible six including pid 1 and the gateway). No re-calibration could
  ever have made the row truthful. It now uses the supervisor the gateway is
  installed under (`launchctl list ai.hermes.gateway`), with a `ps -axww` argv
  token scan as the fallback when no supervisor can answer; `pgrep` is no longer
  invoked by that row. Follow-up #168 removed the same `pgrep` leg from
  `hscc_daemon/health.py:407`, so both consumers share one implementation.
- **GAP-F-4 (MED, silent-off) — OPEN. This is the one gap that keeps the doc
  RED.** kanban pause/enforce had zero doctor/verify coverage *and* zero
  scheduler. The fix (t_51931733) makes enforce a real daemon stream
  (`PERIODIC_INTERVALS["kanban_enforce"] = 60` + a `CHECKS` row, stamping its
  own `~/.hscc/state/kanban_enforce.json` — deliberately *not*
  `kanban_paused.json`, whose non-`_` keys are board names) plus a doctor row
  "kanban enforce (paused-board hold)". Shipped as **PR #173, OPEN, base
  `3c0428ee`, head `0008a53a`, guard checks pass, NOT merged**. 18 new daemon-side
  tests green and the existing 15 `test_kanban_pause` cases green, but the
  card's own note is that **suite-ALL-GREEN was not yet proven** when it
  finished — the fix landed too late in the cycle for the canonical runner to
  have been run to completion on the final tree. Verified here: at main `850b38d1`
  there is still **no** `kanban_enforce` entry in `daemon_ops.py` and no enforce
  row in `doctor.py` — the gap is genuinely still open in shipped code.
- **GAP-F-5 (MED — the t_61457fbd incident class re-silenced) — CLOSED (#161).**
  New `hscc-bootstrap/doctor_supervision.py` + a doctor row comparing the
  pid-file pid against the owning supervisor's pid; the orphan-daemon class now
  prints `ORPHAN DAEMON` instead of staying invisible. Live:
  `✓ daemon supervision: supervised — launchd pid == pid-file pid`.
- **GAP-F-6 (LOW-MED) — CLOSED (#161).** The autodown interlock verdict now
  persists on every tick path and verify's `autodown_interlock` row renders
  UNEVALUABLE as a distinct `ok=None` naming source, reason and cost (the
  fail-safe-to-"never tear down" mode costs money silently today).
- **GAP-F-7 (MED, release-drift shape; cross-refs only) — PENDING-RELEASE.**
  The installed diagnostics were 6 checks thinner than main and `check_plugins`
  grepped three legacy strings in `hscc-commands`, so a deployed `hscc-cluster`
  missing `orch_message.py` entirely printed `plugins ✓`. Root cause is the same
  as E-2/A-5/A-7. Area F's contribution was the *missing-check statement*, now
  implemented: #157 makes the `plugins` row assert the deployed cluster tools and
  name them, and #154 supplies the drift detector. Resolves with the release.
- **GAP-F-8 (LOW) — CLOSED (#157).** The dispatcher self-heal's cap latch
  (`~/.hscc/dispatcher_recover.json`) is now a verify row — armed / cooling /
  expired / incident-latched / `GAVE-UP` (`ok=None`) / corrupt — read-only from
  the existing state file.

**Verdict F: RED — GAP-F-4 open.** Merge PR #173 (with the canonical runner
stamped ALL GREEN on its final head) and F goes GREEN.

## 9. Stopgap retirement table (what the operator can kill after 2.15.0)

All four stopgaps are merged-on-main as built-ins but **not shipped** — every
one of them is load-bearing until 2.15.0 is installed and the daemon restarts.
Source: audit B §2, re-verified against the merged fix cards.

Gate for all four: 2.15.0 installed → daemon restarted (bootstrap does it,
`bootstrap.sh:663`) → the built-in proven doing its job → then delete.

| Stopgap | What it is | Retire when | Retire command |
|---|---|---|---|
| `~/bin/orch-waker` | 148-line poller, PID 89452 with **PPID 1** (unsupervised orphan), injects via `~/bin/hermes-inject` | release installed + daemon restarted + **board watermarks / audit JSONL observed to move** (never the tick stamp — that is what lied before #145) | `pkill -f "$HOME/bin/orch-waker"` then `rm ~/bin/orch-waker`. No `launchctl bootout` — no plist exists (verified via `launchctl list` + `ls ~/Library/LaunchAgents`). Leave `~/.hscc/state/orch_waker.json` in place: the built-in's takeover-compat reads the same schema at the same path. |
| `~/bin/hermes-inject` | 211-line pane injector, invoked per send | same release, once `hscc orch inject --dry-run` resolves the panes you actually use | `rm ~/bin/hermes-inject` — **after** `orch-waker` is gone (it depends on it). Nothing to kill; it is a per-call CLI. |
| `~/bin/hscc-cards` | 7-line zsh wrapper running `hscc cards monitor` from a pinned checkout because the release lacks it | the release contains `hscc cards` — test: `hscc cards --help` | Ctrl-C the monitor in its TTY (it is a viewer), `rm ~/bin/hscc-cards`, then `git -C ~/dev/hscc worktree remove ~/.hscc/cards-monitor-src`. |
| `~/bin/hscc-dev` | 6-line zsh wrapper running any verb from the pinned dev checkout | installed release ≥ the checkout's content (`aa0ae81a` is already an ancestor of main) | `rm ~/bin/hscc-dev` (the pinned-checkout removal above is shared with `hscc-cards` — do it once, after both are gone). |

Two more pieces of stale inventory that are **not** in the four above but belong
in the same sweep (GAP-B-5, #151): `hscc-proxy-watchdog` and `hscc-nas-watchdog`
are Hermes cron jobs in the **default profile's** store, paused since
2026-06-20, superseded by the daemon `proxy`/`nas` streams. `crontab -l` returns
`no crontab for desac` and proves nothing. The verified delete commands are in
`scripts/README.md` → "Retiring the superseded watchdog cron jobs" — run those,
do not paraphrase them.

## 10. 2.15.0 gate

The doc is GREEN iff all six areas are GREEN. Today it is **RED**: five areas
are GREEN or GREEN-PENDING-RELEASE, area F is RED.

What stands between now and green, in order:

1. **Merge PR #173 (GAP-F-4)** — kanban enforce as a supervised daemon stream +
   its doctor row. Before merging, stamp `bash scripts/run_tests.sh` ALL GREEN
   on the final head; the fix card did not get to run the canonical runner to
   completion and said so. This is the only remaining code change.
2. **Cut 2.15.0** (t_5ae7284b, already a child of this card) — fragments →
   CHANGELOG, VERSION + badge, frozen-tree suite green.
3. **Install + restart + verify the deploy is whole** — the release steps added
   by these audits, all operator-side and all owned by t_5ae7284b:
   - `brew upgrade` (or reinstall) so the new verbs exist at all
     (closes A-1, A-5, D-2, B-1);
   - **verify the deployed plugin tree is byte-identical to the released
     payload across all files** before declaring the release good (closes A-6 —
     a partial hand-sync is what hid it for two days; `check_deployed_vs_released`
     from #154 is the tool);
   - tap formula carries `httpx==0.28.1` + `brew reinstall hscc` so the keg
     venv can read a kanban board (closes D-1's operator half; the drift guard
     from #170 is green against the tap today);
   - **GAP-D-3 check**: confirm which `hscc` on PATH answers, and that the two
     entry points no longer differ silently in capability. A release does not
     close this on its own.
4. **Then** run the §9 retirement sequence, gated on observed board watermarks /
   audit JSONL moving — not on tick stamps.
5. **Carry, do not block on:** GAP-D-3 (operator PATH check), GAP-E-4a
   (`config.yaml` comment loss via `safe_dump`; inert while the config has no
   comments — card it the moment it does).

### Receipt index

Every status above traces to a card result quoted in the board handoffs; PRs
are numbered, no URLs. Code-side closures now merged into main: #145 (B-2),
#147 (F-1, F-2), #148 + #168 (F-3), #149 (B-4), #151 (B-5 repo side),
#152 (B-6), #153 (E-1, E-4 half), #154 (E-2), #155 (E-3), #156 (C-2),
#157 (F-8), #158 (C-1), #160 (C-4), #161 (F-5, F-6, F-7 statement), #162
(A-2, A-3), #163 (C-3), #164 (C-5), #166 (A-4), #167 (A-7), #169 (D-4),
#170 (D-1 repo side). Open: #173 (F-4).

Re-verified against main `850b38d1` for this synthesis (not just quoted):
`daemon_ops.py:68` carries the watchdog/triggers cadence (B-6);
`kanban_blocked.py:125` forwards to the seam (C-2); `autodown.py:322`
`board_db_paths` consumed at `orch_waker.py:260` (C-1); `plugin.yaml` declares
23 tools (A-7); `hscc.py:625` `TOP_LEVEL_COMMANDS` (A-4); `verify.py:590`
`unrecognized schema` / `not-checked` (F-1/F-2); `verify.py:1819`
`check_deployed_vs_released` (E-2); and **no** `kanban_enforce` anywhere in
`daemon_ops.py` / `kanban_pause.py` / `doctor.py` (F-4 still open).

### Thin or contradictory evidence, named plainly

- **Audit F's GAP-F-3 cause was wrong in a way that mattered** (pattern
  mismatch vs `pgrep` blindness); the fix card corrected it and the correction
  is recorded above. Read the card's correction, not the audit's diagnosis.
- **Audit B filed GAP-B-5 against the wrong store** ("crontab"); they are Hermes
  cron jobs in the default profile. The retirement commands are written against
  the correct store.
- **Audit D's two self-corrections** (§6) reverse its own first readings; both
  are recorded so nobody re-files them as gaps.
- **Audit E corrected its card's premise** (§7): the live trio is 2.14.0-keg,
  not an operator kmon build.
- **Fix-card receipts that were incomplete at completion and should be
  re-confirmed by whoever merges next:** t_b82ad0fe (D-1) left the full
  `run_tests.sh` stamp in flight and named `brew reinstall hscc` as the
  remaining operator step; t_cd836fee (D-4) left its ALL-GREEN stamp in flight
  (only the targeted hscc_daemon run, 63 passed, was attested). Both PRs were
  merged by the orchestrator; if a reviewer wants the ALL-GREEN stamp on those
  tips, re-run the canonical runner at main.
- **Audit-time anchors are stale.** A/B/E/F audited `66c40bbe`, C/D `797c8cc9`;
  main is `850b38d1`. Nothing in this synthesis re-ran the audits — where a
  claim mattered to a verdict it was re-checked at the current tip and marked
  "re-verified" above.
