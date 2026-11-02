kind: Fixed
task: t_19dbb9de

- **`hscc-cluster/plugin.yaml` under-declared its own tools (audit A
  GAP-A-7).** `provides_tools` listed 13 names while `__init__.py` registers
  23 — `orch_message`, `apply_template`, `list_templates`, `preview_template`,
  `fleet_stats`, `cluster_verify`, `cluster_throughput`, `autoscale_advice`,
  `discovery_status` and `nas_status` were all missing from the manifest hermes
  advertises (`hermes tools`, plugin_catalog). The manifest now mirrors the
  `_*_TOOLS` registry tables exactly, grouped by table. Tool behaviour is
  unchanged; no handler was touched.
---
kind: Added
task: t_19dbb9de

- **Manifest-vs-registry drift test (the class of gap audits kept
  rediscovering).** `hscc_daemon/tests/test_plugin_manifest_drift.py` asserts,
  for every repo plugin dir carrying both a `plugin.yaml` and an
  `__init__.py`, that `provides_tools` equals the set of tools `register(ctx)`
  actually registers — under-declaration and over-declaration both fail, and a
  `_*_TOOLS` table the register() loop never concatenates fails too (that
  would let the manifest test lie). The name/manifest parsers are imported
  from `hscc_daemon.verify` itself, so the drift test and the shipped
  deployed-payload check (GAP-F-8) can never diverge on what a "declared tool"
  is.
---
kind: Verified
order: 1
task: t_19dbb9de

- `hscc_daemon/tests/test_plugin_manifest_drift.py`: **16** cases green
  (2 plugin dirs × 3 assertions + census + the GAP-A-7 receipt pinning 23
  tools and the 10 previously-missing names). Mutation-probed: dropping
  `orch_message` from the manifest → RED; adding a fictitious tool → RED;
  registering a new tool without the manifest → RED; restored → GREEN.
  Manifest parse verified identical through both shipped paths (PyYAML and
  verify's line-scan fallback).
