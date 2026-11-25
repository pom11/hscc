# sparkrun_exec passthrough — registration, reach, and the main-only shape

**Card:** t_471788e0 (LEFT-2 of the main-only audit) · **Date:** 2026-11-23
**Trigger:** driver ruling 6 in `~/fleet-check/verdict-main-only.txt`: *"sparkrun-hermes /
sparkrun_exec passthrough must be main-only too — currently NOT audited."*
**Parent:** `docs/audits/2026-11-23-main-only-surface.md` §9 LEFT-2 ("its tool names, its
`sparkrun_exec` passthrough … and whether it registers hooks at all. Not measured here;
only its enablement was.").
**Kind:** READ-ONLY audit. No fix is implemented on this card; §7 recommends the atomic
cards to cut from it after the operator reviews. nolive — nothing under `~/.hermes` or
`~/.hscc` was written to produce this doc; live state was read, never touched.
**Anchors re-verified:** 2026-11-25 against `origin/main` @ `21da34a8`; the §8 gate
legs re-ran **5 / 18 / 16 / 13 / 30 passed (82 total), RC=0 each**. Three line-number
anchors drifted on `main` since first publication (§4/§6/§7) and are re-pointed; no
finding changed.

---

## 0. Headline

1. **The enforcement half of LEFT-2 already shipped before this card ran.**
   `t_e98c6ad4` (PR #684) put a registration gate inside `sparkrun-hermes` itself, made
   bootstrap write a per-home `plugins.enabled` tier that lists `sparkrun-hermes` only for
   `<main>`, and made doctor report any other home that enables it. The parent doc's
   LEFT-2 wording predates that card; as of `origin/main` today the claim is:
   **`sparkrun_exec` registers in exactly one home — the one resolving to
   `main_orch.main_profile()` — and nothing else.**
2. **QA has already proven it.** `t_035b1b0b` (done 2026-11-23): sandboxed post-#684
   bootstrap gives `<main>` the full cluster tool set while worker/qa/orchestrator homes
   register **0** cluster tools, hooks still fire, and — the decisive G-2 measurement —
   **nothing in the gateway slash path reads the tool catalog**, so no root-home widening
   is needed for the passthrough or anything else.
3. **Two real gaps remain, neither about *where* the tool registers:**
   **(F1)** `sparkrun_exec` calls bypass the cluster-guard capacity gate **and** its
   audit log — `cluster-guard.py:123,155` exit on `toolset != "hscc-cluster"`, so the
   passthrough can `sparkrun stop --all` / `sparkrun proxy stop` ungated and unaudited.
   **(F2)** the plugin's bundled `skills/` are **inert** (all four dirs: `run`, `setup`, `registry`, `data-engineer`): a backend
   plugin's `skills/` dir is never auto-registered, `register()` never calls
   `ctx.register_skill`, and `hscc-skills` never installs them — while the tool
   description tells agents to "Load the run/setup/registry skills".
4. **No worker, dispatcher, hook, or slash path needs the passthrough** (§6). Main-only
   costs nothing; the hook/slash/daemon layers reach `sparkrun` through their own
   subprocesses, which are outside the tool surface ruling 6 is about.

---

## 1. What the plugin is (the measured facts)

| fact | evidence |
|---|---|
| One tool: `sparkrun_exec`, toolset `sparkrun` | `sparkrun-hermes/__init__.py:106-113`; `plugin.yaml:6-7` (`provides_tools`) |
| `kind: backend`; **registers NO hooks, no commands, no skills** | `plugin.yaml:5`; the whole of `register()` is one gated `register_tool` (`__init__.py:100-113`) |
| The "guard" is prefix-only: command must start with `sparkrun`, then `subprocess.run(shlex.split(cmd))` | `execlib.py:25-29,38-43`; timeout default 120 s / max 1800 s (`execlib.py:13-14,31-33`) |
| Handler stringifies dict results (vLLM tool-content contract) | `__init__.py:19-32` |
| The passthrough is unguarded-by-design *inside* the sparkrun verb space — it reaches `stop --all`, `proxy stop`, recipe edits, cluster redefines | README self-description ("a raw `sparkrun …` CLI passthrough"), `_DESC` `__init__.py:54-59` |

`plugin.yaml`'s `provides_tools` does **not** pre-declare the tool at discovery for this
plugin: hermes' deferred-platform pre-declaration only runs for a plugin that also ships a
`tools.py` with `register_tools` (`hermes_cli/plugins_loader.py:446,457-462`);
sparkrun-hermes has neither. The tool exists only if `register()` says so.

## 2. How it registers, and for which home

The gate chain (all repo-side, no hermes change needed):

```
register(ctx)                                    sparkrun-hermes/__init__.py:100
  -> _cluster_tools_allowed(ctx)                 __init__.py:62-97
     -> main_orch.plugin_tools_allowed(ctx)      hscc-roles/main_orch.py:364-371
        -> cluster_tools_allowed(profile=ctx.profile_name, home=$HERMES_HOME)
                                                 main_orch.py:301-336
```

`<main>` is resolved, never spelled (D-A): `cluster_tools_allowed` returns True **only**
when the name equals `main_orch.main_profile()` (`~/.hscc/main-orchestrator.json`).
Root-home sentinel `"default"` (`ROOT_PROFILE_NAME`, `main_orch.py:225`) → **False**
per the R3 tightening of D-B: a model chatting at the gateway root is not the
orchestrator, and the root's wedge verbs live in `hscc-commands`, not in this tool.

Per-home resolution under `multiplex_profiles` (verified in the hermes tree):

* `PluginContext.profile_name` derives from `HERMES_HOME` — `"default"` for `~/.hermes`,
  the `<name>` for `~/.hermes/profiles/<name>`, `"custom"` otherwise
  (`hermes_cli/plugins.py:401-407` → `hermes_cli/profiles.py:1971-1985`). An import
  failure there yields `"default"` → the gate's **False** branch: fail-**closed** in the
  real loader.
* `get_plugin_manager()` caches **per resolved home** — "a profile switch gets its own
  manager and plugin submodules" (`hermes_cli/plugins.py:1629-1648`). One multiplexed
  gateway therefore runs the gate per profile home, correctly labelled.
* Load/no-load is decided first by that home's own `plugins.enabled` (no root→profile
  overlay — parent doc §2); the gate is the second, migration-proof layer.

Verdict table:

| home | loads plugin? | registers `sparkrun_exec`? | why |
|---|---|---|---|
| `<main>` profile | yes | **yes** | `plugins.enabled` tier + gate True |
| root (`"default"`) | yes (parent §6 keeps it listed) | **no** | gate: root is not the orchestrator |
| any other profile (workers, `*-orch`, qa…) | **no** | n/a (gate would also say no) | tier + defense-in-depth |
| stub ctx (no usable `profile_name`) | n/a | yes — fail-open | `main_orch.py:366-368` (`name is None → True`); the phrase "no resolver is no gate" is the plugin's own wording (`sparkrun-hermes/__init__.py:74`), not main_orch's; deliberate (D-B), accepted residual |
| `main_orch` unimportable from the plugin | yes | yes — fail-open | `__init__.py:90,96-97`; same accepted residual |

## 3. Who can call it today (live measurement, read-only)

Measured 2026-11-23 against the live `~/.hermes` (read-only; probe recipe in §8):

* stored main: `~/.hscc/main-orchestrator.json` → `general` (via the resolver; this doc
  spells it only as a measurement, no gate in code or docs reads the literal).
* root `config.yaml`: `plugins.enabled` lists all three HSCC plugins incl.
  `sparkrun-hermes` → loads, registers nothing (gate).
* **51 profile configs: `sparkrun-hermes` enabled in exactly one — `general`.**
  `plugins.enabled` is ABSENT in **0** profiles → the `_migrate_to_21` widening path is
  inoculated everywhere (R5 holding in the live tree, not just in code).
  *Counting:* the probe scans `~/.hermes/config.yaml` **plus** `profiles/*/config.yaml`
  = **52 files**, of which 51 are profiles; "exactly one" counts profiles only, and the
  root's own listing is F3(b) (loads, registers nothing). Re-measured on the review pass:
  52 files, `plugins.enabled` absent in 0, enabled in 1 profile + root.

So today: the only process that can register the tool is a session whose home is
`profiles/general`; even a mis-enabled home registers nothing while the gate is
importable. Removing `sparkrun` from top-level `toolsets:` lists would be cosmetic, not
a control (parent §6 rule 2; QA reaffirmed).

## 4. Hook-site note (why the plugin stays hook-free)

`sparkrun-hermes` registers no hooks, so a denied home registering *nothing* is safe —
unlike `hscc-cluster`, which must stay enabled everywhere for its six hooks (D-C).
Nothing in the `hscc-cluster` hook set, the cluster-guard shell hooks, or
`hscc_daemon` calls `sparkrun_exec`: the guard shells `sparkrun status` itself
(`hscc-bootstrap/hooks/cluster-guard.py:54-62`) and the daemon drives sparkrun through
sparkrun's own venv (`hscc_daemon/health.py`, fleet rule). The tool surface and the
hook/subprocess surface are independent — which is exactly what F1 below is about.

## 5. Who legitimately needs the passthrough (measured: nobody outside `<main>`)

* **Gateway slash verbs (`hscc-commands`)** — registered by
  `plugin_commands_allowed` (root + `<main>`, `hscc-commands/__init__.py:974-1002`), and
  their incident-response verbs shell `sparkrun` themselves (`cmdlib.py:19,145-150,273`)
  — they never route through the tool. QA G-2: nothing in the slash path reads the tool
  catalog. **No widening needed.**
* **Kanban workers / dispatcher** — worker homes don't load the plugin at all; the
  worker/dispatcher lanes are hooks, which fire tool-less (QA (c)). Workers' SOUL line
  is "cluster actions go through the main orchestrator" (ruling 4).
* **Hooks** — see §4; they use their own subprocesses.
* **The daemon** — its own sparkrun path, not the tool.

Conclusion: the operator's ruling (main-only) is the shape the fleet already runs; the
audit finds no consumer that the ruling would break.

## 6. The config/registration shape (what "main-only" means concretely)

Already implemented by `t_e98c6ad4` — recorded here as the audited shape, with the
invariants each future card must preserve:

1. **Registration gate lives in `register()`** (D-B as tightened by R3):
   `sparkrun-hermes/__init__.py:62-113`, resolved via `main_orch.plugin_tools_allowed`,
   pinned by `sparkrun-hermes/tests/test_cluster_surface_gate.py` (worker home → no
   tool; `*-orch` home → no tool; root → no tool; main → tool; stub ctx → fail-open; a
   no-local-name-check source pin).
2. **Bootstrap writes the tier per home** (`hscc-bootstrap/enable_plugins.py`):
   `HSCC_PLUGINS` includes `sparkrun-hermes` (line 56);
   `ensure_plugins_tier_everywhere` / `..._for_home` / `..._for_new_home` seed explicit
   `plugins.enabled` (hooks-only tier `(hscc-cluster,)` off-main —
   `main_orch.py:218` `HOOKS_ONLY_PLUGINS`), prune HSCC-owned cluster plugins from
   homes the tier forbids, and never invent the main name (D-A). Escape hatch:
   `HSCC_CLUSTER_PLUGINS_MAIN_ONLY=0`.
3. **Doctor reports drift**: `doctor_main_only.py` flags `sparkrun-hermes`
   ENABLEMENT (not directory presence) on any non-main profile; the root is deliberately
   out of scope (ruling 3 keeps it listed there).
4. **New profiles are seeded** the same way (`ensure_plugins_tier_for_new_home`,
   `--plugins-tier-only`) — the second half of R5.

## 7. Findings → recommended atomic cards (NOT implemented here)

**F1 — real defect: the passthrough is ungated and unaudited.**
`cluster-guard.py` (the capacity gate + audit hook) exits early unless
`payload.toolset == "hscc-cluster"` (lines 123, 155). `sparkrun_exec` runs under toolset
`sparkrun`, so every passthrough call — including `sparkrun stop --all` and
`sparkrun proxy stop`, the exact verbs that SIGTERM'd the operator's inference proxy
twice (the `t_be4b94ca` incident class, `hscc-cluster/tests/test_sparkrun_fence.py`
header) — clears the gate and writes no audit line. The test-suite fence guards test
argv, not runtime model calls.
*Recommended card (one file + tests):* extend the cluster-guard hook to toolset
`sparkrun` — deny (or operator-confirm) destructive argv verbs (`proxy stop`, `stop
--all`, cluster-define removal) before subprocess, append the same audit line for every
`sparkrun_exec` call. The implementer must first measure how a config `hooks:` `matcher`
value selects the firing toolset (the wired entry is `matcher: hscc-cluster`,
`enable_plugins.py:1414-1423`) and add a second matcher entry or broaden the payload
check as the measurement dictates. Fail-open behaviour must stay (a guard bug must not
brick the tool).

**F2 — the bundled skills are inert (prompt lie + dead payload).**
`register()` never calls `ctx.register_skill`; hermes auto-registers a plugin's
`skills/` dir only for portable/Agent-Plugin kinds (`hermes_cli/plugins_loader.py:746-
748`), and `hscc-skills/hscc.py`'s BUNDLED_SKILLS does not include them. The plugin ships **four**
inert dirs — `run`, `setup`, `registry` **and `data-engineer`** — none in BUNDLED_SKILLS
(which spans `brainstorming`…`devops` only), so a follow-up card must clear all four;
`_DESC` names only three, so `data-engineer` is dead payload without a prompt lie.
Result: none of the four exist in any home's skill catalog, while `_DESC`
(`sparkrun-hermes/__init__.py:58`) instructs every model to "Load the run/setup/registry
skills". (Ruling 4 makes the current state *compliant* for non-main homes by accident —
nobody gets them — but for `<main>` the guidance is unreachable, which is a functional
defect, not a hygiene note.)
*Recommended card (one file):* register the three skills from `register()` **after** the
main-only gate (so they live and die with the tool, no second gate needed), and fix the
description wording in the same diff. Alternative the operator may prefer: vendor them
under `install/hscc-skills/` as main-only cluster skills — either is one atomic card;
the gate-first registration is smaller and needs no installer change.

**F3 — recorded residuals, no card.** (a) Fail-open when the resolver is unreachable is
deliberate (D-B: "no resolver is no gate") — accepted, pinned by the stub-ctx test.
(b) The root keeps `sparkrun-hermes` in `plugins.enabled` (loads, registers nothing) per
ruling 3 / parent §6 — harmless and intentional; do not "fix" by pruning root.
(c) Prefix-guard (`startswith("sparkrun")`) is prefix-only by design; the command still
runs via argv, not a shell. F1 is the control that matters, not more prefix checks.

## 8. Reproducing this audit

* Gate suite (hermes venv python only — the plain/conda pythons produce a **no-verdict**
  run, never a pass). **One pytest process per file, never one combined invocation:** each
  of these plugin suites does `import __init__ as plugin`, so two plugin test dirs collected
  together collide on that module name and the run dies at collection (`ImportError:
  attempted relative import with no known parent package`) — the same reason
  `scripts/run_tests.sh` runs "one pytest process PER dir (true isolation, matching how they
  deploy)". A combined run is a broken probe, not a red audited surface. Measured per leg:
  **5 / 18 / 16 / 13 / 30 passed, RC=0 each (82 total).**

  ```
  for f in sparkrun-hermes/tests/test_cluster_surface_gate.py \
           hscc-cluster/tests/test_sparkrun_fence.py \
           hscc-bootstrap/tests/test_enable_plugins_cluster_tier.py \
           hscc-bootstrap/tests/test_enable_plugins_new_home_tier.py \
           hscc-bootstrap/tests/test_doctor_main_only_surface.py; do
    ~/.hermes/hermes-agent/venv/bin/python -m pytest -q "$f" || echo "RED $f"
  done
  ```
* Live-state probe (read-only): enumerate `~/.hermes/config.yaml` + every
  `~/.hermes/profiles/*/config.yaml`, print only (a) whether `plugins.enabled` exists and
  contains `sparkrun-hermes`, (b) whether `toolsets` names `sparkrun`. Print no tokens,
  no host addresses. Expected steady state (measured today): enabled in `<main>` only,
  key present in every home, root listed-and-inert.
* QA proof of record: card `t_035b1b0b` gate comment (sandbox artifact
  `QA-GATE-t_035b1b0b.md`).

## 9. What must NOT be "fixed" by a future card

* Do not strip `hscc-cluster` from any home (D-C — six hooks).
* Do not prune the root's plugin list (ruling 3 wedge path; §7 F3b).
* Do not edit top-level `toolsets:` lists as a control (parent §6 rule 2).
* No live `~/.hermes` mutation from implementation cards — bootstrap does the writes,
  post-merge, operator verifies live.
