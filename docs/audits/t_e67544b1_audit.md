# Audit note — t_e67544b1: theme hscc-cluster standalone CLI entry points (Rich epic, hscc-cluster 21 raw sites)

## Goal (restated from card)

Convert the raw `print(` / `json.dumps(` in NON-TEST hscc-cluster python code to the
themed Rich CLI surface, using the `_theme` pattern from hscc-project. Hard
invariants: `--json` BYTE-IDENTICAL (no ANSI/reformat); non-TTY falls back to plain,
NO ANSI; one no-ANSI regression test per converted command; assert the constant;
reuse make_console(width=None if TTY else 200) anti-collapse pattern.

## Inventory of all 21 raw json.dumps occurrences (non-test hscc-cluster)

Console `print(` sites (14 — the genuine CLI output to decide on):
  hscc.py:270      "Will run: <cmd>"                          (in _run_or_print)
  hscc.py:272      "  --dry-run: not executing"                (in _run_or_print)
  hscc.py:389      "note: hscc-cluster is merged into the main CLI" (stderr, main())
  hscc.py:393      "Hermes Spark Cluster Control...Usage..."   (help, main())
  hscc.py:415      f"Unknown command: {cmd}"                   (main())
  hscc.py:416      f"Available: ..."                           (main())
  hscc.py:424      "Usage: hscc-cluster stop <container_id>"   (main())
  hscc.py:437      json.dumps(result, indent=2, default=str)   (command result, main())
  hscc.py:439      json.dumps({"error": str(e)})               (exception, main())
  cluster_template.py:2235 json.dumps(result)                  (main() list)
  cluster_template.py:2239 json.dumps(result)                  (main() preview)
  cluster_template.py:2243 json.dumps(result)                  (main() apply)
  cluster_template_cli.py:100 json.dumps(result)               (__main__)
  gateway_restart.py:75  print(result)                         (__main__)

Non-console json.dumps (NOT console output — left untouched):
  sparkrun_bridge.py:44,86,87  — python string template shipped to a REMOTE node
  __init__.py:25               — return value (tool wire format), not a print
  workflow.py:294,420,467      — f.write(json.dumps(...)) file writes (audit trail)

## Design decision (this card)

The engine functions (cmd_cluster_status, cmd_hosts, ...) return plain dicts and
are the MACHINE contract (parsed by hscc-api routes, the iOS console, scripts).
They are library functions and stay untouched. The raw prints live ONLY in the
standalone/back-compat entry points (hscc.py main(), cluster_template_cli.py
__main__, cluster_template.py main(), gateway_restart.py __main__).

Per the hard invariant + the sibling-card convention (errors and dry-run/applied
notices stay PLAIN print on stderr/stderr-style, deliberately NOT themed; --json
machine path stays raw byte-identical):

  THEMED (new `_theme` layer): command-result human views + help/usage text.
  `--json` flag added to each standalone entry point; when present the raw
  `print(json.dumps(result, indent=2, default=str))` is emitted byte-identical
  (this is the machine contract daemon/scripts/iOS parse).
  Default (no `--json`): themed human panel view.
  Stays PLAIN (per sibling convention): error paths, dry-run/execution notices
  (hscc.py _run_or_print "Will run:" / "--dry-run: not executing").

This matches the unified `hscc` CLI contract (hscc cluster <cmd> = themed human;
hscc template validate --json = raw), so the standalone binaries become consistent
with the merged CLI.

## Status

PLANNED — not yet implemented.
