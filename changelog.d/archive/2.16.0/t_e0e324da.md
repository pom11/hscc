kind: Fixed
task: t_e0e324da

- **`ios-app/scripts/widget_view_dispatch/widgetClusterGraph.sh` — the Cluster
  Graph surface is now covered by the fleet's own widget-surface gate.**
  `t_f4551339` shipped the widget as a configurable Home Screen surface — it
  reads the registry, pauses, paces itself from `AppGroupWidgetSettings` and
  gates `configured || paused` in one step in all three bodies — but added no
  per-surface check file and no `MANIFEST` line, so `widget_view_dispatch_check.sh`
  never ran a check against it. The gap was silent by construction: the
  aggregator's orphan guard catches a file with no manifest entry, never a
  surface with no file, so a registered `WidgetSurfaceKind` case could simply be
  uncovered and nothing printed. The new file proves the registry read
  (`setting(for: .widgetClusterGraph)`, which no gate checked — the settings key
  existing in `SharedStore` is not the same claim as the widget consulting it),
  the paused entry, the single owner of the paused sentence, the absence of a
  Lock Screen label in each of the four files the widget ships, and the cadence
  executed off the committed `cluster_graph_cadence` slice. It is the first
  caller of `home-screen-only` families mode in the manifest — every shipped
  surface has since moved to accessory mode, so that branch was dead code until
  now and is negative-proven here twice. Two of the helper's seven checks are
  deliberately not run: the one-step gate and the order inside the fetch, where
  this widget ships invite-before-pause on purpose because
  `ClusterGraphEntry.status` is single-valued and whoever asks first decides the
  sentence the operator reads. Both belong to `cluster_graph_check.sh`; rather
  than copy them, the file asserts the owning gate still carries each rule's own
  pattern, so deleting a rule there reddens this gate instead of orphaning it.
  Aggregate run rc=0, and its diff against the pre-change baseline is additive
  only — the six shipped surfaces print byte-identical output.

---
kind: Changed
task: t_e0e324da

- **`ios-app/scripts/widget_view_dispatch/common.sh` — each widget-surface check
  is now callable on the file it actually measures.** `widget_surface_gate`
  assumed a widget keeps provider, entry and family bodies in one Swift file, so
  all seven of its checks took a single path; the Cluster Graph widget splits
  them three ways, and one extra "bodies file" argument would still have left
  the entry's gate copy in a third file. The checks are now separate functions
  and `widget_surface_gate` is their composition in the same order, so the six
  shipped surfaces call it exactly as before — proved by diffing the aggregate
  run's stdout against the pre-refactor baseline rather than by asserting it. The
  label-absence half of the home-screen-only rule is split out so a split-file
  widget can prove the absence in every file it ships, and
  `widget_gate_rule_has_owner` is added for a rule a surface delegates to a
  sibling gate: it tests that the owner still carries the rule, because a claim
  that lives only in a comment is the claim that silently disappears.
