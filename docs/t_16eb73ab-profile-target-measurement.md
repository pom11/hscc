# t_16eb73ab — STEP-1 measurement: which profile homes are HSCC homes?

Card: `t_16eb73ab` — "Fix install_payload to write HSCC payload to selected profiles"
Measured: 2026-11-02 ~22:05 EET on the daemon host (Hermes host machine; hostname
deliberately omitted — see fleet rule on recording infrastructure addresses).
Base tree: `origin/main` @ `873b2b93` (PR #198 merged).

**This file is the MEASURE-FIRST deliverable only. No installer code has been
changed. Coding is gated on the operator's STEP-1 answer (options at the end).**

---

## 1. The three sets (as measured)

### Set A — `hermes profile list` (49 profiles)

```
architect backend-engineer business-analyst coder content-writer data-engineer
data-scientist default devops-engineer ecofire-app-orch ecofire-bc-engineer
ecofire-bc-orch ecofire-flows-executor ecofire-flows-orch ecofire-pontaje-engineer
ecofire-pontaje-orch efsdriver-orch financial-analyst flightdeck-orch flosana-orch
frontend-engineer general-orch grid-orch headless-display-orch hscc-orch
ios-engineer kronos-orch market-researcher micro-notify-orch ml-engineer
ml-researcher orchestrator pickolo-orch pom-orch pontaje-orch powerbi-orch probe
product-manager project-manager qa radio-orch reviewer security-engineer
social-media-manager soconn-orch sphoin-orch technical-writer ux-designer worker
```

### Set B — `hermes kanban --board hscc assignees` (52 names; 49 `on_disk=yes`)

52 names = the 49 above **plus 3 ghosts** (`on_disk=no`, no profile home):
`other`, `p4`, `sdlc-review`.

A ∩ B(on_disk) is an exact match: every on-disk assignee is a real profile and
every profile appears as an assignee. `comm` both directions returned empty.

### Set C — profile homes that already have `plugins/` (2 homes)

```
backend-engineer   ~/.hermes/profiles/backend-engineer/plugins/   (16 MB, hscc_daemon/ present)
hscc-orch          ~/.hermes/profiles/hscc-orch/plugins/          (16 MB, hscc_daemon/ present)
```

`plugins/hscc_daemon/` — the payload marker — exists in exactly the same 2 homes
(C == C2 == 2/49). The other **47 profile homes have no `plugins/` directory at
all**, so `install_payload.py` currently skips them entirely:
`_named_profile_plugins_dirs()` (install_payload.py:334-365 on origin/main)
only yields a dir when `child/"plugins"` **already exists**:

```python
pdir = child / "plugins"
if pdir.is_dir():        # <-- the gate that makes 47 homes invisible
    out.append(pdir)
```

This is the gap the card names. It is why the `t_5720ef53` doctor row reads red
for those 47 homes: they genuinely never carried the payload.

## 2. The three-way diff, stated plainly

| Comparison | Result |
|---|---|
| A − C (profiles with NO payload) | **47 profiles** — all but `backend-engineer`, `hscc-orch` |
| C − A (payload home that isn't a profile) | empty |
| B(on_disk) − A / A − B(on_disk) | empty (identical sets) |
| B(all) − A (assignees with no profile home) | `other`, `p4`, `sdlc-review` (ghosts) |
| Homes with live cards (todo/running/blocked) that are payload-less | **`devops-engineer`, `qa`, `reviewer`** (live + no payload) |
| Homes that have ever received a card and are payload-less | 11 of 13 (`architect`, `coder`, `default`, `devops-engineer`, `ecofire-bc-engineer`, `ecofire-bc-orch`, `ios-engineer`, `qa`, `reviewer`, `technical-writer`, `worker`) — only `backend-engineer` + `hscc-orch` have it |

**The gap is real for dispatched work**: `ios-engineer` has 139 done cards and
`worker` 41, neither has the payload. Any HSCC verb (`hscc cluster …`,
`hscc project …`, `hscc api …`) invoked from such a profile resolves plugins from
its own `$HERMES_HOME/plugins` (no root fallback — `plugins_discovery.py:184`)
and finds nothing.

## 3. Caveat on the assignees output (required by the operator note)

`hermes kanban assignees` is **not board-scoped**. Verified: `--board hscc`,
`--board sphoin`, `--board ecofire-bc` and `--board default` all return the
**same 52 names** *and the same per-assignee counts* (byte-identical output;
e.g. `ios-engineer done=139` appears under every slug). `hermes kanban list
--assignee X` likewise returns the same payload length under all three slugs.

Consequence: **the assignees list cannot be used to select "HSCC profiles"** —
it is a fleet-global census over a shared DB, not the hscc board's roster. Any
option phrased as "dispatcher assignees" is therefore operationally identical
to option 1 (all profiles) unless the seed rule is refined (see 4).

Also note `hermes profile list` has no `--json` flag; parsing its table is
brittle (the `◆` current-profile marker attaches to the name). A target-selection
rule should not depend on it.

## 4. The three options, with what each actually costs

| # | Rule | Homes written | Disk cost | Notes |
|---|---|---|---|---|
| 1 | **All profiles** (write into every home under `profiles/`, create `plugins/` when absent) | 49 (47 new) | ~+750 MB (47 × 16 MB) + backups | Simplest, self-maintaining, no config. Cost: `video_gen`/`memori`/`docs` shipped to profiles that will never use HSCC; every new profile silently inherits the payload. |
| 2 | **Dispatcher-targetable** | ambiguous — see caveat | 13×16 MB ≈ 208 MB (ever-assigned reading) or 7×16 MB ≈ 112 MB (live-cards reading) | Blocked as a *rule*: the assignees census is global and includes ghosts (`other`, `p4`, `sdlc-review` have no home to write to). "Dispatcher-targetable" needs a definition that survives the caveat. |
| 3 | **Explicit `~/.hscc/` allowlist, seeded by dispatcher-targetables** (orch's recommendation) | operator-controlled; seed = 13 profiles → 11 new homes | ~+176 MB | Needs a small config-schema addition under `~/.hscc/`. Predictable; auditable; `doctor` can diff allowlist vs reality. |

**Orch's recommendation, restated for the operator**: option **3 seeded by 2** —
an explicit `~/.hscc/` allowlist file (default seed = the 13 ever-assigned
on-disk assignees minus ghosts, plus `hscc-orch`/`backend-engineer`), so the
operator can add or drop profiles without a code change, and option 1 stays
reachable by making the allowlist the literal `["*"]`.

Measured seed set for option 3 (13 names, ever-received-a-card ∩ on-disk):
`architect backend-engineer coder default devops-engineer ecofire-bc-engineer
ecofire-bc-orch hscc-orch ios-engineer qa reviewer technical-writer worker`

## 5. What is NOT in question (already pinned, will not be touched)

- Doctor-row semantics from `t_5720ef53` (`test_doctor_budget_hook.py`) —
  unchanged, per card "out of scope".
- The drift guard (GAP-E-1), backup-then-overwrite, `plugins-backups/` retain
  limiting, `PAYLOAD_EXTRA_DESTS` — all inherited as-is; they already work
  per-destination once a destination exists.
- Installer-side only. No changes to `hscc-roles` profile generation, no
  changes to hermes plugin discovery.

## 6. What changes once STEP-1 is answered (1 file + 1 test, pre-agreed shape)

1. `_named_profile_plugins_dirs()` gains a target-selection parameter: resolve
   the allowed profile names, then yield `child/"plugins"` for allowed profiles
   **whether or not `plugins/` exists** (`mkdir(parents=True, exist_ok=True)`
   inside `install_payload` already handles creation — line 256).
2. `install_payload_profiles()` passes the selected targets through and reports
   per-home `created: true|false` so the bootstrap diff can name the homes that
   flipped from payload-less to payload.
3. Option 3 adds `target_profiles` to the `~/.hscc/` config reader (allowlist
   list; `["*"]` == option 1; empty == today's behaviour) with the same
   `hscc_dir=` reader convention `doctor` uses, so tests can seal `~/.hscc`.
4. Named test in `hscc-bootstrap/tests/test_install_payload.py` pinning the
   chosen rule — including "absent `plugins/` gets created for a selected
   profile" and "unselected profile is still skipped".
5. `scripts/run_tests.sh` needs no new leg (`hscc-bootstrap` already registered).

Nothing above is implemented. Awaiting STEP-1.

---

## Appendix — dry-run baseline against the live tree (read-only, writes nothing)

Imported `install_payload` from `origin/main` in this checkout and called the real
`_named_profile_plugins_dirs()` on `~/.hermes/plugins`:

```
profiles on disk      : 49
TARGETED today        : 2 ['backend-engineer', 'hscc-orch']
SKIPPED today         : 47   (reason: no plugins/ dir -> the pdir.is_dir() gate)
would_create_if_selected: 47
```

So the "homes that flip from payload-less to payload" list for the post-decision
real bootstrap run is exactly these 47 (option 1), the 11-new-home seed subset
(option 3 as seeded), or the subset the operator picks. Verified by the code's
own selector, not by directory listing alone.
