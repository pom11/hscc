#!/bin/bash
# swift_source_groups.sh — shared Foundation-safe source groups for the
# headless gates.
#
# Sourced, never run. Every path is relative to ios-app/.
#
# WHY THIS FILE EXISTS (t_1399335e). The gate scripts assemble a compiler
# invocation by hand, so when a compiled file gains a type from another file,
# every gate that pulls the first one must pull the second too. The widget
# checks all compiled `WidgetDataClient.swift` but none of them compiled
# `NeedsMeReading.swift`, where `AlertsLite` lives, and five gates went red on
# main the moment `alerts(limit:)` landed. Adding the missing path to five
# copied lists fixes that one break and guarantees the next; a group declared
# once is what stops the copy from drifting again.
#
# The groups name FILES rather than symbols because Swift has no local-module
# import for the compiler to follow when these compiles are assembled by hand.
# The rule for callers is simple: compile the whole group, never a member.
#
# shellcheck shell=bash

# The shared client every widget gate compiles. `WidgetDataClient.alerts()`
# returns `AlertsLite`, declared in NeedsMeReading.swift, and
# `WidgetDataClient.mergeQueue()` returns `MergeQueueLite`, declared in
# MergeQueueReading.swift, so the client group is not buildable without both
# projections.
SWIFT_GROUP_WIDGET_CLIENT=(
  Sources/Shared/SharedModels.swift
  Sources/Shared/SharedStore.swift
  Sources/Shared/WidgetDataClient.swift
  Sources/Shared/NeedsMeReading.swift
  Sources/Shared/MergeQueueReading.swift
)

# The small cluster tile's data half. `ClusterSmallGrid` builds its GPU trend
# from `ClusterHistorySeriesSet` and `ClusterHistoryDownsampler`, both declared
# in ClusterHistorySeries.swift, so the grid is not buildable without it.
# macOS compiles the same pair by path; that is why this group exists.
SWIFT_GROUP_CLUSTER_GRID=(
  Sources/HSCCWidgets/ClusterSmallGrid.swift
  Sources/Shared/ClusterHistorySeries.swift
)

# swift_group_with_prefix <root> <group> — print one group with <root> in front
# of every path, for gates in a sibling app tree (macOS) that compile the iOS
# files by relative path.
swift_group_with_prefix() {
  local root="$1" name="$2" entry
  case "$name" in
    widget-client) local -a group=("${SWIFT_GROUP_WIDGET_CLIENT[@]}") ;;
    cluster-grid)  local -a group=("${SWIFT_GROUP_CLUSTER_GRID[@]}") ;;
    *)
      echo "unknown Swift source group: $name" >&2
      return 1
      ;;
  esac
  for entry in "${group[@]}"; do
    printf '%s/%s\n' "${root%/}" "$entry"
  done
}
