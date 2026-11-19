kind: Fixed
task: t_0b8dd25f

- **Every tracked widget source now has a manifest owner.** `ClusterWidget`
  (`ClusterWidget.swift`, `ClusterWidgetMetrics.swift`,
  `ClusterWidgetViews.swift`) and `ProjectsWidget` are declared as
  `ios-app/widgets/cluster_widget.json` and `projects_widget.json`, and the four
  files shared with Monitor (`CLIMonitorHostTableView.swift`,
  `CLIMonitorTableReading.swift`, `ClusterSmallGrid.swift`,
  `ClusterSmallGridView.swift`) are owned by `_shared.json`. The eight sources
  that made `widget_registry.py --check` exit 1 on clean main now have exactly
  one owner each.
- **The settings screen can list and pause ClusterWidget and ProjectsWidget
  again.** Both widgets are in the generated widget bundle, and the missing
  manifests were the only reason `widget_settings_check.sh` reported "no
  registry case" for them; its surface-coverage section is green without a
  hand-written case.
- **`board_status_widget_check.sh` reads the directory-entry project.yml
  shape.** Since t_f98764f8 the HSCCWidgets target lists
  `- path: Sources/HSCCWidgets`, so counting per-file lines found 0. The gate
  now accepts a file listed directly or contained by a listed directory, and
  says which files are compiled.
- **CI runs every offline app gate instead of a hand-named few.** The
  `app-build-checks` workflow now runs `widget_registry.py --check` and globs
  every `ios-app/scripts/*_check.sh` and `macos-app/scripts/*_check.sh`; only
  the four that need the live API or a live capture are announced as skipped,
  and the two compile gates stay in their SDK-guarded steps. The probe runner
  also exposes `ios-app/widgets/` to its unmodified control copy, so the
  backtick probe judges the real gate rather than a tree with no manifests.
- **The workflow fetches full history** so `merge_base_check.sh` can resolve
  `origin/main`; that gate never fetches on its own.

---
kind: Verified
task: t_0b8dd25f

- `python3 ios-app/scripts/widget_registry.py --check` passes: 14 surfaces
  (9 widgets, 5 Live Activities), every generated file current, every tracked
  widget source owned.
- `scripts/tests/test_widget_registry.py` passes (4 tests); the whole `scripts`
  pytest leg is green.
- Every offline app gate passes, run the same way CI now runs them:
  `board_status_widget_check.sh`, `widget_settings_check.sh`,
  `projects_widget_check.sh`, `cluster_widget_check.sh`,
  `cluster_graph_check.sh`, `cards_running_widget_check.sh`,
  `kanban_monitor_widget_check.sh`, `widget_view_dispatch_check.sh`,
  `probes_check.sh` and the rest of both script trees.
- iOS and macOS compile gates are clean: `ios-app/scripts/build_check.sh`
  and `macos-app/scripts/build_check.sh` (0 errors, 0 warnings).
- `python3 scripts/address_guard.py --tracked` is clean.
