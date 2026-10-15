# Nodes & serving — the merged screen (t_65d791da)

`ios-app/Sources/HSCC/Views/NodesServingView.swift` — one destination for
"which machines are up" and "what is running on each of them, and the button that
takes one out of service".

Spec: `docs/design/IOS_REDESIGN_2026-10-14.md` §5 "**Nodes & serving** (absorbs
ServingControl + the NodeTopology strip): topology as a header, then a unit list
with per-row Stop/Restart", §8 migration map ("ServingControlView → drawer ·
'Nodes & serving'", "NodeTopologyView | component (kept; shown in Fleet +
Nodes)").

Supersedes two surfaces:
- `ServingControlView.swift` (t_e13c8c2b, restyled by `644286f6`, mutations +
  a11y fixed by `dca7f25c`) — see `docs/servingcontrol.md` for that history.
- `ClusterView.swift` — the topology-only "Cluster" drawer row.

## Surface

One plain `List` (spec §5's list recipe):

| Section | Content | Load state |
|---|---|---|
| **Topology** | `NodeTopologyView` (unchanged component) + the server-authored `speak` line | `status` |
| **Node `<tail>` · <role> (N units)**, one per node | the units hosted there, each row with Stop / Restart | `status` × `hosts` |
| **Node not reported** | units the live report did not place | as above |
| **Nodes with no serving unit** | registered nodes carrying nothing | `hosts` |

Two `LoadState`s, one per read, each rendering its own **inline** loading/failed
row (the Fleet pattern from `45645201`): a dead `/v1/cluster/hosts` cannot blank
the units that `/v1/cluster/status` answered, and vice versa. `/v1/cluster/hosts`
is now cached (`EndpointPath.clusterHosts`) so an unreachable cluster still shows
where its last-known units sat, marked stale.

Mutations are unchanged from `ServingControlView`: `MutationButton` → confirm
dialog that names the unit AND its container → `confirm: true`; Stop is `bad`,
Restart is `accent` (recovery, not destruction — `dca7f25c`); both buttons carry
an `accessibilityLabel` naming the unit. Rows are deliberately **not** combined
into one VoiceOver element, because each carries two individually reachable
buttons (§9).

## The unit → node edge (the one genuinely new piece)

`ClusterWorkload` is `{name, tp, pp, container_id}` — **no node field**. The only
API payload that pairs a job with its machines is `GET /v1/cluster/hosts` →
`live_status.output`, the raw `sparkrun status` **text** the engine forwards
verbatim (`hscc-cluster/hscc.py` `cmd_hosts` → `run_cmd([SPARKRUN, "status"])`,
passed through by `hscc-api/routes_cluster.py` `handle_cluster_hosts`):

```
Job: <recipe.yaml>  (tp=2, pp=1)  [<container_id>]  (2 container(s))
  node_0     10.0.0.1     Up 12 hours    <image>
  node_1     10.0.0.2     Up 12 hours    <image>
```

`Sources/HSCC/ServingPlacement.swift` parses it. Three rules worth knowing
before trusting a header:

1. **Never guess.** A unit the report does not place lands under "Node not
   reported", not under a machine. An operator presses STOP beside these labels.
2. **`didPlaceAnything`** distinguishes "the fleet is empty" from "the blob was
   absent / not in the shape we parse" — so an offline cluster never reads as a
   fleet with no nodes.
3. **`idle_hosts` is NOT used** for node grouping. The engine files every
   indented dotted line there, including containers of RUNNING jobs — all four
   `idle_hosts` entries in the committed capture are also running containers —
   so reading it as "free nodes" would name a busy machine idle.

A `tp=2` unit occupies **both** nodes of its pair and is listed under each; the
footer says so, because otherwise it looks like a duplicated row.

## Gate

`scripts/serving_placement_check.sh` — compiles the REAL parse + the REAL models
into a macOS CLI (never a mirror, same rule as `model_decode_check.sh`) and runs
41 assertions:

- the committed live captures (`fixtures/cluster_hosts.json` +
  `v1_cluster_status.json`) decode and place correctly, incl. "a TP pair appears
  under BOTH nodes" and "the NAS is never a serving node";
- the hostile blobs that would each name a WRONG node under a looser match —
  `999.1.1.1`, a truncated `10.0.0`, an image tag with dots, a `Job:` line with
  no bracketed id (must not steal the previous job's nodes), `logs:`/`stop:`
  trailer lines, unrelated text;
- the nil/empty/whitespace blob cases (→ `didPlaceAnything == false`, units
  unattributed, never guessed).

Negative test: `SERVING_PLACEMENT_NEGATIVE_TEST=1 bash
scripts/serving_placement_check.sh` sabotages the IPv4 rule and requires the
harness to fail.

## Files

- `ios-app/Sources/HSCC/Views/NodesServingView.swift` (was `ServingControlView.swift`)
- `ios-app/Sources/HSCC/ServingPlacement.swift` (new)
- `ios-app/scripts/serving_placement_check.sh` + `serving_placement_check/main.swift` (new)
- `ios-app/Sources/HSCC/StateCache.swift` (`EndpointPath.clusterHosts`)
- `ios-app/project.yml`; `AppShell.swift` (`.clusterHub` → mapped alias,
  `normalized()`); `Sidebar.swift` (Cluster row removed); `HomeView.swift` (dock
  "Cluster" → `.nodesServing`)
- deleted: `ClusterView.swift`, `ServingControlView.swift`

## Verification (re-run after rebasing onto `3687f2fa`, the BoardHygiene merge)

`build_check` 87 files in the HSCC target (95 on disk — two screens retired by
the merge this one sits on top of) / 0 err / 0 warn, all 5 targets ·
`check_theme` CLEAN · `check_sources` in sync 95 · `serving_placement_check`
41/41 **and** the negative test fails as it must · `model_decode_check` 62/62 ·
`reconnect_check` 42/42 · `deeplink_check` 41/41 (the `.clusterHub` alias joins
`.selfHeal`/`.boardHygiene` in `AppShell.normalized`, so stale vocabulary still
resolves instead of wedging) · `ws_decode_check` ok · `turn_state_check` ok ·
`first_run_check` ok · `shared_store_check` ok · `xcodegen generate` re-run after
the add/delete (stale-project pitfall) · **`xcodebuild` (iOS Simulator) BUILD
SUCCEEDED**.

The app was also **installed and LAUNCHED on the iPhone 17 Pro simulator** on
both sides of the rebase: pre-rebase pid 40921, post-rebase pid 69742 — each
confirmed present in `launchctl list`, so the merged screen boots rather than
merely compiling. (The post-rebase build is from the same code as this commit;
this commit changes only docs.)
