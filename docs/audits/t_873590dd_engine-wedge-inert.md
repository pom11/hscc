# Engine-wedge self-heal is inert — measured diagnosis (t_873590dd)

Date: 2026-10-10. Evidence gathered read-only; no container was stopped,
nothing restarted. All fleet facts came from the sanctioned paths: the
`hscc-cluster` wrapper's `cmd_cluster_status()` and the structured sparkrun
query (`_SPARKRUN_STATUS_SCRIPT` under sparkrun's own interpreter, the same
call `health._sparkrun_workloads()` uses).

## TL;DR

The fail-safe fired 2/2 because container resolution for `orch` (and for EVERY
unit in the current fleet) is structurally impossible from the status shape the
resolver consumes — a **multi-match** (4-way ambiguity), reached through a
**zero-match on the primary needle**. The identity data needed to disambiguate
(node + port) is already delivered by sparkrun's structured `to_dict()`; the
resolver just never saw it, because `cmd_cluster_status()` parses only the
human-readable `sparkrun status` text, whose `Job:` lines carry a single
recipe-path name and no host/port at all.

## 1. Reproduction (executed 2026-10-10, fleet healthy at 4/4)

`cmd_cluster_status()` (hscc-cluster/hscc.py:75) returned four workloads, all
identical in `name` — the recipe path — differing only in `container_id`:

```text
WORKLOAD {"name": ".../qwen3.8-flash-next-nvfp4-solo-vllm.yaml", "container_id": "03fae933e3df92bf_de6e280b97b0", "tp": "1", "pp": "1"}
WORKLOAD {"name": ".../qwen3.8-flash-next-nvfp4-solo-vllm.yaml", "container_id": "29d7a6953e51f4e0_9d8bab4a7ecb", "tp": "1", "pp": "1"}
WORKLOAD {"name": ".../qwen3.8-flash-next-nvfp4-solo-vllm.yaml", "container_id": "340f4c543a3e1b1b_086606fbaf71", "tp": "1", "pp": "1"}
WORKLOAD {"name": ".../qwen3.8-flash-next-nvfp4-solo-vllm.yaml", "container_id": "2b0f2f099afaf596_1f5e21bf5c84", "tp": "1", "pp": "1"}
```

(the recipe dir prefix is the operator's home path; omitted here — it plays no
part in the defect.)

Running the REAL resolver `recover._resolve_container_id()` (hscc_daemon/
recover.py:122) against that REAL status output, for each unit in the live
`~/.hscc/serving.json`:

```text
UNIT {"id": "orch",                                "model_matches": 0, "stem_matches": 4, "resolved_container_id": null}
UNIT {"id": "family-coding-Qwen3.8-...-246-8001",  "model_matches": 0, "stem_matches": 4, "resolved_container_id": null}
UNIT {"id": "family-coding-Qwen3.8-...-247-8002",  "model_matches": 0, "stem_matches": 4, "resolved_container_id": null}
UNIT {"id": "family-coding-Qwen3.8-...-248-8003",  "model_matches": 0, "stem_matches": 4, "resolved_container_id": null}
```

## 2. Which branch returns None — and why BOTH branches are implicated

`_resolve_container_id` builds ONE needle and does substring matching against
`workloads[].name` (recover.py:139-154):

1. Primary needle = `unit.model` = `local-inference-lab/Qwen3.8-Flash-Next-NVFP4`.
   The workload names are recipe paths; the model id NEVER appears in them ⇒
   **zero-match** (hypothesis (a) from the card: confirmed).
2. The recipe-stem fallback (tried ONLY when `model` is empty, which never
   happens for a configured unit) would match ALL FOUR workloads ⇒
   **multi-match** ⇒ None by the deliberate ambiguity fail-safe (hypothesis
   (b): confirmed as the fallback branch's fate).
3. `orch` IS effectively present in the workload list (its container is one of
   the four), so the unit-absent hypothesis does NOT apply.

Net: every unit in the current uniform-pool fleet resolves to None. The 2/2
inert episodes (2026-10-09T08:40:06Z, 2026-10-10T04:42:03Z — the only two
"Engine-wedge recovery" episodes ever logged) are the deterministic outcome,
not a race. `~/.hscc/recover.json` corroborates: every unit entry has
`attempts: 0`. The self-heal has never been able to act on this fleet shape.

### 2b. The LIVE path is worse than the reproduction above: dead needle branch

`_recover_locked` passes the **probe's wedged entry** as `unit` — the dict
health.py appends to the stream (keys measured from the live
`~/.hscc/state/engine_wedge.json`: `unit`, `node`, `port`, `status`,
`message`, `last_success`, `stalled_for_s`). It carries NO `model` and NO
`recipe`, so in production `needle` is ALWAYS empty and
`_resolve_container_id` returns None at recover.py:147-148 — BEFORE either
name-match branch runs. Executed against the live status shape:

```text
keys on the real wedged entry: ['last_success', 'message', 'node', 'port', 'stalled_for_s', 'status', 'unit']
model present: False | recipe present: False
resolve_container_id(real wedged entry) -> None   # empty-needle branch
```

So the card's (a)/(b) hypotheses describe what WOULD happen if the needle
existed; the live defect is strictly earlier: **zero-needle**. The node+port
identity that IS on the wedged entry is precisely what disambiguates — see §3.

## 3. The decisive question: is resolution possible from status data?

YES — and the data is already arriving, unused. The structured sparkrun status
(`health._SPARKRUN_STATUS_SCRIPT` → `classify_cluster_status(...).to_dict()`,
the sanctioned path from t_b543e530) carries per-entry identity that the text
`Job:` line does not. Measured for the live fleet (4 solo entries):

```text
SOLO_ENTRY {"cluster_id": "sparkrun_03fa..._de6e...", "entry_host": "LAN-1", "meta_hosts": ["LAN-1"], "meta_port": 8000, ...}
SOLO_ENTRY {"cluster_id": "sparkrun_29d7..._9d8b...", "entry_host": "LAN-2", "meta_hosts": ["LAN-2"], "meta_port": 8001, ...}
SOLO_ENTRY {"cluster_id": "sparkrun_340f..._0866...", "entry_host": "LAN-3", "meta_hosts": ["LAN-3"], "meta_port": 8002, ...}
SOLO_ENTRY {"cluster_id": "sparkrun_2b0f..._1f5e...", "entry_host": "LAN-4", "meta_hosts": ["LAN-4"], "meta_port": 8003, ...}
```

(live LAN addresses replaced with placeholders per the repo guard; each entry
also carries `meta.model` and `meta.recipe`.)

The wedged-unit entries the recovery consumes (health.py:2033/2054) carry
`node` + `port` — and they are matched to `serving.json` units by unit id
(`_engine_wedge_unit_key`), so the candidate dict passed to the resolver also
has `model` + `recipe`. `node` + `port` maps 1:1 to `entry_host` +
`meta.port` / `meta.hosts`. No ssh, no docker, no new sparkrun surface: the
fix is a second match step over data the sanctioned query already returns.

## 4. The stop target is valid for `sparkrun stop`

`cmd_stop(cid)` shells `sparkrun stop <cid>` (hscc-cluster/hscc.py:212).
sparkrun's `_is_cluster_id` (cli/_common.py:954) accepts the digest form
`<intent16>_<placement12>` (which `cmd_cluster_status` already reports and
which the recovery already formats today — re-checked against the actual
regex `[0-9a-f]{16}_[0-9a-f]{12}`, the live ids match) and the canonical
`sparkrun_<intent>_<placement>` form (which the structured `to_dict()`
reports). Either resolves; no stop-target change is needed. The resolution —
not the id format — is the whole defect.

## 5. Fix (smallest correct change; ambiguity fail-safe STAYS)

`hscc_daemon/recover.py`:

- New pure matcher `_match_container_id(entries, unit)`: identity match first
  (unit node ∈ entry hosts AND unit port == entry port; ports compared as
  strings so a `serve_cmd` string port still matches), falling back to the
  legacy name-substring match (model needle, else recipe stem) for status
  shapes that carry no identity fields. In BOTH strategies: zero matches or
  multiple matches ⇒ None (the fail-safe is untouched; it now only fires on
  genuinely ambiguous data).
- `_resolve_container_id` now consumes a NORMALISED workload shape and feeds
  the matcher: structured identity entries (with `hosts`/`port`), the legacy
  `cmd_cluster_status()` shape (`name`/`container_id`), or a raw structured
  `to_dict()` (`solo_entries`/`groups` normalised internally — hosts from
  `entry.host` else `meta.hosts`, port from `meta.overrides.port` else
  `meta.port`).
- New `fetch_identity_entries()`: one sanctioned structured query (same script,
  same venv-python resolution the DGX check uses) returning the identity
  entries for `_recover_locked`. If it is unavailable or yields nothing, the
  recovery falls back to `status_fn()` + the legacy name match — so the old
  behaviour (and the fail-safe) is preserved verbatim on degraded paths.
- `_recover_locked` gains an injectable `identity_fn` (defaults to the real
  fetch ONLY on the production path — when a caller injects the status/stop/up
  fakes, identity stays off unless explicitly passed, so tests never shell
  out). It fetches identity entries once per pass, merges them into the
  status result under `identity`, and the resolver matches node+port FIRST,
  legacy name second. Logged as before.

No caps changed. No ssh/docker hand-rolled. `hscc-cluster` untouched (its CLI
output shape is a deployed-plugin surface; the recovery resolves inside
`hscc_daemon` over the already-sanctioned query instead of widening it).

## 6. Residual risks (stated honestly, not raised as caps)

1. **A stale structured read could target a fresh container.** If the wedge
   cleared and `hscc cluster up` recreated the unit between the status query
   and the stop, the stop addresses a dead cluster_id — sparkrun then errors or
   no-ops; the follow-up fleet `up` is `--ensure`, so no harm. A second wedge
   episode would re-query fresh state.
2. **Structured-query unavailability** falls back to the legacy name match,
   which is inert on this fleet shape — i.e. worst case equals today's
   behaviour, never worse.
3. **Multi-host (group) workloads**: matched by membership (node ∈
   `meta.hosts`) + port, same ambiguity rule; a group spanning several serving
   units on the same node+port stays ambiguous ⇒ None. Correct fail-safe.

## 7. Verification

- `hscc_daemon/tests/test_recover.py::TestContainerIdResolution`: the resolver
  pinned for unique identity match, zero match, multi-match (same node+port
  twice, same port different nodes), port-as-string, no-match-then-legacy-name
  unique, no-match-then-legacy-name ambiguous, the legacy
  `cmd_cluster_status()` shape, and (d) the REAL orch case — the verbatim
  live fleet's four identity entries + the verbatim wedged orch entry, asserted
  to resolve to the 10.0.0.x:8000 container (placeholder address; the
  test fixture contains no live address).
- Full suites green under BOTH interpreters (see completion metadata).
